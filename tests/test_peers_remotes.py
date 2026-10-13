import gzip
import time

import pytest

from dockbare.remotes.base import OWN, Remote, RemoteError, RemoteInfo
from tests.conftest import make_tar, wait_task


class MemRemote(Remote):
    """In-memory remote; ``store`` is the shared 'server' across instances."""

    type = "mem"
    store: dict[str, bytes] = {}

    def _open(self):
        return object()

    def _test(self):
        return "ok"

    def _ensure_dir(self, path):
        pass

    def _upload(self, local_path, remote_path, progress):
        with open(local_path, "rb") as f:
            self.store[remote_path] = f.read()

    def _download(self, remote_path, local_path, progress):
        if remote_path not in self.store:
            raise RemoteError("missing")
        with open(local_path, "wb") as f:
            f.write(self.store[remote_path])

    def _delete(self, remote_path):
        return self.store.pop(remote_path, None) is not None

    def _size(self, remote_path):
        b = self.store.get(remote_path)
        return None if b is None else len(b)

    def _listdir(self, path):
        prefix = path.rstrip("/") + "/"
        names = {}
        for p, data in self.store.items():
            if p.startswith(prefix):
                head, _, tail = p[len(prefix):].partition("/")
                names.setdefault(head, (bool(tail), None if tail else len(data)))
        return [(n, d, s) for n, (d, s) in names.items()]

    def _read_small(self, remote_path, limit):
        b = self.store.get(remote_path)
        return b if b is not None and len(b) <= limit else None


@pytest.fixture(autouse=True)
def _clear_store():
    MemRemote.store.clear()
    yield
    MemRemote.store.clear()


def test_remote_paths_and_layouts():
    info = RemoteInfo("x", "x", "mem", True, {"base_path": "/b", "instance_dir": True}, instance="srv-a")
    r = MemRemote(info)
    assert r.remote_path("v", "f") == "/b/srv-a/v/f"
    assert r.remote_path("v", "f", instance="srv-b") == "/b/srv-b/v/f"
    assert r.remote_path("v", "f", instance=None) == "/b/v/f"
    flat = MemRemote(RemoteInfo("x", "x", "mem", True, {"base_path": "/b", "instance_dir": False}, instance="srv-a"))
    assert flat.remote_path("v", "f") == "/b/v/f" and flat.instance is None
    MemRemote.store.update({
        "/b/vol1/2024-01-01_00-00-00.tar.gz": b"1",
        "/b/srv-a/vol2/2024-01-02_00-00-00.tar.gz.enc": b"22",
        "/b/srv-a/vol2/2024-01-02_00-00-00.tar.gz.enc.meta.json": b"{}",
        "/b/srv-b/vol1/2024-01-03_00-00-00.tar.gz": b"333",
        "/b/srv-b/notes.txt": b"x",
    })
    tree = {(e["instance"], e["volume"]): sorted(f.name for f in e["files"]) for e in r.list_tree()}
    assert set(tree) == {(None, "vol1"), ("srv-a", "vol2"), ("srv-b", "vol1")}
    assert tree[("srv-a", "vol2")] == ["2024-01-02_00-00-00.tar.gz.enc", "2024-01-02_00-00-00.tar.gz.enc.meta.json"]


