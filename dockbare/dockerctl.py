"""Docker Engine access (via the Python SDK over the socket, no docker CLI).

Volume data is moved with the archive endpoints of the Engine API: a helper
container (never started) mounts the volume and ``GET /containers/{id}/archive``
streams a tar of ``/data/.`` — entries are ``./…`` exactly like the legacy
``tar -C /volume .`` format. ``PUT …/archive`` restores it.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Iterator

import docker
from docker.errors import APIError, DockerException, ImageNotFound, InvalidVersion, NotFound

log = logging.getLogger("dockbare.docker")

VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
HELPER_LABEL = "dev.dockbare.helper"
INTERNAL_LABEL = "dev.dockbare.internal"
FALLBACK_IMAGE = "alpine:3.20"
DATA_MOUNT = "/data"


class DockerError(Exception):
    pass


def valid_volume_name(name: str) -> bool:
    return bool(name) and bool(VOLUME_RE.match(name)) and ".." not in name


def parse_stats(s: dict) -> dict | None:
    """Reduce a Docker ``/stats`` payload to the counters Dockbare keeps."""
    if not isinstance(s, dict):
        return None
    cpu = s.get("cpu_stats") or {}
    usage = cpu.get("cpu_usage") or {}
    if not usage.get("total_usage") and not cpu.get("system_cpu_usage"):
        return None  # container not running (all-zero payload)
    mem = s.get("memory_stats") or {}
    mstats = mem.get("stats") or {}
    # Same as `docker stats`: page cache is reclaimable, so it is not counted as used.
    cache = mstats.get("inactive_file", mstats.get("total_inactive_file", mstats.get("cache", 0))) or 0
    used = max(0, int(mem.get("usage") or 0) - int(cache))
    rx = tx = 0
    for n in (s.get("networks") or {}).values():
        rx += int(n.get("rx_bytes") or 0)
        tx += int(n.get("tx_bytes") or 0)
    br = bw = 0
    for e in ((s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
        op = str(e.get("op") or "").lower()
        if op == "read":
            br += int(e.get("value") or 0)
        elif op == "write":
            bw += int(e.get("value") or 0)
    return {
        "cpu_total": int(usage.get("total_usage") or 0),
        "cpu_system": int(cpu.get("system_cpu_usage") or 0),
        "cpus": int(cpu.get("online_cpus") or len(usage.get("percpu_usage") or []) or 1),
        "mem": used,
        "mem_limit": int(mem.get("limit") or 0),
        "net_rx": rx, "net_tx": tx, "blk_r": br, "blk_w": bw,
        "pids": int((s.get("pids_stats") or {}).get("current") or 0),
    }


_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})\s?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_log_lines(raw: bytes | str) -> list[tuple[float, str]]:
    """Split ``docker logs --timestamps`` output into (unix ts, text); ANSI colour codes are removed."""
    from datetime import datetime

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    out: list[tuple[float, str]] = []
    for line in text.splitlines():
        m = _LOG_TS.match(line)
        if not m:
            if out:
                out.append((out[-1][0], _ANSI.sub("", line)))
            continue
        tz = "+00:00" if m.group(4) == "Z" else m.group(4)
        frac = (m.group(3) or "0")[:6].ljust(6, "0")
        try:
            ts = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}.{frac}{tz}").timestamp()
        except ValueError:
            ts = out[-1][0] if out else 0.0
        out.append((ts, _ANSI.sub("", line[m.end():])))
    return out


def _api_message(e: Exception) -> str:
    """The daemon's own explanation (e.g. "volume is in use") instead of the full HTTP error."""
    expl = getattr(e, "explanation", None)
    if isinstance(expl, bytes):
        expl = expl.decode("utf-8", "replace")
    return str(expl or e)


