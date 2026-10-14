"""Container uptime monitoring: incidents, auto-restart, public status page.

A poll loop (plus the Docker event stream for instant reaction) classifies every
container; a container that is failing for longer than its grace period opens an
incident, which is closed when it recovers. Notifications go out both ways.
"""
from __future__ import annotations

import html
import json
import logging
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import requests

from . import __version__
from .dockerctl import DockerError
from .manager import Manager, ManagerError

log = logging.getLogger("dockbare.uptime")

MONITOR_DEFAULTS = {
    "enabled": True,
    "notify": True,
    "auto_restart": False,
    "max_restarts": 3,
    "restart_window_minutes": 30,
    "require_healthy": True,
    "alert_on_stop": False,
    "grace_seconds": 30,
    "public": False,
    "display_name": "",
    "public_icon": "",
    "public_url": "",
    "public_description": "",
}
# External checks: an http(s):// URL (up when it answers below 400) or tcp://host:port (up when the port accepts).
CHECK_DEFAULTS = {
    "enabled": True,
    "url": "",
    "interval_seconds": 60,
    "timeout_seconds": 10,
    "grace_seconds": 30,
    "verify_tls": True,
    "notify": True,
    "public": True,
    "public_icon": "",
    "public_url": "",
    "public_description": "",
}
# Services: several containers and/or checks shown as one entry on the public page.
SERVICE_DEFAULTS = {
    "members": [],
    "member_names": {},  # member key -> name shown on the status page
    "down_if_any": True,  # False: "degraded" while some members are down, "down" only when all are
    "public_icon": "",
    "public_url": "",
    "public_description": "",
}
STR_LIMITS = {"public_url": 300, "public_icon": 300, "public_description": 160, "url": 500}
CHECK_PREFIX = "check:"  # incidents of external checks are keyed "check:<id>"
BADGE = {  # message, shields color, dot color on dark / light pill
    "operational": ("all systems operational", "#2da44e", "#22c55e", "#16a34a"),
    "degraded": ("partial outage", "#bf8700", "#f59e0b", "#d97706"),
    "outage": ("major outage", "#cf222e", "#ef4444", "#dc2626"),
    "maintenance": ("maintenance", "#0969da", "#3b82f6", "#2563eb"),
}
BADGE_THEMES = {  # background, border, text, muted label
    "dark": ("#111a2e", "#233049", "#e5eaf3", "#8593ad"),
    "light": ("#ffffff", "#e2e8f0", "#0f172a", "#64748b"),
}
SETTINGS_DEFAULTS = {
    "poll_seconds": 15,
    "monitor_new": True,
    "notify_down": True,
    "notify_recovery": True,
    "notify_restart": True,
    "default_grace_seconds": 30,
    "quiet_windows": [],  # [{"days": [0..6], "start": "02:00", "end": "03:00", "mode": "mute" | "ignore"}]
    "public_enabled": False,
    "public_title": "Service status",
    "public_group_name": "",
    "public_show_peers": True,
    "public_merge_peers": False,  # one flat list instead of one section per site
    "incident_keep_days": 365,
}
WINDOWS = {"d1": 86400, "d7": 7 * 86400, "d30": 30 * 86400}
HISTORY_DAYS_PUBLIC = 90
HISTORY_DAYS_INTERNAL = 30
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
ANN_TYPES = ("incident", "maintenance")
INCIDENT_STATUSES = ("investigating", "identified", "monitoring", "resolved")
MAINT_STATUSES = ("scheduled", "in_progress", "completed")
SEVERITIES = ("minor", "major")