def test_env_remotes_locked(settings, fake_docker, monkeypatch):
    import dockbare.app as appmod
    from dockbare.config import load_settings

    monkeypatch.setenv("DOCKBARE_INSTANCE_NAME", "srv-a")
    monkeypatch.setenv("DOCKBARE_REMOTE_1_HOST", "ftp.example")
    monkeypatch.setenv("DOCKBARE_REMOTE_1_USER", "u")
    monkeypatch.setenv("DOCKBARE_REMOTE_1_PASSWORD", "pw-from-env")
    monkeypatch.setenv("DOCKBARE_REMOTE_1_PATH", "/bk")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_TYPE", "sftp")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_NAME", "Box")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_HOST", "sftp.example")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_PASSWORD", "x")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_PATH", "backups")
    monkeypatch.setenv("DOCKBARE_REMOTE_2_INSTANCE_DIR", "false")
    s2 = load_settings()
    assert len(s2.env_remotes) == 2
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    app = appmod.create_app(s2, start=True)
    try:
        core = app.extensions["dockbare"]
        c = app.test_client()
        c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
        rem = {r["id"]: r for r in c.get("/api/remotes").get_json()}
        assert set(rem) == {"env-1", "env-2"}
        r1 = rem["env-1"]
        assert r1["source"] == "env" and r1["type"] == "ftp" and r1["config"]["secure"] is True and r1["config"]["port"] == 21
        assert r1["instance"] == "srv-a" and r1["config"]["password_set"]
        assert core.manager.get_remote("env-1", with_secrets=True)["config"]["password"] == "pw-from-env"
        assert "pw-from-env" not in core.db.one("SELECT config FROM remotes WHERE id='env-1'")["config"]
        assert rem["env-2"]["name"] == "Box" and rem["env-2"]["config"]["port"] == 22 and rem["env-2"]["instance"] is None
        assert c.put("/api/remotes/env-1", json={"name": "n", "type": "ftp", "config": {"host": "h", "base_path": "/b"}}).status_code == 400
        assert c.delete("/api/remotes/env-1").status_code == 400
        assert c.get("/api/settings").get_json()["instance"] == {"name": "srv-a", "from_env": True}
        assert c.put("/api/settings/general", json={"instance_name": "other"}).status_code == 400
    finally:
        core.stop()
    # remote 2 removed from env → dropped on next start
    monkeypatch.delenv("DOCKBARE_REMOTE_2_HOST")
    app = appmod.create_app(load_settings(), start=True)
    try:
        assert [r["id"] for r in app.extensions["dockbare"].manager.list_remotes()] == ["env-1"]
    finally:
        app.extensions["dockbare"].stop()


def test_instance_name_generated_and_editable(auth_client, app):
    s = auth_client.get("/api/settings").get_json()["instance"]
    assert s["name"].startswith("dockbare-") and s["from_env"] is False
    assert auth_client.put("/api/settings/general", json={"instance_name": "bad name!"}).status_code == 400
    assert auth_client.put("/api/settings/general", json={"instance_name": "srv-a"}).status_code == 200
    assert auth_client.get("/api/settings").get_json()["instance"]["name"] == "srv-a"


def test_recovery_across_instances(auth_client, app, fake_docker, monkeypatch):
    import dockbare.manager as mgr

    core = app.extensions["dockbare"]
    monkeypatch.setattr(mgr, "build_remote", lambda info: MemRemote(info))
    core.settings.instance_name = "srv-a"
    r = auth_client.post("/api/remotes", json={"name": "shared", "type": "ftp", "config": {"host": "h", "port": 21, "username": "u", "password": "p", "base_path": "/b"}})
    assert r.status_code == 200, r.get_json()
    rid = r.get_json()["id"]
    # backup + upload from this instance
    t = wait_task(auth_client, auth_client.post("/api/backups", json={"volumes": ["vol1"], "remotes": [rid]}).get_json()["tasks"][0]["id"])
    assert t["status"] == "success" and t["result"]["uploads"] == {rid: "synced"}
    own_file = t["result"]["filename"]
    assert f"/b/srv-a/vol1/{own_file}" in MemRemote.store and f"/b/srv-a/vol1/{own_file}.meta.json" in MemRemote.store
    # another instance's backup of a volume with the same name
    other_tar = make_tar({"./other.txt": b"from srv-b"})
    MemRemote.store["/b/srv-b/vol1/2024-05-05_00-00-00.tar.gz"] = gzip.compress(other_tar)
    # disaster: local copy gone
    auth_client.delete("/api/backups", json={"path": t["result"]["path"]})
    t = wait_task(auth_client, auth_client.post("/api/recovery/scan", json={"remote": rid}).get_json()["task"]["id"])
    assert t["status"] == "success", t
    scan = auth_client.get("/api/recovery/last").get_json()
    assert scan["instances"] == ["srv-a", "srv-b"] and scan["own_instance"] == "srv-a" and scan["flat_layout"] is False
    items = {(i["instance"], i["volume"]): i for i in scan["items"]}
    assert items[("srv-a", "vol1")]["own_instance"] and items[("srv-a", "vol1")]["key_status"] == "match"
    assert items[("srv-a", "vol1")]["latest"]["local"] is False and items[("srv-a", "vol1")]["meta"]["hostname"]
    assert not items[("srv-b", "vol1")]["own_instance"] and items[("srv-b", "vol1")]["latest"]["encrypted"] is False
    # restore srv-b's backup into vol2 (created), and our own into vol1
    t = wait_task(auth_client, auth_client.post("/api/recovery/restore", json={"remote": rid, "items": [
        {"volume": "vol1", "instance": "srv-b", "filename": "2024-05-05_00-00-00.tar.gz", "target_volume": "vol2"},
        {"volume": "vol1", "instance": "srv-a", "filename": own_file},
    ]}).get_json()["task"]["id"])
    assert t["status"] == "success", "\n".join(t["log"])
    assert fake_docker.restored["vol2"] == other_tar and fake_docker.restored["vol1"] == fake_docker.tar
    # the own file is again marked synced on the remote; the foreign one is a local copy only
    ups = {b["filename"]: b["uploads"] for b in auth_client.get("/api/backups").get_json()}
    assert ups[own_file][rid]["status"] == "synced" and ups["2024-05-05_00-00-00.tar.gz"] == {}


