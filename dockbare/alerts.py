"""Alert rules on resource metrics and container logs.

A rule compares one metric against a threshold. Rules are evaluated after every
metrics collection: a condition that holds for ``for_minutes`` fires (event +
notification), and the alert resolves as soon as it no longer holds. While firing,
``repeat_minutes`` re-sends the notification. Maintenance windows from the uptime
page suppress container rules for the containers they cover; quiet hours in
"mute" mode delay the notification until they end (the alert still fires).

Log rules count lines matching a regular expression in a container's log over a
sliding window (``window_minutes``); only lines since the last evaluation are
fetched from Docker, so each evaluation reads very little.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from typing import Any

from .dockerctl import DockerError
from .manager import Manager, ManagerError

log = logging.getLogger("dockbare.alerts")

# metric -> label, unit, scope, default comparison and default threshold for new rules
METRICS: dict[str, dict[str, str]] = {
    "host.cpu": {"label": "Host CPU", "unit": "%", "scope": "host", "op": ">", "default": 90},
    "host.mem_pct": {"label": "Host memory used", "unit": "%", "scope": "host", "op": ">", "default": 90},
    "host.swap_pct": {"label": "Host swap used", "unit": "%", "scope": "host", "op": ">", "default": 50},
    "host.load1": {"label": "Host load (1 min)", "unit": "", "scope": "host", "op": ">", "default": 8},
    "host.temp": {"label": "Host temperature", "unit": "°C", "scope": "host", "op": ">", "default": 85},
    "disk.used_pct": {"label": "Disk used", "unit": "%", "scope": "disk", "op": ">", "default": 90},
    "disk.days_to_full": {"label": "Disk full in", "unit": "days", "scope": "disk", "op": "<", "default": 7},
    "container.cpu": {"label": "Container CPU (100 = one core)", "unit": "%", "scope": "container", "op": ">", "default": 90},
    "container.mem_pct": {"label": "Container memory (of its limit, else of host RAM)", "unit": "%", "scope": "container", "op": ">", "default": 90},
    "container.mem_mb": {"label": "Container memory", "unit": "MiB", "scope": "container", "op": ">", "default": 1024},
    "container.log_matches": {"label": "Log lines matching a pattern", "unit": "lines", "scope": "container", "op": ">", "default": 0},
}
RULE_DEFAULTS = {
    "name": "", "enabled": True, "metric": "host.cpu", "target": "*", "op": ">", "threshold": 90.0, "for_minutes": 5,
    "repeat_minutes": 0, "notify_resolved": True, "severity": "warning", "pattern": "", "ignore_case": True, "window_minutes": 5,
}
SEED_RULES = [
    {"name": "Disk almost full", "metric": "disk.used_pct", "target": "*", "op": ">", "threshold": 90, "for_minutes": 15, "severity": "error"},
    {"name": "Disk full within a week", "metric": "disk.days_to_full", "target": "*", "op": "<", "threshold": 7, "for_minutes": 60},
    {"name": "Host memory exhausted", "metric": "host.mem_pct", "target": "", "op": ">", "threshold": 95, "for_minutes": 10},
]
LOG_FETCH_LIMIT = 5000  # lines per container and evaluation, so a log flood cannot stall the collector


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "n/a"
    s = f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}"
    if unit == "lines" and v == 1:
        unit = "line"
    return f"{s}{'' if not unit else ('%' if unit == '%' else ' ' + unit)}"


class Alerts:
    def __init__(self, manager: Manager):
        self.m = manager
        self._lock = threading.RLock()
        self._log_pos: dict[tuple[str, str], float] = {}            # (rule, container) -> last log ts read
        self._log_hits: dict[tuple[str, str], deque] = {}           # (rule, container) -> deque[(ts, count)]
        self._regex: dict[str, re.Pattern] = {}

    # ----------------------------------------------------------- rules
    @staticmethod
    def _rule(row) -> dict:
        cfg = dict(RULE_DEFAULTS)
        try:
            cfg.update(json.loads(row["config"]))
        except json.JSONDecodeError:
            pass
        cfg["id"] = row["id"]
        return cfg

    def rules(self) -> list[dict]:
        return [self._rule(r) for r in self.m.db.query("SELECT id, config FROM alert_rules ORDER BY created_at")]

    def get(self, rid: str) -> dict | None:
        r = self.m.db.one("SELECT id, config FROM alert_rules WHERE id=?", (rid,))
        return self._rule(r) if r else None

    def seed(self) -> None:
        """Add a few sensible default rules once per installation."""
        if self.m.db.get_setting("alert_rules_seeded"):
            return
        self.m.db.set_setting("alert_rules_seeded", True)
        if self.m.db.one("SELECT COUNT(*) AS n FROM alert_rules")["n"]:
            return
        for r in SEED_RULES:
            self.save(r, create=True, quiet=True)

    def validate(self, data: dict, existing: dict | None = None) -> dict:
        cfg = dict(existing or RULE_DEFAULTS)
        for k in RULE_DEFAULTS:
            if k in data:
                cfg[k] = data[k]
        metric = str(cfg["metric"])
        if metric not in METRICS:
            raise ManagerError(f"Unknown metric '{metric}'.")
        scope = METRICS[metric]["scope"]
        cfg["name"] = str(cfg.get("name") or "").strip()[:80] or METRICS[metric]["label"]
        cfg["enabled"] = bool(cfg.get("enabled", True))
        cfg["op"] = cfg["op"] if cfg.get("op") in (">", "<") else METRICS[metric]["op"]
        target = str(cfg.get("target") or "").strip()
        if scope == "host":
            target = ""
        elif scope == "container" and target != "*" and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$", target):
            raise ManagerError("Target must be a container name or * for all containers.")
        elif scope == "disk" and not target:
            target = "*"
        cfg["target"] = target[:255]
        try:
            cfg["threshold"] = float(cfg["threshold"])
            cfg["for_minutes"] = int(cfg.get("for_minutes") or 0)
            cfg["repeat_minutes"] = int(cfg.get("repeat_minutes") or 0)
            cfg["window_minutes"] = int(cfg.get("window_minutes") or 5)
        except (TypeError, ValueError) as e:
            raise ManagerError("Threshold and durations must be numbers.") from e
        if not 0 <= cfg["for_minutes"] <= 1440:
            raise ManagerError("'For' must be between 0 and 1440 minutes.")
        if not 0 <= cfg["repeat_minutes"] <= 10080:
            raise ManagerError("'Repeat' must be between 0 and 10080 minutes.")
        if not 1 <= cfg["window_minutes"] <= 1440:
            raise ManagerError("The log window must be between 1 and 1440 minutes.")
        cfg["notify_resolved"] = bool(cfg.get("notify_resolved", True))
        cfg["ignore_case"] = bool(cfg.get("ignore_case", True))
        cfg["severity"] = "error" if cfg.get("severity") == "error" else "warning"
        cfg["pattern"] = str(cfg.get("pattern") or "")[:300]
        if metric == "container.log_matches":
            if not cfg["pattern"]:
                raise ManagerError("A log rule needs a pattern.")
            try:
                re.compile(cfg["pattern"])
            except re.error as e:
                raise ManagerError(f"Invalid pattern: {e}") from e
        return {k: cfg[k] for k in RULE_DEFAULTS}

    def save(self, data: dict, rid: str | None = None, *, create: bool = False, quiet: bool = False) -> dict:
        existing = self.get(rid) if rid else None
        if rid and not existing and not create:
            raise ManagerError("Alert rule not found.")
        cfg = self.validate(data, existing)
        now = time.time()
        rid = rid or uuid.uuid4().hex[:12]
        with self._lock:
            self.m.db.execute("INSERT INTO alert_rules(id, config, created_at, updated_at) VALUES(?,?,?,?) "
                              "ON CONFLICT(id) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at",
                              (rid, json.dumps(cfg), now, now))
            if existing:  # changed rule: start over (resolve silently)
                self.m.db.execute("DELETE FROM alert_state WHERE rule_id=?", (rid,))
                self._forget(rid)
        if not quiet:
            self.m.event("info", "metrics.rule_saved", f"Alert rule '{cfg['name']}' saved")
        return {**cfg, "id": rid}

    def delete(self, rid: str) -> None:
        r = self.get(rid)
        if not r:
            raise ManagerError("Alert rule not found.")
        with self._lock:
            self.m.db.execute("DELETE FROM alert_rules WHERE id=?", (rid,))
            self.m.db.execute("DELETE FROM alert_state WHERE rule_id=?", (rid,))
            self._forget(rid)
        self.m.event("info", "metrics.rule_deleted", f"Alert rule '{r['name']}' removed")

    def _forget(self, rid: str) -> None:
        for d in (self._log_pos, self._log_hits):
            for k in [k for k in d if k[0] == rid]:
                d.pop(k, None)
        self._regex.pop(rid, None)

    # ---------------------------------------------------------- values
    def values(self, rule: dict, now: float) -> dict[str, float | None]:
        """Current value per target for one rule ({target: value}); a missing target has no entry."""
        metric, target = rule["metric"], rule["target"]
        mt = self.m.metrics
        scope = METRICS[metric]["scope"]
        if scope == "host":
            h = mt.host_now
            if not h:
                return {}
            v = {
                "host.cpu": h.get("cpu"),
                "host.mem_pct": (h["mem_used"] / h["mem_total"] * 100) if h.get("mem_total") else None,
                "host.swap_pct": (h["swap_used"] / h["swap_total"] * 100) if h.get("swap_total") else None,
                "host.load1": h.get("load1"), "host.temp": h.get("temp"),
            }[metric]
            return {"": v} if v is not None else {}
        if scope == "disk":
            out = {}
            disks = mt.disks(now) if metric == "disk.days_to_full" else mt.disks_now
            for d in disks:
                if target not in ("*", d["mount"], d.get("name")):
                    continue
                v = d.get("pct") if metric == "disk.used_pct" else d.get("days_to_full")
                if v is not None:
                    out[d["name"]] = v
            return out
        # container scope
        host_mem = (mt.host() or {}).get("mem_total") or 0
        rows = [r for r in mt.container_rows() if target in ("*", r["name"])]
        if metric == "container.log_matches":
            return self._log_counts(rule, rows, now)
        out = {}
        for r in rows:
            if metric == "container.cpu":
                out[r["name"]] = r["cpu"]
            elif metric == "container.mem_mb":
                out[r["name"]] = r["mem"] / 1024 / 1024
            else:
                limit = r["mem_limit"] if r.get("mem_limit") and (not host_mem or r["mem_limit"] < host_mem) else host_mem
                if limit:
                    out[r["name"]] = r["mem"] / limit * 100
        return out

    def _log_counts(self, rule: dict, rows: list[dict], now: float) -> dict[str, float]:
        rid = rule["id"]
        rx = self._regex.get(rid)
        if rx is None or rx.pattern != rule["pattern"]:
            rx = self._regex[rid] = re.compile(rule["pattern"], re.IGNORECASE if rule.get("ignore_case") else 0)
        window = rule["window_minutes"] * 60
        out = {}
        for r in rows:
            key = (rid, r["name"])
            since = self._log_pos.get(key)
            if since is None:
                self._log_pos[key] = now  # start counting from now, not from the whole history
                self._log_hits[key] = deque()
                continue  # no value yet (unknown), so a firing alert is not resolved by a restart
            try:
                lines = self.m.docker.container_logs(r.get("id") or r["name"], tail=LOG_FETCH_LIMIT, since=since)
            except (DockerError, AttributeError) as e:
                log.debug("Log read for %s failed: %s", r["name"], e)
                continue
            new = [(ts, text) for ts, text in lines if ts > since]
            if new:
                self._log_pos[key] = max(ts for ts, _ in new)
            hits = self._log_hits.setdefault(key, deque())
            n = sum(1 for _, text in new if rx.search(text))
            if n:
                hits.append((now, n))
            while hits and hits[0][0] < now - window:
                hits.popleft()
            out[r["name"]] = float(sum(c for _, c in hits))
        return out

    # -------------------------------------------------------- evaluate
    def _suppressed(self, rule: dict, target: str, now: float) -> bool:
        up = self.m.uptime
        if not up or METRICS[rule["metric"]]["scope"] != "container":
            return False
        try:
            return any(up._covers(mw, target) for mw in up.maintenance_active(now))
        except Exception:  # noqa: BLE001
            return False

    def _muted(self, now: float) -> bool:
        up = self.m.uptime
        try:
            q = up.quiet_window(now) if up else None
        except Exception:  # noqa: BLE001
            q = None
        return bool(q)  # "mute" and "ignore" quiet hours both hold alert notifications back

    def evaluate(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            states = {(r["rule_id"], r["target"]): dict(r) for r in self.m.db.query("SELECT * FROM alert_state")}
            rules = self.rules()
            seen: set[tuple[str, str]] = set()
            muted = self._muted(now)
            for rule in rules:
                if not rule["enabled"]:
                    continue
                try:
                    vals = self.values(rule, now)
                except Exception:  # noqa: BLE001
                    log.exception("Evaluating alert rule %s failed", rule["name"])
                    continue
                for target, v in vals.items():
                    key = (rule["id"], target)
                    seen.add(key)
                    st = states.get(key)
                    hit = v is not None and (v > rule["threshold"] if rule["op"] == ">" else v < rule["threshold"])
                    if hit and self._suppressed(rule, target, now):
                        hit = False
                    if not hit:
                        if st:
                            self._resolve(rule, target, st, v, now, muted)
                        continue
                    if not st:
                        st = {"rule_id": rule["id"], "target": target, "status": "pending", "since": now, "value": v, "notified_at": None}
                        self.m.db.execute("INSERT INTO alert_state(rule_id, target, status, since, value, notified_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                                          (rule["id"], target, "pending", now, v, None, now))
                    if st["status"] == "pending" and now - st["since"] >= rule["for_minutes"] * 60:
                        st["status"] = "firing"
                        self.m.event(rule["severity"], "metrics.alert", self._message(rule, target, v),
                                     data={"rule": rule["id"], "target": target, "value": v})
                    notify_due = st["status"] == "firing" and not muted and (
                        st.get("notified_at") is None
                        or (rule["repeat_minutes"] and now - st["notified_at"] >= rule["repeat_minutes"] * 60))
                    if notify_due:
                        self._notify(rule, target, v, st, now, repeat=st.get("notified_at") is not None)
                        st["notified_at"] = now
                    self.m.db.execute("UPDATE alert_state SET status=?, value=?, notified_at=?, updated_at=? WHERE rule_id=? AND target=?",
                                      (st["status"], v, st.get("notified_at"), now, rule["id"], target))
            # targets without a value this round: rule removed, target gone, or simply no sample yet
            for key, st in states.items():
                if key in seen:
                    continue
                rule = next((r for r in rules if r["id"] == key[0]), None)
                if rule is None or not rule["enabled"]:
                    self.m.db.execute("DELETE FROM alert_state WHERE rule_id=? AND target=?", key)
                elif self._gone(rule, key[1]):
                    self._resolve(rule, key[1], st, None, now, muted, gone=True)
                # else: unknown for now (e.g. first sampling round after a restart) → keep the state

    def _gone(self, rule: dict, target: str) -> bool:
        """Whether a target without a value has really disappeared (rather than not being sampled yet)."""
        mt = self.m.metrics
        scope = METRICS[rule["metric"]]["scope"]
        if scope == "container":
            return target not in mt.running_names()
        if scope == "disk":
            return bool(mt.disks_now) and target not in {d["name"] for d in mt.disks_now} | {d["mount"] for d in mt.disks_now}
        return False  # host: no sample yet

    def _resolve(self, rule: dict, target: str, st: dict, v: float | None, now: float, muted: bool, gone: bool = False) -> None:
        self.m.db.execute("DELETE FROM alert_state WHERE rule_id=? AND target=?", (rule["id"], target))
        if st["status"] != "firing":
            return
        who = f" ({target})" if target else ""
        unit = METRICS[rule["metric"]]["unit"]
        msg = f"Resolved: {rule['name']}{who}" + (" (no longer reported)" if gone else f", now {_fmt(v, unit)}") + f" after {_dur(now - st['since'])}"
        self.m.event("success", "metrics.resolved", msg, data={"rule": rule["id"], "target": target})
        if rule["notify_resolved"] and st.get("notified_at") and not muted:
            self.m.notifier.notify("metrics", f"RESOLVED: {rule['name']}{who}", msg, "success", headline="Alert resolved",
                                   subject_name=f"{rule['name']}{who}", facts=[("Host", self.m.hostname), ("Firing for", _dur(now - st["since"]))])

    def _message(self, rule: dict, target: str, v: float | None) -> str:
        meta = METRICS[rule["metric"]]
        who = f" on {target}" if target else ""
        extra = f" (/{rule['pattern']}/ in {rule['window_minutes']} min)" if rule["metric"] == "container.log_matches" else ""
        return f"{rule['name']}: {meta['label']}{who} is {_fmt(v, meta['unit'])}{extra}, threshold {rule['op']} {_fmt(rule['threshold'], meta['unit'])}"

    def _notify(self, rule: dict, target: str, v: float | None, st: dict, now: float, repeat: bool) -> None:
        who = f" ({target})" if target else ""
        facts = [("Host", self.m.hostname), ("Since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["since"])))]
        if rule["for_minutes"]:
            facts.append(("Condition held for", f"{rule['for_minutes']} min"))
        self.m.notifier.notify("metrics", f"{'STILL ' if repeat else ''}ALERT: {rule['name']}{who}", self._message(rule, target, v),
                               rule["severity"], headline="Resource alert", subject_name=f"{rule['name']}{who}", facts=facts)

    # ------------------------------------------------------------ read
    def states(self) -> list[dict]:
        """Every rule with its targets' current state, for the UI."""
        rows = self.m.db.query("SELECT * FROM alert_state")
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r["rule_id"], []).append({"target": r["target"], "status": r["status"], "since": r["since"], "value": r["value"],
                                                   "notified_at": r["notified_at"]})
        out = []
        for rule in self.rules():
            st = by.get(rule["id"], [])
            status = "firing" if any(s["status"] == "firing" for s in st) else "pending" if st else ("ok" if rule["enabled"] else "disabled")
            out.append({**rule, "status": status, "targets": sorted(st, key=lambda s: s["target"]), "unit": METRICS[rule["metric"]]["unit"]})
        return out

    def firing(self) -> list[dict]:
        return [r for r in self.states() if r["status"] == "firing"]

    def payload(self) -> dict[str, Any]:
        return {"rules": self.states(), "metrics": METRICS, "defaults": RULE_DEFAULTS}


def _dur(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"
