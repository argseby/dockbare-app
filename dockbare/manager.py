"""Orchestration: backups, uploads, sync, schedules, pruning, restore, remotes."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import __version__, archive, crypto
from .config import Settings
from .db import Database, row_to_dict, rows_to_dicts
from .dockerctl import DockerCtl, DockerError, valid_volume_name
from .notify import Notifier, DEFAULT_SETTINGS as NOTIFY_DEFAULTS
from .remotes import Remote, RemoteError, RemoteInfo, SECRET_FIELDS, REMOTE_TYPES, build_remote
from .tasks import Task, TaskManager

log = logging.getLogger("dockbare.manager")

GENERAL_DEFAULTS = {
    "compression_level": 6,
    "encrypt_default": True,
    "stop_containers_default": True,
    "max_parallel_backups": 2,
    "events_keep_days": 365,
    "remote_health_interval_minutes": 15,
    "sync_enabled": False,
    "sync_interval_minutes": 60,
    "display_timezone": "",
    "config_autopush": True,
}

NOTIFY_SECRET_PATHS = (("ntfy", "token"), ("email", "password"), ("webhook", "secret"))


class ManagerError(Exception):
    pass


class Manager:
    def __init__(self, settings: Settings, db: Database, dockerctl: DockerCtl, keyring: crypto.Keyring,
                 tasks: TaskManager, secretbox: crypto.SecretBox):
        self.settings = settings
        self.db = db
        self.docker = dockerctl
        self.keyring = keyring
        self.tasks = tasks
        self.secretbox = secretbox
        self.notifier = Notifier(self._notify_settings_plain)
        self.scheduler = BackgroundScheduler(timezone=settings.timezone, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
        self._volume_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._backup_slots = threading.BoundedSemaphore(max(1, int(self.general().get("max_parallel_backups", 2))))
        self.remote_health: dict[str, dict] = {}
        self._last_scan = 0.0
        self._scan_lock = threading.Lock()
        self.hostname = socket.gethostname()
        self.started_at = time.time()
        self.peers = None  # set by app wiring
        self.uptime = None  # set by app wiring
        self.configsync = None  # set by app wiring

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        self.migrate_legacy()
        self.sync_env_remotes()
        self.docker.cleanup_helpers()
        self.scan_backups()
        self.scheduler.start()
        for s in self.list_schedules():
            self._install_schedule_job(s["volume"], s)
        self._install_sync_job()
        self._install_health_job()
        self.scheduler.add_job(self.housekeeping, IntervalTrigger(hours=6), id="housekeeping", replace_existing=True)
        self.scheduler.add_job(self.check_all_remotes, "date", id="health-initial", replace_existing=True)
        self.event("info", "app.start", f"Dockbare {__version__} started on {self.hostname}")

    def stop(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- settings
    INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

    def instance_name(self) -> str:
        """Stable name of this installation, used for per-instance folders on remotes."""
        if self.settings.instance_name:
            return self.settings.instance_name
        name = self.db.get_setting("instance_name")
        if not name:
            name = "dockbare-" + secrets.token_hex(2)
            self.db.set_setting("instance_name", name)
        return name

    def set_instance_name(self, name: str) -> str:
        if self.settings.instance_name:
            raise ManagerError("The instance name comes from DOCKBARE_INSTANCE_NAME in .env; change it there.")
        name = (name or "").strip()
        if not self.INSTANCE_RE.match(name):
            raise ManagerError("Instance name may contain letters, digits, '.', '_' and '-' (max 64).")
        old = self.instance_name()
        self.db.set_setting("instance_name", name)
        if old != name:
            self.event("warning", "settings.instance_renamed", f"Instance renamed from '{old}' to '{name}'; remotes now use the new folder")
        return name

    def general(self) -> dict:
        g = dict(GENERAL_DEFAULTS)
        g.update(self.db.get_setting("general", {}) or {})
        return g

    def save_general(self, data: dict) -> dict:
        g = self.general()
        for k in GENERAL_DEFAULTS:
            if k in data:
                g[k] = data[k]
        g["compression_level"] = min(9, max(1, int(g.get("compression_level", 6))))
        g["max_parallel_backups"] = min(8, max(1, int(g.get("max_parallel_backups", 2))))
        g["events_keep_days"] = min(3650, max(7, int(g.get("events_keep_days", 365))))
        g["sync_interval_minutes"] = min(100000, max(5, int(g.get("sync_interval_minutes", 60))))
        g["remote_health_interval_minutes"] = min(100000, max(1, int(g.get("remote_health_interval_minutes", 15))))
        g["encrypt_default"] = bool(g.get("encrypt_default"))
        g["stop_containers_default"] = bool(g.get("stop_containers_default"))
        g["sync_enabled"] = bool(g.get("sync_enabled"))
        g["config_autopush"] = bool(g.get("config_autopush", True))
        self.db.set_setting("general", g)
        self._backup_slots = threading.BoundedSemaphore(g["max_parallel_backups"])
        self._install_sync_job()
        self._install_health_job()
        self.event("info", "settings.saved", "General settings saved")
        return g

    def _notify_settings_plain(self) -> dict:
        stored = self.db.get_setting("notifications", {}) or {}
        out = json.loads(json.dumps(stored))
        for section, key in NOTIFY_SECRET_PATHS:
            if isinstance(out.get(section), dict) and out[section].get(key):
                try:
                    out[section][key] = self.secretbox.decrypt(out[section][key])
                except crypto.CryptoError as e:
                    log.error("Cannot decrypt notification secret %s.%s: %s", section, key, e)
                    out[section][key] = ""
        # values from .env / environment take precedence
        for section, vals in self.settings.notify_env.items():
            out.setdefault(section, {}).update(vals)
        return out

    def notify_settings_public(self) -> dict:
        s = self.notifier.settings
        for section, key in NOTIFY_SECRET_PATHS:
            s[section][key + "_set"] = bool(s[section].get(key))
            s[section][key] = ""
        s["env_locked"] = {section: sorted(vals.keys()) for section, vals in self.settings.notify_env.items()}
        return s

    def save_notify_settings(self, data: dict) -> dict:
        current = self._notify_settings_plain()
        merged = json.loads(json.dumps(NOTIFY_DEFAULTS))
        for section in ("ntfy", "email", "webhook", "events"):
            if isinstance(current.get(section), dict):
                merged[section].update(current[section])
            if isinstance(data.get(section), dict):
                incoming = dict(data[section])
                for sec_section, key in NOTIFY_SECRET_PATHS:
                    if sec_section == section and key in incoming and incoming[key] in ("", None):
                        incoming.pop(key)  # blank → keep existing secret
                for key in self.settings.notify_env.get(section, {}):
                    incoming.pop(key, None)  # locked by .env; never persisted from the UI
                merged[section].update(incoming)
        for section, key in NOTIFY_SECRET_PATHS:
            if merged[section].get(key):
                merged[section][key] = self.secretbox.encrypt(merged[section][key])
        self.db.set_setting("notifications", merged)
        self.event("info", "settings.saved", "Notification settings saved")
        return self.notify_settings_public()

    # ----------------------------------------------------------------- events
    def event(self, level: str, kind: str, message: str, **kw) -> None:
        try:
            self.db.add_event(level, kind, message, **kw)
        except Exception:  # noqa: BLE001
            log.exception("Failed to store event")
        getattr(log, "warning" if level in ("warning", "error") else "info")("%s %s", kind, message)
        cs = self.configsync
        if cs is not None and kind in cs.TRIGGER_KINDS and self.scheduler.running:
            cs.schedule_push()

    def list_events(self, *, limit: int = 200, level: str | None = None, kind: str | None = None,
                    volume: str | None = None, q: str | None = None, since: float | None = None,
                    before_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if level:
            sql += " AND level=?"
            params.append(level)
        if kind:
            sql += " AND kind LIKE ?"
            params.append(kind + "%")
        if volume:
            sql += " AND volume=?"
            params.append(volume)
        if q:
            sql += " AND (message LIKE ? OR volume LIKE ? OR backup LIKE ?)"
            params += [f"%{q}%"] * 3
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if before_id:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(1000, max(1, limit)))
        rows = rows_to_dicts(self.db.query(sql, tuple(params)))
        for r in rows:
            if r.get("data"):
                try:
                    r["data"] = json.loads(r["data"])
                except json.JSONDecodeError:
                    pass
        return rows

    # ---------------------------------------------------------------- remotes
    def list_remotes(self, with_secrets: bool = False) -> list[dict]:
        out = []
        for row in self.db.query("SELECT * FROM remotes ORDER BY name COLLATE NOCASE"):
            out.append(self._remote_row(row, with_secrets))
        return out

    def _remote_row(self, row, with_secrets: bool) -> dict:
        d = row_to_dict(row)
        cfg = json.loads(d.pop("config") or "{}")
        if with_secrets:
            for k in SECRET_FIELDS:
                if cfg.get(k):
                    cfg[k] = self.secretbox.decrypt(cfg[k])
        else:
            for k in SECRET_FIELDS:
                cfg[k + "_set"] = bool(cfg.get(k))
                cfg.pop(k, None)
        d["config"] = cfg
        d["enabled"] = bool(d["enabled"])
        d["health"] = self.remote_health.get(d["id"])
        d["source"] = "env" if d["id"].startswith("env-") else "ui"
        d["instance"] = self.instance_name() if cfg.get("instance_dir") else None
        return d

    def get_remote(self, remote_id: str, with_secrets: bool = False) -> dict | None:
        row = self.db.one("SELECT * FROM remotes WHERE id=?", (remote_id,))
        return self._remote_row(row, with_secrets) if row else None

    def build_remote(self, remote_id: str) -> Remote:
        r = self.get_remote(remote_id, with_secrets=True)
        if not r:
            raise ManagerError(f"Remote {remote_id} not found.")
        return build_remote(RemoteInfo(r["id"], r["name"], r["type"], r["enabled"], r["config"], instance=self.instance_name()))

    def _remote_from_payload(self, data: dict, existing_id: str | None = None) -> RemoteInfo:
        rtype = (data.get("type") or "ftp").lower()
        if rtype not in REMOTE_TYPES:
            raise ManagerError("Unsupported remote type.")
        name = (data.get("name") or "").strip()
        if not name:
            raise ManagerError("Remote name is required.")
        cfg = dict(data.get("config") or {})
        if existing_id:
            current = self.get_remote(existing_id, with_secrets=True)
            if current:
                for k in SECRET_FIELDS:
                    if cfg.get(k) in ("", None) and current["config"].get(k):
                        cfg[k] = current["config"][k]
        cfg = {k: v for k, v in cfg.items() if not k.endswith("_set")}
        cfg["host"] = (cfg.get("host") or "").strip()
        if not cfg["host"]:
            raise ManagerError("Host is required.")
        cfg["port"] = int(cfg.get("port") or (21 if rtype == "ftp" else 22))
        if not (0 < cfg["port"] < 65536):
            raise ManagerError("Port is out of range.")
        cfg["base_path"] = (cfg.get("base_path") or "").strip()
        if not cfg["base_path"] or cfg["base_path"].strip("/") == "":
            raise ManagerError("Base path is required and must not be the root directory.")
        cfg["instance_dir"] = bool(cfg.get("instance_dir", True))
        return RemoteInfo(existing_id or uuid.uuid4().hex[:12], name, rtype, bool(data.get("enabled", True)), cfg, instance=self.instance_name())

    def save_remote(self, data: dict, remote_id: str | None = None, *, allow_env: bool = False) -> dict:
        if remote_id and remote_id.startswith("env-") and not allow_env:
            raise ManagerError("This remote is configured in .env (DOCKBARE_REMOTE_*); change it there and restart.")
        info = self._remote_from_payload(data, remote_id)
        cfg = dict(info.config)
        for k in SECRET_FIELDS:
            if cfg.get(k):
                cfg[k] = self.secretbox.encrypt(cfg[k])
        now = time.time()
        if remote_id and self.get_remote(remote_id):
            self.db.execute("UPDATE remotes SET name=?, type=?, enabled=?, config=?, updated_at=? WHERE id=?",
                            (info.name, info.type, int(info.enabled), json.dumps(cfg), now, remote_id))
            if not allow_env:
                self.event("info", "remote.updated", f"Remote '{info.name}' updated", remote_id=remote_id)
        elif remote_id and not allow_env:
            raise ManagerError("Remote not found.")
        else:
            self.db.execute("INSERT INTO remotes(id, name, type, enabled, config, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                            (info.id, info.name, info.type, int(info.enabled), json.dumps(cfg), now, now))
            self.event("info", "remote.created", f"Remote '{info.name}' added ({info.type}{', from .env' if allow_env else ''})", remote_id=info.id)
        self.remote_health.pop(info.id, None)
        threading.Thread(target=self.check_remote_health, args=(info.id,), daemon=True).start()
        return self.get_remote(info.id)

    def delete_remote(self, remote_id: str, *, allow_env: bool = False) -> None:
        r = self.get_remote(remote_id)
        if not r:
            raise ManagerError("Remote not found.")
        if r["source"] == "env" and not allow_env:
            raise ManagerError("This remote is configured in .env (DOCKBARE_REMOTE_*); remove it there and restart.")
        self.db.execute("DELETE FROM remotes WHERE id=?", (remote_id,))
        self.db.execute("DELETE FROM uploads WHERE remote_id=?", (remote_id,))
        for s in self.list_schedules():
            if remote_id in s.get("remotes", []):
                s["remotes"] = [x for x in s["remotes"] if x != remote_id]
                self._store_schedule(s["volume"], s)
        self.remote_health.pop(remote_id, None)
        self.event("warning", "remote.deleted", f"Remote '{r['name']}' deleted", remote_id=remote_id)

    def sync_env_remotes(self) -> None:
        """Mirror DOCKBARE_REMOTE_<n>_* blocks into the remotes table (ids ``env-<n>``)."""
        wanted = {f"env-{r['index']}": r for r in self.settings.env_remotes}
        for rid, r in wanted.items():
            try:
                self.save_remote({"name": r["name"], "type": r["type"], "enabled": r["enabled"], "config": r["config"]}, rid, allow_env=True)
            except ManagerError as e:
                log.error("Invalid env remote %s: %s", rid, e)
        for row in self.db.query("SELECT id, name FROM remotes WHERE id LIKE 'env-%'"):
            if row["id"] not in wanted:
                self.delete_remote(row["id"], allow_env=True)
                log.info("Removed env remote %s (no longer in environment)", row["name"])

    def test_remote(self, data: dict | None = None, remote_id: str | None = None) -> tuple[bool, str]:
        if data is not None:
            info = self._remote_from_payload(data, remote_id)
            remote = build_remote(info)
        elif remote_id:
            remote = self.build_remote(remote_id)
        else:
            raise ManagerError("Nothing to test.")
        ok, msg = remote.test()
        learned = getattr(remote, "learned_fingerprint", None)
        if ok and learned and remote_id and not remote.config.get("host_key_fingerprint"):
            # pin the host key (TOFU) on the stored remote
            r = self.get_remote(remote_id, with_secrets=True)
            if r:
                r["config"]["host_key_fingerprint"] = learned
                self.save_remote({"name": r["name"], "type": r["type"], "enabled": r["enabled"], "config": r["config"]}, remote_id, allow_env=True)
        return ok, msg

    def check_remote_health(self, remote_id: str) -> dict:
        r = self.get_remote(remote_id)
        if not r:
            return {"ok": False, "message": "not found", "checked_at": time.time()}
        if not r["enabled"]:
            h = {"ok": None, "message": "disabled", "checked_at": time.time()}
        else:
            try:
                ok, msg = self.test_remote(remote_id=remote_id)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
            h = {"ok": ok, "message": msg, "checked_at": time.time()}
            prev = self.remote_health.get(remote_id)
            if prev and prev.get("ok") and not ok:
                self.event("warning", "remote.unreachable", f"Remote '{r['name']}' became unreachable: {msg}", remote_id=remote_id)
            elif prev and prev.get("ok") is False and ok:
                self.event("info", "remote.reachable", f"Remote '{r['name']}' is reachable again", remote_id=remote_id)
        self.remote_health[remote_id] = h
        return h

    def check_all_remotes(self) -> None:
        for r in self.list_remotes():
            self.check_remote_health(r["id"])

    def _install_health_job(self) -> None:
        minutes = int(self.general().get("remote_health_interval_minutes", 15))
        self.scheduler.add_job(self.check_all_remotes, IntervalTrigger(minutes=minutes), id="remote-health", replace_existing=True)

    # ---------------------------------------------------------------- backups
    def resolve_backup_path(self, user_path: str, must_exist: bool = True) -> Path:
        """Confine a user-supplied path to the backup directory."""
        if not user_path or "\x00" in user_path:
            raise ManagerError("Invalid path.")
        root = self.settings.backup_dir.resolve()
        p = Path(user_path)
        if not p.is_absolute():
            p = root / p
        try:
            resolved = p.resolve()
        except OSError as e:
            raise ManagerError(f"Invalid path: {e}") from e
        if resolved != root and root not in resolved.parents:
            raise ManagerError("Path is outside the backup directory.")
        if not archive.is_backup_filename(resolved.name):
            raise ManagerError("Not a backup file.")
        if resolved.parent.parent != root:
            raise ManagerError("Unexpected backup location.")
        if must_exist and not resolved.is_file():
            raise ManagerError("Backup file not found.")
        return resolved

    def scan_backups_if_stale(self, max_age: float = 5.0) -> None:
        """Re-scan the backup directory unless it was scanned within ``max_age`` seconds."""
        if time.time() - self._last_scan > max_age:
            self.scan_backups()

    def scan_backups(self) -> int:
        """Synchronise the backups table with the filesystem (files may be added/removed externally)."""
        with self._scan_lock:
            self._last_scan = time.time()
            return self._scan_backups()

    def _scan_backups(self) -> int:
        root = self.settings.backup_dir
        known = {r["path"]: row_to_dict(r) for r in self.db.query("SELECT * FROM backups")}
        seen: set[str] = set()
        added = 0
        for vdir in sorted(p for p in root.iterdir() if p.is_dir()):
            volume = vdir.name
            for f in sorted(vdir.iterdir()):
                if not f.is_file() or not archive.is_backup_filename(f.name):
                    continue
                path = str(f)
                seen.add(path)
                try:
                    st = f.stat()
                except OSError:
                    continue
                row = known.get(path)
                if row and row["size"] == st.st_size:
                    continue
                ts, encrypted = archive.parse_backup_filename(f.name)
                meta = archive.read_meta(f) or {}
                created = meta.get("created_at") or (ts.timestamp() if ts else st.st_mtime)
                enc = bool(meta.get("encrypted", encrypted)) or archive.is_encrypted_file(f)
                self.db.execute(
                    "INSERT INTO backups(path, volume, filename, size, sha256, encrypted, created_at, source, meta) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET size=excluded.size, encrypted=excluded.encrypted, meta=excluded.meta",
                    (path, volume, f.name, st.st_size, meta.get("sha256"), int(enc), created,
                     meta.get("source", "backup" if meta else "found"), json.dumps(meta) if meta else None))
                added += 1
        for path in set(known) - seen:
            self.db.execute("DELETE FROM backups WHERE path=?", (path,))
            self.db.execute("DELETE FROM uploads WHERE path=?", (path,))
        return added

    def list_backups(self, volume: str | None = None) -> list[dict]:
        self.scan_backups_if_stale()
        sql = "SELECT * FROM backups" + (" WHERE volume=?" if volume else "") + " ORDER BY volume, created_at DESC"
        rows = rows_to_dicts(self.db.query(sql, (volume,) if volume else ()))
        uploads: dict[str, dict] = {}
        for u in self.db.query("SELECT * FROM uploads"):
            uploads.setdefault(u["path"], {})[u["remote_id"]] = {"status": u["status"], "updated_at": u["updated_at"], "error": u["error"]}
        for r in rows:
            r["encrypted"] = bool(r["encrypted"])
            r["meta"] = json.loads(r["meta"]) if r.get("meta") else {}
            r["uploads"] = uploads.get(r["path"], {})
            r["rel_path"] = os.path.relpath(r["path"], self.settings.backup_dir)
        return rows

    def get_backup(self, path: str) -> dict | None:
        p = str(self.resolve_backup_path(path, must_exist=False))
        rows = [b for b in self.list_backups() if b["path"] == p]
        return rows[0] if rows else None

    def _volume_lock(self, volume: str) -> threading.Lock:
        with self._locks_guard:
            return self._volume_locks.setdefault(volume, threading.Lock())

    def _resolve_backup_options(self, volume: str, *, remotes: list[str] | None, encrypt: bool | None,
                                stop_containers: bool | None, notify: bool | None = None) -> dict:
        g = self.general()
        sched = self.get_schedule(volume) or {}
        if remotes is None:
            remotes = sched.get("remotes") if sched else [r["id"] for r in self.list_remotes() if r["enabled"]]
        valid_ids = {r["id"] for r in self.list_remotes() if r["enabled"]}
        remotes = [r for r in (remotes or []) if r in valid_ids]
        if encrypt is None:
            encrypt = sched.get("encrypt") if sched.get("encrypt") is not None else g["encrypt_default"]
        encrypt = bool(encrypt) and self.keyring.available
        if stop_containers is None:
            stop_containers = sched.get("stop_containers", g["stop_containers_default"])
        return {"remotes": remotes, "encrypt": encrypt, "stop_containers": bool(stop_containers),
                "notify": True if notify is None else bool(notify), "compression_level": int(g["compression_level"])}

    def start_backup(self, volumes: list[str], *, remotes: list[str] | None = None, encrypt: bool | None = None,
                     stop_containers: bool | None = None, source: str = "manual") -> list[Task]:
        names = self.docker.volume_names()
        tasks = []
        for volume in volumes:
            if not valid_volume_name(volume) or volume not in names:
                raise ManagerError(f"Unknown volume: {volume}")
            if self.tasks.active_for_volume(volume):
                raise ManagerError(f"A task for volume '{volume}' is already running.")
            opts = self._resolve_backup_options(volume, remotes=remotes, encrypt=encrypt, stop_containers=stop_containers)
            tasks.append(self.tasks.submit("backup", f"Backup {volume}", lambda t, v=volume, o=opts: self._do_backup(t, v, o, source),
                                           volume=volume, meta={"source": source}))
        return tasks

    def _do_backup(self, task: Task, volume: str, opts: dict, source: str) -> dict:
        lock = self._volume_lock(volume)
        if not lock.acquire(blocking=False):
            raise ManagerError(f"Volume '{volume}' is busy.")
        stopped: list[dict] = []
        started = time.time()
        try:
            with self._backup_slots:
                task.add_log(f"Backup of '{volume}' started ({source}); encrypt={opts['encrypt']}, remotes={len(opts['remotes'])}")
                self.event("info", "backup.started", f"Backup started for {volume}", volume=volume, data={"source": source})
                containers = self.docker.containers_using(volume)
                if opts["stop_containers"]:
                    for c in containers:
                        if c["status"] == "running":
                            task.add_log(f"Stopping container {c['name']}")
                            if self.docker.stop_container(c["id"]):
                                stopped.append(c)
                            else:
                                raise ManagerError(f"Could not stop container {c['name']}; backup aborted.")
                task.set_progress(phase="archiving", bytes=0)
                result = archive.write_backup(
                    self.docker, self.keyring, volume, self.settings.backup_dir / volume,
                    encrypt=opts["encrypt"], compression_level=opts["compression_level"],
                    progress=lambda n: task.set_progress(bytes=n),
                )
                for c in stopped:
                    task.add_log(f"Starting container {c['name']}")
                    if not self.docker.start_container(c["id"]):
                        self.event("error", "container.start_failed", f"Container {c['name']} did not start after backup", volume=volume)
                        self.notifier.notify("backup_failure", f"Container {c['name']} did not restart",
                                             f"After backing up {volume}, container {c['name']} could not be started. Check it manually.", "error")
                stopped.clear()
                meta_extra = {
                    "hostname": self.hostname, "instance": self.instance_name(), "app_version": __version__, "source": source,
                    "containers": [{k: c.get(k) for k in ("name", "image", "project", "service", "destination")} for c in containers],
                    "key_fingerprint": self.keyring.fingerprint() if opts["encrypt"] else None,
                    "kdf_salt": self.keyring.app_salt.hex() if opts["encrypt"] else None,
                    "compression": "gzip",
                }
                archive.write_meta(result, meta_extra)
                self.db.execute(
                    "INSERT OR REPLACE INTO backups(path, volume, filename, size, sha256, encrypted, created_at, source, meta) VALUES(?,?,?,?,?,?,?,?,?)",
                    (result.path, volume, result.filename, result.size, result.sha256, int(result.encrypted), result.created_at, source,
                     json.dumps({**result.__dict__, **meta_extra})))
                task.set_progress(phase="done", bytes=result.size)
                msg = f"Backup for {volume} completed: {_fmt_bytes(result.size)} ({_fmt_bytes(result.raw_bytes)} raw) in {result.duration:.1f}s"
                task.add_log(msg)
                self.event("success", "backup.completed", msg, volume=volume, backup=result.filename,
                           data={"size": result.size, "raw": result.raw_bytes, "encrypted": result.encrypted, "duration": result.duration, "source": source})
                if opts["notify"]:
                    self.notifier.notify("backup_success", f"Backup completed: {volume}", msg, "success")
                self.storage_sample()
        except Exception as e:
            for c in stopped:
                self.docker.start_container(c["id"])
            msg = f"Backup for {volume} failed: {e}"
            self.event("error", "backup.failed", msg, volume=volume, data={"source": source, "duration": time.time() - started})
            if opts.get("notify", True):
                self.notifier.notify("backup_failure", f"Backup failed: {volume}", msg, "error")
            raise
        finally:
            lock.release()

        upload_results = {}
        if opts["remotes"]:
            task.set_progress(phase="uploading")
            upload_results = self._upload_backup(result.path, volume, result.filename, opts["remotes"], task)
        return {"path": result.path, "filename": result.filename, "size": result.size, "sha256": result.sha256,
                "encrypted": result.encrypted, "uploads": upload_results}

    def _set_upload_status(self, path: str, remote_id: str, status: str, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO uploads(path, remote_id, status, updated_at, error) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path, remote_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, error=excluded.error",
            (path, remote_id, status, time.time(), error))

    def _upload_backup(self, path: str, volume: str, filename: str, remote_ids: list[str], task: Task | None = None,
                       remote_objs: dict[str, Remote] | None = None) -> dict[str, str]:
        results: dict[str, str] = {}
        meta_path = archive.meta_path_for(path)
        for rid in remote_ids:
            r = self.get_remote(rid)
            if not r or not r["enabled"]:
                results[rid] = "skipped"
                continue
            try:
                remote = remote_objs[rid] if remote_objs and rid in remote_objs else self.build_remote(rid)
                if task:
                    task.add_log(f"Uploading to {r['name']} ({r['type']})")
                    task.set_progress(remote=r["name"], upload_bytes=0, upload_total=os.path.getsize(path))
                with remote.session():
                    remote.upload(path, volume, filename, progress=(lambda n: task.set_progress(upload_bytes=n)) if task else None)
                    if os.path.isfile(meta_path):
                        remote.upload(meta_path, volume, os.path.basename(meta_path))
                self._set_upload_status(path, rid, "synced")
                self.event("success", "upload.completed", f"Uploaded {filename} to {r['name']}", volume=volume, remote_id=rid, backup=filename,
                           data={"size": os.path.getsize(path)})
                results[rid] = "synced"
            except Exception as e:  # noqa: BLE001
                self._set_upload_status(path, rid, "failed", str(e))
                self.event("error", "upload.failed", f"Upload of {filename} to {r['name']} failed: {e}", volume=volume, remote_id=rid, backup=filename)
                self.notifier.notify("upload_failure", f"Upload failed: {volume} → {r['name']}", str(e), "error")
                results[rid] = "failed"
                if task:
                    task.add_log(f"Upload to {r['name']} failed: {e}")
        return results

    def start_push(self, path: str, remote_ids: list[str]) -> Task:
        p = self.resolve_backup_path(path)
        volume = p.parent.name
        return self.tasks.submit("upload", f"Upload {p.name} ({volume})",
                                 lambda t: self._upload_backup(str(p), volume, p.name, remote_ids, t), volume=volume)

    def start_restore(self, path: str, volume: str) -> Task:
        p = self.resolve_backup_path(path)
        if not valid_volume_name(volume) or not self.docker.volume_exists(volume):
            raise ManagerError(f"Target volume '{volume}' does not exist.")
        if self.tasks.active_for_volume(volume):
            raise ManagerError(f"A task for volume '{volume}' is already running.")
        return self.tasks.submit("restore", f"Restore {p.name} → {volume}", lambda t: self._do_restore(t, str(p), volume), volume=volume)

    def _do_restore(self, task: Task, path: str, volume: str, *, notify: bool = True) -> dict:
        lock = self._volume_lock(volume)
        if not lock.acquire(blocking=False):
            raise ManagerError(f"Volume '{volume}' is busy.")
        filename = os.path.basename(path)
        stopped: list[dict] = []
        started = time.time()
        try:
            self.event("warning", "restore.started", f"Restore of {filename} into {volume} started", volume=volume, backup=filename)
            if notify:
                self.notifier.notify("restore", f"Restore started: {volume}", f"Restoring {filename} into {volume}", "warning")
            if archive.is_encrypted_file(path) and not self.keyring.available:
                raise ManagerError("This backup is encrypted but no encryption key is configured.")
            for c in self.docker.containers_using(volume):
                if c["status"] == "running":
                    task.add_log(f"Stopping container {c['name']}")
                    if self.docker.stop_container(c["id"]):
                        stopped.append(c)
                    else:
                        raise ManagerError(f"Could not stop container {c['name']}; restore aborted.")
            task.set_progress(phase="clearing")
            task.add_log("Clearing volume")
            total = os.path.getsize(path)
            task.set_progress(phase="restoring", bytes=0, total=total)
            archive.restore_backup(self.docker, self.keyring, volume, path, clear=True,
                                   progress=lambda n: task.set_progress(bytes=n))
            msg = f"Restore of {filename} into {volume} completed in {time.time() - started:.1f}s"
            task.add_log(msg)
            self.event("success", "restore.completed", msg, volume=volume, backup=filename, data={"duration": time.time() - started})
            if notify:
                self.notifier.notify("restore", f"Restore completed: {volume}", msg, "success")
            return {"volume": volume, "filename": filename}
        except Exception as e:
            msg = f"Restore of {filename} into {volume} failed: {e}"
            self.event("error", "restore.failed", msg, volume=volume, backup=filename)
            if notify:
                self.notifier.notify("restore", f"Restore failed: {volume}", msg, "error")
            raise
        finally:
            for c in stopped:
                task.add_log(f"Starting container {c['name']}")
                if not self.docker.start_container(c["id"]):
                    self.event("error", "container.start_failed", f"Container {c['name']} did not start after restore", volume=volume)
            lock.release()

    def start_verify(self, path: str) -> Task:
        p = self.resolve_backup_path(path)
        volume = p.parent.name

        def run(task: Task) -> dict:
            task.set_progress(phase="verifying", bytes=0, total=p.stat().st_size)
            res = archive.verify_backup(p, self.keyring, progress=lambda n: task.set_progress(bytes=n))
            meta = archive.read_meta(p) or {}
            meta.update({"verified_at": time.time(), "verify": res})
            row = self.db.one("SELECT meta FROM backups WHERE path=?", (str(p),))
            stored = json.loads(row["meta"]) if row and row["meta"] else {}
            stored.update({"verified_at": meta["verified_at"], "verify": res, "files": res.get("files")})
            self.db.execute("UPDATE backups SET meta=? WHERE path=?", (json.dumps(stored), str(p)))
            if res["ok"]:
                self.event("success", "backup.verified", f"Verified {p.name}: {res['files']} files, {_fmt_bytes(res['bytes'])}", volume=volume, backup=p.name, data=res)
            else:
                self.event("error", "backup.verify_failed", f"Verification of {p.name} failed: {res.get('error')}", volume=volume, backup=p.name, data=res)
                raise ManagerError(res.get("error") or "verification failed")
            return res

        return self.tasks.submit("verify", f"Verify {p.name} ({volume})", run, volume=volume)

    def delete_backup(self, path: str, delete_remote: bool = False) -> dict:
        p = self.resolve_backup_path(path)
        volume = p.parent.name
        remote_results: dict[str, str] = {}
        if delete_remote:
            for u in self.db.query("SELECT remote_id FROM uploads WHERE path=? AND status='synced'", (str(p),)):
                rid = u["remote_id"]
                try:
                    remote = self.build_remote(rid)
                    with remote.session():
                        remote.delete(volume, p.name)
                        remote.delete(volume, p.name + archive.META_EXT)
                    remote_results[rid] = "deleted"
                except Exception as e:  # noqa: BLE001
                    remote_results[rid] = f"failed: {e}"
                    self.event("warning", "remote.delete_failed", f"Could not delete {p.name} on remote {rid}: {e}", volume=volume, remote_id=rid, backup=p.name)
        size = p.stat().st_size
        p.unlink()
        meta_p = Path(archive.meta_path_for(p))
        if meta_p.is_file():
            meta_p.unlink()
        self.db.execute("DELETE FROM backups WHERE path=?", (str(p),))
        self.db.execute("DELETE FROM uploads WHERE path=?", (str(p),))
        self.event("info", "backup.deleted", f"Deleted {p.name} ({_fmt_bytes(size)})", volume=volume, backup=p.name, data={"size": size, "remote": remote_results})
        return {"deleted": str(p), "size": size, "remote": remote_results}

    def prune(self, volume: str, *, keep_seconds: int = 0, keep_max: int = 0, dry_run: bool = False,
              delete_remote: bool = False, min_keep: int = 1) -> dict:
        if keep_seconds <= 0 and keep_max <= 0:
            return {"deleted": [], "kept": 0, "bytes": 0}
        backups = self.list_backups(volume)  # newest first
        now = time.time()
        victims = []
        for idx, b in enumerate(backups):
            if idx < min_keep:
                continue
            too_old = keep_seconds > 0 and (now - b["created_at"]) > keep_seconds
            too_many = keep_max > 0 and idx >= keep_max
            if too_old or too_many:
                victims.append(b)
        freed = 0
        deleted = []
        for b in victims:
            if dry_run:
                deleted.append({"path": b["path"], "filename": b["filename"], "size": b["size"], "created_at": b["created_at"]})
                freed += b["size"]
                continue
            try:
                res = self.delete_backup(b["path"], delete_remote=delete_remote)
                deleted.append({"path": b["path"], "filename": b["filename"], "size": res["size"], "created_at": b["created_at"]})
                freed += res["size"]
            except Exception as e:  # noqa: BLE001
                self.event("error", "prune.failed", f"Could not delete {b['filename']}: {e}", volume=volume, backup=b["filename"])
        if deleted and not dry_run:
            msg = f"Pruned {len(deleted)} backup(s) of {volume}, freed {_fmt_bytes(freed)}"
            self.event("info", "prune.completed", msg, volume=volume, data={"count": len(deleted), "bytes": freed, "remote": delete_remote})
            self.notifier.notify("prune", f"Pruned {volume}", msg, "info")
            self.storage_sample()
        return {"deleted": deleted, "kept": len(backups) - len(deleted), "bytes": freed}

    def save_uploaded_backup(self, volume: str, file_storage) -> dict:
        if not valid_volume_name(volume):
            raise ManagerError("Invalid volume name.")
        filename = os.path.basename(file_storage.filename or "")
        if not archive.is_backup_filename(filename) or "/" in filename or filename.startswith("."):
            raise ManagerError("Only .tar.gz or .tar.gz.enc backup files are accepted.")
        ts, _ = archive.parse_backup_filename(filename)
        if ts is None:
            filename = archive.make_filename(encrypted=filename.endswith(archive.ENC_EXT))
        vdir = self.settings.backup_dir / volume
        vdir.mkdir(parents=True, exist_ok=True)
        dest = vdir / filename
        if dest.exists():
            raise ManagerError(f"'{filename}' already exists for {volume}.")
        tmp = str(dest) + ".part"
        file_storage.save(tmp)
        os.replace(tmp, dest)
        self.scan_backups()
        self.event("info", "backup.uploaded", f"Uploaded {filename} for {volume} via UI", volume=volume, backup=filename)
        return {"path": str(dest), "filename": filename}

    # ------------------------------------------------------------------- sync
    def start_sync(self, remote_ids: list[str] | None = None) -> Task:
        return self.tasks.submit("sync", "Sync backups to remotes", lambda t: self._do_sync(t, remote_ids))

    def _do_sync(self, task: Task | None, remote_ids: list[str] | None = None, notify: bool = True) -> dict:
        self.scan_backups()
        remotes = [r for r in self.list_remotes() if r["enabled"] and (remote_ids is None or r["id"] in remote_ids)]
        backups = self.list_backups()
        summary = {"uploaded": 0, "failed": 0, "already": 0, "remotes": {}}
        for r in remotes:
            rid = r["id"]
            pending = [b for b in backups if b["uploads"].get(rid, {}).get("status") != "synced"]
            if task:
                task.add_log(f"{r['name']}: {len(pending)} backup(s) to check")
            if not pending:
                summary["remotes"][rid] = {"uploaded": 0, "failed": 0}
                continue
            up = fail = 0
            try:
                remote = self.build_remote(rid)
                with remote.session():
                    for b in pending:
                        if task:
                            task.check_cancel()
                        try:
                            rsize = remote.size(b["volume"], b["filename"])
                        except RemoteError:
                            rsize = None
                        if rsize is not None and rsize == b["size"]:
                            self._set_upload_status(b["path"], rid, "synced")
                            summary["already"] += 1
                            continue
                        res = self._upload_backup(b["path"], b["volume"], b["filename"], [rid], task, {rid: remote})
                        if res.get(rid) == "synced":
                            up += 1
                        else:
                            fail += 1
            except Exception as e:  # noqa: BLE001
                fail += len(pending)
                self.event("error", "sync.failed", f"Sync to {r['name']} failed: {e}", remote_id=rid)
            summary["uploaded"] += up
            summary["failed"] += fail
            summary["remotes"][rid] = {"uploaded": up, "failed": fail}
        msg = f"Sync finished: {summary['uploaded']} uploaded, {summary['failed']} failed, {summary['already']} already present"
        self.event("warning" if summary["failed"] else "info", "sync.completed", msg, data=summary)
        if notify:
            self.notifier.notify("sync", "Remote sync finished", msg, "warning" if summary["failed"] else "info")
        return summary

    def _install_sync_job(self) -> None:
        g = self.general()
        if g.get("sync_enabled") and int(g.get("sync_interval_minutes", 0)) > 0:
            self.scheduler.add_job(lambda: self._do_sync(None), IntervalTrigger(minutes=int(g["sync_interval_minutes"])),
                                   id="sync", replace_existing=True)
        else:
            try:
                self.scheduler.remove_job("sync")
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- schedules
    SCHEDULE_DEFAULTS = {
        "mode": "interval",  # interval | daily
        "interval_seconds": 86400,
        "time": "03:00",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "keep_seconds": 30 * 86400,
        "keep_max": 0,
        "remotes": [],
        "encrypt": None,
        "stop_containers": True,
        "notify": True,
        "prune_remote": True,
        "enabled": True,
    }

    def _normalize_schedule(self, volume: str, cfg: dict, existing: dict | None = None) -> dict:
        s = dict(self.SCHEDULE_DEFAULTS)
        if existing:
            s.update(existing)
        for k in self.SCHEDULE_DEFAULTS:
            if k in cfg:
                s[k] = cfg[k]
        s["mode"] = "daily" if s.get("mode") == "daily" else "interval"
        s["interval_seconds"] = int(s.get("interval_seconds") or 0)
        if s["mode"] == "interval" and s["interval_seconds"] < 60:
            raise ManagerError("Interval must be at least 60 seconds.")
        t = str(s.get("time") or "03:00")
        try:
            hh, mm = [int(x) for x in t.split(":")]
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception as e:
            raise ManagerError("Time must be HH:MM.") from e
        s["time"] = f"{hh:02d}:{mm:02d}"
        wd = [int(d) for d in (s.get("weekdays") or []) if 0 <= int(d) <= 6]
        s["weekdays"] = sorted(set(wd)) or list(range(7))
        s["keep_seconds"] = max(0, int(s.get("keep_seconds") or 0))
        s["keep_max"] = max(0, int(s.get("keep_max") or 0))
        valid = {r["id"] for r in self.list_remotes()}
        s["remotes"] = [r for r in (s.get("remotes") or []) if r in valid]
        s["encrypt"] = None if s.get("encrypt") is None else bool(s["encrypt"])
        for b in ("stop_containers", "notify", "prune_remote", "enabled"):
            s[b] = bool(s.get(b))
        return s

    def list_schedules(self) -> list[dict]:
        out = []
        for row in self.db.query("SELECT * FROM schedules ORDER BY volume COLLATE NOCASE"):
            out.append(self._schedule_row(row))
        return out

    def _schedule_row(self, row) -> dict:
        cfg = json.loads(row["config"])
        cfg["volume"] = row["volume"]
        cfg["enabled"] = bool(row["enabled"])
        cfg["created_at"] = row["created_at"]
        cfg["updated_at"] = row["updated_at"]
        job = self.scheduler.get_job(f"backup:{row['volume']}") if self.scheduler.running else None
        cfg["next_run"] = job.next_run_time.timestamp() if job and job.next_run_time else None
        cfg["interval_str"] = describe_schedule(cfg)
        return cfg

    def get_schedule(self, volume: str) -> dict | None:
        row = self.db.one("SELECT * FROM schedules WHERE volume=?", (volume,))
        return self._schedule_row(row) if row else None

    def _store_schedule(self, volume: str, cfg: dict) -> None:
        stored = {k: cfg.get(k, v) for k, v in self.SCHEDULE_DEFAULTS.items()}
        now = time.time()
        self.db.execute(
            "INSERT INTO schedules(volume, enabled, config, created_at, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(volume) DO UPDATE SET enabled=excluded.enabled, config=excluded.config, updated_at=excluded.updated_at",
            (volume, int(stored["enabled"]), json.dumps(stored), now, now))
        self._install_schedule_job(volume, stored)

    def save_schedule(self, volume: str, cfg: dict) -> dict:
        if not valid_volume_name(volume):
            raise ManagerError("Invalid volume name.")
        existing = self.get_schedule(volume)
        s = self._normalize_schedule(volume, cfg, existing)
        self._store_schedule(volume, s)
        self.event("info", "schedule.saved", f"Schedule for {volume} {'updated' if existing else 'created'}: {describe_schedule(s)}", volume=volume)
        return self.get_schedule(volume)

    def delete_schedule(self, volume: str) -> None:
        if not self.get_schedule(volume):
            raise ManagerError("Schedule not found.")
        self.db.execute("DELETE FROM schedules WHERE volume=?", (volume,))
        try:
            self.scheduler.remove_job(f"backup:{volume}")
        except Exception:  # noqa: BLE001
            pass
        self.event("info", "schedule.deleted", f"Schedule for {volume} removed", volume=volume)

    def toggle_schedule(self, volume: str, enabled: bool) -> dict:
        s = self.get_schedule(volume)
        if not s:
            raise ManagerError("Schedule not found.")
        s["enabled"] = bool(enabled)
        self._store_schedule(volume, s)
        self.event("info", "schedule.toggled", f"Schedule for {volume} {'resumed' if enabled else 'paused'}", volume=volume)
        return self.get_schedule(volume)

    def _install_schedule_job(self, volume: str, cfg: dict) -> None:
        job_id = f"backup:{volume}"
        try:
            self.scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            pass
        if not cfg.get("enabled", True):
            return
        if cfg.get("mode") == "daily":
            hh, mm = cfg["time"].split(":")
            dow = ",".join(str(d) for d in cfg.get("weekdays", range(7)))
            trigger = CronTrigger(day_of_week=dow, hour=int(hh), minute=int(mm), timezone=self.settings.timezone)
        else:
            trigger = IntervalTrigger(seconds=max(60, int(cfg.get("interval_seconds") or 86400)))
        self.scheduler.add_job(self._scheduled_backup, trigger, args=[volume], id=job_id, replace_existing=True)

    def _scheduled_backup(self, volume: str) -> None:
        s = self.get_schedule(volume)
        if not s or not s["enabled"]:
            return
        if not self.docker.volume_exists(volume):
            self.event("error", "backup.failed", f"Scheduled backup skipped: volume {volume} no longer exists", volume=volume)
            self.notifier.notify("backup_failure", f"Volume missing: {volume}", "Scheduled backup skipped because the volume does not exist.", "error")
            return
        if self.tasks.active_for_volume(volume):
            self.event("warning", "backup.skipped", f"Scheduled backup skipped: {volume} is busy", volume=volume)
            return
        opts = self._resolve_backup_options(volume, remotes=s["remotes"], encrypt=s["encrypt"],
                                            stop_containers=s["stop_containers"], notify=s["notify"])

        def run(task: Task) -> dict:
            res = self._do_backup(task, volume, opts, "schedule")
            if s["keep_seconds"] > 0 or s["keep_max"] > 0:
                task.add_log("Applying retention")
                pr = self.prune(volume, keep_seconds=s["keep_seconds"], keep_max=s["keep_max"], delete_remote=s["prune_remote"])
                res["pruned"] = len(pr["deleted"])
            return res

        self.tasks.submit("backup", f"Scheduled backup {volume}", run, volume=volume, meta={"source": "schedule"})

    def run_schedule_now(self, volume: str) -> None:
        if not self.get_schedule(volume):
            raise ManagerError("Schedule not found.")
        self._scheduled_backup(volume)

    # --------------------------------------------------------------- overview
    def storage_sample(self) -> dict:
        row = self.db.one("SELECT COALESCE(SUM(size),0) AS total, COUNT(*) AS n FROM backups")
        total, n = int(row["total"]), int(row["n"])
        self.db.add_storage_sample(total, n)
        return {"total_bytes": total, "backup_count": n}

    def disk_usage(self) -> dict:
        try:
            u = shutil.disk_usage(self.settings.backup_dir)
            return {"total": u.total, "used": u.used, "free": u.free}
        except OSError:
            return {}

    def volumes_overview(self) -> list[dict]:
        try:
            volumes = self.docker.list_volumes()
        except DockerError as e:
            raise ManagerError(str(e)) from e
        backups = self.list_backups()
        by_vol: dict[str, list[dict]] = {}
        for b in backups:
            by_vol.setdefault(b["volume"], []).append(b)
        schedules = {s["volume"]: s for s in self.list_schedules()}
        active = {t.volume: t.to_dict() for t in self.tasks.list(active_only=True) if t.volume}
        for v in volumes:
            vb = by_vol.get(v["name"], [])
            v["backup_count"] = len(vb)
            v["backup_bytes"] = sum(b["size"] for b in vb)
            v["last_backup"] = vb[0] if vb else None
            v["schedule"] = schedules.get(v["name"])
            v["active_task"] = active.get(v["name"])
        orphan_volumes = [name for name in by_vol if name not in {v["name"] for v in volumes}]
        for name in orphan_volumes:
            vb = by_vol[name]
            volumes.append({"name": name, "missing": True, "containers": [], "backup_count": len(vb),
                            "backup_bytes": sum(b["size"] for b in vb), "last_backup": vb[0], "schedule": schedules.get(name), "active_task": active.get(name)})
        return volumes

    def overdue_schedules(self, now: float | None = None) -> list[str]:
        """Enabled schedules whose last backup is older than twice their interval."""
        now = now or time.time()
        last: dict[str, float] = {}
        for b in self.list_backups():
            last[b["volume"]] = max(last.get(b["volume"], 0), b["created_at"])
        out = []
        for s in self.list_schedules():
            if not s["enabled"]:
                continue
            expected = s["interval_seconds"] if s["mode"] == "interval" else 86400 * (7 if len(s.get("weekdays") or []) < 7 else 1)
            grace = max(expected * 2, 3600)
            ref = last.get(s["volume"]) or s.get("created_at") or now
            if now - ref > grace:
                out.append(s["volume"])
        return out

    def overview(self) -> dict:
        backups = self.list_backups()
        total = sum(b["size"] for b in backups)
        encrypted = sum(1 for b in backups if b["encrypted"])
        remotes = self.list_remotes()
        schedules = self.list_schedules()
        next_runs = [s["next_run"] for s in schedules if s.get("next_run")]
        try:
            volume_names = self.docker.volume_names()
            docker_ok = True
        except DockerError:
            volume_names, docker_ok = set(), False
        protected = {b["volume"] for b in backups} & volume_names
        samples = rows_to_dicts(self.db.query("SELECT ts, total_bytes, backup_count FROM storage_samples WHERE ts > ? ORDER BY ts", (time.time() - 30 * 86400,)))
        return {
            "app": {"version": __version__, "hostname": self.hostname, "instance": self.instance_name(), "started_at": self.started_at, "timezone": self.settings.timezone},
            "docker": {"ok": docker_ok, **(self.docker.info() if docker_ok else {})},
            "volumes": {"total": len(volume_names), "protected": len(protected), "scheduled": len([s for s in schedules if s["enabled"]])},
            "backups": {"count": len(backups), "bytes": total, "encrypted": encrypted},
            "disk": self.disk_usage(),
            "remotes": [{"id": r["id"], "name": r["name"], "type": r["type"], "enabled": r["enabled"], "health": r["health"]} for r in remotes],
            "encryption": {"configured": self.keyring.available, "fingerprint": self.keyring.fingerprint(), "default": self.general()["encrypt_default"]},
            "next_run": min(next_runs) if next_runs else None,
            "tasks": [t.to_dict() for t in self.tasks.list()[:20]],
            "recent_events": self.list_events(limit=12),
            "storage_history": samples,
            "peers": self.peers.summary() if self.peers else [],
            "uptime": self.uptime.summary() if self.uptime else None,
            "security": self.security_warnings(),
            "sync": {"enabled": self.general()["sync_enabled"], "interval_minutes": self.general()["sync_interval_minutes"],
                     "next_run": (lambda j: j.next_run_time.timestamp() if j and j.next_run_time else None)(self.scheduler.get_job("sync") if self.scheduler.running else None)},
        }

    def security_warnings(self) -> list[str]:
        out = []
        auth = getattr(self, "auth", None)
        if auth is not None and getattr(auth, "weak_password", None):
            out.append(f"Weak admin login: {auth.weak_password}. Set a long random DOCKBARE_ADMIN_PASSWORD.")
        if self.settings.generated_secret:
            out.append("DOCKBARE_SECRET_KEY is auto-generated; put it into .env so sessions and stored passwords survive a reinstall.")
        if not self.keyring.available:
            out.append("No DOCKBARE_ENCRYPTION_KEY: backups and configuration snapshots are stored and uploaded unencrypted.")
        if auth is not None and not getattr(auth, "totp_enabled", False) and self.settings.behind_proxy:
            out.append("Reachable through a proxy without two-factor login. Consider DOCKBARE_TOTP_SECRET (python -m dockbare totp-setup).")
        return out

    def housekeeping(self) -> None:
        try:
            self.scan_backups()
            self.storage_sample()
            self.docker.cleanup_helpers()
            n = self.db.prune_events(int(self.general().get("events_keep_days", 365)))
            if self.uptime:
                self.uptime.housekeeping()
            if n:
                log.info("Pruned %d old events", n)
            # drop sessions older than max lifetime
            self.db.execute("DELETE FROM sessions WHERE created_at < ?", (time.time() - self.settings.session_max_hours * 3600,))
            self.db.execute("DELETE FROM login_attempts WHERE ts < ?", (time.time() - 30 * 86400,))
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping failed")

    # -------------------------------------------------------------- migration
    def migrate_legacy(self) -> None:
        """Import v2 config (backup_config.json / ftp_sync_status.json in the backup dir)."""
        legacy = self.settings.backup_dir / "backup_config.json"
        if not legacy.is_file() or self.db.get_setting("legacy_migrated"):
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Legacy config unreadable: %s", e)
            return
        log.info("Migrating legacy v2 configuration from %s", legacy)
        remote_id = None
        ftp = data.get("ftp_settings") or {}
        if ftp.get("host"):
            try:
                r = self.save_remote({
                    "name": f"FTP {ftp['host']}", "type": "ftp", "enabled": True,
                    "config": {"host": ftp.get("host"), "port": ftp.get("port", 21), "username": ftp.get("username", ""),
                               "password": ftp.get("password", ""), "base_path": ftp.get("remote_base_path") or "/backups",
                               "passive": ftp.get("passive", True), "secure": ftp.get("secure", False),
                               "tls_mode": ftp.get("mode", "explicit"), "nat_pasv_fix": ftp.get("nat_pasv_fix", False),
                               "instance_dir": False},
                })
                remote_id = r["id"]
                g = self.general()
                g["sync_enabled"] = bool(ftp.get("sync_enabled"))
                g["sync_interval_minutes"] = max(5, int(ftp.get("sync_interval_minutes") or 60))
                self.db.set_setting("general", g)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not migrate FTP settings: %s", e)
        ntfy = data.get("notification_settings") or {}
        if ntfy.get("topic"):
            self.save_notify_settings({"ntfy": {"enabled": True, "base_url": ntfy.get("base_url", "https://ntfy.sh"), "topic": ntfy["topic"]}})
        for volume, s in (data.get("schedules") or {}).items():
            try:
                interval = int(s.get("interval_days", 0)) * 86400 + int(s.get("interval_hours", 0)) * 3600 + int(s.get("interval_minutes", 0)) * 60 + int(s.get("interval_seconds", 0))
                keep = int(s.get("prune_keep_days", 0)) * 86400 + int(s.get("prune_keep_hours", 0)) * 3600 + int(s.get("prune_keep_minutes", 0)) * 60
                self.save_schedule(volume, {
                    "mode": "interval", "interval_seconds": max(60, interval), "keep_seconds": keep,
                    "remotes": [remote_id] if (remote_id and s.get("ftp_upload_enabled", True)) else [],
                    "notify": bool(s.get("notifications_enabled", True)), "enabled": bool(s.get("enabled", True)),
                    "encrypt": False, "prune_remote": False,
                })
            except Exception as e:  # noqa: BLE001
                log.warning("Could not migrate schedule for %s: %s", volume, e)
        status_file = self.settings.backup_dir / "ftp_sync_status.json"
        if remote_id and status_file.is_file():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8"))
                for path, status in st.items():
                    if status == "synced":
                        self._set_upload_status(path, remote_id, "synced")
            except (OSError, json.JSONDecodeError):
                pass
        self.db.set_setting("legacy_migrated", {"at": time.time(), "from": str(legacy)})
        for f in (legacy, status_file):
            try:
                if f.is_file():
                    f.rename(f.with_suffix(f.suffix + ".migrated"))
            except OSError:
                pass
        self.event("info", "app.migrated", "Imported configuration from Dockbare v2")


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def describe_schedule(cfg: dict) -> str:
    if cfg.get("mode") == "daily":
        days = cfg.get("weekdays") or list(range(7))
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        if len(days) == 7:
            d = "daily"
        elif days == [0, 1, 2, 3, 4]:
            d = "weekdays"
        else:
            d = ", ".join(names[i] for i in days)
        return f"{d} at {cfg.get('time', '03:00')}"
    secs = int(cfg.get("interval_seconds") or 0)
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            parts.append(f"{secs // size}{label}")
            secs %= size
    return "every " + (" ".join(parts) or "0s")


fmt_bytes = _fmt_bytes