def test_peer_status_endpoint(client, app):
    core = app.extensions["dockbare"]
    assert client.get("/api/peer/status").status_code == 401
    assert client.get("/api/peer/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/api/peer/status", headers={"Authorization": f"Bearer {core.peers.token()}"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["app"] == "dockbare" and d["docker_ok"] is True and isinstance(d["problems"], list) and d["encryption"] is True


def test_peer_check_alerts(auth_client, app, monkeypatch):
    from dockbare import peers as peers_mod

    core = app.extensions["dockbare"]
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((event, level, title)))
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: (_ for _ in ()).throw(peers_mod.PeerError("connection refused")))
    assert auth_client.post("/api/peers", json={"name": "B", "url": "ftp://x"}).status_code == 400
    r = auth_client.post("/api/peers", json={"name": "Server B", "url": "https://backup.other.example/", "token": "tok", "failure_threshold": 2, "interval_minutes": 1})
    assert r.status_code == 200, r.get_json()
    p = r.get_json()
    assert p["url"] == "https://backup.other.example" and p["token_set"] and "token" not in p
    for _ in range(50):  # the background check after save
        p = auth_client.get("/api/peers").get_json()["peers"][0]
        if p.get("last_checked_at"):
            break
        time.sleep(0.05)
    assert p["consecutive_failures"] == 1 and p["alert_active"] is False
    p = auth_client.post(f"/api/peers/{p['id']}/check").get_json()
    assert p["consecutive_failures"] == 2 and p["alert_active"] is True and p["status"]["ok"] is False
    assert sent and sent[-1][0] == "peer" and sent[-1][1] == "error"
    assert any(e["kind"] == "peer.down" for e in auth_client.get("/api/events").get_json())
    # peer comes back, reporting a problem
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": ["Docker engine unreachable"]})
    p = auth_client.post(f"/api/peers/{p['id']}/check").get_json()
    assert p["alert_active"] is False and p["consecutive_failures"] == 0 and p["status"]["problems"] == ["Docker engine unreachable"]
    kinds = [e["kind"] for e in auth_client.get("/api/events").get_json()]
    assert "peer.up" in kinds and "peer.problems" in kinds
    assert [x[1] for x in sent[-2:]] == ["success", "warning"]
    # same problems again → no duplicate alert; cleared → info event
    before = len(sent)
    auth_client.post(f"/api/peers/{p['id']}/check")
    assert len(sent) == before
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": []})
    auth_client.post(f"/api/peers/{p['id']}/check")
    assert "peer.problems_cleared" in [e["kind"] for e in auth_client.get("/api/events").get_json()]
    # overview + summary
    o = auth_client.get("/api/overview").get_json()
    assert o["peers"][0]["name"] == "Server B" and o["peers"][0]["ok"] is True
    assert auth_client.delete(f"/api/peers/{p['id']}").status_code == 200


def test_peer_token_regenerate(auth_client, app):
    core = app.extensions["dockbare"]
    t1 = auth_client.get("/api/peers").get_json()["token"]
    t2 = auth_client.post("/api/peers/token/regenerate").get_json()["token"]
    assert t1 != t2 and core.peers.verify_token(t2) and not core.peers.verify_token(t1)
    core.settings.peer_token = "from-env"
    assert core.peers.verify_token("from-env")
    assert auth_client.post("/api/peers/token/regenerate").status_code == 400


