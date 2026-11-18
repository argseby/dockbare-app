"""Utilities: engine health, image update check, report, container actions, volume/backup browser, volume copy."""
import io
import tarfile

import pytest

from dockbare.dockerctl import DockerError
from dockbare.manager import ManagerError
from dockbare.utilities import clean_member, clean_rel
from tests.conftest import FakeDocker, make_tar, wait_task


def test_clean_rel_and_member():
    assert clean_rel("") == "" and clean_rel("/") == "" and clean_rel("a//b/") == "a/b" and clean_rel("a/./b") == "a/b"
    assert clean_rel("..cache/x") == "..cache/x" and clean_rel(".env") == ".env"
    for bad in ("..", "../x", "a/../../x", "a/..", "x\x00", "a\\b"):
        with pytest.raises(ManagerError):
            clean_rel(bad)
    assert [clean_member(x) for x in ("./", ".", "./.env", "./a/b/", "/x")] == ["", "", ".env", "a/b", "x"]


def tar_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            if data is None:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class UtilDocker:
    def __init__(self, base):
        self.base = base
        self.containers = [{"id": "c-web", "name": "web", "state": "running"}, {"id": "c-self", "name": "dockbare", "state": "running"}]
        self.actions = []
        self.refs = [{"name": "web", "ref": "nginx:1.25", "image_id": "sha256:n"}, {"name": "web2", "ref": "nginx:1.25", "image_id": "sha256:n"},
                     {"name": "db", "ref": "postgres:16", "image_id": "sha256:p"}, {"name": "app", "ref": "myapp:dev", "image_id": "sha256:m"},
                     {"name": "pin", "ref": "redis@sha256:abc", "image_id": "sha256:r"}, {"name": "raw", "ref": "sha256:zz", "image_id": "sha256:zz"},
                     {"name": "priv", "ref": "ghcr.io/me/priv:1", "image_id": "sha256:q"}]
        self.digests = {"sha256:n": ["nginx@sha256:old"], "sha256:p": ["postgres@sha256:same"], "sha256:m": [], "sha256:q": ["ghcr.io/me/priv@sha256:x"]}
        self.remote = {"nginx:1.25": "sha256:new", "postgres:16": "sha256:same"}
        self.dirs = {"": [{"name": "conf", "type": "dir", "size": 4096, "mtime": 1}, {"name": "app.db", "type": "file", "size": 5, "mtime": 2},
                          {"name": "link", "type": "link", "size": 4, "mtime": 3}], "conf": [{"name": "a.ini", "type": "file", "size": 3, "mtime": 4}]}
        self.created = []
        self.restored = {}
        self.stopped, self.started = [], []
        self.users = []

    def __getattr__(self, item):
        return getattr(self.base, item)

    # engine
    def engine_info(self):
        return {"info": {"Warnings": ["WARNING: No swap limit support"], "Driver": "vfs", "LiveRestoreEnabled": False, "CgroupVersion": "2",
                         "OperatingSystem": "Debian", "NCPU": 4, "MemTotal": 8, "Containers": 3, "ContainersRunning": 2, "Images": 5},
                "version": {"Version": "27.0.1", "ApiVersion": "1.46"}}

    def container_log_configs(self):
        return [{"name": "web", "state": "running", "driver": "json-file", "options": {}},
                {"name": "db", "state": "running", "driver": "json-file", "options": {"max-size": "10m"}},
                {"name": "app", "state": "exited", "driver": "local", "options": {}}]

    # updates
    def running_image_refs(self):
        return self.refs

    def image_repo_digests(self, iid):
        return self.digests.get(iid)

    def registry_digest(self, ref):
        if ref not in self.remote:
            raise DockerError("unauthorized: authentication required")
        return self.remote[ref]

    # actions
    def list_containers(self):
        return self.containers

    def own_container_id(self):
        return "c-self"

    def start_container(self, cid):
        self.actions.append(("start", cid))
        self.started.append(cid)
        return True

    def stop_container(self, cid, timeout=30):
        self.actions.append(("stop", cid))
        self.stopped.append(cid)
        return True

    def restart_container(self, cid, timeout=10):
        self.actions.append(("restart", cid))
        return cid != "c-broken"

    # browse
    def volume_exists(self, n):
        return n in self.base.vols

    def list_volume_dir(self, volume, rel):
        return self.dirs.get(rel)

    def volume_path_archive(self, volume, rel):
        if rel == "app.db":
            yield tar_of({"app.db": b"hello"})
        else:
            yield tar_of({"conf": None, "conf/a.ini": b"x=1"})

    # copy
    def create_volume(self, n):
        self.created.append(n)
        self.base.vols.add(n)

    def restore_archive(self, volume, stream, clear=True):
        self.restored[volume] = (b"".join(stream), clear)

    def containers_using(self, v):
        return self.users


