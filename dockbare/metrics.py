"""Resource metrics: containers (Docker stats) and the host (/proc, /sys), with rollups.

A collector thread samples every ``interval_seconds``:

* every running container via ``/containers/{id}/stats`` (one-shot, no streaming);
* the host via :mod:`hostmetrics` (CPU, load, memory, disk I/O, network, temperature);
* the filesystems behind Dockbare's data and backup directories (disk usage).

Docker and /proc report cumulative counters, so rates come from the difference to the
previous reading; the first reading after start and readings across a counter reset
(container restarted or recreated, host rebooted) are skipped.

Raw samples (``res = 0``) are kept for 24 h, 5-minute rollups (``res = 300``: averages
plus peaks) for 7 days and hourly rollups (``res = 3600``) for 90 days. Reads pick the
coarsest resolution that still gives ~120 points and add the raw tail that has not
been rolled up yet.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .dockerctl import DockerError
from .hostmetrics import HostReader, host_rates
from .manager import Manager, ManagerError

log = logging.getLogger("dockbare.metrics")

# notify_logs: put the last log lines of a crashed container into the "down" notification (logs may hold secrets)
SETTINGS_DEFAULTS = {"enabled": True, "interval_seconds": 30, "public_host": False, "notify_logs": False}
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}
RETENTION = {0: 86400, 300: 7 * 86400, 3600: 90 * 86400}
CHART_POINTS = 120
SPARK_POINTS = 60
HOST_INFO_TTL = 600
ROLLUP_EVERY = 60
FORECAST_WINDOW = 7 * 86400
FORECAST_MIN_SPAN = 6 * 3600
COUNTERS = ("net_rx", "net_tx", "blk_r", "blk_w")
BACKUP_KINDS = ("backup.started", "backup.completed", "backup.failed")

# table -> (key columns, {column: aggregate used for rollups})
ROLLUP_SPECS: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "container_samples": (("name",), {
        "cpu": "AVG(cpu)", "cpu_max": "MAX(cpu_max)", "mem": "AVG(mem)", "mem_max": "MAX(mem_max)", "mem_limit": "MAX(mem_limit)",
        "net_rx": "AVG(net_rx)", "net_tx": "AVG(net_tx)", "blk_r": "AVG(blk_r)", "blk_w": "AVG(blk_w)", "pids": "MAX(pids)"}),
    "host_samples": ((), {
        "cpu": "AVG(cpu)", "cpu_max": "MAX(cpu_max)", "load1": "AVG(load1)", "mem_used": "AVG(mem_used)", "mem_max": "MAX(mem_max)",
        "mem_total": "MAX(mem_total)", "swap_used": "AVG(swap_used)", "swap_total": "MAX(swap_total)", "disk_r": "AVG(disk_r)",
        "disk_w": "AVG(disk_w)", "net_rx": "AVG(net_rx)", "net_tx": "AVG(net_tx)", "temp": "AVG(temp)", "temp_max": "MAX(temp_max)"}),
    "disk_samples": (("mount",), {"used": "AVG(used)", "total": "MAX(total)"}),
}


def rates(prev: dict, cur: dict, dt: float) -> dict | None:
    """Turn two raw container stats readings ``dt`` seconds apart into one sample, or None to skip."""
    if dt <= 0 or prev.get("id") != cur.get("id"):
        return None
    if cur["cpu_total"] < prev["cpu_total"] or any(cur[k] < prev[k] for k in COUNTERS):
        return None  # counters went backwards: the container restarted in between
    d_sys = cur["cpu_system"] - prev["cpu_system"]
    d_cpu = cur["cpu_total"] - prev["cpu_total"]
    cpu = (d_cpu / d_sys) * cur["cpus"] * 100.0 if d_sys > 0 else 0.0
    out = {"cpu": round(max(0.0, cpu), 2), "mem": cur["mem"], "mem_limit": cur["mem_limit"], "pids": cur["pids"]}
    for k in COUNTERS:
        out[k] = round((cur[k] - prev[k]) / dt, 1)
    return out


def res_for(span: int) -> int:
    """Stored resolution used for a time range."""
    return 0 if span <= 86400 else 300 if span <= 7 * 86400 else 3600


def _round(r) -> dict:
    return {k: (round(r[k], 2) if isinstance(r[k], float) else r[k]) for k in r.keys()}


class Metrics:
    def __init__(self, manager: Manager, reader: HostReader | None = None):
        self.m = manager
        s = manager.settings
        paths = [("Dockbare data", str(s.data_dir)), ("Backups", str(s.backup_dir))] + [(p, p) for p in (getattr(s, "host_disks", None) or [])]
        self.reader = reader or HostReader(proc=getattr(s, "host_proc", "/proc") or "/proc", disk_paths=paths)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev: dict[str, dict] = {}       # container name -> last raw reading (+ ts, id)
        self._host_prev: dict | None = None
        self.latest: dict[str, dict] = {}      # container name -> last computed sample + meta
        self.last_seen: dict[str, dict] = {}   # like latest, but kept after the container stops (incident context)
        self.host_now: dict | None = None
        self.disks_now: list[dict] = []
        self._host: dict | None = None
        self._host_at = 0.0
        self._last_prune = 0.0
        self._last_rollup = 0.0
        self.last_collect: float | None = None
        self.last_error: str | None = None
        self.listeners: list[Callable[[float], None]] = []  # called after every collection (alert rules)

    # ----------------------------------------------------------- settings
    def settings(self) -> dict:
        s = dict(SETTINGS_DEFAULTS)
        s.update(self.m.db.get_setting("metrics", {}) or {})
        return {k: s[k] for k in SETTINGS_DEFAULTS}

    def save_settings(self, data: dict) -> dict:
        s = self.settings()
        for b in ("enabled", "public_host", "notify_logs"):
            if b in data:
                s[b] = bool(data[b])
        if "interval_seconds" in data:
            try:
                iv = int(data["interval_seconds"])
            except (TypeError, ValueError) as e:
                raise ManagerError("Interval must be a number of seconds.") from e
            if not 10 <= iv <= 300:
                raise ManagerError("Interval must be between 10 and 300 seconds.")
            s["interval_seconds"] = iv
        self.m.db.set_setting("metrics", s)
        self.m.event("info", "settings.saved", "Metrics settings saved")
        if not s["enabled"]:
            self.m.db.execute("DELETE FROM alert_state")  # nothing is evaluated any more; don't show stale alerts
            self._prev.clear()
            self._host_prev = None
            self.latest.clear()
            self.host_now = None
        self._wake.set()
        return s

    # --------------------------------------------------------------- loop
    def restore(self, now: float | None = None) -> None:
        """Load the last stored readings, so the page shows values right after a restart.

        Rates need two readings, so without this the current values stay empty for one
        sampling interval after every restart (the history is in the database either way).
        Only readings from the last few intervals are used; live samples replace them.
        """
        now = now if now is not None else time.time()
        since = now - max(600, 3 * self.settings()["interval_seconds"])
        db = self.m.db
        h = db.one("SELECT * FROM host_samples WHERE res = 0 AND ts >= ? ORDER BY ts DESC LIMIT 1", (since,))
        if h and self.host_now is None:
            self.host_now = {k: h[k] for k in ("ts", "cpu", "load1", "mem_used", "mem_total", "swap_used", "swap_total",
                                               "disk_r", "disk_w", "net_rx", "net_tx", "temp")}
        rows = db.query("SELECT c.* FROM container_samples c JOIN (SELECT name, MAX(ts) AS ts FROM container_samples "
                        "WHERE res = 0 AND ts >= ? GROUP BY name) l ON c.name = l.name AND c.ts = l.ts WHERE c.res = 0", (since,))
        for r in rows:
            if r["name"] not in self.latest:
                self.latest[r["name"]] = {k: r[k] for k in ("ts", "cpu", "mem", "mem_limit", "net_rx", "net_tx", "blk_r", "blk_w", "pids")} | {
                    "id": None, "project": None, "service": None, "image": None}
        last = max([r["ts"] for r in rows] + ([h["ts"]] if h else []), default=None)
        if last and self.last_collect is None:
            self.last_collect = last

    def start(self, background: bool = True) -> None:
        try:
            self.restore()
        except Exception:  # noqa: BLE001
            log.exception("Restoring the last metrics failed")
        if not background:
            return
        self._thread = threading.Thread(target=self._loop, name="dockbare-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        self._stop.wait(5)  # let start-up (uptime poll, scheduler) settle first
        while not self._stop.is_set():
            s = self.settings()
            if s["enabled"]:
                try:
                    self.collect()
                except Exception:  # noqa: BLE001
                    log.exception("Metrics collection failed")
            self._wake.wait(timeout=s["interval_seconds"])
            self._wake.clear()

    # ------------------------------------------------------------ collect
    def host(self) -> dict:
        """Static host facts: name and OS from Docker, CPU count and RAM from /proc (Docker as fallback)."""
        now = time.time()
        if self._host is None or now - self._host_at > HOST_INFO_TTL:
            info: dict = {"cpus": 0, "mem_total": 0, "name": "", "os": "", "kernel": ""}
            try:
                info.update(self.m.docker.host_info())
            except (DockerError, AttributeError) as e:
                log.debug("Docker host info unavailable: %s", e)
            cpu, mem = self.reader.cpu(), self.reader.memory()
            if cpu:
                info["cpus"] = cpu["cpus"]
            if mem:
                info["mem_total"] = mem["mem_total"]
            info["network"] = self.reader.host_proc
            self._host, self._host_at = info, now
        return self._host

    def _read(self, c: dict) -> tuple[dict, dict | None, str | None]:
        try:
            return c, self.m.docker.container_stats(c["id"]), None
        except DockerError as e:
            return c, None, str(e)

    def collect(self, now: float | None = None) -> int:
        """Take one sample of the host and every running container. Returns the container rows written."""
        with self._lock:
            n = self._collect_containers(now)
            now = self.last_collect or time.time()
            self._collect_host(now)
            if now - self._last_rollup >= ROLLUP_EVERY:
                self.rollup(now)
            if now - self._last_prune > 3600:
                self.prune(now)
        for fn in list(self.listeners):
            try:
                fn(now)
            except Exception:  # noqa: BLE001
                log.exception("Metrics listener failed")
        return n

    def _collect_containers(self, now: float | None) -> int:
        try:
            containers = [c for c in self.m.docker.list_containers() if c.get("state") == "running"]
        except DockerError as e:
            self.last_error = str(e)
            self.last_collect = now if now is not None else time.time()
            return 0
        if containers:
            with ThreadPoolExecutor(max_workers=min(8, len(containers)), thread_name_prefix="dockbare-stats") as ex:
                results = list(ex.map(self._read, containers))
        else:
            results = []
        now = now if now is not None else time.time()
        rows: list[tuple] = []
        latest: dict[str, dict] = {}
        errors = [e for _, _, e in results if e]
        for c, raw, _ in results:
            if raw is None:
                continue
            name = c["name"]
            raw = {**raw, "ts": now, "id": c["id"]}
            prev = self._prev.get(name)
            self._prev[name] = raw
            meta = {"id": c["id"], "project": c.get("project"), "service": c.get("service"), "image": c.get("image")}
            sample = rates(prev, raw, now - prev["ts"]) if prev else None
            if sample is None:
                old = self.latest.get(name)
                if old and old.get("id") in (None, c["id"]):  # None: restored from the database after a restart
                    latest[name] = {**old, **meta}  # keep showing the last value during a skipped cycle
                continue
            latest[name] = {**sample, **meta, "ts": now}
            self.last_seen.pop(name, None)
            self.last_seen[name] = latest[name]
            rows.append((now, name, sample["cpu"], sample["cpu"], sample["mem"], sample["mem"], sample["mem_limit"],
                         sample["net_rx"], sample["net_tx"], sample["blk_r"], sample["blk_w"], sample["pids"]))
        running = {c["name"] for c in containers}
        self._prev = {k: v for k, v in self._prev.items() if k in running}
        self.latest = latest
        while len(self.last_seen) > 500:
            self.last_seen.pop(next(iter(self.last_seen)))
        if rows:
            with self.m.db.tx() as conn:
                conn.executemany("INSERT INTO container_samples(res, ts, name, cpu, cpu_max, mem, mem_max, mem_limit, net_rx, net_tx, blk_r, blk_w, pids) "
                                 "VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.last_collect = now
        self.last_error = errors[0] if errors and len(errors) == len(results) else None
        return len(rows)

    def _collect_host(self, now: float) -> None:
        try:
            raw = self.reader.read()
        except Exception as e:  # noqa: BLE001
            log.debug("Host metrics unavailable: %s", e)
            raw = {}
        prev, self._host_prev = self._host_prev, {**raw, "ts": now}
        sample = host_rates(prev, raw, now - prev["ts"]) if prev else None
        if sample is not None:
            sample["ts"] = now
            self.host_now = sample
            self.m.db.execute(
                "INSERT INTO host_samples(res, ts, cpu, cpu_max, load1, mem_used, mem_max, mem_total, swap_used, swap_total, disk_r, disk_w, net_rx, net_tx, temp, temp_max) "
                "VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, sample["cpu"], sample["cpu"], sample["load1"], sample["mem_used"], sample["mem_used"], sample["mem_total"],
                 sample["swap_used"], sample["swap_total"], sample["disk_r"], sample["disk_w"], sample["net_rx"], sample["net_tx"],
                 sample["temp"], sample["temp"]))
        try:
            disks = self.reader.disks()
        except Exception as e:  # noqa: BLE001
            log.debug("Disk usage unavailable: %s", e)
            disks = []
        self.disks_now = disks
        if disks:
            with self.m.db.tx() as conn:
                conn.executemany("INSERT INTO disk_samples(res, ts, mount, used, total) VALUES(0,?,?,?,?)",
                                 [(now, d["mount"], d["used"], d["total"]) for d in disks])

    # ------------------------------------------------------ rollup, prune
    def rollup(self, now: float | None = None) -> int:
        """Aggregate finished buckets: raw → 5 min, 5 min → 1 h. Returns rows written."""
        now = now if now is not None else time.time()
        self._last_rollup = now
        written = 0
        db = self.m.db
        for table, (keys, aggs) in ROLLUP_SPECS.items():
            cols = list(keys) + list(aggs)
            for res, src in ((300, 0), (3600, 300)):
                last = db.one(f"SELECT MAX(ts) AS t FROM {table} WHERE res = ?", (res,))["t"]
                if last is not None:
                    start = last + res
                else:
                    first = db.one(f"SELECT MIN(ts) AS t FROM {table} WHERE res = ?", (src,))["t"]
                    if first is None:
                        continue
                    start = (int(first) // res) * res
                end = (int(now) // res) * res
                if end <= start:
                    continue
                group = ", ".join([*keys, "b"])
                cur = db.execute(
                    f"INSERT INTO {table}(res, ts, {', '.join(cols)}) "
                    f"SELECT {res}, b, {', '.join([*keys, *aggs.values()])} FROM "
                    f"(SELECT *, CAST(ts / {res} AS INTEGER) * {res} AS b FROM {table} WHERE res = ? AND ts >= ? AND ts < ?) "
                    f"GROUP BY {group}", (src, start, end))
                written += max(0, cur.rowcount or 0)
        return written

    def prune(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        self._last_prune = now
        n = 0
        for table in ROLLUP_SPECS:
            for res, keep in RETENTION.items():
                n += self.m.db.execute(f"DELETE FROM {table} WHERE res = ? AND ts < ?", (res, now - keep)).rowcount or 0
        return n

    # --------------------------------------------------------------- read
    @staticmethod
    def window(rng: str) -> int:
        if rng not in RANGES:
            raise ManagerError(f"Unknown range '{rng}'. Use one of: {', '.join(RANGES)}.")
        return RANGES[rng]

    def _query(self, table: str, select: str, *, span: int, now: float, points: int, where: str = "", params: dict | None = None,
               group: str = "t") -> tuple[list, int]:
        """Rows bucketed for a chart: rollups of the right resolution plus the raw tail not rolled up yet."""
        res = res_for(span)
        since = now - span
        bucket = max(res or 1, span // points)
        p = {"b": bucket, "s": since, "r": res, **(params or {})}
        if res == 0:
            src = "res = 0"
        else:
            last = self.m.db.one(f"SELECT MAX(ts) AS t FROM {table} WHERE res = ?", (res,))["t"]
            p["tail"] = (last + res) if last is not None else since
            src = "res IN (0, :r) AND (res = :r OR ts >= :tail)"
        rows = self.m.db.query(
            f"SELECT CAST(ts / :b AS INTEGER) * :b AS t, {select} FROM {table} WHERE {src} AND ts >= :s {where} GROUP BY {group} ORDER BY t", p)
        return rows, bucket

    def series(self, name: str, rng: str = "1h", now: float | None = None) -> list[dict]:
        """Downsampled samples of one container: averages per bucket plus peaks."""
        span = self.window(rng)
        now = now if now is not None else time.time()
        rows, _ = self._query("container_samples",
                              "AVG(cpu) AS cpu, MAX(cpu_max) AS cpu_max, AVG(mem) AS mem, MAX(mem_max) AS mem_max, MAX(mem_limit) AS mem_limit, "
                              "AVG(net_rx) AS net_rx, AVG(net_tx) AS net_tx, AVG(blk_r) AS blk_r, AVG(blk_w) AS blk_w, MAX(pids) AS pids",
                              span=span, now=now, points=CHART_POINTS, where="AND name = :n", params={"n": name})
        return [_round(r) for r in rows]

    def host_series(self, rng: str = "1h", now: float | None = None) -> list[dict]:
        span = self.window(rng)
        now = now if now is not None else time.time()
        rows, _ = self._query("host_samples",
                              "AVG(cpu) AS cpu, MAX(cpu_max) AS cpu_max, AVG(load1) AS load1, AVG(mem_used) AS mem_used, MAX(mem_max) AS mem_max, "
                              "MAX(mem_total) AS mem_total, AVG(swap_used) AS swap_used, AVG(disk_r) AS disk_r, AVG(disk_w) AS disk_w, "
                              "AVG(net_rx) AS net_rx, AVG(net_tx) AS net_tx, AVG(temp) AS temp, MAX(temp_max) AS temp_max",
                              span=span, now=now, points=CHART_POINTS)
        return [_round(r) for r in rows]

    def disk_forecast(self, mount: str, free: float, now: float | None = None) -> dict:
        """Linear growth of the used space over the last 7 days → bytes per day and days until full."""
        now = now if now is not None else time.time()
        rows, _ = self._query("disk_samples", "AVG(used) AS used", span=FORECAST_WINDOW, now=now, points=200,
                              where="AND mount = :m", params={"m": mount})
        pts = [(r["t"], r["used"]) for r in rows if r["used"] is not None]
        if len(pts) < 6 or pts[-1][0] - pts[0][0] < FORECAST_MIN_SPAN:
            return {"growth_per_day": None, "days_to_full": None}
        n = len(pts)
        mx = sum(t for t, _ in pts) / n
        my = sum(u for _, u in pts) / n
        var = sum((t - mx) ** 2 for t, _ in pts)
        slope = sum((t - mx) * (u - my) for t, u in pts) / var if var else 0.0
        per_day = slope * 86400
        days = (free / per_day) if per_day > 0 else None
        return {"growth_per_day": round(per_day), "days_to_full": round(days, 1) if days is not None else None}

    def disks(self, now: float | None = None) -> list[dict]:
        out = []
        for d in self.disks_now:
            out.append({**d, **self.disk_forecast(d["mount"], d["free"], now)})
        return out

    def backup_marks(self, since: float) -> list[dict]:
        """Backup runs in the window as [start, end] spans, for chart markers."""
        rows = self.m.db.query(
            f"SELECT ts, kind, volume FROM events WHERE ts >= ? AND kind IN ({','.join('?' * len(BACKUP_KINDS))}) ORDER BY ts",
            (since - 6 * 3600, *BACKUP_KINDS))
        open_: dict[str, float] = {}
        out = []
        for r in rows:
            if r["kind"] == "backup.started":
                open_[r["volume"] or ""] = r["ts"]
                continue
            start = open_.pop(r["volume"] or "", None)
            if start is None or r["ts"] < since:
                continue
            out.append({"start": max(start, since), "end": r["ts"], "volume": r["volume"], "ok": r["kind"] == "backup.completed"})
        out.extend({"start": max(t, since), "end": None, "volume": v, "ok": None} for v, t in open_.items() if t >= since - 6 * 3600)
        return out[-200:]

    def running_names(self) -> set[str]:
        """Containers that were running at the last collection (sampled or not)."""
        return set(self._prev)

    def container_rows(self) -> list[dict]:
        """Current sample per running container (for alerts and the peer status)."""
        return [{"name": n, **v} for n, v in self.latest.items()]

    def overview(self, rng: str = "1h", now: float | None = None) -> dict[str, Any]:
        span = self.window(rng)
        now = now if now is not None else time.time()
        since = now - span
        rows, bucket = self._query("container_samples",
                                   "name, AVG(cpu) AS cpu, AVG(mem) AS mem, AVG(net_rx) AS net_rx, AVG(net_tx) AS net_tx",
                                   span=span, now=now, points=SPARK_POINTS, group="name, t")
        sparks: dict[str, list] = {}
        totals: dict[int, dict] = {}
        for r in rows:
            sparks.setdefault(r["name"], []).append([r["t"], round(r["cpu"], 2), int(r["mem"])])
            t = totals.setdefault(r["t"], {"t": r["t"], "cpu": 0.0, "mem": 0, "net_rx": 0.0, "net_tx": 0.0})
            t["cpu"] += r["cpu"]
            t["mem"] += int(r["mem"])
            t["net_rx"] += r["net_rx"]
            t["net_tx"] += r["net_tx"]
        host = self.host()
        snap = getattr(self.m.uptime, "snapshot", None) or {}
        names = set(sparks) | set(self.latest)
        containers = []
        for name in names:
            cur = self.latest.get(name)
            u = snap.get(name) or {}
            limited = bool(cur and cur["mem_limit"] and host["mem_total"] and cur["mem_limit"] < host["mem_total"])
            containers.append({
                "name": name, "project": (cur or {}).get("project") or u.get("project"), "service": (cur or {}).get("service") or u.get("service"),
                "image": (cur or {}).get("image") or u.get("image"), "state": u.get("state") or ("running" if cur else "gone"),
                "health": u.get("health"), "failure": u.get("failure"),
                "latest": ({k: cur[k] for k in ("ts", "cpu", "mem", "mem_limit", "net_rx", "net_tx", "blk_r", "blk_w", "pids")} | {"mem_limited": limited}) if cur else None,
                "spark": sparks.get(name, []),
            })
        containers.sort(key=lambda c: (c["latest"] is None, -((c["latest"] or {}).get("cpu") or 0), c["name"]))
        live = [c["latest"] for c in containers if c["latest"]]
        s = self.settings()
        alerts = getattr(self.m, "alerts", None)
        return {
            "enabled": s["enabled"], "interval_seconds": s["interval_seconds"], "settings": s,
            "last_collect": self.last_collect, "error": self.last_error,
            "range": rng, "ranges": list(RANGES), "since": since, "now": now, "bucket": bucket, "host": host,
            "host_now": self.host_now, "host_series": self.host_series(rng, now), "disks": self.disks(now),
            "current": {
                "cpu": round(sum(c["cpu"] for c in live), 2), "mem": sum(c["mem"] for c in live),
                "net_rx": round(sum(c["net_rx"] for c in live), 1), "net_tx": round(sum(c["net_tx"] for c in live), 1),
                "blk_r": round(sum(c["blk_r"] for c in live), 1), "blk_w": round(sum(c["blk_w"] for c in live), 1),
                "running": len(live),
            },
            "totals": [{**t, "cpu": round(t["cpu"], 2), "net_rx": round(t["net_rx"], 1), "net_tx": round(t["net_tx"], 1)} for t in sorted(totals.values(), key=lambda x: x["t"])],
            "containers": containers,
            "backups": self.backup_marks(since),
            "alerts": alerts.states() if alerts else [],
        }

    def container(self, name: str, rng: str = "1h", now: float | None = None) -> dict | None:
        now = now if now is not None else time.time()
        series = self.series(name, rng, now)
        cur = self.latest.get(name)
        if not series and cur is None:
            return None
        u = (getattr(self.m.uptime, "snapshot", None) or {}).get(name) or {}
        span = self.window(rng)
        return {"name": name, "range": rng, "ranges": list(RANGES), "since": now - span, "now": now, "bucket": max(res_for(span) or 1, span // CHART_POINTS),
                "latest": cur and {k: cur[k] for k in ("ts", "cpu", "mem", "mem_limit", "net_rx", "net_tx", "blk_r", "blk_w", "pids")},
                "project": (cur or {}).get("project") or u.get("project"), "image": (cur or {}).get("image") or u.get("image"),
                "state": u.get("state") or ("running" if cur else "gone"), "health": u.get("health"),
                "host": self.host(), "series": series, "backups": self.backup_marks(now - span)}

    def public_host(self) -> dict | None:
        """Host health for the public status page (only when enabled in the metrics settings)."""
        if not self.settings()["public_host"] or not self.host_now:
            return None
        s = self.summary()
        return {k: s[k] for k in ("cpu", "mem_pct", "disk_pct", "temp")}

    def report(self, start: float, end: float) -> dict:
        """Resource figures for a report period: host averages and peaks, top containers, disks, alerts."""
        span = int(end - start)
        res = res_for(span)
        db = self.m.db
        h = db.one("SELECT COUNT(*) AS n, AVG(cpu) AS cpu, MAX(cpu_max) AS cpu_max, AVG(mem_used * 100.0 / mem_total) AS mem, "
                   "MAX(mem_max * 100.0 / mem_total) AS mem_max, MAX(temp_max) AS temp_max FROM host_samples "
                   "WHERE res = ? AND ts >= ? AND ts < ? AND mem_total > 0", (res, start, end))
        def top(order: str) -> list[dict]:
            rows = db.query(f"SELECT name, AVG(cpu) AS cpu, MAX(cpu_max) AS cpu_max, AVG(mem) AS mem, MAX(mem_max) AS mem_max "
                            f"FROM container_samples WHERE res = ? AND ts >= ? AND ts < ? GROUP BY name ORDER BY {order} DESC LIMIT 3", (res, start, end))
            return [_round(r) for r in rows]
        alerts = db.query("SELECT ts, level, message FROM events WHERE kind = 'metrics.alert' AND ts >= ? AND ts < ? ORDER BY ts", (start, end))
        return {
            "host": _round(h) if h and h["n"] else None,
            "top_cpu": top("AVG(cpu)"), "top_mem": top("AVG(mem)"),
            "disks": self.disks(end), "alerts_fired": len(alerts),
            "alerts": [{"ts": a["ts"], "level": a["level"], "message": a["message"]} for a in alerts[-10:]],
        }

    def summary(self) -> dict:
        """Compact current state (for peers, the status page and reports)."""
        h, host = self.host_now, self.host()
        disks = self.disks()
        worst = max(disks, key=lambda d: d.get("pct") or 0) if disks else None
        alerts = getattr(self.m, "alerts", None)
        return {
            "cpu": h["cpu"] if h else None, "load1": h["load1"] if h else None,
            "mem_pct": round(h["mem_used"] / h["mem_total"] * 100, 1) if h and h.get("mem_total") else None,
            "temp": h.get("temp") if h else None, "cpus": host.get("cpus"), "mem_total": host.get("mem_total"),
            "disk_pct": worst.get("pct") if worst else None, "disk_name": worst.get("name") if worst else None,
            "disk_days_to_full": min((d["days_to_full"] for d in disks if d.get("days_to_full") is not None), default=None),
            "containers": len(self.latest), "alerts_firing": [a["name"] for a in alerts.firing()] if alerts else [],
            "at": self.last_collect,
        }
