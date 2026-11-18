"""Cleanup of unused images, volumes and build cache: every safety layer has a test."""
import threading

import pytest

from dockbare.dockerctl import DockerError
from dockbare.tasks import Task
from tests.conftest import FakeDocker, wait_task

ANON = "a" * 64


class CleanupDocker:
    """FakeDocker plus images, containers and removal calls."""

    def __init__(self, base):
        self.base = base
        self.containers = [
            {"Id": "c-web", "Names": ["/web"], "Image": "app:1", "ImageID": "sha256:used", "State": "exited", "Status": "Exited (0) 3 days ago",
             "Mounts": [{"Type": "volume", "Name": "vol3"}, {"Type": "bind", "Source": "/srv"}]},
            {"Id": "c-helper", "Names": ["/dockbare-helper"], "ImageID": "sha256:used", "State": "created", "Labels": {"dev.dockbare.helper": "1"},
             "Mounts": [{"Type": "volume", "Name": "vol4"}]},
        ]
        self.images = [
            {"Id": "sha256:used", "RepoTags": ["app:1"], "RepoDigests": ["app@sha256:x"], "Size": 100, "Created": 1},
            {"Id": "sha256:dangling", "RepoTags": ["<none>:<none>"], "RepoDigests": [], "Size": 200, "Created": 2},
            {"Id": "sha256:nginx", "RepoTags": ["nginx:1.25", "nginx:latest"], "RepoDigests": ["nginx@sha256:y"], "Size": 300, "Created": 3,
             "ParentId": "sha256:base"},
            {"Id": "sha256:base", "RepoTags": ["base:1"], "RepoDigests": ["base@sha256:z"], "Size": 50, "Created": 1},
            {"Id": "sha256:local", "RepoTags": ["myapp:dev"], "RepoDigests": [], "Size": 400, "Created": 4},
            {"Id": "sha256:helper", "RepoTags": ["dockbare:local"], "RepoDigests": [], "Size": 500, "Created": 5},
        ]
        self.vol_list = [
            {"Name": "vol1", "CreatedAt": "2026-01-01T00:00:00Z", "Driver": "local", "Labels": None},
            {"Name": "vol2", "CreatedAt": "2026-01-02T00:00:00Z", "Driver": "local", "Labels": {}},
            {"Name": "vol3", "CreatedAt": "2026-01-03T00:00:00Z", "Driver": "local", "Labels": {}},
            {"Name": "vol4", "CreatedAt": "2026-01-04T00:00:00Z", "Driver": "local", "Labels": {}},
            {"Name": "dockbare_data", "CreatedAt": "2026-01-05T00:00:00Z", "Driver": "local", "Labels": {"dev.dockbare.internal": "1"}},
            {"Name": ANON, "CreatedAt": "2026-01-06T00:00:00Z", "Driver": "local", "Labels": {}},
            {"Name": "shop_db", "CreatedAt": "2026-01-07T00:00:00Z", "Driver": "local", "Labels": {"com.docker.compose.project": "shop"}},
        ]
        self.df = {"Volumes": [{"Name": "vol1", "UsageData": {"Size": 1000}}, {"Name": ANON, "UsageData": {"Size": -1}}],
                   "BuildCache": [{"Size": 70, "InUse": False, "Shared": False}, {"Size": 30, "InUse": True}], "Images": [], "LayersSize": 999}
        self.containers += [
            {"Id": "c-run", "Names": ["/api"], "Image": "app:1", "ImageID": "sha256:used", "State": "running", "Mounts": []},
            {"Id": "c-mon", "Names": ["/db"], "Image": "app:1", "ImageID": "sha256:used", "State": "exited", "Mounts": []},
            {"Id": "c-self", "Names": ["/dockbare"], "Image": "dockbare", "ImageID": "sha256:used", "State": "exited", "Mounts": []},
        ]
        self.removed_containers: list[str] = []
        self.removed_images: list[str] = []
        self.removed_volumes: list[str] = []
        self.users: dict[str, list[str]] = {}
        self.refuse: dict[str, str] = {}
        self.pruned = False

    def __getattr__(self, item):
        return getattr(self.base, item)

    def cleanup_inventory(self, with_sizes=True):
        return {"containers": [c for c in self.containers if c["Id"] not in self.removed_containers], "images": [i for i in self.images if i["Id"] not in self.gone_images()],
                "volumes": [v for v in self.vol_list if v["Name"] not in self.removed_volumes], "df": self.df if with_sizes else None}

    def gone_images(self):
        return {i["Id"] for i in self.images if all(t in self.removed_images for t in (i["RepoTags"] if i["RepoTags"] != ["<none>:<none>"] else [i["Id"]]))}

    def own_container_id(self):
        return "c-self"

    def container_raw(self, cid):
        return {"State": {"ExitCode": 0, "FinishedAt": "2026-11-15T10:00:00.123456789Z"}}

    def remove_container(self, cid):
        if cid in self.refuse:
            raise DockerError(self.refuse[cid])
        self.removed_containers.append(cid)

    def protected_image_ids(self):
        return {"sha256:helper"}

    def remove_image(self, ref):
        if ref in self.refuse:
            raise DockerError(self.refuse[ref])
        self.removed_images.append(ref)

    def remove_volume(self, name):
        if name in self.refuse:
            raise DockerError(self.refuse[name])
        self.removed_volumes.append(name)
        self.base.vols.discard(name)

    def volume_users(self, name):
        return self.users.get(name, [])

    def prune_build_cache(self):
        self.pruned = True
        return 70