@pytest.fixture
def util_app(settings, monkeypatch):
    import dockbare.app as appmod

    d = UtilDocker(FakeDocker(volumes=("vol1", "vol2"), tar=make_tar({"./a.txt": b"hello", "./sub/b.bin": b"\x01" * 3000, "./.env": b"SECRET=1"})))
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: d)
    settings.testing = True
    app = appmod.create_app(settings, start=True)
    c = app.test_client()
    c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    yield app, c, d
    app.extensions["dockbare"].stop()


def test_engine_health(util_app):
    app, c, d = util_app
    e = c.get("/api/utilities/engine").get_json()
    assert e["engine"]["version"] == "27.0.1" and e["engine"]["storage_driver"] == "vfs"
    titles = [x["title"] for x in e["checks"]]
    assert e["checks"][0]["level"] == "warn"
    unrot = next(x for x in e["checks"] if "without a size limit" in x["title"])
    assert unrot["items"] == ["web"]                       # db has max-size, app uses the "local" driver
    assert any("Live restore is off" in t for t in titles) and any("Storage driver" in t for t in titles)
    assert app.test_client().get("/api/utilities/engine").status_code == 401


def test_image_update_check(util_app):
    app, c, d = util_app
    assert c.get("/api/utilities/updates").get_json()["last"] is None
    r = c.post("/api/utilities/updates").get_json()
    t = wait_task(c, r["task"]["id"])
    assert t["status"] == "success"
    items = {i["ref"]: i for i in c.get("/api/utilities/updates").get_json()["last"]["items"]}
    assert items["nginx:1.25"]["status"] == "update" and items["nginx:1.25"]["containers"] == ["web", "web2"]
    assert items["postgres:16"]["status"] == "current"
    assert items["myapp:dev"]["status"] == "local"
    assert items["redis@sha256:abc"]["status"] == "pinned" and items["sha256:zz"]["status"] == "skipped"
    assert items["ghcr.io/me/priv:1"]["status"] == "unknown" and "authentication" in items["ghcr.io/me/priv:1"]["detail"]


def test_report_and_settings(util_app, monkeypatch):
    app, c, d = util_app
    core = app.extensions["dockbare"]
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((event, title, msg)) or {"ntfy": "ok"})
    monkeypatch.setattr(core.cleanup, "scan", lambda with_sizes=True: {
        "images": [{"protected": None}], "volumes": [], "containers": [{"protected": None}, {"protected": "x"}],
        "disk": {"images_unused": 3 * 1024 ** 3, "containers_unused": 0, "volumes_unused": 0, "build_cache_unused": 1024 ** 3}})
    r = core.utilities.send_report()
    assert r["worth"] and sent and sent[0][0] == "utilities" and "4.0 GB could be freed" in sent[0][1]
    assert "1 image(s) with a newer version: nginx:1.25" in sent[0][2] and "Nothing was deleted" in sent[0][2]
    sent.clear()
    c.put("/api/utilities/settings", json={"report_min_gb": 100, "report_updates": False})
    r = core.utilities.send_report()
    assert not r["worth"] and not sent                     # below the threshold, no updates checked: nothing sent
    assert c.post("/api/utilities/report").get_json()["sent"]   # "send now" always sends
    assert c.put("/api/utilities/settings", json={"report": "hourly"}).status_code == 400
    assert c.put("/api/utilities/settings", json={"report_time": "25:00"}).status_code == 400
    s = c.put("/api/utilities/settings", json={"report": "weekly", "report_time": "7:05"}).get_json()
    assert s["report"] == "weekly" and s["report_time"] == "07:05"
    job = core.manager.scheduler.get_job("utilities-report") if core.manager.scheduler.running else None
    if core.manager.scheduler.running:
        assert job is not None
    c.put("/api/utilities/settings", json={"report": "off"})
    assert core.manager.scheduler.get_job("utilities-report") is None
    assert core.configsync.export()["utilities"]["report"] == "off"