class Uptime:
    def __init__(self, manager: Manager):
        self.m = manager
        self._lock = threading.RLock()
        self._failing_since: dict[str, float] = {}
        self._restarts: dict[str, list[float]] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self.snapshot: dict[str, dict] = {}
        self._seen_running: set[str] = set()
        self.last_poll: float = 0
        self.last_error: str | None = None
        self._own_id: str | None = None
        self.check_state: dict[str, dict] = {}  # check id -> last result
        self._check_next: dict[str, float] = {}

    # ----------------------------------------------------------- settings
    def settings(self) -> dict:
        s = dict(SETTINGS_DEFAULTS)
        s.update(self.m.db.get_setting("uptime", {}) or {})
        return s

    def save_settings(self, data: dict) -> dict:
        s = self.settings()
        for k in SETTINGS_DEFAULTS:
            if k in data:
                s[k] = data[k]
        s["poll_seconds"] = max(5, min(600, int(s.get("poll_seconds") or 15)))
        s["incident_keep_days"] = max(7, min(3650, int(s.get("incident_keep_days") or 365)))
        s["public_title"] = str(s.get("public_title") or "Service status")[:80]
        for b in ("notify_down", "notify_recovery", "notify_restart"):
            s[b] = bool(s.get(b, True))
        s["default_grace_seconds"] = max(0, min(3600, int(s.get("default_grace_seconds") or 30)))
        wins = []
        for w in (s.get("quiet_windows") or [])[:20]:
            if not isinstance(w, dict):
                continue
            try:
                sh, sm = [int(x) for x in str(w.get("start") or "00:00").split(":")]
                eh, em = [int(x) for x in str(w.get("end") or "00:00").split(":")]
                assert 0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60
            except Exception as e:  # noqa: BLE001
                raise ManagerError("Quiet window times must be HH:MM.") from e
            days = sorted({int(d) for d in (w.get("days") or []) if 0 <= int(d) <= 6}) or list(range(7))
            wins.append({"days": days, "start": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}",
                         "mode": "ignore" if w.get("mode") == "ignore" else "mute", "label": str(w.get("label") or "")[:60]})
        s["quiet_windows"] = wins
        s["public_group_name"] = str(s.get("public_group_name") or "").strip()[:80]
        for b in ("monitor_new", "public_enabled", "public_show_peers", "public_merge_peers"):
            s[b] = bool(s.get(b))
        self.m.db.set_setting("uptime", s)
        self.m.event("info", "settings.saved", "Uptime settings saved")
        self._wake.set()
        return s

    def monitors(self) -> dict[str, dict]:
        out = {}
        for row in self.m.db.query("SELECT name, config FROM monitors"):
            cfg = dict(MONITOR_DEFAULTS)
            try:
                cfg.update(json.loads(row["config"]))
            except json.JSONDecodeError:
                pass
            out[row["name"]] = cfg
        return out

    def monitor_for(self, name: str, explicit: dict[str, dict] | None = None) -> dict:
        explicit = self.monitors() if explicit is None else explicit
        if name in explicit:
            return {**explicit[name], "explicit": True}
        st = self.settings()
        cfg = dict(MONITOR_DEFAULTS)
        cfg["enabled"] = bool(st.get("monitor_new", True))
        cfg["grace_seconds"] = int(st.get("default_grace_seconds", 30))
        cfg["explicit"] = False
        return cfg

    def quiet_window(self, now: float | None = None) -> dict | None:
        """The quiet window active right now (in the configured time zone), if any."""
        wins = self.settings().get("quiet_windows") or []
        if not wins:
            return None
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(self.m.settings.timezone)
        except Exception:  # noqa: BLE001
            tz = None
        dt = datetime.fromtimestamp(now or time.time(), tz)
        minutes = dt.hour * 60 + dt.minute
        today, yesterday = dt.weekday(), (dt.weekday() - 1) % 7
        for w in wins:
            sh, sm = [int(x) for x in w["start"].split(":")]
            eh, em = [int(x) for x in w["end"].split(":")]
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                if today in w["days"] and start <= minutes < end:
                    return w
            else:  # crosses midnight, e.g. 23:00-02:00
                if (today in w["days"] and minutes >= start) or (yesterday in w["days"] and minutes < end):
                    return w
        return None

    def save_monitor(self, name: str, data: dict) -> dict:
        if not CONTAINER_RE.match(name or ""):
            raise ManagerError("Invalid container name.")
        cfg = self.monitor_for(name)
        cfg.pop("explicit", None)
        for k, default in MONITOR_DEFAULTS.items():
            if k in data:
                v = data[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v or 0)
                else:
                    v = str(v or "").strip()[:STR_LIMITS.get(k, 80)]
                cfg[k] = v
        cfg["max_restarts"] = max(0, min(100, cfg["max_restarts"]))
        cfg["restart_window_minutes"] = max(1, min(1440, cfg["restart_window_minutes"]))
        cfg["grace_seconds"] = max(0, min(3600, cfg["grace_seconds"]))
        self._check_public_fields(cfg)
        self.m.db.execute("INSERT INTO monitors(name, config, updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at",
                          (name, json.dumps(cfg), time.time()))
        self.m.event("info", "uptime.monitor_saved", f"Monitor settings for '{name}' saved")
        self._repoll()
        return {**cfg, "explicit": True}

    def delete_monitor(self, name: str) -> None:
        self.m.db.execute("DELETE FROM monitors WHERE name=?", (name,))
        self._failing_since.pop(name, None)
        self._repoll()

    def _repoll(self) -> None:
        """Apply configuration changes to the snapshot right away."""
        try:
            self.poll()
        except Exception:  # noqa: BLE001
            log.exception("Re-poll after configuration change failed")

    # --------------------------------------------------------------- loop
    def start(self, background: bool = True) -> None:
        if not background:
            return
        t = threading.Thread(target=self._poll_loop, name="dockbare-uptime", daemon=True)
        e = threading.Thread(target=self._events_loop, name="dockbare-uptime-events", daemon=True)
        k = threading.Thread(target=self._checks_loop, name="dockbare-uptime-checks", daemon=True)
        self._threads = [t, e, k]
        t.start()
        e.start()
        k.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("Uptime poll failed")
            self._wake.wait(timeout=self.settings().get("poll_seconds", 15))
            if self._wake.is_set():
                self._wake.clear()
                time.sleep(1.5)  # debounce bursts of events

    def _checks_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_checks()
            except Exception:  # noqa: BLE001
                log.exception("External checks failed")
            self._stop.wait(5)

    def _events_loop(self) -> None:
        interesting = ("die", "oom", "start", "stop", "kill", "destroy", "restart", "health_status")
        while not self._stop.is_set():
            try:
                for ev in self.m.docker.events(self._stop):
                    action = str(ev.get("Action") or "")
                    if action.startswith(interesting):
                        self._wake.set()
            except Exception as e:  # noqa: BLE001
                log.debug("Docker event stream ended: %s", e)
                self._stop.wait(5)

    # ----------------------------------------------------------- evaluate
    @staticmethod
    def classify(c: dict, cfg: dict) -> str | None:
        """Return the failure kind for a container, or None when it is fine."""
        state = c.get("state")
        if state == "running":
            if cfg.get("require_healthy") and c.get("health") == "unhealthy":
                return "unhealthy"
            return None
        if state == "restarting":
            return "restart-loop"
        if state in ("exited", "dead"):
            if c.get("oom_killed"):
                return "oom"
            if c.get("exit_code") == 0 and not cfg.get("alert_on_stop"):
                return None
            return "down" if state == "exited" else "dead"
        return None  # created / paused / removing are intentional states

    def poll(self) -> dict[str, dict]:
        with self._lock:
            now = time.time()
            try:
                containers = self.m.docker.list_containers()
                self.last_error = None
            except DockerError as e:
                self.last_error = str(e)
                self.last_poll = now
                return self.snapshot
            if self._own_id is None:
                try:
                    self._own_id = self.m.docker.own_container_id() or ""
                except Exception:  # noqa: BLE001
                    self._own_id = ""
            explicit = self.monitors()
            open_incidents = {i["container"]: i for i in self.incidents(open_only=True) if not i["container"].startswith(CHECK_PREFIX)}
            first_seen = {r["name"]: r["first_seen"] for r in self.m.db.query("SELECT name, first_seen FROM container_seen")}
            maint = self.maintenance_active(now)
            quiet = self.quiet_window(now)
            snapshot: dict[str, dict] = {}
            seen: set[str] = set()
            for c in containers:
                name = c["name"]
                seen.add(name)
                if name not in first_seen:
                    self.m.db.execute("INSERT OR IGNORE INTO container_seen(name, first_seen) VALUES(?,?)", (name, now))
                    first_seen[name] = now
                if self._own_id and c["id"] == self._own_id:
                    c["self"] = True
                cfg = self.monitor_for(name, explicit)
                c["monitor"] = cfg
                c["monitored"] = bool(cfg["enabled"]) and not c.get("self")
                if c.get("state") == "running":
                    self._seen_running.add(name)
                kind = self.classify(c, cfg) if c["monitored"] else None
                if kind and not cfg.get("explicit") and name not in self._seen_running and name not in open_incidents:
                    # Stopped before Dockbare ever saw it running: assume it is stopped on purpose.
                    # Enable the monitor explicitly to alert on it anyway.
                    c["ignored"] = "stopped before monitoring started"
                    kind = None
                if kind and name not in open_incidents and any(self._covers(mw, name) for mw in maint):
                    c["maintenance"] = next(mw["title"] for mw in maint if self._covers(mw, name))
                    self._failing_since.pop(name, None)
                    kind = None
                if kind and name not in open_incidents and quiet and quiet["mode"] == "ignore":
                    c["maintenance"] = quiet.get("label") or f"quiet hours {quiet['start']}–{quiet['end']}"
                    self._failing_since.pop(name, None)
                    kind = None
                if kind in ("down", "dead", "unhealthy"):
                    info = self.m.docker.inspect_container(c["id"]) or {}
                    c.update({k: info.get(k) for k in ("oom_killed", "restart_count", "finished_at", "error", "health_log", "restart_policy")})
                    if info.get("oom_killed"):
                        kind = "oom"
                    if info.get("exit_code") is not None:
                        c["exit_code"] = info["exit_code"]
                c["failure"] = kind
                inc = open_incidents.pop(name, None)
                if kind:
                    since = self._failing_since.setdefault(name, now)
                    c["failing_for"] = now - since
                    if inc is None and now - since >= cfg["grace_seconds"]:
                        inc = self._open_incident(c, kind, cfg, now, muted=bool(quiet and quiet["mode"] == "mute"))
                    elif inc is not None and inc["kind"] != kind:
                        self.m.db.execute("UPDATE incidents SET kind=?, detail=? WHERE id=?", (kind, self._detail(c, kind), inc["id"]))
                    if inc is not None and cfg["auto_restart"] and kind in ("down", "dead", "unhealthy", "oom"):
                        self._maybe_restart(c, cfg, inc, now)
                else:
                    self._failing_since.pop(name, None)
                    if inc is not None:
                        self._close_incident(inc, c, cfg, now)
                c["incident"] = inc
                snapshot[name] = c
            # explicitly monitored containers that vanished
            for name, cfg in explicit.items():
                if name in seen or not cfg.get("enabled"):
                    continue
                inc = open_incidents.pop(name, None)
                since = self._failing_since.setdefault(name, now)
                if inc is None and now - since >= cfg["grace_seconds"]:
                    inc = self._open_incident({"name": name, "state": "missing"}, "missing", cfg, now)
                snapshot[name] = {"name": name, "state": "missing", "status": "container not found", "monitored": True, "monitor": {**cfg, "explicit": True}, "failure": "missing", "incident": inc, "missing": True}
            # incidents whose container is no longer monitored → close as resolved-by-config
            for name, inc in open_incidents.items():
                self._close_incident(inc, {"name": name}, MONITOR_DEFAULTS, now, note="monitoring disabled or container removed")
            self.snapshot = snapshot
            self.last_poll = now
            return snapshot

    @staticmethod
    def _clean_output(text: str, limit: int = 300) -> str:
        """Make health-check / error output readable: drop curl progress noise, keep the last lines."""
        lines = []
        for ln in str(text or "").splitlines():
            ln = " ".join(ln.split())
            if not ln or ln.startswith("%") or "Dload" in ln or re.fullmatch(r"[\d\s:.-]+", ln):
                continue
            lines.append(ln)
        out = " ".join(lines[-3:]).strip()
        return (out[: limit - 1] + "…") if len(out) > limit else out

    @classmethod
    def _detail(cls, c: dict, kind: str) -> str:
        """JSON string with the structured facts of a failure (stored in incidents.detail)."""
        health = ""
        if kind == "unhealthy" and c.get("health_log"):
            last = c["health_log"][-1]
            if last and last[2]:
                health = cls._clean_output(last[2])
        info = {
            "kind": kind, "exit_code": c.get("exit_code"), "oom": bool(c.get("oom_killed")),
            "error": cls._clean_output(c.get("error") or "", 200), "health": health, "status": c.get("status") or "",
            "image": c.get("image"), "restart_count": c.get("restart_count"),
        }
        return json.dumps({k: v for k, v in info.items() if v not in (None, "", False)})

    @staticmethod
    def detail_text(detail) -> str:
        """Short human sentence from a structured detail (or a legacy string)."""
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                return detail
        if not isinstance(detail, dict):
            return ""
        bits = []
        if detail.get("exit_code") is not None:
            bits.append(f"exit code {detail['exit_code']}")
        if detail.get("oom"):
            bits.append("OOM killed")
        if detail.get("error"):
            bits.append(detail["error"])
        if detail.get("health"):
            bits.append("health check: " + detail["health"])
        return "; ".join(bits) or detail.get("kind", "")

    def _open_incident(self, c: dict, kind: str, cfg: dict, now: float, muted: bool = False) -> dict:
        detail = self._detail(c, kind)
        cur = self.m.db.execute("INSERT INTO incidents(container, kind, started_at, detail, actions) VALUES(?,?,?,?,?)",
                                (c["name"], kind, self._failing_since.get(c["name"], now), detail, "[]"))
        inc = {"id": cur.lastrowid, "container": c["name"], "kind": kind, "started_at": self._failing_since.get(c["name"], now), "ended_at": None, "detail": detail, "actions": []}
        label = {"down": "exited", "dead": "dead", "oom": "killed (out of memory)", "unhealthy": "unhealthy", "restart-loop": "stuck in a restart loop", "missing": "missing",
                 "unreachable": "unreachable", "http-error": "answering with an error"}.get(kind, kind)
        who = c.get("label") or f"Container '{c['name']}'"
        msg = f"{who} is {label}" + (f": {self.detail_text(detail)}" if self.detail_text(detail) else "")
        self.m.event("error", "uptime.down", msg + (" (quiet hours: not notified)" if muted else ""), data={"container": c["name"], "kind": kind, "incident": inc["id"], "muted": muted})
        if cfg.get("notify", True) and self.settings().get("notify_down", True) and not muted:
            self.m.notifier.notify("uptime", f"DOWN: {c.get('title') or c['name']}", msg, "error")
        return inc

    def _close_incident(self, inc: dict, c: dict, cfg: dict, now: float, note: str | None = None) -> None:
        self.m.db.execute("UPDATE incidents SET ended_at=? WHERE id=?", (now, inc["id"]))
        dur = now - inc["started_at"]
        who = c.get("label") or f"Container '{inc['container']}'"
        msg = f"{who} recovered after {_fmt_dur(dur)}" + (f" ({note})" if note else "")
        self.m.event("success", "uptime.up", msg, data={"container": inc["container"], "incident": inc["id"], "duration": dur})
        if cfg.get("notify", True) and not note and self.settings().get("notify_recovery", True) and not self.quiet_window(now):
            self.m.notifier.notify("uptime", f"UP: {c.get('title') or inc['container']}", msg, "success")

    def _maybe_restart(self, c: dict, cfg: dict, inc: dict, now: float) -> None:
        name = c["name"]
        window = cfg["restart_window_minutes"] * 60
        hist = [t for t in self._restarts.get(name, []) if now - t < window]
        actions = list(inc.get("actions") or [])
        if hist and now - hist[-1] < 20:
            return  # give the last restart a moment
        if len(hist) >= cfg["max_restarts"]:
            if not any(a.get("type") == "budget" for a in actions):
                actions.append({"ts": now, "type": "budget", "ok": False, "note": f"restart budget exhausted ({cfg['max_restarts']} per {cfg['restart_window_minutes']} min)"})
                self.m.db.execute("UPDATE incidents SET actions=? WHERE id=?", (json.dumps(actions), inc["id"]))
                inc["actions"] = actions
                msg = f"Container '{name}' keeps failing; restart budget of {cfg['max_restarts']} per {cfg['restart_window_minutes']} min exhausted. Manual action needed."
                self.m.event("error", "uptime.restart_budget", msg, data={"container": name})
                if cfg.get("notify", True) and self.settings().get("notify_restart", True):
                    self.m.notifier.notify("uptime", f"GIVING UP: {name}", msg, "error")
            return
        ok = self.m.docker.restart_container(c["id"]) if c.get("id") else False
        hist.append(now)
        self._restarts[name] = hist
        actions.append({"ts": now, "type": "restart", "ok": ok, "note": f"auto-restart {len(hist)}/{cfg['max_restarts']}"})
        self.m.db.execute("UPDATE incidents SET actions=? WHERE id=?", (json.dumps(actions), inc["id"]))
        inc["actions"] = actions
        self.m.event("warning" if ok else "error", "uptime.restarted", f"Auto-restarted '{name}' ({len(hist)}/{cfg['max_restarts']} in window)" if ok else f"Auto-restart of '{name}' failed", data={"container": name})
        if cfg.get("notify", True) and self.settings().get("notify_restart", True):
            self.m.notifier.notify("uptime", f"Restarted: {name}", f"Dockbare restarted '{name}' automatically ({len(hist)}/{cfg['max_restarts']} in {cfg['restart_window_minutes']} min). Reason: {inc.get('detail')}", "warning")

    def restart_now(self, name: str) -> bool:
        c = self.snapshot.get(name) or next((x for x in self.m.docker.list_containers() if x["name"] == name), None)
        if not c or not c.get("id"):
            raise ManagerError("Container not found.")
        ok = self.m.docker.restart_container(c["id"])
        self.m.event("warning" if ok else "error", "uptime.manual_restart", f"Manual restart of '{name}' {'requested' if ok else 'failed'}", data={"container": name})
        self._wake.set()
        return ok

    # ---------------------------------------------------------- queries
    def incidents(self, *, open_only: bool = False, limit: int = 200, container: str | None = None, since: float | None = None) -> list[dict]:
        sql = "SELECT * FROM incidents WHERE 1=1"
        params: list[Any] = []
        if open_only:
            sql += " AND ended_at IS NULL"
        if container:
            sql += " AND container=?"
            params.append(container)
        if since:
            sql += " AND (ended_at IS NULL OR ended_at >= ?)"
            params.append(since)
        sql += " ORDER BY (ended_at IS NULL) DESC, started_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self.m.db.query(sql, tuple(params)):
            d = dict(r)
            try:
                d["actions"] = json.loads(d.get("actions") or "[]")
            except json.JSONDecodeError:
                d["actions"] = []
            d["duration"] = (d["ended_at"] or time.time()) - d["started_at"]
            raw = d.get("detail")
            try:
                parsed = json.loads(raw) if raw and raw.startswith("{") else None
            except json.JSONDecodeError:
                parsed = None
            d["facts"] = parsed or ({"text": raw} if raw else {})
            d["detail"] = self.detail_text(raw)
            out.append(d)
        return out

    # Downtime is handled as spans (start, end, kinds) so that single containers, checks and
    # services (union or intersection of their members' outages) share the same arithmetic.
    def _incident_spans(self, keys: list[str], since: float, now: float) -> dict[str, list[tuple[float, float, set[str]]]]:
        per: dict[str, list] = {k: [] for k in keys}
        if not keys:
            return per
        q = ",".join("?" * len(keys))
        for r in self.m.db.query(f"SELECT container, started_at, ended_at, kind FROM incidents WHERE container IN ({q}) AND (ended_at IS NULL OR ended_at >= ?)", (*keys, since)):
            per[r["container"]].append((r["started_at"], r["ended_at"] or now, {r["kind"]}))
        return per

    @staticmethod
    def _merge(spans: list) -> list:
        """Union of possibly overlapping spans."""
        out: list = []
        for a, b, kinds in sorted(spans, key=lambda s: s[0]):
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b), out[-1][2] | set(kinds))
            else:
                out.append((a, b, set(kinds)))
        return out

    @staticmethod
    def _intersect(x: list, y: list) -> list:
        """Spans where both (merged, sorted) lists are down at the same time."""
        out: list = []
        i = j = 0
        while i < len(x) and j < len(y):
            a, b = max(x[i][0], y[j][0]), min(x[i][1], y[j][1])
            if b > a:
                out.append((a, b, x[i][2] | y[j][2]))
            if x[i][1] < y[j][1]:
                i += 1
            else:
                j += 1
        return out

    def _merged_spans(self, keys: list[str], since: float, now: float, *, require_all: bool = False) -> list:
        per = self._incident_spans(keys, since, now)
        if not require_all:
            return self._merge([sp for lst in per.values() for sp in lst])
        lists = [self._merge(lst) for lst in per.values()]
        out = lists[0] if lists else []
        for other in lists[1:]:
            out = self._intersect(out, other)
            if not out:
                break
        return out

    @staticmethod
    def _pct_from(spans: list, now: float) -> dict:
        out = {}
        for key, win in WINDOWS.items():
            start = now - win
            down = 0.0
            for a, b, _ in spans:
                x, y = max(a, start), min(b, now)
                if y > x:
                    down += y - x
            out[key] = round(100.0 * (1 - down / win), 3)
        return out

    @staticmethod
    def _history_from(spans: list, first: float | None, days: int, now: float) -> list[dict]:
        """Per UTC day: availability percentage (None before monitoring began) and down seconds."""
        today = int(now // 86400) * 86400
        start = today - (days - 1) * 86400
        out = []
        for i in range(days):
            a = start + i * 86400
            b = min(a + 86400, now)
            if b <= a or first is None or b <= first:
                out.append({"day": a, "pct": None, "down": 0, "kinds": []})
                continue
            a_eff = max(a, first)
            span = b - a_eff
            down = 0.0
            kinds: list[str] = []
            for s_a, s_b, s_kinds in spans:
                x, y = max(s_a, a_eff), min(s_b, b)
                if y > x:
                    down += y - x
                    kinds.extend(k for k in sorted(s_kinds) if k not in kinds)
            out.append({"day": a, "pct": round(100.0 * (1 - down / span), 3) if span > 0 else None, "down": int(down), "kinds": kinds})
        return out

    def _first_seen(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        q = ",".join("?" * len(keys))
        return {r["name"]: r["first_seen"] for r in self.m.db.query(f"SELECT name, first_seen FROM container_seen WHERE name IN ({q})", tuple(keys))}

    def uptime_pct(self, container: str, now: float | None = None) -> dict:
        now = now or time.time()
        return self._pct_from(self._incident_spans([container], now - WINDOWS["d30"], now)[container], now)

    def daily_history(self, container: str, days: int = HISTORY_DAYS_PUBLIC, now: float | None = None) -> list[dict]:
        now = now or time.time()
        start = int(now // 86400) * 86400 - (days - 1) * 86400
        spans = self._incident_spans([container], start, now)[container]
        return self._history_from(spans, self._first_seen([container]).get(container), days, now)

    def rows(self) -> list[dict]:
        if time.time() - self.last_poll > 60:
            self.poll()
        now = time.time()
        out = []
        for name, c in sorted(self.snapshot.items(), key=lambda kv: kv[0].lower()):
            d = {k: c.get(k) for k in ("id", "short_id", "name", "state", "status", "health", "exit_code", "image", "project", "service", "monitored", "monitor", "failure", "failing_for", "missing", "self", "restart_count", "ignored", "maintenance")}
            d["uptime"] = self.uptime_pct(name, now)
            d["history"] = self.daily_history(name, HISTORY_DAYS_INTERNAL, now)
            d["incident"] = c.get("incident")
            d["restarts_in_window"] = len([t for t in self._restarts.get(name, []) if now - t < (c.get("monitor") or {}).get("restart_window_minutes", 30) * 60])
            out.append(d)
        return out

    def summary(self) -> dict:
        snap = self.snapshot
        down = sorted(n for n, c in snap.items() if c.get("failure") and c.get("incident"))
        monitored = [n for n, c in snap.items() if c.get("monitored")]
        since = time.time() - 86400
        inc24 = self.m.db.one("SELECT COUNT(*) AS n FROM incidents WHERE started_at >= ? OR ended_at >= ?", (since, since))["n"]
        q = self.quiet_window()
        checks_down = sorted(st["name"] for st in self.check_state.values() if st.get("incident"))
        return {"monitored": len(monitored), "total": len(snap), "down": down, "checks_down": checks_down, "incidents_24h": int(inc24), "last_poll": self.last_poll, "error": self.last_error,
                "quiet": ({"label": q.get("label") or f"{q['start']}–{q['end']}", "mode": q["mode"]} if q else None),
                "public_items": self.public_items(), "announcements": [a for a in self.public_announcements() if a["active"] or a["upcoming"]],
                "public_group_name": self.public_group_name(remote=True)}

    def public_group_name(self, *, remote: bool = False) -> str:
        """Name of this site's group on the status page: the setting, else the instance name, else 'This site'.

        What peers get to see (``remote=True``) leaves the generic fallback empty, so their page can use
        the name they gave this instance instead of a meaningless "This site".
        """
        custom = self.settings().get("public_group_name")
        if custom:
            return custom
        name = self.m.instance_name()
        if re.fullmatch(r"dockbare-[0-9a-f]{4}", name):
            return "" if remote else "This site"
        return name

    @staticmethod
    def _public_extras(cfg: dict) -> dict:
        return {"icon": cfg.get("public_icon") or None, "url": cfg.get("public_url") or None, "description": cfg.get("public_description") or None}

    def _member_state(self, key: str, checks: dict[str, dict], names: dict[str, str] | None = None) -> dict:
        """Current state of a service member: a container name or 'check:<id>'."""
        label = (names or {}).get(key)
        if key.startswith(CHECK_PREFIX):
            chk = checks.get(key[len(CHECK_PREFIX):])
            st = self.check_state.get(key[len(CHECK_PREFIX):]) if chk and chk["enabled"] else None
            name = label or (chk["name"] if chk else key)
            inc = st.get("incident") if st else None
            return {"key": key, "name": name, "known": st is not None, "incident": inc, "check": True}
        c = self.snapshot.get(key)
        cfg = (c or {}).get("monitor") or {}
        inc = c.get("incident") if c else None
        return {"key": key, "name": label or cfg.get("display_name") or key, "known": bool(c and c.get("monitored")), "incident": inc, "check": False}

    def _service_item(self, s: dict, checks: dict[str, dict], now: float) -> dict:
        members = [self._member_state(k, checks, s.get("member_names")) for k in s["members"]]
        known = [m for m in members if m["known"]]
        down = [m for m in known if m["incident"]]
        if not down:
            status = "up"
        elif s["down_if_any"] or len(down) == len(known):
            status = "down"
        else:
            status = "degraded"
        first_inc = min((m["incident"] for m in down), key=lambda i: i["started_at"]) if down else None
        seen = self._first_seen([m["key"] for m in members])
        keys = [k for k in s["members"] if k in seen]  # members that have ever been monitored
        since = int(now // 86400) * 86400 - (HISTORY_DAYS_PUBLIC - 1) * 86400
        spans = self._merged_spans(keys, since, now, require_all=not s["down_if_any"])
        return {"id": s["id"], "name": s["name"], "service": True, "status": status,
                "since": first_inc["started_at"] if first_inc else None, "kind": first_inc["kind"] if first_inc else None,
                "uptime": self._pct_from(spans, now), "history": self._history_from(spans, min(seen.values()) if seen else None, HISTORY_DAYS_PUBLIC, now),
                "components": [{"name": m["name"], "status": ("down" if m["incident"] else "up") if m["known"] else "unknown", "kind": m["incident"]["kind"] if m["incident"] else None} for m in members],
                **self._public_extras(s)}

    def public_items(self) -> list[dict]:
        """Entries of this site's group on the status page: public containers, public checks and services."""
        now = time.time()
        services = self.services()
        checks = {c["id"]: c for c in self.checks()}
        in_service = {k for s in services for k in s["members"]}
        items = []
        for name, c in self.snapshot.items():
            cfg = c.get("monitor") or {}
            if not cfg.get("public") or not c.get("monitored") or name in in_service:
                continue
            inc = c.get("incident")
            items.append({"name": cfg.get("display_name") or name, "status": "down" if inc else "up", "since": inc["started_at"] if inc else None,
                          "kind": inc["kind"] if inc else None, "uptime": self.uptime_pct(name, now),
                          "history": self.daily_history(name, HISTORY_DAYS_PUBLIC, now), **self._public_extras(cfg)})
        for cid, chk in checks.items():
            key = CHECK_PREFIX + cid
            if not chk["public"] or not chk["enabled"] or key in in_service:
                continue
            inc = (self.check_state.get(cid) or {}).get("incident")
            items.append({"name": chk["name"], "check": True, "status": "down" if inc else "up", "since": inc["started_at"] if inc else None,
                          "kind": inc["kind"] if inc else None, "uptime": self.uptime_pct(key, now),
                          "history": self.daily_history(key, HISTORY_DAYS_PUBLIC, now), **self._public_extras(chk)})
        for s in services:
            items.append(self._service_item(s, checks, now))
        items.sort(key=lambda i: i["name"].lower())
        return items

    def public_display_names(self) -> dict[str, str]:
        """Incident key -> name shown on the public page, for everything that is public (directly or via a service)."""
        services = self.services()
        checks = {c["id"]: c for c in self.checks()}
        in_service = {k for s in services for k in s["members"]}
        out = {}
        for name, c in self.snapshot.items():
            cfg = c.get("monitor") or {}
            if cfg.get("public") and c.get("monitored") and name not in in_service:
                out[name] = cfg.get("display_name") or name
        for cid, chk in checks.items():
            if chk["public"] and chk["enabled"] and CHECK_PREFIX + cid not in in_service:
                out[CHECK_PREFIX + cid] = chk["name"]
        for s in services:
            for m in s["members"]:
                out[m] = f"{s['name']} · {self._member_state(m, checks, s.get('member_names'))['name']}"
        return out

    def public_payload(self) -> dict:
        s = self.settings()
        if time.time() - self.last_poll > 60:
            self.poll()
        anns = self.public_announcements()
        groups = [{"name": self.public_group_name(), "self": True, "online": True, "items": self.public_items(), "announcements": [a for a in anns if a["active"] or a["upcoming"]]}]
        if s.get("public_show_peers") and self.m.peers:
            for p in self.m.peers.list():
                if not p["enabled"]:
                    continue
                st = p.get("status") or {}
                remote = st.get("remote") or {}
                up_info = remote.get("uptime") or {}
                online = not (p["alert_active"] or st.get("ok") is False)
                groups.append({"name": p.get("display_name") or up_info.get("public_group_name") or p["name"], "self": False, "online": online,
                               "checked_at": st.get("refreshed_at") or st.get("checked_at"),
                               "items": (up_info.get("public_items") or []) if online else [], "announcements": (up_info.get("announcements") or []) if online else []})
        all_items = [i for g in groups for i in g["items"]]
        down = [i for i in all_items if i["status"] == "down"] + [g for g in groups if not g["online"]]
        degraded = [i for i in all_items if i["status"] == "degraded"]
        active_anns = [a for g in groups for a in g["announcements"] if a.get("active")]
        major = any(a["type"] == "incident" and a.get("severity") == "major" for a in active_anns)
        minor = any(a["type"] == "incident" for a in active_anns)
        in_maint = any(a["type"] == "maintenance" for a in active_anns)
        if major or (down and len(down) >= max(1, len(all_items) + len(groups) - 1)):
            overall = "outage"
        elif down or degraded or minor:
            overall = "degraded"
        elif in_maint:
            overall = "maintenance"
        else:
            overall = "operational"
        since = time.time() - HISTORY_DAYS_PUBLIC * 86400
        display = self.public_display_names()
        incidents = [{"name": display[i["container"]], "kind": i["kind"], "started_at": i["started_at"], "ended_at": i["ended_at"], "duration": i["duration"]}
                     for i in self.incidents(since=since, limit=400) if i["container"] in display]
        history_anns = [a for a in anns if not (a["active"] or a["upcoming"])]
        unreachable = [g["name"] for g in groups if not g["online"]]
        if s.get("public_merge_peers") and len(groups) > 1:
            # one flat section: peers' items and announcements are tagged with their site instead
            merged_items = [i for g in groups for i in ({**i, "site": None if g["self"] else g["name"], "site_checked": None if g["self"] else g.get("checked_at")} for i in g["items"])]
            merged_anns = [{**a, "site": None if g["self"] else g["name"]} for g in groups for a in g["announcements"]]
            groups = [{**groups[0], "items": sorted(merged_items, key=lambda i: i["name"].lower()), "announcements": merged_anns, "merged": True}]
        return {"title": s.get("public_title") or "Service status", "updated": time.time(), "overall": overall, "groups": groups, "incidents": incidents,
                "announcements": history_anns, "history_days": HISTORY_DAYS_PUBLIC, "unreachable": unreachable}

    def badge(self, label: str = "") -> dict:
        """Overall status in the shields.io endpoint format (plus the raw overall state)."""
        pl = self.public_payload()
        message, color, dot_dark, dot_light = BADGE.get(pl["overall"], (pl["overall"], "#6e7781", "#8593ad", "#64748b"))
        label = re.sub(r"[^\w .:/()+-]", "", str(label or ""))[:40].strip()
        return {"schemaVersion": 1, "label": label or "status", "custom_label": label, "message": message, "color": color,
                "dot": {"dark": dot_dark, "light": dot_light}, "overall": pl["overall"], "updated": pl["updated"]}

    # ------------------------------------------------- public item fields
    @staticmethod
    def _check_public_fields(cfg: dict) -> None:
        url = cfg.get("public_url") or ""
        if url and not re.match(r"^https?://\S+$", url):
            raise ManagerError("Website must be a http:// or https:// URL.")
        icon = cfg.get("public_icon") or ""
        if "://" in icon:
            if not re.match(r"^https?://\S+$", icon):
                raise ManagerError("Icon must be an http(s) image URL or a short text/emoji.")
        elif len(icon) > 8:
            raise ManagerError("Icon text is limited to 8 characters; use an image URL for anything longer.")

    # ------------------------------------------------------ external checks
    def checks(self) -> list[dict]:
        out = []
        for row in self.m.db.query("SELECT id, name, config, updated_at FROM checks ORDER BY name COLLATE NOCASE"):
            cfg = dict(CHECK_DEFAULTS)
            try:
                cfg.update(json.loads(row["config"]))
            except json.JSONDecodeError:
                pass
            cfg.update({"id": row["id"], "name": row["name"], "updated_at": row["updated_at"]})
            out.append(cfg)
        return out

    def get_check(self, cid: str) -> dict | None:
        return next((c for c in self.checks() if c["id"] == cid), None)

    def check_rows(self) -> list[dict]:
        """Checks with their last result and availability, for the admin page."""
        now = time.time()
        out = []
        for c in self.checks():
            key = CHECK_PREFIX + c["id"]
            out.append({**c, "state": self.check_state.get(c["id"]), "uptime": self.uptime_pct(key, now), "history": self.daily_history(key, HISTORY_DAYS_INTERNAL, now)})
        return out

    @staticmethod
    def _normalize_check_url(url: str) -> str:
        url = (url or "").strip()
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme in ("http", "https") and parts.hostname:
            return url
        if parts.scheme == "tcp" and parts.hostname and port:
            return f"tcp://{parts.hostname}:{port}"
        raise ManagerError("URL must be http(s)://host[/path] or tcp://host:port")

    def save_check(self, data: dict, cid: str | None = None, *, create: bool = False) -> dict:
        existing = self.get_check(cid) if cid else None
        if cid and not existing and not create:
            raise ManagerError("Check not found.")
        cfg = {k: (existing.get(k, v) if existing else v) for k, v in CHECK_DEFAULTS.items()}
        name = str(data.get("name", existing["name"] if existing else "") or "").strip()[:80]
        if not name:
            raise ManagerError("Name is required.")
        for k, default in CHECK_DEFAULTS.items():
            if k in data:
                v = data[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v or 0)
                else:
                    v = str(v or "").strip()[:STR_LIMITS.get(k, 80)]
                cfg[k] = v
        cfg["url"] = self._normalize_check_url(cfg["url"])
        cfg["interval_seconds"] = max(10, min(86400, cfg["interval_seconds"]))
        cfg["timeout_seconds"] = max(1, min(120, cfg["timeout_seconds"]))
        cfg["grace_seconds"] = max(0, min(3600, cfg["grace_seconds"]))
        self._check_public_fields(cfg)
        cid = cid or uuid.uuid4().hex[:12]
        self.m.db.execute("INSERT INTO checks(id, name, config, updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, config=excluded.config, updated_at=excluded.updated_at",
                          (cid, name, json.dumps(cfg), time.time()))
        self.m.event("info", "uptime.check_saved", f"External check '{name}' saved ({cfg['url']})")
        self._check_next[cid] = 0  # re-check soon
        if cid in self.check_state:
            self.check_state[cid].update({"name": name, "url": cfg["url"]})
        return self.get_check(cid)

    def delete_check(self, cid: str) -> None:
        c = self.get_check(cid)
        if not c:
            raise ManagerError("Check not found.")
        key = CHECK_PREFIX + cid
        with self._lock:
            self.m.db.execute("DELETE FROM checks WHERE id=?", (cid,))
            self.check_state.pop(cid, None)
            self._check_next.pop(cid, None)
            self._failing_since.pop(key, None)
            for inc in self.incidents(open_only=True, container=key):
                self._close_incident(inc, {"name": key, "label": f"Check '{c['name']}'"}, CHECK_DEFAULTS, time.time(), note="check removed")
            for s in self.services():
                if key in s["members"]:
                    self.save_service({"members": [m for m in s["members"] if m != key]}, s["id"], prune=True)
        self.m.event("info", "uptime.check_deleted", f"External check '{c['name']}' removed")

    def _probe(self, c: dict) -> dict:
        """Run one check; never raises."""
        url, timeout = c["url"], c["timeout_seconds"]
        t0 = time.monotonic()
        ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
        try:
            if url.startswith("tcp://"):
                parts = urlsplit(url)
                with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
                    pass
                return {"ok": True, "latency_ms": ms()}
            r = requests.get(url, timeout=timeout, allow_redirects=True, verify=c.get("verify_tls", True), stream=True,
                             headers={"User-Agent": f"Dockbare/{__version__} (uptime check)"})
            try:
                code = r.status_code
            finally:
                r.close()
            if code >= 400:
                return {"ok": False, "kind": "http-error", "error": f"HTTP {code}", "status_code": code, "latency_ms": ms()}
            return {"ok": True, "status_code": code, "latency_ms": ms()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "kind": "unreachable", "error": f"{e.__class__.__name__}: {str(e)[:200]}", "latency_ms": ms()}

    def run_checks(self, *, force: bool = False, only: str | None = None) -> dict[str, dict]:
        """Run the checks that are due (all of them with ``force``), open/close incidents accordingly."""
        now = time.time()
        due = [c for c in self.checks() if c["enabled"] and (only is None or c["id"] == only) and (force or now >= self._check_next.get(c["id"], 0))]
        if not due:
            return self.check_state
        with ThreadPoolExecutor(max_workers=min(8, len(due))) as ex:
            results = list(ex.map(lambda c: (c, self._probe(c)), due))
        with self._lock:
            now = time.time()
            open_incidents = {i["container"]: i for i in self.incidents(open_only=True) if i["container"].startswith(CHECK_PREFIX)}
            quiet = self.quiet_window(now)
            maint = self.maintenance_active(now)
            for c, res in results:
                key = CHECK_PREFIX + c["id"]
                self._check_next[c["id"]] = now + c["interval_seconds"]
                self.m.db.execute("INSERT OR IGNORE INTO container_seen(name, first_seen) VALUES(?,?)", (key, now))
                kind = None if res["ok"] else res["kind"]
                st = {"id": c["id"], "name": c["name"], "url": c["url"], "ok": res["ok"], "kind": kind, "error": res.get("error"),
                      "status_code": res.get("status_code"), "latency_ms": res.get("latency_ms"), "checked_at": now, "maintenance": None, "failing_for": None}
                if kind and key not in open_incidents and any(self._covers(mw, key) for mw in maint):
                    st["maintenance"] = next(mw["title"] for mw in maint if self._covers(mw, key))
                    self._failing_since.pop(key, None)
                    kind = None
                if kind and key not in open_incidents and quiet and quiet["mode"] == "ignore":
                    st["maintenance"] = quiet.get("label") or f"quiet hours {quiet['start']}–{quiet['end']}"
                    self._failing_since.pop(key, None)
                    kind = None
                inc = open_incidents.get(key)
                if kind:
                    since = self._failing_since.setdefault(key, now)
                    st["failing_for"] = now - since
                    if inc is None and now - since >= c["grace_seconds"]:
                        inc = self._open_incident({"name": key, "title": c["name"], "label": f"Check '{c['name']}'", "error": res.get("error"), "status": c["url"]}, kind, c, now,
                                                  muted=bool(quiet and quiet["mode"] == "mute"))
                    elif inc is not None and inc["kind"] != kind:
                        self.m.db.execute("UPDATE incidents SET kind=? WHERE id=?", (kind, inc["id"]))
                        inc["kind"] = kind
                else:
                    self._failing_since.pop(key, None)
                    if inc is not None:
                        self._close_incident(inc, {"name": key, "title": c["name"], "label": f"Check '{c['name']}'"}, c, now)
                        inc = None
                st["incident"] = inc
                self.check_state[c["id"]] = st
        return self.check_state

    # ------------------------------------------------------------ services
    def services(self) -> list[dict]:
        out = []
        for row in self.m.db.query("SELECT id, name, config, updated_at FROM services ORDER BY name COLLATE NOCASE"):
            cfg = dict(SERVICE_DEFAULTS)
            try:
                cfg.update(json.loads(row["config"]))
            except json.JSONDecodeError:
                pass
            cfg["members"] = [str(m) for m in (cfg.get("members") or [])]
            cfg["member_names"] = {str(k): str(v) for k, v in (cfg.get("member_names") or {}).items() if isinstance(cfg.get("member_names"), dict)}
            cfg.update({"id": row["id"], "name": row["name"], "updated_at": row["updated_at"]})
            out.append(cfg)
        return out

    def get_service(self, sid: str) -> dict | None:
        return next((s for s in self.services() if s["id"] == sid), None)

    def save_service(self, data: dict, sid: str | None = None, *, create: bool = False, prune: bool = False) -> dict:
        existing = self.get_service(sid) if sid else None
        if sid and not existing and not create:
            raise ManagerError("Service not found.")
        cfg = {k: (existing.get(k, v) if existing else v) for k, v in SERVICE_DEFAULTS.items()}
        name = str(data.get("name", existing["name"] if existing else "") or "").strip()[:80]
        if not name:
            raise ManagerError("Name is required.")
        for k, default in SERVICE_DEFAULTS.items():
            if k not in data:
                continue
            v = data[k]
            if k == "members":
                members: list[str] = []
                for m in (v or []):
                    m = str(m or "").strip()
                    if m.startswith(CHECK_PREFIX):
                        if not self.get_check(m[len(CHECK_PREFIX):]):
                            raise ManagerError(f"Unknown check '{m}'.")
                    elif not CONTAINER_RE.match(m):
                        raise ManagerError(f"Invalid container name '{m}'.")
                    if m not in members:
                        members.append(m)
                cfg[k] = members[:50]
            elif k == "member_names":
                if not isinstance(v, dict):
                    raise ManagerError("member_names must be an object.")
                cfg[k] = {str(key): str(val or "").strip()[:80] for key, val in v.items() if str(val or "").strip()}
            elif isinstance(default, bool):
                cfg[k] = bool(v)
            else:
                cfg[k] = str(v or "").strip()[:STR_LIMITS.get(k, 80)]
        cfg["member_names"] = {k: v for k, v in cfg["member_names"].items() if k in cfg["members"]}
        if not cfg["members"] and not prune:
            raise ManagerError("A service needs at least one container or check.")
        self._check_public_fields(cfg)
        sid = sid or uuid.uuid4().hex[:12]
        self.m.db.execute("INSERT INTO services(id, name, config, updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, config=excluded.config, updated_at=excluded.updated_at",
                          (sid, name, json.dumps(cfg), time.time()))
        if not prune:
            self.m.event("info", "uptime.service_saved", f"Service '{name}' saved ({len(cfg['members'])} member(s))")
        return self.get_service(sid)

    def delete_service(self, sid: str) -> None:
        s = self.get_service(sid)
        if not s:
            raise ManagerError("Service not found.")
        self.m.db.execute("DELETE FROM services WHERE id=?", (sid,))
        self.m.event("info", "uptime.service_deleted", f"Service '{s['name']}' removed")

    # ---------------------------------------------------- announcements
    @staticmethod
    def _covers(ann: dict, name: str) -> bool:
        return not ann.get("services") or name in ann["services"]

    def _ann_row(self, r, now: float | None = None) -> dict:
        now = now or time.time()
        d = dict(r)
        try:
            d["services"] = json.loads(d.get("services") or "[]")
        except json.JSONDecodeError:
            d["services"] = []
        d["public"] = bool(d["public"])
        if d["type"] == "maintenance" and d["status"] != "completed":
            if d["ends_at"] and now > d["ends_at"]:
                d["status"] = "completed"
            elif now >= d["starts_at"]:
                d["status"] = "in_progress"
            else:
                d["status"] = "scheduled"
        d["active"] = (d["type"] == "incident" and d["status"] != "resolved") or (d["type"] == "maintenance" and d["status"] == "in_progress")
        d["upcoming"] = d["type"] == "maintenance" and d["status"] == "scheduled"
        d["updates"] = [dict(u) for u in self.m.db.query("SELECT id, ts, status, message FROM announcement_updates WHERE announcement_id=? ORDER BY ts DESC, id DESC", (d["id"],))]
        return d

    def post_update(self, ann_id: int, status: str | None, message: str) -> dict:
        """Add a timestamped update to an announcement, optionally changing its status."""
        a = self.get_announcement(ann_id)
        if not a:
            raise ManagerError("Announcement not found.")
        message = str(message or "").strip()[:4000]
        if status and status != a["status"]:
            return self.save_announcement({"status": status, "update_message": message}, ann_id)
        if not message:
            raise ManagerError("Update message is required.")
        self.m.db.execute("INSERT INTO announcement_updates(announcement_id, ts, status, message) VALUES(?,?,?,?)", (ann_id, time.time(), a["status"], message))
        self.m.db.execute("UPDATE announcements SET updated_at=? WHERE id=?", (time.time(), ann_id))
        self.m.event("info", "uptime.announcement", f"Update on '{a['title']}': {message[:120]}", data={"id": ann_id})
        self.m.notifier.notify("uptime", f"Update: {a['title']}", f"[{a['status']}] {message}", "info")
        return self.get_announcement(ann_id)

    def announcements(self, *, limit: int = 100, since: float | None = None, public_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM announcements WHERE 1=1"
        params: list[Any] = []
        if since:
            sql += " AND (resolved_at IS NULL OR resolved_at >= ? OR ends_at IS NULL OR ends_at >= ?)"
            params += [since, since]
        if public_only:
            sql += " AND public=1"
        sql += " ORDER BY starts_at DESC LIMIT ?"
        params.append(limit)
        now = time.time()
        return [self._ann_row(r, now) for r in self.m.db.query(sql, tuple(params))]

    def get_announcement(self, ann_id: int) -> dict | None:
        r = self.m.db.one("SELECT * FROM announcements WHERE id=?", (ann_id,))
        return self._ann_row(r) if r else None

    def maintenance_active(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        rows = self.m.db.query("SELECT * FROM announcements WHERE type='maintenance' AND status<>'completed' AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?)", (now, now))
        return [self._ann_row(r, now) for r in rows]

    def save_announcement(self, data: dict, ann_id: int | None = None) -> dict:
        existing = self.get_announcement(ann_id) if ann_id else None
        if ann_id and not existing:
            raise ManagerError("Announcement not found.")
        typ = str(data.get("type") or (existing or {}).get("type") or "incident")
        if typ not in ANN_TYPES:
            raise ManagerError("type must be incident or maintenance.")
        title = str(data.get("title", (existing or {}).get("title") or "")).strip()[:120]
        if not title:
            raise ManagerError("Title is required.")
        message = str(data.get("message", (existing or {}).get("message") or "")).strip()[:4000]
        services = data.get("services", (existing or {}).get("services") or [])
        if not isinstance(services, list) or any(not CONTAINER_RE.match(str(x)) for x in services):
            raise ManagerError("services must be a list of container names.")
        now = time.time()
        starts_at = float(data.get("starts_at") or (existing or {}).get("starts_at") or now)
        ends_at = data.get("ends_at", (existing or {}).get("ends_at"))
        ends_at = float(ends_at) if ends_at else None
        severity = str(data.get("severity") or (existing or {}).get("severity") or "minor")
        if typ == "incident":
            status = str(data.get("status") or (existing or {}).get("status") or "investigating")
            if status not in INCIDENT_STATUSES:
                raise ManagerError("Invalid incident status.")
            if severity not in SEVERITIES:
                raise ManagerError("severity must be minor or major.")
        else:
            if not ends_at or ends_at <= starts_at:
                raise ManagerError("Maintenance needs an end time after the start time.")
            status = str(data.get("status") or (existing or {}).get("status") or "scheduled")
            if status not in MAINT_STATUSES:
                raise ManagerError("Invalid maintenance status.")
        public = bool(data.get("public", (existing or {}).get("public", True)))
        resolved_at = (existing or {}).get("resolved_at")
        if typ == "incident" and status == "resolved" and not resolved_at:
            resolved_at = now
            ends_at = now
        if typ == "incident" and status != "resolved":
            resolved_at = None
            ends_at = None
        if typ == "maintenance" and status == "completed" and (not ends_at or ends_at > now):
            ends_at = now  # completed early
        if typ == "maintenance" and status != "completed" and existing and existing.get("status") == "completed" and ends_at and ends_at <= now and "ends_at" not in data:
            raise ManagerError("This maintenance window is in the past; set a new end time to reopen it.")
        if ann_id:
            self.m.db.execute("UPDATE announcements SET type=?, title=?, message=?, services=?, status=?, severity=?, starts_at=?, ends_at=?, public=?, updated_at=?, resolved_at=? WHERE id=?",
                              (typ, title, message, json.dumps([str(x) for x in services]), status, severity, starts_at, ends_at, int(public), now, resolved_at, ann_id))
        else:
            cur = self.m.db.execute("INSERT INTO announcements(type, title, message, services, status, severity, starts_at, ends_at, public, created_at, updated_at, resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (typ, title, message, json.dumps([str(x) for x in services]), status, severity, starts_at, ends_at, int(public), now, now, resolved_at))
            ann_id = cur.lastrowid
        changed_status = not existing or existing.get("status") != status
        update_message = str(data.get("update_message") or "").strip()[:4000]
        if not existing:
            self.m.db.execute("INSERT INTO announcement_updates(announcement_id, ts, status, message) VALUES(?,?,?,?)", (ann_id, now, status, message))
        elif changed_status or update_message:
            self.m.db.execute("INSERT INTO announcement_updates(announcement_id, ts, status, message) VALUES(?,?,?,?)", (ann_id, now, status, update_message))
        if typ == "incident":
            if not existing:
                self.m.event("warning" if severity == "minor" else "error", "uptime.announcement", f"Incident announced: {title} ({severity})", data={"id": ann_id})
                self.m.notifier.notify("uptime", f"Incident: {title}", message or title, "error" if severity == "major" else "warning")
            elif changed_status:
                lvl = "success" if status == "resolved" else "info"
                self.m.event(lvl, "uptime.announcement", f"Incident '{title}' → {status}" + (f": {update_message[:120]}" if update_message else ""), data={"id": ann_id})
                self.m.notifier.notify("uptime", f"Incident {status}: {title}", update_message or message or title, lvl)
        elif not existing:
            self.m.event("info", "uptime.announcement", f"Maintenance scheduled: {title} ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(starts_at))} UTC)", data={"id": ann_id})
            self.m.notifier.notify("uptime", f"Maintenance scheduled: {title}", f"{message or title}\nWindow: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(starts_at))} – {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ends_at))} UTC", "info")
        self._wake.set()
        return self.get_announcement(ann_id)

    def _sync_from_updates(self, ann_id: int) -> None:
        """After editing/deleting updates, the announcement status follows the newest update."""
        a = self.get_announcement(ann_id)
        if not a or not a["updates"]:
            return
        latest = a["updates"][0]["status"]
        now = time.time()
        if a["type"] == "incident":
            resolved_at = (a.get("resolved_at") or a["updates"][0]["ts"]) if latest == "resolved" else None
            self.m.db.execute("UPDATE announcements SET status=?, resolved_at=?, ends_at=?, updated_at=? WHERE id=?", (latest, resolved_at, resolved_at, now, ann_id))
        else:
            ends = a.get("ends_at")
            if latest == "completed" and (not ends or ends > now):
                ends = now
            self.m.db.execute("UPDATE announcements SET status=?, ends_at=?, updated_at=? WHERE id=?", (latest, ends, now, ann_id))
        self._wake.set()

    def edit_update(self, ann_id: int, update_id: int, message: str | None, status: str | None) -> dict:
        a = self.get_announcement(ann_id)
        if not a:
            raise ManagerError("Announcement not found.")
        row = next((u for u in a["updates"] if u["id"] == update_id), None)
        if not row:
            raise ManagerError("Update not found.")
        valid = INCIDENT_STATUSES if a["type"] == "incident" else MAINT_STATUSES
        new_status = status or row["status"]
        if new_status not in valid:
            raise ManagerError("Invalid status.")
        new_message = row["message"] if message is None else str(message).strip()[:4000]
        self.m.db.execute("UPDATE announcement_updates SET message=?, status=? WHERE id=?", (new_message, new_status, update_id))
        self._sync_from_updates(ann_id)
        self.m.event("info", "uptime.announcement", f"Update on '{a['title']}' edited", data={"id": ann_id})
        return self.get_announcement(ann_id)

    def delete_update(self, ann_id: int, update_id: int) -> dict:
        a = self.get_announcement(ann_id)
        if not a:
            raise ManagerError("Announcement not found.")
        if len(a["updates"]) <= 1:
            raise ManagerError("An announcement keeps at least its first update; delete the announcement instead.")
        n = self.m.db.execute("DELETE FROM announcement_updates WHERE id=? AND announcement_id=?", (update_id, ann_id)).rowcount
        if not n:
            raise ManagerError("Update not found.")
        self._sync_from_updates(ann_id)
        return self.get_announcement(ann_id)

    def delete_announcement(self, ann_id: int) -> None:
        if not self.get_announcement(ann_id):
            raise ManagerError("Announcement not found.")
        self.m.db.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
        self.m.db.execute("DELETE FROM announcement_updates WHERE announcement_id=?", (ann_id,))
        self._wake.set()

    def public_announcements(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for a in self.announcements(public_only=True, since=now - HISTORY_DAYS_PUBLIC * 86400, limit=400):
            if a["active"] or a["upcoming"] or (a["type"] == "incident" and a["status"] == "resolved") or a["status"] == "completed":
                out.append({k: a.get(k) for k in ("id", "type", "title", "message", "services", "status", "severity", "starts_at", "ends_at", "resolved_at", "active", "upcoming", "updated_at", "updates")})
        return out

    def housekeeping(self) -> None:
        keep = int(self.settings().get("incident_keep_days", 365))
        self.m.db.execute("DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at < ?", (time.time() - keep * 86400,))


def _text_width(text: str, size: float = 13, bold: bool = False) -> float:
    """Rough width of UI-font text: per-glyph classes instead of one average, so short/wide words fit."""
    narrow, wide = set("iljtfrI !.,:;'|"), set("mwMW@")
    w = 0.0
    for ch in text:
        w += 0.33 if ch in narrow else 0.86 if ch in wide else 0.66 if ch.isupper() or ch.isdigit() else 0.56
    return w * size * (1.06 if bold else 1.0)


def badge_pill_svg(label: str, message: str, dot: str, theme: str = "dark") -> str:
    """Status badge in the look of the status page: a rounded pill with a glowing status dot."""
    bg, border, text, muted = BADGE_THEMES.get(theme, BADGE_THEMES["dark"])
    message = message[:1].upper() + message[1:]
    h, pad, gap = 32, 14, 9
    lw = _text_width(label, 12.5) if label else 0.0
    mw = _text_width(message, 13, bold=True)
    x = pad
    parts = []
    if label:
        parts.append(f'<text x="{x:.1f}" y="20.5" font-size="12.5" font-weight="500" fill="{muted}" textLength="{lw:.1f}" lengthAdjust="spacingAndGlyphs">{html.escape(label)}</text>')
        x += lw + gap + 2
    cx = x + 5
    parts.append(f'<circle cx="{cx:.1f}" cy="16" r="7" fill="{dot}" fill-opacity=".2"/><circle cx="{cx:.1f}" cy="16" r="4" fill="{dot}"/>')
    x = cx + 5 + gap
    parts.append(f'<text x="{x:.1f}" y="20.5" font-size="13" font-weight="600" fill="{text}" textLength="{mw:.1f}" lengthAdjust="spacingAndGlyphs">{html.escape(message)}</text>')
    total = int(round(x + mw + pad))
    title = html.escape(f"{label}: {message}" if label else message)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="{h}" viewBox="0 0 {total} {h}" role="img" aria-label="{title}">'
        f'<title>{title}</title>'
        f'<rect x=".5" y=".5" width="{total - 1}" height="{h - 1}" rx="{(h - 1) / 2}" fill="{bg}" stroke="{border}"/>'
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Inter,Roboto,Helvetica,Arial,sans-serif">'
        + "".join(parts) + "</g></svg>"
    )


def badge_svg(label: str, message: str, color: str) -> str:
    """A flat shields.io-style badge."""
    def width(text: str) -> int:
        return int(round(len(text) * 6.3)) + 12

    lw, mw = width(label), width(message)
    total = lw + mw
    el, em = html.escape(label), html.escape(message)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{el}: {em}">'
        f'<title>{el}: {em}</title>'
        '<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{mw}" height="20" fill="{html.escape(color)}"/><rect width="{total}" height="20" fill="url(#s)"/></g>'
        '<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{lw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3" textLength="{lw - 12}">{el}</text><text x="{lw / 2:.1f}" y="14" textLength="{lw - 12}">{el}</text>'
        f'<text x="{lw + mw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3" textLength="{mw - 12}">{em}</text><text x="{lw + mw / 2:.1f}" y="14" textLength="{mw - 12}">{em}</text>'
        '</g></svg>'
    )


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"
