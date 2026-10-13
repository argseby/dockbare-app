"""Configuration snapshots: export/import of everything that is not in .env.

The snapshot (schedules, remotes, notification channels, peers, container
monitors, report and uptime settings, general settings) is encrypted with the
backup key and pushed to every enabled remote as
``<base>/[<instance>/]_dockbare/config.json.enc`` whenever the configuration
changes (debounced) and once a day. Recovery can import it with one click.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

from apscheduler.triggers.interval import IntervalTrigger

from . import __version__, crypto
from .manager import Manager, ManagerError
from .remotes import OWN, RemoteError

log = logging.getLogger("dockbare.configsync")

CONFIG_DIR = "_dockbare"
CONFIG_FILE = "config.json.enc"
FORMAT = "dockbare-config"
TRIGGER_KINDS = {
    "schedule.saved", "schedule.deleted", "schedule.toggled", "remote.created", "remote.updated", "remote.deleted",
    "settings.saved", "settings.instance_renamed", "peer.created", "peer.updated", "peer.deleted", "peer.token_regenerated",
    "uptime.monitor_saved",
}


class ConfigSync:
    TRIGGER_KINDS = TRIGGER_KINDS

    def __init__(self, manager: Manager):
        self.m = manager
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.last_push: dict | None = None

    # ----------------------------------------------------------- export
    def export(self, include_secrets: bool = True) -> dict:
        m = self.m
        # Anything that comes from .env is deliberately left out: .env is the other half of a recovery.
        remotes = []
        for r in m.list_remotes(with_secrets=include_secrets):
            if r["source"] == "env":
                continue
            remotes.append({k: r[k] for k in ("id", "name", "type", "enabled", "config", "source")})
        peers = []
        if m.peers:
            for p in m.peers.list():
                if p["source"] == "env":
                    continue
                full = m.peers.get(p["id"], with_token=include_secrets) or p
                peers.append({k: full.get(k) for k in ("id", "name", "url", "token", "enabled", "interval_minutes", "failure_threshold", "alert_on_problems", "verify_tls", "timeout", "source")})
        notifications = m._notify_settings_plain() if include_secrets else m.notify_settings_public()
        for section, keys in m.settings.notify_env.items():
            for k in keys:
                notifications.get(section, {}).pop(k, None)
        return {
            "format": FORMAT, "version": 1, "exported_at": time.time(), "app_version": __version__,
            "instance": m.instance_name(), "hostname": m.hostname,
            "general": m.general(),
            "remotes": remotes,
            "schedules": [{k: v for k, v in s.items() if k not in ("next_run", "interval_str", "created_at", "updated_at")} for s in m.list_schedules()],
            "notifications": notifications,
            "reports": m.reports.settings() if getattr(m, "reports", None) else None,
            "peers": peers,
            "peer_token": (m.peers.token() if (m.peers and include_secrets and m.peers.token_source() != "env") else None),
            "monitors": m.uptime.monitors() if m.uptime else {},
            "uptime": m.uptime.settings() if m.uptime else None,
        }

    def to_bytes(self, encrypt: bool = True) -> bytes:
        raw = json.dumps(self.export(), indent=2).encode("utf-8")
        if encrypt and self.m.keyring.available:
            return crypto.encrypt_bytes(raw, self.m.keyring)
        return raw

    def from_bytes(self, data: bytes) -> dict:
        if crypto.is_encrypted_header(data[:4]):
            if not self.m.keyring.available:
                raise ManagerError("The snapshot is encrypted but no encryption key is configured.")
            data = crypto.decrypt_bytes(data, self.m.keyring)
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ManagerError(f"Not a Dockbare configuration file: {e}") from e
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ManagerError("Not a Dockbare configuration file.")
        return doc

    @staticmethod
    def describe(doc: dict) -> dict:
        return {
            "exported_at": doc.get("exported_at"), "instance": doc.get("instance"), "hostname": doc.get("hostname"),
            "app_version": doc.get("app_version"),
            "counts": {"remotes": len(doc.get("remotes") or []), "schedules": len(doc.get("schedules") or []),
                       "peers": len(doc.get("peers") or []), "monitors": len(doc.get("monitors") or {})},
            "channels": [k for k, v in (doc.get("notifications") or {}).items() if isinstance(v, dict) and v.get("enabled")],
        }

    # ----------------------------------------------------------- import
    def apply(self, doc: dict, *, sections: list[str] | None = None) -> dict:
        """Import a snapshot. Rows configured through .env are never touched."""
        m = self.m
        want = set(sections or ["general", "remotes", "schedules", "notifications", "reports", "peers", "monitors", "uptime"])
        summary: dict = {}
        if "remotes" in want:
            n = 0
            for r in doc.get("remotes") or []:
                if not isinstance(r, dict) or str(r.get("id", "")).startswith("env-") or r.get("source") == "env":
                    continue
                try:
                    m.save_remote({"name": r["name"], "type": r["type"], "enabled": r.get("enabled", True), "config": r.get("config") or {}}, r.get("id"), allow_env=True)
                    n += 1
                except (ManagerError, KeyError) as e:
                    log.warning("Skipped remote %s: %s", r.get("name"), e)
            summary["remotes"] = n
        if "general" in want and isinstance(doc.get("general"), dict):
            m.save_general(doc["general"])
            summary["general"] = True
        if "notifications" in want and isinstance(doc.get("notifications"), dict):
            m.save_notify_settings(doc["notifications"])
            summary["notifications"] = True
        if "schedules" in want:
            n = 0
            for s in doc.get("schedules") or []:
                try:
                    m.save_schedule(s["volume"], s)
                    n += 1
                except (ManagerError, KeyError) as e:
                    log.warning("Skipped schedule %s: %s", s.get("volume"), e)
            summary["schedules"] = n
        if "reports" in want and isinstance(doc.get("reports"), dict) and getattr(m, "reports", None):
            m.reports.save_settings(doc["reports"])
            summary["reports"] = True
        if "peers" in want and m.peers:
            n = 0
            for p in doc.get("peers") or []:
                if not isinstance(p, dict) or str(p.get("id", "")).startswith("env-") or p.get("source") == "env":
                    continue
                try:
                    m.peers.save(p, p.get("id"), allow_env=True)
                    n += 1
                except ManagerError as e:
                    log.warning("Skipped peer %s: %s", p.get("name"), e)
            if doc.get("peer_token") and m.peers.token_source() != "env":
                m.db.set_setting("peer_token", doc["peer_token"])
            summary["peers"] = n
        if "monitors" in want and m.uptime and isinstance(doc.get("monitors"), dict):
            for name, cfg in doc["monitors"].items():
                try:
                    m.uptime.save_monitor(name, cfg)
                except ManagerError:
                    pass
            summary["monitors"] = len(doc["monitors"])
        if "uptime" in want and m.uptime and isinstance(doc.get("uptime"), dict):
            m.uptime.save_settings(doc["uptime"])
            summary["uptime"] = True
        m.event("warning", "config.imported", f"Configuration imported from snapshot of {doc.get('instance')} ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(doc.get('exported_at') or 0))} UTC)", data=summary)
        return summary

    # ------------------------------------------------------------- push
    def push(self, remote_ids: list[str] | None = None) -> dict:
        results: dict[str, str] = {}
        if not self.m.keyring.available:
            self.last_push = {"at": time.time(), "results": {}, "error": "no encryption key; snapshots are only pushed encrypted"}
            return self.last_push
        data = self.to_bytes(encrypt=True)
        fd, tmp = tempfile.mkstemp(prefix="dockbare-config-", suffix=".enc")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            for r in self.m.list_remotes():
                if not r["enabled"] or (remote_ids and r["id"] not in remote_ids):
                    continue
                try:
                    remote = self.m.build_remote(r["id"])
                    remote.upload(tmp, CONFIG_DIR, CONFIG_FILE)
                    results[r["id"]] = "ok"
                except Exception as e:  # noqa: BLE001
                    results[r["id"]] = str(e)
                    log.warning("Config push to %s failed: %s", r["name"], e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.last_push = {"at": time.time(), "results": results, "size": len(data)}
        if results:
            failed = [k for k, v in results.items() if v != "ok"]
            self.m.event("warning" if failed else "info", "config.pushed", f"Configuration snapshot pushed to {len(results) - len(failed)}/{len(results)} remote(s)", data=self.last_push)
        return self.last_push

    def schedule_push(self, delay: float = 60.0) -> None:
        if not self.m.general().get("config_autopush", True):
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._timed_push)
            self._timer.daemon = True
            self._timer.start()

    def _timed_push(self) -> None:
        try:
            self.push()
        except Exception:  # noqa: BLE001
            log.exception("Scheduled config push failed")

    def install_job(self) -> None:
        self.m.scheduler.add_job(self._timed_push, IntervalTrigger(hours=24), id="config-push", replace_existing=True)

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()

    # ----------------------------------------------------------- remote
    def fetch(self, remote_id: str, instance=OWN) -> dict | None:
        remote = self.m.build_remote(remote_id)
        try:
            raw = remote.read_small_file(CONFIG_DIR, CONFIG_FILE, limit=8 * 1024 * 1024, instance=instance)
        except RemoteError:
            return None
        if not raw:
            return None
        return self.from_bytes(raw)