def test_container_actions(util_app):
    app, c, d = util_app
    assert c.post("/api/containers/web/restart").get_json()["ok"]
    assert c.post("/api/containers/web/stop").get_json()["ok"]
    assert d.actions == [("restart", "c-web"), ("stop", "c-web")]
    r = c.post("/api/containers/dockbare/stop")
    assert r.status_code == 400 and "own container" in r.get_json()["error"]
    assert c.post("/api/containers/web/remove").status_code == 400
    assert c.post("/api/containers/nope/start").status_code == 400
    assert c.post("/api/containers/bad%20name/start").status_code == 400
    assert app.test_client().post("/api/containers/web/start").status_code == 401
    kinds = [e["kind"] for e in app.extensions["dockbare"].db.query("SELECT kind FROM events WHERE kind LIKE 'container.%'")]
    assert kinds.count("container.action") == 2


def test_volume_browser(util_app):
    app, c, d = util_app
    r = c.get("/api/browse/volume/vol1").get_json()
    assert [e["name"] for e in r["entries"]] == ["conf", "app.db", "link"]      # folders first
    assert c.get("/api/browse/volume/vol1?path=conf").get_json()["entries"][0]["name"] == "a.ini"
    assert c.get("/api/browse/volume/vol1?path=nope").status_code == 400
    assert c.get("/api/browse/volume/vol1?path=../etc").status_code == 400
    assert c.get("/api/browse/volume/missing").status_code == 400
    f = c.get("/api/browse/volume/vol1/download?path=app.db")
    assert f.status_code == 200 and f.data == b"hello" and 'filename="app.db"' in f.headers["Content-Disposition"]
    t = c.get("/api/browse/volume/vol1/download?path=conf")
    assert t.headers["Content-Disposition"].endswith('conf.tar"') and tarfile.open(fileobj=io.BytesIO(t.data)).getnames() == ["conf", "conf/a.ini"]
    assert c.get("/api/browse/volume/vol1/download?path=link").status_code == 400
    assert app.test_client().get("/api/browse/volume/vol1").status_code == 401


def test_backup_browser(util_app):
    app, c, d = util_app
    core = app.extensions["dockbare"]
    t = core.manager.start_backup(["vol1"])[0]
    assert wait_task(c, t.id)["status"] == "success"
    backup = core.db.one("SELECT path FROM backups WHERE volume='vol1'")["path"]
    root = c.get("/api/browse/backup", query_string={"backup": backup}).get_json()
    assert sorted(e["name"] for e in root["entries"]) == [".env", "a.txt", "sub"]
    sub = c.get("/api/browse/backup", query_string={"backup": backup, "path": "sub"}).get_json()
    assert sub["entries"] == [{"name": "b.bin", "type": "file", "size": 3000, "mtime": sub["entries"][0]["mtime"]}]
    f = c.get("/api/browse/backup/download", query_string={"backup": backup, "path": "sub/b.bin"})
    assert f.status_code == 200 and f.data == b"\x01" * 3000
    assert c.get("/api/browse/backup/download", query_string={"backup": backup, "path": ".env"}).data == b"SECRET=1"
    assert c.get("/api/browse/backup/download", query_string={"backup": backup, "path": "sub"}).status_code == 400
    assert c.get("/api/browse/backup", query_string={"backup": "../../etc/passwd"}).status_code == 400


def test_volume_copy(util_app):
    app, c, d = util_app
    assert c.post("/api/volumes/copy", json={"source": "vol1", "target": "vol2"}).status_code == 400      # never overwrites
    assert c.post("/api/volumes/copy", json={"source": "vol1", "target": "vol1"}).status_code == 400
    assert c.post("/api/volumes/copy", json={"source": "nope", "target": "x"}).status_code == 400
    assert c.post("/api/volumes/copy", json={"source": "vol1", "target": "bad name"}).status_code == 400
    d.users = [{"id": "c1", "name": "web", "status": "running"}, {"id": "c2", "name": "old", "status": "exited"}]
    r = c.post("/api/volumes/copy", json={"source": "vol1", "target": "vol1-copy"}).get_json()
    t = wait_task(c, r["task"]["id"])
    assert t["status"] == "success", t
    data, clear = d.restored["vol1-copy"]
    assert data == d.base.tar and clear is False and d.created == ["vol1-copy"]
    assert d.stopped == ["c1"] and d.started == ["c1"]                                             # only the running one, restarted after
    assert "vol1" in d.base.vols                                                                   # source untouched
