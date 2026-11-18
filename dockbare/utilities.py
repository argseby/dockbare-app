"""Server utilities: engine health, image update check, scheduled cleanup report,
container start/stop/restart, a read-only volume and backup browser, and volume copy.

None of these delete anything. The ones that change state (container actions, volume
copy) are explicit single actions with their own checks and an event-log entry.
"""
from __future__ import annotations

import io
import logging
import posixpath
import tarfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

from . import archive
from .dockerctl import DockerError, valid_volume_name
from .manager import Manager, ManagerError, fmt_bytes
from .tasks import Task

log = logging.getLogger("dockbare.utilities")

SETTINGS_DEFAULTS = {"report": "off", "report_time": "08:00", "report_updates": True, "report_min_gb": 1.0}
REPORT_SCHEDULES = ("off", "daily", "weekly")
CONTAINER_ACTIONS = ("start", "stop", "restart")
BACKUP_INDEX_CACHE = 3          # backups whose file list is kept in memory
BACKUP_INDEX_MAX = 200_000      # entries read from one backup for browsing


def clean_rel(path: str | None) -> str:
    """A path inside a volume, relative and normalised; refuses anything that could leave it."""
    p = (path or "").strip().strip("/")
    if not p:
        return ""
    if "\x00" in p or "\\" in p:
        raise ManagerError("Invalid path.")
    norm = posixpath.normpath(p)
    if norm in (".", "") or any(part == ".." for part in norm.split("/")):
        raise ManagerError("Invalid path.")
    return norm


