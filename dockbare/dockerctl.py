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
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

log = logging.getLogger("dockbare.docker")

VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
HELPER_LABEL = "dev.dockbare.helper"
FALLBACK_IMAGE = "alpine:3.20"
DATA_MOUNT = "/data"


class DockerError(Exception):
    pass


def valid_volume_name(name: str) -> bool:
    return bool(name) and bool(VOLUME_RE.match(name)) and ".." not in name


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
            return {v.name for v in self.client.volumes.list()}
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