@pytest.fixture
def cleanup_app(settings, monkeypatch):
    import dockbare.app as appmod

    d = CleanupDocker(FakeDocker(volumes=("vol1", "vol2", ANON, "shop_db")))
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: d)
    settings.testing = True
    app = appmod.create_app(settings, start=True)
    c = app.test_client()
    c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    core = app.extensions["dockbare"]
    core.manager.save_schedule("vol2", {"enabled": True, "mode": "interval", "interval_hours": 24})
    yield app, c, d
    core.stop()


def test_scan_lists_only_unused_and_marks_protection(cleanup_app):
    app, c, d = cleanup_app
    s = c.get("/api/cleanup").get_json()
    imgs = {i["id"]: i for i in s["images"]}
    assert "sha256:used" not in imgs                                  # used by a *stopped* container
    assert imgs["sha256:dangling"]["dangling"] and not imgs["sha256:dangling"]["protected"]
    assert imgs["sha256:helper"]["protected"].startswith("Dockbare needs")
    assert imgs["sha256:base"]["protected"] == "other images are built on it"
    assert imgs["sha256:local"]["local_only"] and not imgs["sha256:nginx"]["local_only"]
    vols = {v["name"]: v for v in s["volumes"]}
    assert set(vols) == {"vol1", "vol2", ANON, "shop_db"}              # vol3 (stopped container), vol4 (helper), dockbare_data hidden
    assert vols["vol2"]["protected"].startswith("has a backup schedule")
    assert vols[ANON]["anonymous"] and vols[ANON]["size"] is None
    assert vols["vol1"]["size"] == 1000 and vols["shop_db"]["project"] == "shop"
    assert s["build_cache"]["reclaimable"] == 70 and s["totals"]["volumes_in_use"] == 2
    assert d.removed_images == [] and d.removed_volumes == []          # scanning never deletes
    assert app.test_client().get("/api/cleanup").status_code == 401


def test_run_removes_only_valid_selection(cleanup_app):
    app, c, d = cleanup_app
    s = c.get("/api/cleanup").get_json()
    created = {v["name"]: v["created"] for v in s["volumes"]}
    r = c.post("/api/cleanup/run", json={
        "images": ["sha256:dangling", "sha256:nginx", "sha256:helper", "sha256:used", "sha256:nope"],
        "volumes": [{"name": ANON, "created": created[ANON]}, {"name": "vol2", "created": created["vol2"]},
                    {"name": "vol3", "created": "x"}, {"name": "shop_db", "created": "2020-01-01"}],
        "backup_first": False, "build_cache": True}).get_json()
    t = wait_task(c, r["task"]["id"])
    assert t["status"] == "success", t
    res = t["result"]
    assert d.removed_images == ["sha256:dangling", "nginx:1.25", "nginx:latest"]   # multi-tag image: untagged tag by tag, never forced
    assert d.removed_volumes == [ANON]
    reasons = {x["item"]: x["reason"] for x in res["skipped"]}
    assert reasons["dockbare:local"].startswith("Dockbare needs")
    assert "no longer unused" in reasons["used"]                      # in use by a stopped container
    assert "no longer unused" in reasons["nope"]                      # unknown id
    assert reasons["vol2"].startswith("has a backup schedule")
    assert "no longer unused" in reasons["vol3"]
    assert "changed since the scan" in reasons["shop_db"]
    assert d.pruned and res["freed"] == 200 + 300 + 70
    kinds = [e["kind"] for e in app.extensions["dockbare"].db.query("SELECT kind FROM events WHERE kind LIKE 'cleanup.%'")]
    assert kinds.count("cleanup.image_removed") == 2 and kinds.count("cleanup.volume_removed") == 1 and "cleanup.finished" in kinds


def _run(app, images=(), volumes=(), backup_first=True):
    core = app.extensions["dockbare"]
    s = core.cleanup.scan()
    created = {v["name"]: v["created"] for v in s["volumes"]}
    task = Task("cleanup", "test")
    return core.cleanup._run(task, list(images), [{"name": n, "created": created[n]} for n in volumes], backup_first, False), task