class Utilities:
    def __init__(self, manager: Manager):
        self.m = manager
        self._index: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        s = dict(SETTINGS_DEFAULTS)
        s.update(self.m.db.get_setting("utilities", {}) or {})
        return {k: s[k] for k in SETTINGS_DEFAULTS}

    def save_settings(self, data: dict) -> dict:
        s = self.settings()
        if "report" in data:
            if data["report"] not in REPORT_SCHEDULES:
                raise ManagerError("report must be off, daily or weekly.")
            s["report"] = data["report"]
        if "report_time" in data:
            try:
                hh, mm = [int(x) for x in str(data["report_time"]).split(":")]
                assert 0 <= hh < 24 and 0 <= mm < 60
            except Exception as e:  # noqa: BLE001
                raise ManagerError("Time must be HH:MM.") from e
            s["report_time"] = f"{hh:02d}:{mm:02d}"
        if "report_updates" in data:
            s["report_updates"] = bool(data["report_updates"])
        if "report_min_gb" in data:
            try:
                s["report_min_gb"] = max(0.0, min(10_000.0, float(data["report_min_gb"])))
            except (TypeError, ValueError) as e:
                raise ManagerError("Minimum must be a number of GB.") from e
        self.m.db.set_setting("utilities", s)
        self.m.event("info", "settings.saved", "Utilities settings saved")
        self.install_job()
        return s

    def install_job(self) -> None:
        sched = self.m.scheduler
        try:
            sched.remove_job("utilities-report")
        except Exception:  # noqa: BLE001
            pass
        s = self.settings()
        if s["report"] == "off" or not sched.running:
            return
        from apscheduler.triggers.cron import CronTrigger

        hh, mm = s["report_time"].split(":")
        trig = CronTrigger(hour=int(hh), minute=int(mm), day_of_week="mon" if s["report"] == "weekly" else "*", timezone=self.m.settings.timezone)
        sched.add_job(self.send_report, trig, id="utilities-report", replace_existing=True)

    # -------------------------------------------------------- engine health
    def engine(self) -> dict[str, Any]:
        d = self.m.docker.engine_info()
        info, ver = d["info"], d["version"]
        checks: list[dict] = []
        for w in info.get("Warnings") or []:
            checks.append({"level": "warn", "title": "Docker reports a warning", "detail": w})
        try:
            logs = self.m.docker.container_log_configs()
        except DockerError as e:
            logs = []
            checks.append({"level": "warn", "title": "Log settings could not be read", "detail": str(e)})
        unrotated = [c for c in logs if c["driver"] == "json-file" and not c["options"].get("max-size")]
        if unrotated:
            checks.append({"level": "warn", "title": f"{len(unrotated)} container(s) keep their logs without a size limit",
                           "detail": "The json-file log driver grows forever unless max-size is set. Set it in /etc/docker/daemon.json "
                                     '(e.g. {"log-driver": "local"} or {"log-opts": {"max-size": "10m", "max-file": "3"}}), restart Docker, '
                                     "and recreate the containers; or set logging options per service in compose.",
                           "items": [c["name"] for c in unrotated]})
        else:
            checks.append({"level": "ok", "title": "Container logs are size-limited", "detail": "Every container uses a log driver or option with rotation."})
        if info.get("Driver") and info["Driver"] not in ("overlay2", "overlayfs", "btrfs", "zfs"):
            checks.append({"level": "info", "title": f"Storage driver: {info['Driver']}", "detail": "overlay2 is the recommended driver on most Linux hosts."})
        if not info.get("LiveRestoreEnabled"):
            checks.append({"level": "info", "title": "Live restore is off",
                           "detail": 'Containers stop while the Docker daemon restarts (for example during an update). "live-restore": true in daemon.json keeps them running.'})
        if str(info.get("CgroupVersion") or "") == "1":
            checks.append({"level": "info", "title": "cgroup v1", "detail": "The host still uses cgroup v1; current distributions use v2."})
        order = {"warn": 0, "info": 1, "ok": 2}
        checks.sort(key=lambda c: order[c["level"]])
        return {
            "engine": {
                "version": ver.get("Version"), "api": ver.get("ApiVersion"), "os": info.get("OperatingSystem"), "kernel": info.get("KernelVersion"),
                "arch": info.get("Architecture"), "storage_driver": info.get("Driver"), "logging_driver": info.get("LoggingDriver"),
                "cgroup": f"{info.get('CgroupDriver') or '?'} v{info.get('CgroupVersion') or '?'}", "root_dir": info.get("DockerRootDir"),
                "cpus": info.get("NCPU"), "memory": info.get("MemTotal"), "containers": info.get("Containers"),
                "running": info.get("ContainersRunning"), "images": info.get("Images"), "live_restore": bool(info.get("LiveRestoreEnabled")),
            },
            "checks": checks, "logs": logs,
        }

    # -------------------------------------------------------- image updates
    def last_update_check(self) -> dict | None:
        return self.m.db.get_setting("image_updates")

    def start_update_check(self) -> Task:
        if any(t.kind == "image-updates" for t in self.m.tasks.list(active_only=True)):
            raise ManagerError("An update check is already running.")
        return self.m.tasks.submit("image-updates", "Check images for updates", lambda t: self.check_updates(t))

    def check_updates(self, task: Task | None = None) -> dict:
        """Compare each running container's image with what its registry serves now. Reports only."""
        d = self.m.docker
        by_ref: dict[str, dict] = {}
        for c in d.running_image_refs():
            e = by_ref.setdefault(c["ref"], {"ref": c["ref"], "image_id": c["image_id"], "containers": []})
            e["containers"].append(c["name"])
        items = []
        for ref, e in sorted(by_ref.items()):
            item = {"ref": ref, "containers": e["containers"], "status": "unknown", "detail": ""}
            if task:
                task.add_log(f"Checking {ref}")
            if not ref or ref.startswith("sha256:"):
                item.update(status="skipped", detail="started from an image id, not a tag")
            elif "@sha256:" in ref:
                item.update(status="pinned", detail="pinned to a digest; it never changes")
            else:
                try:
                    local = d.image_repo_digests(e["image_id"])
                    if not local:
                        item.update(status="local", detail="built locally; no registry copy to compare with")
                    else:
                        remote = d.registry_digest(ref)
                        if not remote:
                            item.update(detail="the registry did not return a digest")
                        elif any(x.endswith("@" + remote) for x in local):
                            item.update(status="current", detail="up to date")
                        else:
                            item.update(status="update", detail="a newer image is available", remote_digest=remote)
                except DockerError as ex:
                    item.update(status="unknown", detail=f"could not ask the registry: {ex}"[:300])
            items.append(item)
        result = {"checked_at": time.time(), "items": items, "updates": sum(1 for i in items if i["status"] == "update")}
        self.m.db.set_setting("image_updates", result)
        msg = f"Image update check: {result['updates']} of {len(items)} image(s) have a newer version"
        if task:
            task.add_log(msg)
        self.m.event("info", "utilities.updates_checked", msg, data={"updates": [i["ref"] for i in items if i["status"] == "update"]})
        return result

    # ---------------------------------------------------- scheduled report
    def send_report(self, force: bool = False) -> dict:
        """Notify how much could be freed and which images have updates. Never deletes anything."""
        s = self.settings()
        cl = getattr(self.m, "cleanup", None)
        scan = cl.scan() if cl else None
        free_imgs = [i for i in (scan or {}).get("images", []) if not i["protected"]]
        free_vols = [v for v in (scan or {}).get("volumes", []) if not v["protected"]]
        free_cons = [c for c in (scan or {}).get("containers", []) if not c["protected"]]
        disk = (scan or {}).get("disk") or {}
        reclaim = sum(disk.get(k, 0) or 0 for k in ("images_unused", "containers_unused", "volumes_unused", "build_cache_unused"))
        updates = None
        if s["report_updates"]:
            try:
                updates = self.check_updates()
            except Exception as e:  # noqa: BLE001
                log.warning("Update check for the report failed: %s", e)
        upd = [i for i in (updates or {}).get("items", []) if i["status"] == "update"]
        worth = reclaim >= s["report_min_gb"] * 1024 ** 3 or bool(upd)
        lines = [f"About {fmt_bytes(reclaim)} could be freed on {self.m.hostname}:",
                 f"- {len(free_imgs)} unused image(s), {len(free_vols)} unused volume(s), {len(free_cons)} stopped container(s)",
                 f"- {fmt_bytes(disk.get('build_cache_unused') or 0)} unused build cache"]
        if updates is not None:
            lines.append(f"- {len(upd)} image(s) with a newer version" + (": " + ", ".join(i["ref"] for i in upd[:10]) if upd else ""))
        lines.append("Nothing was deleted. Review it under Utilities in Dockbare.")
        msg = "\n".join(lines)
        sent = {}
        if worth or force:
            sent = self.m.notifier.notify("utilities", f"Cleanup report: {fmt_bytes(reclaim)} could be freed", msg, "info", force=force,
                                          headline="Cleanup and update report", subject_name=self.m.hostname)
        self.m.event("info", "utilities.report", msg.splitlines()[0] + (" (sent)" if sent else " (nothing worth sending)" if not worth else ""),
                     data={"reclaimable": reclaim, "updates": len(upd)})
        return {"reclaimable": reclaim, "updates": len(upd), "sent": sent, "message": msg, "worth": worth}

    # ---------------------------------------------------- container actions
    def container_action(self, name: str, action: str) -> dict:
        if action not in CONTAINER_ACTIONS:
            raise ManagerError("Action must be start, stop or restart.")
        d = self.m.docker
        c = next((x for x in d.list_containers() if x["name"] == name), None)
        if c is None:
            raise ManagerError(f"Container '{name}' not found.")
        own = ""
        try:
            own = d.own_container_id() or ""
        except Exception:  # noqa: BLE001
            pass
        if own and c["id"] == own and action != "start":
            raise ManagerError("Dockbare cannot stop or restart its own container from here.")
        ok = {"start": d.start_container, "stop": d.stop_container, "restart": d.restart_container}[action](c["id"])
        verb = {"start": "Started", "stop": "Stopped", "restart": "Restarted"}[action]
        if not ok:
            self.m.event("error", "container.action_failed", f"{action.capitalize()} of container '{name}' failed", data={"container": name, "action": action})
            raise ManagerError(f"Docker could not {action} '{name}'.")
        self.m.event("warning", "container.action", f"{verb} container '{name}' from the UI", data={"container": name, "action": action})
        if self.m.uptime:
            try:
                self.m.uptime._repoll()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "container": name, "action": action}

    # --------------------------------------------------------- volume browse
    def _volume(self, name: str) -> str:
        if not valid_volume_name(name) or not self.m.docker.volume_exists(name):
            raise ManagerError("Unknown volume.")
        return name

    def browse_volume(self, volume: str, path: str = "") -> dict:
        volume = self._volume(volume)
        rel = clean_rel(path)
        entries = self.m.docker.list_volume_dir(volume, rel)
        if entries is None:
            raise ManagerError("Not a folder in this volume.")
        truncated = len(entries) > 2000
        entries = sorted(entries[:2000], key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"source": "volume", "volume": volume, "path": rel, "entries": entries, "truncated": truncated}

    def volume_download(self, volume: str, path: str) -> tuple[str, str, Iterator[bytes]]:
        """(filename, mimetype, stream): a single file as itself, a folder as .tar."""
        volume = self._volume(volume)
        rel = clean_rel(path)
        parent, base = posixpath.split(rel)
        listing = self.m.docker.list_volume_dir(volume, parent)
        entry = next((e for e in listing or [] if e["name"] == base), None) if rel else {"type": "dir", "name": volume}
        if entry is None:
            raise ManagerError("Not found in this volume.")
        if entry["type"] == "link":
            raise ManagerError("Symbolic links are not downloaded.")
        self.m.event("info", "utilities.download", f"Downloaded {'folder' if entry['type'] == 'dir' else 'file'} /{rel} from volume {volume}", volume=volume)
        stream = self.m.docker.volume_path_archive(volume, rel)
        if entry["type"] == "dir":
            return f"{(base or volume)}.tar", "application/x-tar", stream
        return base, "application/octet-stream", _single_file(stream)

    # --------------------------------------------------------- backup browse
    def _backup_index(self, path: Path) -> dict:
        key = str(path)
        mtime = path.stat().st_mtime
        with self._index_lock:
            hit = self._index.get(key)
            if hit and hit[0] == mtime:
                self._index.move_to_end(key)
                return hit[1]
        idx: dict[str, dict] = {}
        truncated = False
        reader = archive._StreamReader(archive.open_backup_stream(path, self.m.keyring))
        try:
            with tarfile.open(fileobj=io.BufferedReader(reader, archive.CHUNK), mode="r|") as tar:
                for mbr in tar:
                    name = clean_member(mbr.name)
                    if not name:
                        continue
                    if len(idx) >= BACKUP_INDEX_MAX:
                        truncated = True
                        break
                    t = "dir" if mbr.isdir() else "link" if (mbr.issym() or mbr.islnk()) else "file" if mbr.isfile() else "other"
                    idx[name] = {"type": t, "size": mbr.size if t == "file" else 0, "mtime": int(mbr.mtime)}
                    # parents that have no own entry in the tar
                    parent = posixpath.dirname(name)
                    while parent and parent not in idx:
                        idx[parent] = {"type": "dir", "size": 0, "mtime": int(mbr.mtime)}
                        parent = posixpath.dirname(parent)
        except (tarfile.TarError, OSError, archive.ArchiveError) as e:
            raise ManagerError(f"Cannot read the backup: {e}") from e
        except Exception as e:  # noqa: BLE001  (crypto errors, wrong key)
            raise ManagerError(f"Cannot read the backup: {e}") from e
        data = {"entries": idx, "truncated": truncated}
        with self._index_lock:
            self._index[key] = (mtime, data)
            while len(self._index) > BACKUP_INDEX_CACHE:
                self._index.popitem(last=False)
        return data

    def browse_backup(self, backup: str, path: str = "") -> dict:
        p = self.m.resolve_backup_path(backup)
        rel = clean_rel(path)
        idx = self._backup_index(p)
        ents = idx["entries"]
        if rel and ents.get(rel, {}).get("type") != "dir":
            raise ManagerError("Not a folder in this backup.")
        out = []
        for name, e in ents.items():
            if posixpath.dirname(name) == rel:
                out.append({"name": posixpath.basename(name), **e})
        out.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return {"source": "backup", "backup": str(p.relative_to(self.m.settings.backup_dir.resolve())), "path": rel,
                "entries": out[:2000], "truncated": idx["truncated"] or len(out) > 2000}

    def backup_download(self, backup: str, path: str) -> tuple[str, Iterator[bytes]]:
        p = self.m.resolve_backup_path(backup)
        rel = clean_rel(path)
        if not rel or self._backup_index(p)["entries"].get(rel, {}).get("type") != "file":
            raise ManagerError("Only single files can be downloaded from a backup.")
        self.m.event("info", "utilities.download", f"Downloaded /{rel} from backup {p.name}", volume=p.parent.name)

        def gen() -> Iterator[bytes]:
            reader = archive._StreamReader(archive.open_backup_stream(p, self.m.keyring))
            with tarfile.open(fileobj=io.BufferedReader(reader, archive.CHUNK), mode="r|") as tar:
                for mbr in tar:
                    if clean_member(mbr.name) == rel and mbr.isfile():
                        f = tar.extractfile(mbr)
                        while f is not None:
                            chunk = f.read(archive.CHUNK)
                            if not chunk:
                                break
                            yield chunk
                        return
        return posixpath.basename(rel), gen()

    # ------------------------------------------------------------ volume copy
    def start_copy(self, source: str, target: str, *, stop_containers: bool = True) -> Task:
        if not valid_volume_name(source) or not self.m.docker.volume_exists(source):
            raise ManagerError("Unknown source volume.")
        if not valid_volume_name(target):
            raise ManagerError("Invalid target name (letters, digits, _ . - only).")
        if source == target:
            raise ManagerError("Source and target are the same.")
        if self.m.docker.volume_exists(target):
            raise ManagerError(f"A volume named '{target}' already exists. Copying never overwrites a volume.")
        if self.m.tasks.active_for_volume(source) or self.m.tasks.active_for_volume(target):
            raise ManagerError("A task for one of these volumes is already running.")
        return self.m.tasks.submit("volume-copy", f"Copy {source} → {target}", lambda t: self._copy(t, source, target, stop_containers),
                                   volume=source, meta={"target": target})

    def _copy(self, task: Task, source: str, target: str, stop_containers: bool) -> dict:
        d = self.m.docker
        stopped: list[dict] = []
        counted = {"bytes": 0}

        def counting(it):
            for chunk in it:
                counted["bytes"] += len(chunk)
                task.set_progress(bytes=counted["bytes"])
                yield chunk
        try:
            if d.volume_exists(target):
                raise ManagerError(f"A volume named '{target}' appeared in the meantime; nothing was copied.")
            if stop_containers:
                for c in d.containers_using(source):
                    if c["status"] == "running":
                        task.add_log(f"Stopping {c['name']} for a consistent copy")
                        if not d.stop_container(c["id"]):
                            raise ManagerError(f"Could not stop {c['name']}; nothing was copied.")
                        stopped.append(c)
            d.create_volume(target)
            task.add_log(f"Created volume {target}; copying data")
            try:
                d.restore_archive(target, counting(d.volume_archive_stream(source)), clear=False)
            except Exception:
                task.add_log(f"Copy failed; the new volume {target} is left in place for inspection (the source is untouched)")
                raise
        finally:
            for c in stopped:
                task.add_log(f"Starting {c['name']} again")
                if not d.start_container(c["id"]):
                    self.m.event("error", "container.start_failed", f"Container {c['name']} did not start after copying {source}", volume=source)
        msg = f"Copied volume {source} to {target} ({fmt_bytes(counted['bytes'])} of tar data); {source} is unchanged"
        task.add_log(msg)
        self.m.event("success", "utilities.volume_copied", msg, volume=source, data={"target": target, "bytes": counted["bytes"]})
        return {"source": source, "target": target, "bytes": counted["bytes"]}


def clean_member(name: str) -> str:
    """Tar member name ("./a/b", "a/b/") → "a/b"; "" for the root entry."""
    n = (name or "").lstrip("/")
    while n.startswith("./"):
        n = n[2:]
    return "" if n in (".", "") else n.strip("/")


def _single_file(tar_stream: Iterator[bytes]) -> Iterator[bytes]:
    """Content of the one regular file in a tar stream (as returned by the Docker archive API)."""
    reader = archive._StreamReader(tar_stream)
    with tarfile.open(fileobj=io.BufferedReader(reader, archive.CHUNK), mode="r|") as tar:
        for mbr in tar:
            if mbr.isfile():
                f = tar.extractfile(mbr)
                while f is not None:
                    chunk = f.read(archive.CHUNK)
                    if not chunk:
                        break
                    yield chunk
                return
