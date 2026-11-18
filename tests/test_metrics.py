"""Container resource metrics: stats parsing, rate maths, collection, downsampling, API."""
import pytest

from dockbare.dockerctl import parse_stats
from dockbare.manager import ManagerError
from dockbare.metrics import rates

GiB = 1024 ** 3


def stats_payload(cpu_total, system, *, mem=500_000_000, cache_key="inactive_file", cache=100_000_000, limit=2 * GiB,
                  rx=0, tx=0, br=0, bw=0, pids=7, cpus=4):
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": cpu_total}, "system_cpu_usage": system, "online_cpus": cpus},
        "memory_stats": {"usage": mem, "limit": limit, "stats": {cache_key: cache}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}, "eth1": {"rx_bytes": rx, "tx_bytes": 0}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": br}, {"op": "write", "value": bw}, {"op": "Total", "value": br + bw}]},
        "pids_stats": {"current": pids},
    }


def test_parse_stats_cgroup_v2_and_v1():
    v2 = parse_stats(stats_payload(10, 1000, rx=300, tx=100, br=4096, bw=8192))
    assert v2["mem"] == 400_000_000 and v2["mem_limit"] == 2 * GiB
    assert v2["net_rx"] == 600 and v2["net_tx"] == 100  # summed over networks
    assert v2["blk_r"] == 4096 and v2["blk_w"] == 8192  # "Total" is not counted twice
    assert v2["cpus"] == 4 and v2["pids"] == 7
    v1 = parse_stats(stats_payload(10, 1000, cache_key="cache", cache=200_000_000))
    assert v1["mem"] == 300_000_000
    # a stopped container answers with zeros
    assert parse_stats({"cpu_stats": {"cpu_usage": {"total_usage": 0}}, "memory_stats": {}}) is None
    assert parse_stats(None) is None


def raw(cpu_total, system, rx=0, tx=0, cid="c1", **kw):
    r = parse_stats(stats_payload(cpu_total, system, rx=rx, tx=tx, **kw))
    r["id"] = cid
    return r


def test_rates_cpu_and_counters():
    a, b = raw(1_000, 10_000, rx=0), raw(1_500, 12_000, rx=3_000, tx=600)
    s = rates(a, b, 30)
    # 500 / 2000 of the host's time on 4 CPUs = one full core
    assert s["cpu"] == pytest.approx(100.0)
    assert s["net_rx"] == pytest.approx(200.0)  # (6000 - 0) / 30, both networks
    assert s["net_tx"] == pytest.approx(20.0)


def test_rates_skip_reset_and_recreate():
    a = raw(5_000, 10_000, rx=10_000)
    assert rates(a, raw(100, 12_000, rx=10_000), 30) is None          # CPU counter went back (restart)
    assert rates(a, raw(6_000, 12_000, rx=50), 30) is None            # network counter reset
    assert rates(a, raw(6_000, 12_000, rx=10_000, cid="c2"), 30) is None  # recreated under the same name
    assert rates(a, raw(6_000, 12_000, rx=10_000), 0) is None
    assert rates(a, raw(6_000, 10_000, rx=10_000), 30)["cpu"] == 0.0  # no system delta → no division by zero


class StatsDocker:
    """FakeDocker with running containers whose counters grow on every read."""

    def __init__(self, base):
        self.base = base
        self.containers = {
            "web": {"id": "c-web", "state": "running", "project": "shop", "service": "web"},
            "db": {"id": "c-db", "state": "running", "project": "shop", "service": "db"},
            "old": {"id": "c-old", "state": "exited"},
        }
        self.reads = {}
        self.logs = {}

    def __getattr__(self, item):
        return getattr(self.base, item)

    def list_containers(self):
        return [{"short_id": c["id"], "name": n, "image": "img", "status": "", "health": None, "exit_code": None, "created": 0,
                 "project": None, "service": None, **c} for n, c in self.containers.items()]

    def container_stats(self, cid):
        n = self.reads[cid] = self.reads.get(cid, 0) + 1
        share = 0.5 if cid == "c-web" else 0.1  # web uses half the host (2 of 4 cores), db 10 %
        return parse_stats(stats_payload(int(n * 1_000_000 * share), n * 1_000_000, rx=n * 3_000, tx=n * 300,
                                         limit=GiB if cid == "c-db" else 64 * GiB))

    def container_logs(self, cid, tail=500, since=None):
        lines = self.logs.get(cid, [])
        return [ln for ln in lines if since is None or ln[0] >= int(since)][-int(tail):] if tail != "all" else list(lines)

    def host_info(self):
        return {"cpus": 4, "mem_total": 64 * GiB, "name": "box", "os": "Linux", "kernel": "6"}

    def inspect_container(self, cid):
        return {"exit_code": None, "oom_killed": False, "restart_count": 0, "error": "", "health_log": []}

    def own_container_id(self):
        return None


class FakeReader:
    """Stands in for HostReader: 4 CPUs at 25 % busy, 16 GiB RAM, one disk growing 1 GiB per reading."""

    host_proc = False

    def __init__(self):
        self.n = 0
        self.busy = 0.25
        self.total = self.idle = 0
        self.used = 100 * GiB

    def cpu(self):
        return {"cpu_total": self.total, "cpu_idle": self.idle, "cpus": 4}

    def memory(self):
        return {"mem_total": 16 * GiB, "mem_used": 8 * GiB, "swap_total": 0, "swap_used": 0}

    def read(self):
        self.n += 1
        self.total += 400
        self.idle += round(400 * (1 - self.busy))
        return {"cpu": self.cpu(), "load": (1.5, 1.0, 0.5), "mem": self.memory(),
                "disk": {"disk_r": self.n * 3000, "disk_w": self.n * 6000}, "net": None, "temp": 55.0}

    def disks(self):
        self.used += GiB
        free = 500 * GiB - self.used
        return [{"mount": "/data", "name": "Dockbare data + Backups", "total": 500 * GiB, "used": self.used, "free": free,
                 "pct": round(self.used / (self.used + free) * 100, 1)}]