# ---------------------------------------------------------------- uptime
class UptimeDocker:
    """FakeDocker extension with containers that can fail."""

    def __init__(self, base):
        self.base = base
        self.containers = {
            "web": {"id": "c-web", "state": "running", "status": "Up 2 hours (healthy)", "health": "healthy", "exit_code": None},
            "db": {"id": "c-db", "state": "running", "status": "Up 2 hours", "health": None, "exit_code": None},
        }
        self.restarted = []
        self.inspect = {}

    def __getattr__(self, item):
        return getattr(self.base, item)

    def list_containers(self):
        return [{"short_id": c["id"], "name": n, "image": "img", "created": 0, "project": None, "service": None, **c} for n, c in self.containers.items()]

    def inspect_container(self, cid):
        return self.inspect.get(cid, {"exit_code": None, "oom_killed": False, "restart_count": 0, "error": "", "health_log": []})

    def restart_container(self, cid, timeout=10):
        self.restarted.append(cid)
        return True

    def own_container_id(self):
        return None


@pytest.fixture
def uptime_app(settings, monkeypatch):
    import dockbare.app as appmod
    from tests.conftest import FakeDocker

    d = UptimeDocker(FakeDocker())
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: d)
    settings.testing = True
    app = appmod.create_app(settings, start=True)
    c = app.test_client()
    c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    yield app, c, d
    app.extensions["dockbare"].stop()


def test_uptime_incident_lifecycle(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((event, level, title)))
    up = core.uptime
    rows = {r["name"]: r for r in c.get("/api/uptime").get_json()["containers"]}
    assert rows["web"]["monitored"] and rows["web"]["failure"] is None and rows["web"]["uptime"]["d30"] == 100.0
    # db crashes with exit code 1; grace 30s → no incident yet
    d.containers["db"].update({"state": "exited", "status": "Exited (1) 3 seconds ago", "exit_code": 1})
    up.poll()
    assert up.incidents(open_only=True) == [] and not sent
    # after the grace period: incident + notification
    up._failing_since["db"] -= 60
    up.poll()
    inc = up.incidents(open_only=True)
    assert len(inc) == 1 and inc[0]["container"] == "db" and inc[0]["kind"] == "down" and "exit code 1" in inc[0]["detail"]
    assert sent[-1][0] == "uptime" and sent[-1][1] == "error" and not d.restarted
    # clean stop (exit 0) is not an outage by default
    d.containers["web"].update({"state": "exited", "status": "Exited (0) 1 second ago", "exit_code": 0, "health": None})
    up._failing_since["web"] = time.time() - 100
    up.poll()
    assert {i["container"] for i in up.incidents(open_only=True)} == {"db"}
    # unless alert_on_stop is set for that container
    assert c.put("/api/uptime/monitors/web", json={"alert_on_stop": True, "public": True, "display_name": "Website"}).status_code == 200
    up._failing_since["web"] = time.time() - 100
    up.poll()
    assert {i["container"] for i in up.incidents(open_only=True)} == {"db", "web"}
    # recovery closes incidents and notifies
    d.containers["db"].update({"state": "running", "status": "Up 1 second", "exit_code": None})
    d.containers["web"].update({"state": "running", "status": "Up 1 second (healthy)", "health": "healthy"})
    up.poll()
    assert up.incidents(open_only=True) == []
    closed = up.incidents()
    assert all(i["ended_at"] for i in closed) and sent[-1][1] == "success"
    pct = up.uptime_pct("db")
    assert 99.0 < pct["d1"] < 100.0 and pct["d30"] > pct["d1"]
    summary = c.get("/api/uptime").get_json()["summary"]
    assert summary["monitored"] == 2 and summary["down"] == [] and summary["incidents_24h"] == 2