def test_volume_is_backed_up_before_deletion(cleanup_app):
    app, c, d = cleanup_app
    res, task = _run(app, volumes=["vol1"])
    assert res["removed_volumes"] == ["vol1"]
    rows = app.extensions["dockbare"].db.query("SELECT source, size FROM backups WHERE volume='vol1'")
    assert len(rows) == 1 and rows[0]["source"] == "cleanup" and rows[0]["size"] > 0
    assert any("Final backup of vol1:" in line for line in task.log)


def test_failed_backup_keeps_the_volume(cleanup_app, monkeypatch):
    app, c, d = cleanup_app
    m = app.extensions["dockbare"].manager

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(m, "_do_backup", boom)
    res, _ = _run(app, volumes=["vol1"])
    assert res["removed_volumes"] == [] and d.removed_volumes == []
    assert "final backup failed: disk full" in res["skipped"][0]["reason"]
    # a failed upload keeps it too
    monkeypatch.setattr(m, "_do_backup", lambda *a, **k: {"filename": "x", "size": 10, "uploads": {"r1": "synced", "r2": "failed"}})
    res, _ = _run(app, volumes=["vol1"])
    assert res["removed_volumes"] == [] and "upload failed" in res["skipped"][0]["reason"]
    monkeypatch.setattr(m, "_do_backup", lambda *a, **k: {"filename": "x", "size": 10, "uploads": {"r1": "synced", "r2": "skipped"}})
    res, _ = _run(app, volumes=["vol1"])
    assert res["removed_volumes"] == ["vol1"]


def test_volume_taken_into_use_or_refused_is_kept(cleanup_app):
    app, c, d = cleanup_app
    d.users["vol1"] = ["new-container"]
    res, _ = _run(app, volumes=["vol1"], backup_first=False)
    assert res["removed_volumes"] == [] and "now used by new-container" in res["skipped"][0]["reason"]
    d.users.clear()
    d.refuse["vol1"] = "volume is in use"
    res, _ = _run(app, volumes=["vol1"], backup_first=False)
    assert res["removed_volumes"] == [] and "Docker refused: volume is in use" in res["skipped"][0]["reason"]
    d.refuse["sha256:dangling"] = "conflict: image is being used"
    res, _ = _run(app, images=["sha256:dangling"])
    assert res["removed_images"] == [] and "Docker refused" in res["skipped"][0]["reason"]


def test_run_validation_and_single_cleanup_at_a_time(cleanup_app):
    app, c, d = cleanup_app
    assert c.post("/api/cleanup/run", json={}).status_code == 400
    assert c.post("/api/cleanup/run", json={"images": "sha256:dangling"}).status_code == 400
    assert c.post("/api/cleanup/run", json={"volumes": ["vol1"]}).status_code == 400
    assert app.test_client().post("/api/cleanup/run", json={"images": ["x"]}).status_code == 401
    gate = threading.Event()
    blocker = app.extensions["dockbare"].tasks.submit("cleanup", "blocker", lambda t: gate.wait(5))
    try:
        r = c.post("/api/cleanup/run", json={"images": ["sha256:dangling"]})
        assert r.status_code == 400 and "already running" in r.get_json()["error"]
    finally:
        gate.set()
        wait_task(c, blocker.id)
    assert d.removed_images == []


def test_stopped_containers(cleanup_app):
    app, c, d = cleanup_app
    c.put("/api/uptime/monitors/db", json={"enabled": True})
    s = c.get("/api/cleanup").get_json()
    cons = {x["name"]: x for x in s["containers"]}
    assert set(cons) == {"web", "db"}                 # running "api", the helper and Dockbare's own container are not listed
    assert cons["db"]["protected"].startswith("has an Uptime monitor") and not cons["web"]["protected"]
    from datetime import datetime, timezone

    assert cons["web"]["volumes"] == ["vol3"]
    assert cons["web"]["finished_at"] == pytest.approx(datetime(2026, 11, 15, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp())
    r = c.post("/api/cleanup/run", json={"containers": ["c-web", "c-mon", "c-run", "c-self"], "backup_first": False}).get_json()
    t = wait_task(c, r["task"]["id"])
    assert t["result"]["removed_containers"] == ["web"] and d.removed_containers == ["c-web"]
    reasons = {x["item"]: x["reason"] for x in t["result"]["skipped"]}
    assert reasons["db"].startswith("has an Uptime monitor")
    assert "no longer stopped" in reasons["c-run"] and "no longer stopped" in reasons["c-self"]
    # its volume is now unused and shows up in the next scan; it was not removed with the container
    assert "vol3" in {v["name"] for v in c.get("/api/cleanup").get_json()["volumes"]}
