"""Host metrics, rollups, disk forecast, alert rules, container logs, incident context, peers, reports, status page."""
import sqlite3
import time

import pytest

from dockbare.dockerctl import parse_log_lines
from dockbare.hostmetrics import HostReader, host_rates
from tests.test_metrics import GiB, metrics_app  # noqa: F401  (fixture)


# ------------------------------------------------------------------ /proc
def fake_proc(tmp_path, n=1):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    (proc / "1" / "net").mkdir(parents=True, exist_ok=True)
    (proc / "stat").write_text(f"cpu  {100 * n} 0 {100 * n} {600 * n} {100 * n} 0 0 0 0 0\ncpu0 1 1 1 1\ncpu1 1 1 1 1\nintr 1\n")
    (proc / "meminfo").write_text("MemTotal: 16000000 kB\nMemFree: 1000000 kB\nMemAvailable: 6000000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1500000 kB\n")
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/200 999\n")
    (proc / "diskstats").write_text(
        f"   8       0 sda 1 0 {10 * n} 0 1 0 {20 * n} 0 0 0 0\n"
        f"   8       1 sda1 1 0 {10 * n} 0 1 0 {20 * n} 0 0 0 0\n"
        f"   7       0 loop0 1 0 999 0 1 0 999 0 0 0 0\n"
        f" 259       0 nvme0n1 1 0 {30 * n} 0 1 0 {40 * n} 0 0 0 0\n"
        f" 259       1 nvme0n1p1 1 0 {30 * n} 0 1 0 {40 * n} 0 0 0 0\n")
    (proc / "1" / "net" / "dev").write_text(
        "Inter-|   Receive |  Transmit\n face |bytes packets|bytes\n"
        f"    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
        f"  eth0: {1000 * n} 1 0 0 0 0 0 0 {500 * n} 1 0 0 0 0 0 0\n"
        f"vethab: 777 1 0 0 0 0 0 0 777 1 0 0 0 0 0 0\n")
    for d in ("sda", "nvme0n1", "loop0"):
        (sys / "block" / d).mkdir(parents=True, exist_ok=True)
    (sys / "class" / "hwmon" / "hwmon0").mkdir(parents=True, exist_ok=True)
    (sys / "class" / "hwmon" / "hwmon0" / "temp1_input").write_text("48500\n")
    (sys / "class" / "hwmon" / "hwmon0" / "temp2_input").write_text("61250\n")
    return str(proc), str(sys)


def test_host_reader_and_rates(tmp_path):
    proc, sys = fake_proc(tmp_path, 1)
    r = HostReader(proc=proc, sys=sys, disk_paths=[("Data", str(tmp_path)), ("Backups", str(tmp_path / "proc"))])
    a = r.read()
    assert a["cpu"] == {"cpu_total": 900, "cpu_idle": 700, "cpus": 2}
    assert a["mem"]["mem_total"] == 16000000 * 1024 and a["mem"]["mem_used"] == 10000000 * 1024
    assert a["mem"]["swap_used"] == 500000 * 1024
    assert a["load"] == (0.5, 0.4, 0.3)
    assert a["disk"] == {"disk_r": 40 * 512, "disk_w": 60 * 512}   # partitions and loop devices skipped
    assert a["net"] == {"net_rx": 1000, "net_tx": 500}             # lo and veth skipped
    assert a["temp"] == 61.2 or a["temp"] == 61.3
    fake_proc(tmp_path, 3)
    b = r.read()
    s = host_rates(a, b, 10)
    assert s["cpu"] == pytest.approx((1 - 1400 / 1800) * 100, abs=0.01)
    assert s["disk_r"] == pytest.approx(80 * 512 / 10) and s["net_rx"] == pytest.approx(200)
    assert s["load1"] == 0.5 and s["mem_total"] == 16000000 * 1024
    assert host_rates(b, a, 10) is None   # counters went backwards (reboot)
    disks = r.disks()
    assert len(disks) == 1 and disks[0]["name"] == "Data + Backups" and disks[0]["total"] > 0
    d0 = disks[0]
    assert d0["pct"] == pytest.approx(d0["used"] / (d0["used"] + d0["free"]) * 100, abs=0.1)   # like df