def test_uptime_auto_restart_budget(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    up = core.uptime
    c.put("/api/uptime/monitors/db", json={"auto_restart": True, "max_restarts": 2, "grace_seconds": 0})
    d.containers["db"].update({"state": "exited", "status": "Exited (137) 1 second ago", "exit_code": 137})
    d.inspect["c-db"] = {"exit_code": 137, "oom_killed": True, "restart_count": 3, "error": "", "health_log": []}
    up.poll()
    inc = up.incidents(open_only=True)[0]
    assert inc["kind"] == "oom" and d.restarted == ["c-db"] and inc["actions"][0]["type"] == "restart"
    # still down: second restart only after the 20 s cool-down, then budget exhausted
    up._restarts["db"] = [time.time() - 30]
    up.poll()
    assert d.restarted == ["c-db", "c-db"]
    up._restarts["db"] = [time.time() - 60, time.time() - 30]
    up.poll()
    inc = up.incidents(open_only=True)[0]
    assert d.restarted == ["c-db", "c-db"] and any(a["type"] == "budget" for a in inc["actions"])
    assert any(e["kind"] == "uptime.restart_budget" for e in c.get("/api/events").get_json())
    # manual restart endpoint
    assert c.post("/api/uptime/containers/db/restart").get_json()["ok"] is True and d.restarted[-1] == "c-db"
    assert c.post("/api/uptime/containers/nope/restart").status_code == 400


def test_uptime_missing_and_unmonitored(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    up = core.uptime
    c.put("/api/uptime/monitors/db", json={"enabled": True, "grace_seconds": 0})
    c.put("/api/uptime/monitors/web", json={"enabled": False})
    del d.containers["db"]
    d.containers["web"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1})
    up.poll()
    inc = up.incidents(open_only=True)
    assert [i["container"] for i in inc] == ["db"] and inc[0]["kind"] == "missing"  # web is not monitored
    rows = {r["name"]: r for r in up.rows()}
    assert rows["db"]["missing"] and rows["web"]["monitored"] is False
    # turning off "monitor new containers" leaves only explicit monitors
    c.put("/api/uptime/settings", json={"monitor_new": False})
    c.delete("/api/uptime/monitors/db")
    up.poll()
    assert up.incidents(open_only=True) == []  # closed because monitoring was disabled


def test_uptime_public_page(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    anon = app.test_client()
    assert anon.get("/api/uptime/public").status_code == 404 and anon.get("/uptime").status_code == 404
    c.put("/api/uptime/settings", json={"public_enabled": True, "public_title": "My services"})
    c.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website"})
    r = anon.get("/api/uptime/public")
    assert r.status_code == 200
    pl = r.get_json()
    assert pl["title"] == "My services" and pl["overall"] == "operational"
    it = pl["groups"][0]["items"]
    assert len(it) == 1 and it[0]["name"] == "Website" and it[0]["status"] == "up" and it[0]["since"] is None and len(it[0]["history"]) == 90
    assert "db" not in str(pl)  # non-public containers never appear
    assert anon.get("/uptime").status_code == 200 and b"uptime.js" in anon.get("/uptime").data
    d.containers["web"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1, "health": None})
    core.uptime._failing_since["web"] = time.time() - 100
    core.uptime.poll()
    pl = anon.get("/api/uptime/public").get_json()
    assert pl["overall"] == "outage" and pl["groups"][0]["items"][0]["status"] == "down" and pl["incidents"][0]["name"] == "Website"
    # peers see the outage in the status endpoint
    st = anon.get("/api/peer/status", headers={"Authorization": f"Bearer {core.peers.token()}"}).get_json()
    assert st["uptime"]["down"] == ["web"] and "Containers down: web" in st["problems"]


# ---------------------------------------------------------- config snapshot
def test_config_snapshot_roundtrip(auth_client, app, fake_docker, monkeypatch, settings):
    import dockbare.app as appmod
    import dockbare.manager as mgr

    core = app.extensions["dockbare"]
    monkeypatch.setattr(mgr, "build_remote", lambda info: MemRemote(info))
    core.settings.instance_name = "srv-a"
    rid = auth_client.post("/api/remotes", json={"name": "shared", "type": "ftp", "config": {"host": "h", "port": 21, "username": "u", "password": "pw", "base_path": "/b"}}).get_json()["id"]
    auth_client.post("/api/schedules", json={"volume": "vol1", "mode": "daily", "time": "01:00", "remotes": [rid]})
    auth_client.put("/api/settings/notifications", json={"ntfy": {"enabled": True, "topic": "t1", "token": "secret-token"}})
    auth_client.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website"})
    auth_client.post("/api/peers", json={"name": "B", "url": "https://b.example", "token": "ptok"})
    # export (encrypted) and push to the remote
    r = auth_client.get("/api/config/export")
    assert r.status_code == 200 and r.data[:4] == b"DKBE" and r.headers["Content-Disposition"].endswith('.json.enc"')
    exported = r.data
    st = auth_client.get("/api/config/status").get_json()
    assert st["encrypted"] and st["snapshot"]["counts"] == {"remotes": 1, "schedules": 1, "peers": 1, "monitors": 1}
    push = auth_client.post("/api/config/push").get_json()
    assert push["results"] == {rid: "ok"} and "/b/srv-a/_dockbare/config.json.enc" in MemRemote.store
    # recovery scan on a fresh installation finds the snapshot
    core.stop()
    for f in (settings.data_dir / "dockbare.db",):
        f.unlink()
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    settings.instance_name = "srv-a"
    app2 = appmod.create_app(settings, start=True)
    try:
        c2 = app2.test_client()
        c2.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
        c2.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
        assert c2.get("/api/schedules").get_json() == [] and c2.get("/api/remotes").get_json() == []
        # import the exported file directly
        r = c2.post("/api/config/import", data={"file": (__import__("io").BytesIO(exported), "config.json.enc")}, content_type="multipart/form-data")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["summary"]["schedules"] == 1 and r.get_json()["snapshot"]["instance"] == "srv-a"
        core2 = app2.extensions["dockbare"]
        rem = c2.get("/api/remotes").get_json()
        assert rem[0]["id"] == rid and core2.manager.get_remote(rid, with_secrets=True)["config"]["password"] == "pw"
        sch = c2.get("/api/schedules").get_json()
        assert sch[0]["volume"] == "vol1" and sch[0]["remotes"] == [rid] and sch[0]["time"] == "01:00"
        assert core2.manager.notifier.settings["ntfy"]["token"] == "secret-token"
        assert core2.uptime.monitors()["web"]["display_name"] == "Website"
        assert core2.peers.get(core2.peers.list()[0]["id"], with_token=True)["token"] == "ptok"
        # and via recovery scan + remote import
        t = wait_task(c2, c2.post("/api/recovery/scan", json={"remote": rid}).get_json()["task"]["id"])
        assert t["status"] == "success"
        scan = c2.get("/api/recovery/last").get_json()
        assert scan["configs"] and scan["configs"][0]["instance"] == "srv-a" and scan["configs"][0]["counts"]["schedules"] == 1
        r = c2.post("/api/recovery/config/import", json={"remote": rid, "instance": "srv-a", "sections": ["schedules"]})
        assert r.status_code == 200 and r.get_json()["summary"] == {"schedules": 1}
        assert c2.post("/api/recovery/config/import", json={"remote": rid, "instance": "nope"}).status_code == 404
    finally:
        app2.extensions["dockbare"].stop()


def test_env_peers_locked(settings, fake_docker, monkeypatch):
    import dockbare.app as appmod
    from dockbare.config import load_settings
    from dockbare import peers as peers_mod

    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: (_ for _ in ()).throw(peers_mod.PeerError("x")))
    monkeypatch.setenv("DOCKBARE_PEER_1_URL", "https://b.example")
    monkeypatch.setenv("DOCKBARE_PEER_1_TOKEN", "t")
    monkeypatch.setenv("DOCKBARE_PEER_1_THRESHOLD", "7")
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    app = appmod.create_app(load_settings(), start=True)
    try:
        c = app.test_client()
        c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
        ps = c.get("/api/peers").get_json()["peers"]
        assert len(ps) == 1 and ps[0]["id"] == "env-1" and ps[0]["source"] == "env" and ps[0]["failure_threshold"] == 7 and ps[0]["name"] == "Peer 1"
        assert c.put("/api/peers/env-1", json={"name": "x", "url": "https://b.example"}).status_code == 400
        assert c.delete("/api/peers/env-1").status_code == 400
    finally:
        app.extensions["dockbare"].stop()