@pytest.fixture
def metrics_app(settings, monkeypatch):
    import dockbare.app as appmod
    from tests.conftest import FakeDocker

    d = StatsDocker(FakeDocker())
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: d)
    settings.testing = True
    app = appmod.create_app(settings, start=True)
    app.extensions["dockbare"].metrics.reader = FakeReader()
    c = app.test_client()
    c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    yield app, c, d
    app.extensions["dockbare"].stop()


def test_collect_overview_and_series(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    now = 1_800_000_000.0
    assert m.collect(now=now - 60) == 0          # first reading: nothing to compare yet
    assert "c-old" not in d.reads                # stopped containers are not queried
    assert m.collect(now=now - 30) == 2
    assert m.collect(now=now) == 2
    o = m.overview("1h", now=now)
    by = {x["name"]: x for x in o["containers"]}
    assert set(by) == {"web", "db"}
    assert o["containers"][0]["name"] == "web"   # sorted by current CPU
    assert by["web"]["latest"]["cpu"] == pytest.approx(200.0)  # 2 cores in "docker stats" terms
    assert by["web"]["latest"]["net_rx"] == pytest.approx(200.0)
    assert by["db"]["latest"]["mem_limited"] is True and by["web"]["latest"]["mem_limited"] is False
    assert by["web"]["project"] == "shop"
    assert o["current"]["running"] == 2 and o["current"]["cpu"] == pytest.approx(240.0)
    assert o["host"]["cpus"] == 4
    assert o["totals"] and sum(len(x["spark"]) for x in o["containers"]) >= 2
    s = m.series("web", "1h", now=now)
    assert s and s[-1]["cpu"] == pytest.approx(200.0) and s[-1]["cpu_max"] >= s[-1]["cpu"]


def test_downsampling_and_prune(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    now = 1_800_000_000.0
    rows = [(0, now - i * 30, "web", float(i % 10), float(i % 10), 100, 100, 1000, 1.0, 1.0, 0.0, 0.0, 3) for i in range(2 * 3600 // 30)]
    rows.append((0, now - 30 * 3600, "web", 1.0, 1.0, 1, 1, 1, 0.0, 0.0, 0.0, 0.0, 1))  # older than the raw retention window
    with m.m.db.tx() as conn:
        conn.executemany("INSERT INTO container_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    hour = m.series("web", "1h", now=now)
    assert 110 <= len(hour) <= 122            # ~120 buckets of 30 s
    day = m.series("web", "24h", now=now)
    assert 10 <= len(day) <= 12               # 2 h of data in 12-minute buckets
    assert day[-2]["cpu"] == pytest.approx(4.5, abs=0.6) and day[-2]["cpu_max"] == 9.0
    assert m.prune(now) == 1
    assert m.m.db.one("SELECT COUNT(*) AS n FROM container_samples WHERE res = 0 AND ts < ?", (now - 24 * 3600,))["n"] == 0
    with pytest.raises(ManagerError):
        m.series("web", "1y", now=now)


def test_backup_marks(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    db = m.m.db
    for ts, kind, vol in [(1000, "backup.started", "a"), (1050, "backup.completed", "a"), (1100, "backup.started", "b"),
                          (1200, "backup.failed", "b"), (1300, "backup.started", "c")]:
        db.execute("INSERT INTO events(ts, level, kind, volume, message) VALUES(?,?,?,?,?)", (ts, "info", kind, vol, "x"))
    marks = m.backup_marks(900)
    assert [(x["volume"], x["start"], x["end"], x["ok"]) for x in marks] == [("a", 1000, 1050, True), ("b", 1100, 1200, False), ("c", 1300, None, None)]


def test_metrics_api(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    m.collect()
    m.collect()
    r = c.get("/api/metrics?range=24h")
    assert r.status_code == 200
    body = r.get_json()
    assert body["range"] == "24h" and body["enabled"] is True and len(body["containers"]) == 2
    assert c.get("/api/metrics?range=1y").status_code == 400
    assert c.get("/api/metrics?range=90d").status_code == 200
    one = c.get("/api/metrics/containers/web?range=1h").get_json()
    assert one["name"] == "web" and one["series"] and one["latest"]["pids"] == 7
    assert c.get("/api/metrics/containers/nope").status_code == 404
    # settings
    assert c.put("/api/metrics/settings", json={"interval_seconds": 5}).status_code == 400
    assert c.put("/api/metrics/settings", json={"interval_seconds": "x"}).status_code == 400
    s = c.put("/api/metrics/settings", json={"interval_seconds": 60, "enabled": False, "junk": 1}).get_json()
    assert s == {"enabled": False, "interval_seconds": 60, "public_host": False, "notify_logs": False}
    assert m.latest == {}
    assert c.get("/api/metrics").get_json()["interval_seconds"] == 60
    # included in the configuration snapshot
    assert app.extensions["dockbare"].configsync.export()["metrics"] == {"enabled": False, "interval_seconds": 60, "public_host": False, "notify_logs": False}


def test_metrics_api_requires_login(metrics_app):
    app, c, d = metrics_app
    anon = app.test_client()
    assert anon.get("/api/metrics").status_code == 401
    assert anon.get("/api/metrics/containers/web").status_code == 401
    assert anon.put("/api/metrics/settings", json={}).status_code == 401
