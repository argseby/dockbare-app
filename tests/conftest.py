import io
import tarfile

import pytest

from dockbare.config import load_settings


def make_tar(files=None) -> bytes:
    files = files or {"./a.txt": b"hello", "./sub/b.bin": b"\x00" * 5000}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeDocker:
    """Stands in for DockerCtl without a Docker daemon."""

    def __init__(self, volumes=("vol1", "vol2"), tar=None):
        self.vols = set(volumes)
        self.tar = tar or make_tar()
        self.restored = {}
        self.cleared = []
        self._helper_image = "fake:latest"

    def volume_names(self):
        return set(self.vols)

    def volume_exists(self, n):
        return n in self.vols

    def create_volume(self, n):
        self.vols.add(n)

    def list_volumes(self):
        return [{"name": v, "driver": "local", "created": None, "mountpoint": "", "labels": {}, "project": None,
                 "compose_volume": None, "containers": []} for v in sorted(self.vols)]

    def container_map(self):
        return {}

    def containers_using(self, v):
        return []

    def stop_container(self, cid, timeout=30):
        return True

    def start_container(self, cid):
        return True

    def volume_archive_stream(self, volume, chunk_size=65536):
        if volume not in self.vols:
            raise RuntimeError("no such volume")
        for i in range(0, len(self.tar), chunk_size):
            yield self.tar[i:i + chunk_size]

    def restore_archive(self, volume, stream, clear=True):
        if clear:
            self.cleared.append(volume)
        self.restored[volume] = b"".join(stream)

    def clear_volume(self, v):
        self.cleared.append(v)

    def cleanup_helpers(self):
        return 0

    def info(self):
        return {"ok": True, "version": "fake", "api": "1.0"}

    def ping(self):
        return True

    def helper_image(self):
        return self._helper_image


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKBARE_ENV_FILE", "")
    monkeypatch.setenv("DOCKBARE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOCKBARE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOCKBARE_ADMIN_USER", "admin")
    monkeypatch.setenv("DOCKBARE_ADMIN_PASSWORD", "test-password-1234")
    monkeypatch.setenv("DOCKBARE_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("DOCKBARE_ENCRYPTION_KEY", "test-encryption-key")
    monkeypatch.setenv("DOCKBARE_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("DOCKBARE_LOG_LEVEL", "WARNING")
    for k in ("DOCKBARE_TOTP_SECRET", "DOCKBARE_ADMIN_PASSWORD_HASH", "DOCKBARE_HELPER_IMAGE"):
        monkeypatch.delenv(k, raising=False)
    return load_settings()


@pytest.fixture
def fake_docker():
    return FakeDocker()


@pytest.fixture
def app(settings, fake_docker, monkeypatch):
    import dockbare.app as appmod

    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    application = appmod.create_app(settings, start=True)
    yield application
    application.extensions["dockbare"].stop()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    assert r.status_code == 200, r.get_json()
    client.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    return client


def wait_task(client, task_id, timeout=20):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").get_json()
        if t["status"] in ("success", "failure", "cancelled"):
            return t
        time.sleep(0.05)
    raise AssertionError("task did not finish")