def test_uptime_baseline_stopped_not_alerted(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    up = core.uptime
    d.containers["old"] = {"id": "c-old", "state": "exited", "status": "Exited (137) 3 weeks ago", "health": None, "exit_code": 137}
    up.poll()
    up._failing_since["old"] = time.time() - 100
    up.poll()
    assert up.incidents(open_only=True) == []
    assert {r["name"]: r.get("ignored") for r in up.rows()}["old"]
    # explicit monitor → alerts even if never seen running
    c.put("/api/uptime/monitors/old", json={"enabled": True, "grace_seconds": 0})
    up.poll()
    assert [i["container"] for i in up.incidents(open_only=True)] == ["old"]


def test_uptime_daily_history(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    up = core.uptime
    up.poll()
    now = time.time()
    # pretend the container has been known for 5 days and had a 6 h outage spanning midnight two days ago
    core.db.execute("UPDATE container_seen SET first_seen=? WHERE name='web'", (now - 5 * 86400,))
    today = int(now // 86400) * 86400
    core.db.execute("INSERT INTO incidents(container, kind, started_at, ended_at, detail, actions) VALUES('web','down',?,?,'',' []')", (today - 86400 - 3 * 3600, today - 86400 + 3 * 3600))
    h = up.daily_history("web", 10, now)
    assert len(h) == 10 and h[-1]["day"] == today
    assert h[0]["pct"] is None and h[3]["pct"] is None  # before first_seen → no data
    assert h[-2]["pct"] == 87.5 and h[-2]["kinds"] == ["down"] and h[-2]["down"] == 3 * 3600  # yesterday: 3 h down
    assert h[-3]["pct"] == 87.5  # day before: 3 h down
    assert h[-1]["pct"] == 100.0
    pub = c.put("/api/uptime/settings", json={"public_enabled": True}) and c.put("/api/uptime/monitors/web", json={"public": True})
    pl = app.test_client().get("/api/uptime/public").get_json()
    assert pl["history_days"] == 90 and len(pl["groups"][0]["items"][0]["history"]) == 90
    assert len(c.get("/api/uptime").get_json()["containers"][0]["history"]) == 30


def test_uptime_announcements_and_maintenance(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((level, title)))
    up = core.uptime
    c.put("/api/uptime/settings", json={"public_enabled": True})
    c.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website", "grace_seconds": 0})
    c.put("/api/uptime/monitors/db", json={"grace_seconds": 0})
    assert c.post("/api/uptime/announcements", json={"type": "incident", "title": ""}).status_code == 400
    assert c.post("/api/uptime/announcements", json={"type": "maintenance", "title": "x", "starts_at": time.time()}).status_code == 400
    # manual incident, major
    r = c.post("/api/uptime/announcements", json={"type": "incident", "title": "Storage degraded", "message": "Disk array rebuilding", "severity": "major", "services": ["web"]})
    assert r.status_code == 200, r.get_json()
    a = r.get_json()
    assert a["status"] == "investigating" and a["active"] and sent[-1] == ("error", "Incident: Storage degraded")
    anon = app.test_client()
    pl = anon.get("/api/uptime/public").get_json()
    assert pl["overall"] == "outage" and pl["groups"][0]["announcements"][0]["title"] == "Storage degraded"
    r = c.put(f"/api/uptime/announcements/{a['id']}", json={"status": "resolved"})
    assert r.get_json()["status"] == "resolved" and r.get_json()["resolved_at"] and sent[-1][0] == "success"
    pl = anon.get("/api/uptime/public").get_json()
    assert pl["overall"] == "operational" and pl["announcements"][0]["status"] == "resolved" and not pl["groups"][0]["announcements"]
    # maintenance window covering db suppresses alerts for db but not web
    now = time.time()
    r = c.post("/api/uptime/announcements", json={"type": "maintenance", "title": "DB upgrade", "services": ["db"], "starts_at": now - 60, "ends_at": now + 3600})
    assert r.status_code == 200 and r.get_json()["status"] == "in_progress" and sent[-1][0] == "info"
    assert anon.get("/api/uptime/public").json["overall"] == "maintenance"
    d.containers["db"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1})
    d.containers["web"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1, "health": None})
    up.poll()
    assert [i["container"] for i in up.incidents(open_only=True)] == ["web"]
    assert up.rows()[0]["name"] == "db" and up.rows()[0]["maintenance"] == "DB upgrade"
    # scheduled (future) maintenance shows as upcoming, does not suppress
    r = c.post("/api/uptime/announcements", json={"type": "maintenance", "title": "Later", "starts_at": now + 86400, "ends_at": now + 90000})
    assert r.get_json()["status"] == "scheduled" and r.get_json()["upcoming"]
    assert len(up.maintenance_active()) == 1
    # peers carry announcements
    st = anon.get("/api/peer/status", headers={"Authorization": f"Bearer {core.peers.token()}"}).get_json()
    assert [x["title"] for x in st["uptime"]["announcements"]] == ["Later", "DB upgrade"]
    assert c.delete(f"/api/uptime/announcements/{a['id']}").status_code == 200


def test_announcement_updates(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    a = c.post("/api/uptime/announcements", json={"type": "incident", "title": "Mail down", "message": "Looking into it", "severity": "major"}).get_json()
    assert [u["status"] for u in a["updates"]] == ["investigating"] and a["updates"][0]["message"] == "Looking into it"
    assert c.post(f"/api/uptime/announcements/{a['id']}/updates", json={"message": ""}).status_code == 400
    u = c.post(f"/api/uptime/announcements/{a['id']}/updates", json={"message": "Provider confirms an outage"}).get_json()
    assert len(u["updates"]) == 2 and u["status"] == "investigating" and u["updates"][0]["message"] == "Provider confirms an outage"
    u = c.post(f"/api/uptime/announcements/{a['id']}/updates", json={"status": "identified", "message": "Bad DNS record"}).get_json()
    assert u["status"] == "identified" and u["updates"][0] == {**u["updates"][0], "status": "identified", "message": "Bad DNS record"}
    u = c.put(f"/api/uptime/announcements/{a['id']}", json={"status": "resolved"}).get_json()
    assert [x["status"] for x in u["updates"]] == ["resolved", "identified", "investigating", "investigating"] and u["resolved_at"]
    c.put("/api/uptime/settings", json={"public_enabled": True})
    pl = app.test_client().get("/api/uptime/public").get_json()
    assert len(pl["announcements"][0]["updates"]) == 4
    c.delete(f"/api/uptime/announcements/{a['id']}")
    assert core.db.query("SELECT * FROM announcement_updates") == []


def test_announcement_close_and_delete_update(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    now = time.time()
    m = c.post("/api/uptime/announcements", json={"type": "maintenance", "title": "Upgrade", "starts_at": now - 60, "ends_at": now + 3600}).get_json()
    assert m["status"] == "in_progress"
    m = c.put(f"/api/uptime/announcements/{m['id']}", json={"status": "completed"}).get_json()
    assert m["status"] == "completed" and m["ends_at"] <= time.time() + 1 and not m["active"]
    a = c.post("/api/uptime/announcements", json={"type": "incident", "title": "Oops", "message": "first"}).get_json()
    u = c.post(f"/api/uptime/announcements/{a['id']}/updates", json={"message": "second"}).get_json()
    assert len(u["updates"]) == 2
    assert c.delete(f"/api/uptime/announcements/{a['id']}/updates/{u['updates'][0]['id']}").get_json()["updates"][0]["message"] == "first"
    assert c.delete(f"/api/uptime/announcements/{a['id']}/updates/{u['updates'][1]['id']}").status_code == 400  # keeps the first
    # resolve, then it is history on the public page; reopen brings it back
    c.put("/api/uptime/settings", json={"public_enabled": True})
    r = c.put(f"/api/uptime/announcements/{a['id']}", json={"status": "resolved"}).get_json()
    pl = app.test_client().get("/api/uptime/public").get_json()
    assert not pl["groups"][0]["announcements"] and [x["title"] for x in pl["announcements"]] == ["Oops", "Upgrade"]
    r = c.put(f"/api/uptime/announcements/{a['id']}", json={"status": "monitoring"}).get_json()
    assert r["active"] and r["resolved_at"] is None
    pl = app.test_client().get("/api/uptime/public").get_json()
    assert pl["groups"][0]["announcements"][0]["title"] == "Oops"


def test_edit_update_changes_status(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)
    a = c.post("/api/uptime/announcements", json={"type": "incident", "title": "X", "message": "start"}).get_json()
    u = c.post(f"/api/uptime/announcements/{a['id']}/updates", json={"status": "resolved", "message": "fixed"}).get_json()
    assert u["status"] == "resolved" and u["resolved_at"]
    uid = u["updates"][0]["id"]
    e = c.put(f"/api/uptime/announcements/{a['id']}/updates/{uid}", json={"message": "fixed, typo corrected", "status": "monitoring"}).get_json()
    assert e["updates"][0]["message"] == "fixed, typo corrected" and e["status"] == "monitoring" and e["resolved_at"] is None and e["active"]
    assert c.put(f"/api/uptime/announcements/{a['id']}/updates/{uid}", json={"status": "bogus"}).status_code == 400
    # deleting the newest update rolls the status back to the previous entry
    e = c.delete(f"/api/uptime/announcements/{a['id']}/updates/{uid}").get_json()
    assert e["status"] == "investigating" and len(e["updates"]) == 1