class DockerCtl:
    def __init__(self, helper_image: str | None = None, timeout: int = 120):
        self._client: docker.DockerClient | None = None
        self._lock = threading.Lock()
        self._helper_override = helper_image
        self._helper_image: str | None = None
        self._timeout = timeout

    # --- connection ----------------------------------------------------------
    @property
    def client(self) -> docker.DockerClient:
        with self._lock:
            if self._client is None:
                try:
                    self._client = docker.from_env(timeout=self._timeout)
                except DockerException as e:
                    raise DockerError(f"Cannot connect to Docker: {e}") from e
            return self._client

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            with self._lock:
                self._client = None
            return False

    def info(self) -> dict:
        try:
            v = self.client.version()
            return {"ok": True, "version": v.get("Version"), "api": v.get("ApiVersion"), "os": v.get("Os")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def host_info(self) -> dict:
        """CPU count, total memory and name of the Docker host (from ``/info``)."""
        try:
            i = self.client.info()
        except DockerException as e:
            raise DockerError(f"Cannot read Docker info: {e}") from e
        return {"cpus": int(i.get("NCPU") or 0), "mem_total": int(i.get("MemTotal") or 0), "name": i.get("Name") or "",
                "os": i.get("OperatingSystem") or "", "kernel": i.get("KernelVersion") or ""}

    # --- volumes & containers ------------------------------------------------
    def container_map(self) -> dict[str, list[dict]]:
        """Map volume name → containers that mount it (one pass over all containers)."""
        mapping: dict[str, list[dict]] = {}
        try:
            containers = self.client.containers.list(all=True)
        except DockerException as e:
            raise DockerError(f"Cannot list containers: {e}") from e
        for c in containers:
            attrs = c.attrs or {}
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            if labels.get(HELPER_LABEL):
                continue
            for m in attrs.get("Mounts") or []:
                if m.get("Type") != "volume" or not m.get("Name"):
                    continue
                mapping.setdefault(m["Name"], []).append({
                    "id": c.id,
                    "short_id": c.short_id,
                    "name": c.name,
                    "status": c.status,
                    "image": (attrs.get("Config") or {}).get("Image"),
                    "project": labels.get("com.docker.compose.project"),
                    "service": labels.get("com.docker.compose.service"),
                    "destination": m.get("Destination"),
                })
        return mapping

    def list_volumes(self) -> list[dict]:
        try:
            vols = self.client.volumes.list()
        except DockerException as e:
            raise DockerError(f"Cannot list volumes: {e}") from e
        cmap = self.container_map()
        out = []
        for v in vols:
            a = v.attrs or {}
            labels = a.get("Labels") or {}
            if labels.get(INTERNAL_LABEL):
                continue  # Dockbare's own data/backups volumes
            out.append({
                "name": v.name,
                "driver": a.get("Driver"),
                "created": a.get("CreatedAt"),
                "mountpoint": a.get("Mountpoint"),
                "labels": labels,
                "project": labels.get("com.docker.compose.project"),
                "compose_volume": labels.get("com.docker.compose.volume"),
                "containers": cmap.get(v.name, []),
            })
        out.sort(key=lambda x: x["name"].lower())
        return out

    def volume_names(self) -> set[str]:
        try:
            return {v.name for v in self.client.volumes.list() if not ((v.attrs or {}).get("Labels") or {}).get(INTERNAL_LABEL)}
        except DockerException as e:
            raise DockerError(f"Cannot list volumes: {e}") from e

    def volume_exists(self, name: str) -> bool:
        try:
            self.client.volumes.get(name)
            return True
        except NotFound:
            return False
        except DockerException as e:
            raise DockerError(str(e)) from e

    def create_volume(self, name: str) -> None:
        if not valid_volume_name(name):
            raise DockerError("Invalid volume name.")
        try:
            self.client.volumes.create(name=name, labels={"dev.dockbare.created": "1"})
        except DockerException as e:
            raise DockerError(f"Cannot create volume {name}: {e}") from e

    def containers_using(self, volume: str) -> list[dict]:
        return self.container_map().get(volume, [])

    def stop_container(self, cid: str, timeout: int = 30) -> bool:
        try:
            c = self.client.containers.get(cid)
            c.stop(timeout=timeout)
            return True
        except DockerException as e:
            log.warning("Failed to stop container %s: %s", cid[:12], e)
            return False

    def start_container(self, cid: str) -> bool:
        try:
            c = self.client.containers.get(cid)
            c.start()
            for _ in range(10):
                time.sleep(0.5)
                c.reload()
                if c.status == "running":
                    return True
            return c.status == "running"
        except DockerException as e:
            log.warning("Failed to start container %s: %s", cid[:12], e)
            return False

    # --- container monitoring ------------------------------------------------
    def list_containers(self) -> list[dict]:
        """One API call: every container with state, health (parsed from Status) and labels."""
        try:
            raw = self.client.api.containers(all=True)
        except DockerException as e:
            raise DockerError(f"Cannot list containers: {e}") from e
        out = []
        for c in raw:
            labels = c.get("Labels") or {}
            if labels.get(HELPER_LABEL):
                continue
            status = c.get("Status") or ""
            health = ("healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status
                      else "starting" if "health: starting" in status else None)
            m = re.search(r"Exited \((\d+)\)", status)
            out.append({
                "id": c["Id"], "short_id": c["Id"][:12], "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
                "state": c.get("State"), "status": status, "health": health,
                "exit_code": int(m.group(1)) if m else None, "image": c.get("Image"), "created": c.get("Created"),
                "project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
            })
        return out

    def inspect_container(self, cid: str) -> dict | None:
        try:
            a = self.client.api.inspect_container(cid)
        except NotFound:
            return None
        except DockerException as e:
            raise DockerError(f"Cannot inspect container: {e}") from e
        st = a.get("State") or {}
        return {
            "exit_code": st.get("ExitCode"), "oom_killed": bool(st.get("OOMKilled")), "started_at": st.get("StartedAt"),
            "finished_at": st.get("FinishedAt"), "restart_count": a.get("RestartCount", 0), "error": st.get("Error") or "",
            "health": ((st.get("Health") or {}).get("Status")), "restart_policy": ((a.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
            "health_log": [(h.get("End"), h.get("ExitCode"), (h.get("Output") or "")[:300]) for h in ((st.get("Health") or {}).get("Log") or [])[-3:]],
        }

    def container_stats(self, cid: str) -> dict | None:
        """Raw resource counters of a running container (one ``/stats`` call, no streaming).

        ``one_shot`` makes the daemon answer at once instead of waiting ~1 s for a second
        sample; rates are computed by the caller from two consecutive calls.
        """
        try:
            try:
                s = self.client.api.stats(cid, stream=False, one_shot=True)
            except NotFound:
                raise
            except (TypeError, APIError, InvalidVersion):  # old SDK or daemon without one_shot (API < 1.41)
                s = self.client.api.stats(cid, stream=False)
        except NotFound:
            return None
        except DockerException as e:
            raise DockerError(f"Cannot read container stats: {e}") from e
        return parse_stats(s)

    def container_logs(self, cid: str, *, tail: int | str = 500, since: float | None = None) -> list[tuple[float, str]]:
        """Log lines (stdout and stderr merged) as (unix timestamp, text), oldest first."""
        kw: dict = {"stdout": True, "stderr": True, "timestamps": True, "tail": tail}
        if since:
            kw["since"] = int(since)  # whole seconds; callers drop lines they already saw by timestamp
        try:
            raw = self.client.api.logs(cid, **kw)
        except NotFound:
            return []
        except DockerException as e:
            raise DockerError(f"Cannot read container logs: {e}") from e
        return parse_log_lines(raw)

    # --- cleanup ---------------------------------------------------------------
    def cleanup_inventory(self, with_sizes: bool = True) -> dict:
        """Raw containers (all, including stopped and Dockbare's helpers), top-level images, volumes and disk usage."""
        api = self.client.api
        try:
            containers = api.containers(all=True)
            images = api.images()
            volumes = (api.volumes() or {}).get("Volumes") or []
        except DockerException as e:
            raise DockerError(f"Cannot list Docker objects: {e}") from e
        df = None
        if with_sizes:
            try:
                df = api.df()  # computes volume sizes; can take a few seconds on big volumes
            except DockerException as e:
                log.warning("Docker disk usage unavailable: %s", e)
        return {"containers": containers, "images": images, "volumes": volumes, "df": df}

    def image_id(self, ref: str) -> str | None:
        try:
            return self.client.api.inspect_image(ref).get("Id")
        except DockerException:
            return None

    def protected_image_ids(self) -> set[str]:
        """Images Dockbare needs for backups and restores: its own image and the helper image (never pulled here)."""
        ids: set[str] = set()
        try:
            own = self.own_container()
            if own is not None:
                ids.add(own.image.id)
        except Exception:  # noqa: BLE001
            pass
        for ref in (self._helper_image, self._helper_override, FALLBACK_IMAGE):
            if ref:
                iid = self.image_id(ref)
                if iid:
                    ids.add(iid)
        return ids

    def volume_users(self, name: str) -> list[str]:
        """Names of all containers (running or not, helpers included) that mount the volume."""
        try:
            cs = self.client.api.containers(all=True, filters={"volume": name})
        except DockerException as e:
            raise DockerError(f"Cannot check volume users: {e}") from e
        return [(c.get("Names") or ["/?"])[0].lstrip("/") for c in cs]

    def remove_image(self, ref: str) -> None:
        """Remove an image or one of its tags. Never forced: Docker refuses images that are in use."""
        try:
            self.client.api.remove_image(ref, force=False, noprune=False)
        except NotFound:
            return
        except DockerException as e:
            raise DockerError(_api_message(e)) from e

    def remove_volume(self, name: str) -> None:
        """Remove a volume. Never forced: Docker refuses volumes that are in use."""
        try:
            self.client.api.remove_volume(name, force=False)
        except NotFound:
            return
        except DockerException as e:
            raise DockerError(_api_message(e)) from e

    def container_raw(self, cid: str) -> dict | None:
        try:
            return self.client.api.inspect_container(cid)
        except NotFound:
            return None
        except DockerException as e:
            raise DockerError(_api_message(e)) from e

    def remove_container(self, cid: str) -> None:
        """Remove a stopped container. Its volumes are kept; never forced, so running containers are refused."""
        try:
            self.client.api.remove_container(cid, v=False, force=False)
        except NotFound:
            return
        except DockerException as e:
            raise DockerError(_api_message(e)) from e

    # --- utilities -------------------------------------------------------------
    def engine_info(self) -> dict:
        try:
            return {"info": self.client.info(), "version": self.client.version()}
        except DockerException as e:
            raise DockerError(f"Cannot read Docker info: {e}") from e

    def container_log_configs(self) -> list[dict]:
        """Name, state and logging driver/options of every container (helpers excluded)."""
        out = []
        try:
            for c in self.client.api.containers(all=True):
                if (c.get("Labels") or {}).get(HELPER_LABEL):
                    continue
                a = self.client.api.inspect_container(c["Id"])
                lc = (a.get("HostConfig") or {}).get("LogConfig") or {}
                out.append({"name": (c.get("Names") or ["/?"])[0].lstrip("/"), "state": c.get("State"),
                            "driver": lc.get("Type") or "", "options": lc.get("Config") or {}})
        except DockerException as e:
            raise DockerError(f"Cannot read container log settings: {e}") from e
        return out

    def running_image_refs(self) -> list[dict]:
        """Image reference (as written in compose/run), image id and name of every running container."""
        try:
            cs = self.client.api.containers()
        except DockerException as e:
            raise DockerError(f"Cannot list containers: {e}") from e
        return [{"name": (c.get("Names") or ["/?"])[0].lstrip("/"), "ref": c.get("Image") or "", "image_id": c.get("ImageID") or "",
                 "labels": c.get("Labels") or {}} for c in cs if not (c.get("Labels") or {}).get(HELPER_LABEL)]

    def image_repo_digests(self, image_id: str) -> list[str] | None:
        try:
            return self.client.api.inspect_image(image_id).get("RepoDigests") or []
        except NotFound:
            return None
        except DockerException as e:
            raise DockerError(_api_message(e)) from e

    def registry_digest(self, ref: str) -> str:
        """Digest the registry currently serves for a tag (asks the daemon, which talks to the registry)."""
        try:
            d = self.client.api.inspect_distribution(ref)
        except DockerException as e:
            raise DockerError(_api_message(e)) from e
        return ((d or {}).get("Descriptor") or {}).get("digest") or ""

    _LIST_SCRIPT = (
        'cd "/data/$1" 2>/dev/null || { echo "__NOTDIR__"; exit 0; }; '
        'case "$(pwd -P)" in /data|/data/*) ;; *) echo "__OUTSIDE__"; exit 0;; esac; '
        'for f in * .[!.]* ..?*; do if [ -e "$f" ] || [ -L "$f" ]; then stat -c "%F|%s|%Y|%n" "./$f"; fi; done | head -n 2001'
    )

    def list_volume_dir(self, volume: str, rel: str) -> list[dict] | None:
        """Entries of one folder in a volume (read-only helper; symlinks are listed, never followed out of the volume).

        Returns None when the path is not a folder inside the volume."""
        out = self._run_helper(self.helper_image(), ["sh", "-c", self._LIST_SCRIPT, "sh", rel], volume, "ro", timeout=120)
        if "__NOTDIR__" in out or "__OUTSIDE__" in out:
            return None
        entries = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[3].startswith("./"):
                continue
            kind, size, mtime, name = parts
            t = "dir" if "directory" in kind else "link" if "symbolic" in kind else "file" if "regular" in kind else "other"
            try:
                entries.append({"name": name[2:], "type": t, "size": int(size), "mtime": int(mtime)})
            except ValueError:
                continue
        return entries

    def volume_path_archive(self, volume: str, rel: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Tar stream of one file or folder inside a volume (read-only helper)."""
        with self._helper(volume, "ro") as c:
            try:
                stream, _stat = c.get_archive(f"{DATA_MOUNT}/{rel}" if rel else f"{DATA_MOUNT}/.", chunk_size=chunk_size)
            except NotFound as e:
                raise DockerError("Not found in the volume.") from e
            except APIError as e:
                raise DockerError(_api_message(e)) from e
            for chunk in stream:
                yield chunk

    def prune_build_cache(self) -> int:
        """Remove build cache that no build uses (``docker builder prune``). Returns bytes reclaimed."""
        try:
            r = self.client.api.prune_builds()
        except DockerException as e:
            raise DockerError(_api_message(e)) from e
        return int((r or {}).get("SpaceReclaimed") or 0)

    def restart_container(self, cid: str, timeout: int = 10) -> bool:
        try:
            self.client.api.restart(cid, timeout=timeout)
            return True
        except DockerException as e:
            log.warning("Failed to restart container %s: %s", cid[:12], e)
            return False

    def own_container_id(self) -> str | None:
        own = self.own_container()
        return own.id if own is not None else None

    def events(self, stop_event):
        """Yield container events until ``stop_event`` is set (uses its own connection)."""
        client = docker.from_env(timeout=self._timeout)
        try:
            for ev in client.events(decode=True, filters={"type": "container"}):
                if stop_event.is_set():
                    return
                yield ev
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # --- helper image --------------------------------------------------------
    def own_container(self):
        """Best-effort detection of the container we run in."""
        candidates = [os.environ.get("HOSTNAME"), socket.gethostname()]
        for cid in candidates:
            if not cid:
                continue
            try:
                return self.client.containers.get(cid)
            except Exception:
                continue
        return None

    def helper_image(self) -> str:
        if self._helper_image:
            return self._helper_image
        image = self._helper_override
        if not image:
            own = self.own_container()
            if own is not None:
                tags = own.image.tags
                image = tags[0] if tags else own.image.id
        if not image:
            image = FALLBACK_IMAGE
        try:
            self.client.images.get(image)
        except ImageNotFound:
            log.info("Pulling helper image %s", image)
            try:
                self.client.images.pull(image)
            except DockerException as e:
                raise DockerError(f"Helper image {image} not available: {e}") from e
        except DockerException as e:
            raise DockerError(f"Cannot inspect helper image {image}: {e}") from e
        self._helper_image = image
        return image

    def cleanup_helpers(self) -> int:
        n = 0
        try:
            for c in self.client.containers.list(all=True, filters={"label": HELPER_LABEL}):
                try:
                    c.remove(force=True)
                    n += 1
                except DockerException:
                    pass
        except DockerException:
            pass
        return n

    @contextmanager
    def _helper(self, volume: str, mode: str):
        if not valid_volume_name(volume):
            raise DockerError("Invalid volume name.")
        image = self.helper_image()
        try:
            c = self.client.containers.create(
                image,
                command=["true"],
                entrypoint=["true"],
                volumes={volume: {"bind": DATA_MOUNT, "mode": mode}},
                labels={HELPER_LABEL: "1", f"{HELPER_LABEL}.volume": volume},
                network_disabled=True,
                name=f"dockbare-helper-{int(time.time() * 1000)}",
            )
        except DockerException as e:
            raise DockerError(f"Cannot create helper container: {e}") from e
        try:
            yield c
        finally:
            try:
                c.remove(force=True)
            except DockerException as e:
                log.warning("Could not remove helper container %s: %s", c.short_id, e)

    # --- archive streaming ---------------------------------------------------
    def volume_archive_stream(self, volume: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Yield the tar stream of a volume (entries are ``./…``)."""
        with self._helper(volume, "ro") as c:
            try:
                stream, _stat = c.get_archive(f"{DATA_MOUNT}/.", chunk_size=chunk_size)
            except NotFound as e:
                raise DockerError(f"Volume data not found: {e}") from e
            except APIError as e:
                raise DockerError(f"Docker archive error: {e.explanation or e}") from e
            for chunk in stream:
                yield chunk

    def restore_archive(self, volume: str, tar_stream: Iterable[bytes], clear: bool = True) -> None:
        if clear:
            self.clear_volume(volume)
        with self._helper(volume, "rw") as c:
            try:
                ok = c.put_archive(DATA_MOUNT, _IterReader(tar_stream))
            except APIError as e:
                raise DockerError(f"Docker restore error: {e.explanation or e}") from e
            if not ok:
                raise DockerError("Docker rejected the archive.")

    def clear_volume(self, volume: str) -> None:
        if not valid_volume_name(volume):
            raise DockerError("Invalid volume name.")
        image = self.helper_image()
        try:
            self._run_helper(image, ["find", DATA_MOUNT, "-mindepth", "1", "-delete"], volume, "rw")
        except DockerException as e:
            raise DockerError(f"Cannot clear volume {volume}: {e}") from e

    def _run_helper(self, image: str, cmd: list[str], volume: str, mode: str, timeout: int = 600) -> str:
        c = self.client.containers.create(
            image,
            command=cmd,
            entrypoint=[],
            volumes={volume: {"bind": DATA_MOUNT, "mode": mode}},
            labels={HELPER_LABEL: "1"},
            network_disabled=True,
            name=f"dockbare-helper-{int(time.time() * 1000)}",
        )
        try:
            c.start()
            res = c.wait(timeout=timeout)
            out = c.logs(stdout=True, stderr=True).decode("utf-8", "replace")
            if res.get("StatusCode", 1) != 0:
                raise DockerError(f"Helper command failed ({res.get('StatusCode')}): {out.strip()[:500]}")
            return out
        finally:
            try:
                c.remove(force=True)
            except DockerException:
                pass

    def volume_usage_bytes(self, volume: str) -> int | None:
        try:
            out = self._run_helper(self.helper_image(), ["du", "-sk", DATA_MOUNT], volume, "ro", timeout=300)
            return int(out.split()[0]) * 1024
        except Exception as e:
            log.debug("du failed for %s: %s", volume, e)
            return None


class _IterReader:
    """File-like adapter over an iterator of bytes (for requests' streaming upload)."""

    def __init__(self, it: Iterable[bytes]):
        self._it = iter(it)
        self._buf = b""

    def __iter__(self):
        for chunk in self._it:
            if chunk:
                yield chunk

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return self._buf + b"".join(self._it)
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out
