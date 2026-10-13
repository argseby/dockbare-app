"""Container uptime monitoring: incidents, auto-restart, public status page.

A poll loop (plus the Docker event stream for instant reaction) classifies every
container; a container that is failing for longer than its grace period opens an
incident, which is closed when it recovers. Notifications go out both ways.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

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
        for b in ("monitor_new", "public_enabled", "public_show_peers"):
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
                    v = str(v or "")[:80]
                cfg[k] = v
        cfg["max_restarts"] = max(0, min(100, cfg["max_restarts"]))
        cfg["restart_window_minutes"] = max(1, min(1440, cfg["restart_window_minutes"]))
        cfg["grace_seconds"] = max(0, min(3600, cfg["grace_seconds"]))
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
        self._threads = [t, e]
        t.start()
        e.start()

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
            open_incidents = {i["container"]: i for i in self.incidents(open_only=True)}
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
                snapshot[name] = {"name": name, "state": "missing", "status": "container not found", "monitored": True, "monitor": cfg, "failure": "missing", "incident": inc, "missing": True}
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
        label = {"down": "exited", "dead": "dead", "oom": "killed (out of memory)", "unhealthy": "unhealthy", "restart-loop": "stuck in a restart loop", "missing": "missing"}.get(kind, kind)
        msg = f"Container '{c['name']}' is {label}" + (f": {self.detail_text(detail)}" if self.detail_text(detail) else "")
        self.m.event("error", "uptime.down", msg + (" (quiet hours: not notified)" if muted else ""), data={"container": c["name"], "kind": kind, "incident": inc["id"], "muted": muted})
        if cfg.get("notify", True) and self.settings().get("notify_down", True) and not muted:
            self.m.notifier.notify("uptime", f"DOWN: {c['name']}", msg, "error")
        return inc

    def _close_incident(self, inc: dict, c: dict, cfg: dict, now: float, note: str | None = None) -> None:
        self.m.db.execute("UPDATE incidents SET ended_at=? WHERE id=?", (now, inc["id"]))
        dur = now - inc["started_at"]
        msg = f"Container '{inc['container']}' recovered after {_fmt_dur(dur)}" + (f" ({note})" if note else "")
        self.m.event("success", "uptime.up", msg, data={"container": inc["container"], "incident": inc["id"], "duration": dur})
        if cfg.get("notify", True) and not note and self.settings().get("notify_recovery", True) and not self.quiet_window(now):
            self.m.notifier.notify("uptime", f"UP: {inc['container']}", msg, "success")

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

    def uptime_pct(self, container: str, now: float | None = None) -> dict:
        now = now or time.time()
        rows = self.m.db.query("SELECT started_at, ended_at FROM incidents WHERE container=? AND (ended_at IS NULL OR ended_at >= ?)", (container, now - WINDOWS["d30"]))
        out = {}
        for key, win in WINDOWS.items():
            start = now - win
            down = 0.0
            for r in rows:
                a = max(r["started_at"], start)
                b = min(r["ended_at"] or now, now)
                if b > a:
                    down += b - a
            out[key] = round(100.0 * (1 - down / win), 3)
        return out

    def daily_history(self, container: str, days: int = HISTORY_DAYS_PUBLIC, now: float | None = None) -> list[dict]:
        """Per UTC day: availability percentage (None before monitoring began) and down seconds."""
        now = now or time.time()
        today = int(now // 86400) * 86400
        start = today - (days - 1) * 86400
        row = self.m.db.one("SELECT first_seen FROM container_seen WHERE name=?", (container,))
        first = row["first_seen"] if row else None
        incs = self.m.db.query("SELECT started_at, ended_at, kind FROM incidents WHERE container=? AND (ended_at IS NULL OR ended_at >= ?)", (container, start))
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
            for r in incs:
                x = max(r["started_at"], a_eff)
                y = min(r["ended_at"] or now, b)
                if y > x:
                    down += y - x
                    if r["kind"] not in kinds:
                        kinds.append(r["kind"])
            out.append({"day": a, "pct": round(100.0 * (1 - down / span), 3) if span > 0 else None, "down": int(down), "kinds": kinds})
        return out

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
        return {"monitored": len(monitored), "total": len(snap), "down": down, "incidents_24h": int(inc24), "last_poll": self.last_poll, "error": self.last_error,
                "quiet": ({"label": q.get("label") or f"{q['start']}–{q['end']}", "mode": q["mode"]} if q else None),
                "public_items": self.public_items(), "announcements": [a for a in self.public_announcements() if a["active"] or a["upcoming"]],
                "public_group_name": self.public_group_name()}

    def public_group_name(self) -> str:
        """Name of this site's group on the status page: the setting, else the instance name, else 'This site'."""
        custom = self.settings().get("public_group_name")
        if custom:
            return custom
        name = self.m.instance_name()
        return "This site" if re.fullmatch(r"dockbare-[0-9a-f]{4}", name) else name

    def public_items(self) -> list[dict]:
        now = time.time()
        items = []
        for name, c in sorted(self.snapshot.items(), key=lambda kv: kv[0].lower()):
            cfg = c.get("monitor") or {}
            if not cfg.get("public") or not c.get("monitored"):
                continue
            inc = c.get("incident")
            items.append({"name": cfg.get("display_name") or name, "status": "down" if inc else "up", "since": inc["started_at"] if inc else None,
                          "kind": inc["kind"] if inc else None, "uptime": self.uptime_pct(name, now),
                          "history": self.daily_history(name, HISTORY_DAYS_PUBLIC, now)})
        return items

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
                groups.append({"name": up_info.get("public_group_name") or p["name"], "self": False, "online": not (p["alert_active"] or st.get("ok") is False),
                               "checked_at": st.get("checked_at"), "items": up_info.get("public_items") or [], "announcements": up_info.get("announcements") or []})
        all_items = [i for g in groups for i in g["items"]]
        down = [i for i in all_items if i["status"] == "down"] + [g for g in groups if not g["online"]]
        active_anns = [a for g in groups for a in g["announcements"] if a.get("active")]
        major = any(a["type"] == "incident" and a.get("severity") == "major" for a in active_anns)
        minor = any(a["type"] == "incident" for a in active_anns)
        in_maint = any(a["type"] == "maintenance" for a in active_anns)
        if major or (down and len(down) >= max(1, len(all_items) + len(groups) - 1)):
            overall = "outage"
        elif down or minor:
            overall = "degraded"
        elif in_maint:
            overall = "maintenance"
        else:
            overall = "operational"
        since = time.time() - HISTORY_DAYS_PUBLIC * 86400
        public_names = {n for n, c in self.snapshot.items() if (c.get("monitor") or {}).get("public")}
        display = {n: ((c.get("monitor") or {}).get("display_name") or n) for n, c in self.snapshot.items()}
        incidents = [{"name": display.get(i["container"], i["container"]), "kind": i["kind"], "started_at": i["started_at"], "ended_at": i["ended_at"], "duration": i["duration"]}
                     for i in self.incidents(since=since, limit=400) if i["container"] in public_names]
        history_anns = [a for a in anns if not (a["active"] or a["upcoming"])]
        return {"title": s.get("public_title") or "Service status", "updated": time.time(), "overall": overall, "groups": groups, "incidents": incidents,
                "announcements": history_anns, "history_days": HISTORY_DAYS_PUBLIC}

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


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"