def test_container_proc_has_no_host_network(tmp_path):
    proc, sys = fake_proc(tmp_path)
    r = HostReader(proc=proc, sys=sys)
    r.host_proc = False
    assert r.network() is None


def test_parse_log_lines():
    raw = (b"2026-11-18T10:00:00.123456789Z first line\n"
           b"2026-11-18T10:00:01Z \x1b[31mred\x1b[0m error\n"
           b"  continuation of the error\n"
           b"2026-11-18T12:00:02.5+02:00 local time\n")
    lines = parse_log_lines(raw)
    assert [t for _, t in lines] == ["first line", "red error", "  continuation of the error", "local time"]
    assert lines[0][0] == pytest.approx(1794996000.123456, abs=1e-3)
    assert lines[2][0] == lines[1][0]
    assert lines[3][0] == pytest.approx(1794996002.5, abs=1e-3)


# --------------------------------------------------------- rollups, forecast
def test_rollups_long_ranges_and_prune(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    now = 1_800_000_000.0
    db = m.m.db
    with db.tx() as conn:
        for i in range(0, 3 * 3600, 30):
            ts = now - 3 * 3600 + i
            conn.execute("INSERT INTO container_samples VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, "web", 10.0, 10.0 + (i % 7), 100, 150, 1000, 1, 1, 0, 0, 3))
            conn.execute("INSERT INTO host_samples(res, ts, cpu, cpu_max, load1, mem_used, mem_max, mem_total, disk_r, disk_w, temp, temp_max) "
                         "VALUES(0,?,?,?,?,?,?,?,?,?,?,?)", (ts, 20.0, 20.0 + (i % 5), 1, 50, 60, 100, 0, 0, 40, 40))
    written = m.rollup(now)
    assert written > 0
    five = db.one("SELECT COUNT(*) AS n, MAX(cpu_max) AS peak, MAX(mem_max) AS mem_peak FROM container_samples WHERE res = 300")
    assert 34 <= five["n"] <= 36 and five["peak"] == 16.0 and five["mem_peak"] == 150
    assert db.one("SELECT COUNT(*) AS n FROM container_samples WHERE res = 3600")["n"] >= 2
    assert m.rollup(now) == 0                  # nothing new: idempotent
    week = m.series("web", "7d", now=now)
    assert week and week[-1]["cpu"] == pytest.approx(10.0) and max(p["cpu_max"] for p in week) == 16.0
    month = m.host_series("30d", now=now)
    assert month and month[0]["mem_total"] == 100
    # retention: 5-minute rollups older than 7 days go, hourly ones stay for 90 days
    db.execute("INSERT INTO container_samples VALUES(300,?,?,1,1,1,1,1,0,0,0,0,1)", (now - 8 * 86400, "web"))
    db.execute("INSERT INTO container_samples VALUES(3600,?,?,1,1,1,1,1,0,0,0,0,1)", (now - 8 * 86400, "web"))
    m.prune(now)
    assert db.one("SELECT COUNT(*) AS n FROM container_samples WHERE ts < ?", (now - 7 * 86400,))["n"] == 1


def test_disk_forecast(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    now = 1_800_000_000.0
    with m.m.db.tx() as conn:
        for h in range(24):
            conn.execute("INSERT INTO disk_samples VALUES(0,?,?,?,?)", (now - (23 - h) * 3600, "/data", 100 * GiB + h * GiB, 500 * GiB))
    f = m.disk_forecast("/data", free=48 * GiB, now=now)
    assert f["growth_per_day"] == pytest.approx(24 * GiB, rel=0.02)
    assert f["days_to_full"] == pytest.approx(2.0, abs=0.1)
    assert m.disk_forecast("/other", free=1, now=now) == {"growth_per_day": None, "days_to_full": None}


def test_overview_has_host_and_disks(metrics_app):
    app, c, d = metrics_app
    m = app.extensions["dockbare"].metrics
    m.collect()
    m.collect()
    o = c.get("/api/metrics?range=1h").get_json()
    assert o["host_now"]["cpu"] == pytest.approx(25.0) and o["host_now"]["temp"] == 55.0
    assert o["host"]["cpus"] == 4 and o["host"]["mem_total"] == 16 * GiB and o["host"]["network"] is False
    assert o["host_series"] and o["disks"][0]["name"] == "Dockbare data + Backups"
    assert o["ranges"] == ["1h", "24h", "7d", "30d", "90d"]
    assert len(o["alerts"]) == 3              # the seeded default rules


# ----------------------------------------------------------------- alerts
def quiet(core, monkeypatch):
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((event, title, msg, level)))
    return sent


def test_alert_lifecycle_host_rule(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    r = c.post("/api/alerts", json={"name": "CPU busy", "metric": "host.cpu", "op": ">", "threshold": 20, "for_minutes": 5}).get_json()
    m, al = core.metrics, core.alerts
    t0 = time.time()
    m.collect(now=t0)
    m.collect(now=t0 + 30)                       # 25 % > 20 % → pending
    st = {x["id"]: x for x in al.states()}[r["id"]]
    assert st["status"] == "pending" and not sent
    al.evaluate(t0 + 30 + 301)                   # held for 5 min → firing + notification
    st = {x["id"]: x for x in al.states()}[r["id"]]
    assert st["status"] == "firing" and st["targets"][0]["value"] == pytest.approx(25.0)
    assert sent[-1][0] == "metrics" and sent[-1][1] == "ALERT: CPU busy" and "25%" in sent[-1][2]
    al.evaluate(t0 + 30 + 400)                   # no repeat configured
    assert len(sent) == 1
    m.reader.busy = 0.05
    m.collect(now=t0 + 500)                      # 5 % → resolved
    assert {x["id"]: x for x in al.states()}[r["id"]]["status"] == "ok"
    assert sent[-1][1] == "RESOLVED: CPU busy" and sent[-1][3] == "success"
    kinds = [e["kind"] for e in core.db.query("SELECT kind FROM events WHERE kind LIKE 'metrics.%'")]
    assert "metrics.alert" in kinds and "metrics.resolved" in kinds


def test_alert_repeat_mute_and_maintenance(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    al, m = core.alerts, core.metrics
    rule = al.save({"name": "Hot container", "metric": "container.cpu", "target": "*", "threshold": 150, "for_minutes": 0, "repeat_minutes": 10})
    m.collect()
    monkeypatch.setattr(core.uptime, "quiet_window", lambda now=None: {"mode": "mute"})
    now = time.time()
    m.collect(now=now)
    st = {x["id"]: x for x in al.states()}[rule["id"]]
    assert st["status"] == "firing" and [t["target"] for t in st["targets"]] == ["web"]   # 200 % of one core; db is at 40 %
    assert not sent                              # quiet hours: fires, but no message yet
    monkeypatch.setattr(core.uptime, "quiet_window", lambda now=None: None)
    al.evaluate(now + 30)
    assert [s[1] for s in sent] == ["ALERT: Hot container (web)"]
    al.evaluate(now + 30 + 601)
    assert sent[-1][1] == "STILL ALERT: Hot container (web)"
    # a maintenance window covering the container resolves it
    monkeypatch.setattr(core.uptime, "maintenance_active", lambda now=None: [{"services": ["web"]}])
    al.evaluate(now + 700)
    assert {x["id"]: x for x in al.states()}[rule["id"]]["status"] == "ok"


def test_log_rule_counts_matches(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    al, m = core.alerts, core.metrics
    rule = al.save({"name": "Panics", "metric": "container.log_matches", "target": "web", "pattern": "panic|fatal", "threshold": 1,
                    "for_minutes": 0, "window_minutes": 5})
    t = time.time()
    d.logs["c-web"] = [(t - 100, "old FATAL before the rule existed")]
    m.collect(now=t)
    m.collect(now=t + 1)                         # first look only sets the read position
    assert {x["id"]: x for x in al.states()}[rule["id"]]["status"] == "ok"
    d.logs["c-web"] += [(t + 2, "GET / 200"), (t + 3, "PANIC: nil map"), (t + 4, "fatal error: out of memory")]
    m.collect(now=t + 5)
    st = {x["id"]: x for x in al.states()}[rule["id"]]
    assert st["status"] == "firing" and st["targets"][0]["value"] == 2.0
    assert "Panics" in sent[-1][1]
    m.collect(now=t + 5 + 400)                   # window passed, no new matches
    assert {x["id"]: x for x in al.states()}[rule["id"]]["status"] == "ok"


def test_alert_api_validation_and_crud(metrics_app):
    app, c, d = metrics_app
    assert len(c.get("/api/alerts").get_json()["rules"]) == 3
    assert c.post("/api/alerts", json={"metric": "nope"}).status_code == 400
    assert c.post("/api/alerts", json={"metric": "container.log_matches", "target": "web"}).status_code == 400
    assert c.post("/api/alerts", json={"metric": "container.log_matches", "target": "web", "pattern": "("}).status_code == 400
    assert c.post("/api/alerts", json={"metric": "container.cpu", "target": "bad name!"}).status_code == 400
    assert c.post("/api/alerts", json={"metric": "host.cpu", "for_minutes": 5000}).status_code == 400
    r = c.post("/api/alerts", json={"metric": "host.load1", "target": "ignored", "threshold": "4"}).get_json()
    assert r["target"] == "" and r["threshold"] == 4.0 and r["name"] == "Host load (1 min)"
    r2 = c.put(f"/api/alerts/{r['id']}", json={"threshold": 8, "enabled": False}).get_json()
    assert r2["threshold"] == 8.0 and r2["enabled"] is False and r2["metric"] == "host.load1"
    assert c.put("/api/alerts/missing", json={}).status_code == 400
    assert c.delete(f"/api/alerts/{r['id']}").status_code == 200
    assert len(c.get("/api/alerts").get_json()["rules"]) == 3
    exported = app.extensions["dockbare"].configsync.export()["alert_rules"]
    assert len(exported) == 3 and all("metric" in x for x in exported)
    anon = app.test_client()
    assert anon.get("/api/alerts").status_code == 401


def test_disk_rules_from_seed(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    m = core.metrics
    m.reader.used = 460 * GiB                    # 92 % after the next reading
    now = time.time()
    m.collect(now=now)
    m.collect(now=now + 30)
    core.alerts.evaluate(now + 30 + 16 * 60)
    firing = [r["name"] for r in core.alerts.firing()]
    assert "Disk almost full" in firing and any("Disk almost full" in s[1] for s in sent)


# ------------------------------------------------------------------- logs
def test_logs_api(metrics_app):
    app, c, d = metrics_app
    t = 1_800_000_000.0
    d.logs["web"] = [(t, "one"), (t + 1, "two"), (t + 2, "three")]
    r = c.get("/api/logs/web?tail=2").get_json()
    assert [x[1] for x in r["lines"]] == ["two", "three"] and r["last_ts"] == t + 2
    r = c.get(f"/api/logs/web?since={t + 1}").get_json()
    assert [x[1] for x in r["lines"]] == ["three"]
    dl = c.get("/api/logs/web?download=1")
    assert dl.status_code == 200 and "attachment" in dl.headers["Content-Disposition"] and b"three" in dl.data
    assert c.get("/api/logs/bad%20name").status_code == 400
    assert c.get("/api/logs/web?tail=x").status_code == 400
    assert app.test_client().get("/api/logs/web").status_code == 401


# ---------------------------------------------------- incident context etc.
def test_incident_gets_logs_and_last_sample(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    core.metrics.collect()
    core.metrics.collect()
    core.uptime.poll()                           # sees "web" running
    d.containers["web"]["state"] = "exited"
    d.containers["web"]["exit_code"] = 137
    d.logs["c-web"] = [(1.0, "starting"), (2.0, "Killed: out of memory")]
    c.put("/api/uptime/monitors/web", json={"enabled": True, "grace_seconds": 0})
    core.uptime.poll()
    inc = core.uptime.incidents(container="web")[0]
    assert inc["facts"]["logs"] == ["starting", "Killed: out of memory"]
    assert inc["facts"]["resources"]["cpu"] == pytest.approx(200.0)
    assert inc["facts"]["resources"]["mem_limit"] is None    # 64 GiB "limit" = host RAM in the fake, i.e. no limit
    down = [s for s in sent if s[1].startswith("DOWN")]
    assert down and "Last log lines:" not in down[0][2]      # logs stay local unless enabled


def test_delayed_down_notice_includes_context(metrics_app, monkeypatch):
    """A notification sent on a later poll uses the incident read back from the database."""
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    calls = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: calls.append((title, msg, kw.get("facts"))))
    c.put("/api/metrics/settings", json={"notify_logs": True})
    core.metrics.collect()
    core.metrics.collect()
    core.uptime.poll()
    c.put("/api/uptime/monitors/web", json={"enabled": True, "grace_seconds": 0, "notify_after_minutes": 2})
    d.containers["web"]["state"] = "exited"
    d.containers["web"]["exit_code"] = 1
    d.logs["c-web"] = [(1.0, "boom: config missing")]
    core.uptime.poll()
    assert not [x for x in calls if x[0].startswith("DOWN")]        # delayed
    inc = core.uptime.incidents(open_only=True, container="web")[0]
    cfg = core.uptime.monitor_for("web")
    core.uptime._maybe_notify_down(inc, {"name": "web", "image": "img"}, cfg, inc["started_at"] + 180)
    title, msg, facts = [x for x in calls if x[0].startswith("DOWN")][0]
    assert "boom: config missing" in msg and any(k == "Last sample" for k, _ in facts)


def test_uptime_payload_trims_old_log_context(metrics_app):
    import json as _json

    app, c, d = metrics_app
    db = app.extensions["dockbare"].db
    detail = _json.dumps({"kind": "down", "exit_code": 1, "logs": ["x" * 200] * 30})
    for i in range(45):
        db.execute("INSERT INTO incidents(container, kind, started_at, ended_at, detail, actions) VALUES(?,?,?,?,?,?)",
                   ("web", "down", 1000.0 + i, 1001.0 + i, detail, "[]"))
    inc = c.get("/api/uptime").get_json()["incidents"]
    assert len(inc[0]["facts"]["logs"]) == 30 and "logs" not in inc[44]["facts"] and inc[44]["facts"]["exit_code"] == 1


def test_disabling_metrics_clears_alerts(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    quiet(core, monkeypatch)
    core.alerts.save({"name": "Any CPU", "metric": "host.cpu", "threshold": 1, "for_minutes": 0})
    core.metrics.collect()
    core.metrics.collect()
    assert core.alerts.firing()
    c.put("/api/metrics/settings", json={"enabled": False})
    assert not core.alerts.firing()


def test_stats_fallback_for_old_daemons():
    from docker.errors import InvalidVersion, NotFound

    from dockbare.dockerctl import DockerCtl
    from tests.test_metrics import stats_payload

    calls = []

    class Api:
        def stats(self, cid, stream=False, one_shot=None):
            calls.append(one_shot)
            if cid == "gone":
                raise NotFound("no such container")
            if one_shot:
                raise InvalidVersion("one_shot is not supported for API version < 1.41")
            return stats_payload(10, 1000)

    class Client:
        api = Api()

    ctl = DockerCtl()
    ctl._client = Client()
    assert ctl.container_stats("c1")["cpu_total"] == 10 and calls == [True, None]
    calls.clear()
    assert ctl.container_stats("gone") is None and calls == [True]   # no second call for a missing container


def test_peer_status_report_and_public_host(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    quiet(core, monkeypatch)
    core.alerts.save({"name": "Any CPU", "metric": "host.cpu", "threshold": 1, "for_minutes": 0})
    core.metrics.collect()
    core.metrics.collect()
    st = core.peers.self_status()
    assert st["metrics"]["cpu"] == pytest.approx(25.0) and st["metrics"]["public"] is False
    assert st["metrics"]["mem_pct"] == 50.0 and st["metrics"]["alerts_firing"] == ["Any CPU"]
    assert "Alerts firing: Any CPU" in st["problems"]
    rep = core.reports.build("daily")
    res = rep["stats"]["resources"]
    assert res["host"]["cpu"] == pytest.approx(25.0) and res["alerts_fired"] == 1
    assert res["top_cpu"][0]["name"] == "web"
    assert rep["stats"]["has_problems"] and "Resources:" in rep["text"] and "Top CPU" in rep["html"]
    # host health on the public page is opt-in
    assert core.uptime.public_payload()["groups"][0]["host"] is None
    c.put("/api/metrics/settings", json={"public_host": True})
    host = core.uptime.public_payload()["groups"][0]["host"]
    assert host["cpu"] == pytest.approx(25.0) and host["mem_pct"] == 50.0


def test_old_metrics_table_is_replaced(tmp_path):
    from dockbare.db import Database

    p = tmp_path / "db.sqlite"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE container_samples (ts REAL, name TEXT, cpu REAL, mem INTEGER, mem_limit INTEGER, net_rx REAL, net_tx REAL, blk_r REAL, blk_w REAL, pids INTEGER)")
    conn.execute("CREATE INDEX idx_csamples_name_ts ON container_samples(name, ts)")
    conn.commit()
    conn.close()
    db = Database(p)
    cols = {r["name"] for r in db.query("PRAGMA table_info(container_samples)")}
    assert {"res", "cpu_max", "mem_max"} <= cols
    db.close()


def test_restart_does_not_resolve_firing_alerts(metrics_app, monkeypatch):
    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    sent = quiet(core, monkeypatch)
    m, al = core.metrics, core.alerts
    rule = al.save({"name": "Hot", "metric": "container.cpu", "target": "web", "threshold": 150, "for_minutes": 0})
    host_rule = al.save({"name": "Busy host", "metric": "host.cpu", "threshold": 10, "for_minutes": 0})
    now = time.time()
    m.collect(now=now)
    m.collect(now=now + 30)
    assert {r["name"] for r in al.firing()} == {"Hot", "Busy host"}
    n = len(sent)
    # "restart": in-memory readings are gone, the first round has no rates yet
    m.latest, m._prev, m._host_prev, m.host_now = {}, {}, None, None
    m.collect(now=now + 60)
    assert {r["name"] for r in al.firing()} == {"Hot", "Busy host"}
    assert len(sent) == n and not any("RESOLVED" in s[1] for s in sent)
    # a container that really stops resolves
    d.containers["web"]["state"] = "exited"
    m.collect(now=now + 90)
    assert "Hot" not in {r["name"] for r in al.firing()}
    assert sent[-1][1] == "RESOLVED: Hot (web)" and "no longer reported" in sent[-1][2]
    assert rule["id"] and host_rule["id"]


def test_restart_shows_last_values_at_once(metrics_app):
    """After a restart the current values come from the database until two new readings exist."""
    from dockbare.metrics import Metrics
    from tests.test_metrics import FakeReader

    app, c, d = metrics_app
    core = app.extensions["dockbare"]
    now = time.time()
    core.metrics.collect(now=now - 60)
    core.metrics.collect(now=now - 30)
    m2 = Metrics(core.manager, reader=FakeReader())      # a fresh process: nothing in memory
    assert m2.host_now is None and not m2.latest
    m2.start(background=False)
    assert m2.host_now["cpu"] == pytest.approx(25.0) and m2.last_collect == pytest.approx(now - 30)
    assert m2.latest["web"]["cpu"] == pytest.approx(200.0) and m2.latest["web"]["id"] is None
    core.manager.metrics = m2
    m2.collect(now=now)                                   # first live round has no rates yet
    assert m2.latest["web"]["cpu"] == pytest.approx(200.0)
    o = m2.overview("1h", now=now)
    assert o["host_now"] and {x["name"] for x in o["containers"] if x["latest"]} == {"web", "db"}
    # stopped while Dockbare was down: dropped at the first live round
    m3 = Metrics(core.manager, reader=FakeReader())
    m3.start(background=False)
    d.containers["db"]["state"] = "exited"
    m3.collect(now=now + 30)
    assert "db" not in m3.latest and "web" in m3.latest
    # old readings are not restored
    m4 = Metrics(core.manager, reader=FakeReader())
    m4.restore(now=now + 3600)
    assert m4.host_now is None and not m4.latest
