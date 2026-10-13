import json

import pytest

from dockbare.manager import ManagerError, describe_schedule, fmt_bytes


def test_describe_schedule():
    assert describe_schedule({"mode": "interval", "interval_seconds": 90000}) == "every 1d 1h"
    assert describe_schedule({"mode": "daily", "time": "03:00", "weekdays": list(range(7))}) == "daily at 03:00"
    assert describe_schedule({"mode": "daily", "time": "03:00", "weekdays": [0, 1, 2, 3, 4]}) == "weekdays at 03:00"
    assert fmt_bytes(1536) == "1.5 KB" and fmt_bytes(0) == "0 B"


def test_resolve_backup_path(app):
    m = app.extensions["dockbare"].manager
    root = m.settings.backup_dir
    (root / "vol1").mkdir()
    f = root / "vol1" / "2024-01-01_00-00-00.tar.gz"
    f.write_bytes(b"x")
    assert m.resolve_backup_path(str(f)) == f.resolve()
    assert m.resolve_backup_path("vol1/2024-01-01_00-00-00.tar.gz") == f.resolve()
    with pytest.raises(ManagerError):
        m.resolve_backup_path(str(root / "vol1" / "nested" / "x.tar.gz"), must_exist=False)
    with pytest.raises(ManagerError):
        m.resolve_backup_path(str(root / "x.tar.gz"), must_exist=False)  # not inside a volume dir
    with pytest.raises(ManagerError):
        m.resolve_backup_path(str(f) + ".meta.json")
    with pytest.raises(ManagerError):
        m.resolve_backup_path("vol1/../../etc/passwd.tar.gz", must_exist=False)
    # symlink escaping the backup dir
    link = root / "vol1" / "2024-02-02_00-00-00.tar.gz"
    outside = m.settings.data_dir / "evil.tar.gz"
    outside.write_bytes(b"y")
    link.symlink_to(outside)
    with pytest.raises(ManagerError):
        m.resolve_backup_path(str(link))


def test_legacy_migration(settings, fake_docker, monkeypatch):
    import dockbare.app as appmod

    legacy = {
        "schedules": {"vol1": {"interval_days": 1, "interval_hours": 0, "interval_minutes": 0, "interval_seconds": 0, "prune_keep_days": 14,
                               "notifications_enabled": True, "ftp_upload_enabled": True, "enabled": True}},
        "ftp_settings": {"host": "ftp.example", "port": 21, "username": "u", "password": "p", "remote_base_path": "/bk", "secure": True, "mode": "explicit", "sync_enabled": True, "sync_interval_minutes": 30},
        "notification_settings": {"base_url": "https://ntfy.sh", "topic": "old-topic"},
    }
    (settings.backup_dir / "backup_config.json").write_text(json.dumps(legacy))
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    app = appmod.create_app(settings, start=True)
    try:
        m = app.extensions["dockbare"].manager
        remotes = m.list_remotes()
        assert len(remotes) == 1 and remotes[0]["config"]["host"] == "ftp.example" and remotes[0]["config"]["secure"] is True
        assert m.get_remote(remotes[0]["id"], with_secrets=True)["config"]["password"] == "p"
        s = m.get_schedule("vol1")
        assert s and s["interval_seconds"] == 86400 and s["keep_seconds"] == 14 * 86400 and s["remotes"] == [remotes[0]["id"]]
        assert m.notifier.settings["ntfy"]["topic"] == "old-topic" and m.notifier.settings["ntfy"]["enabled"]
        assert m.general()["sync_enabled"] and m.general()["sync_interval_minutes"] == 30
        assert (settings.backup_dir / "backup_config.json.migrated").exists()
    finally:
        app.extensions["dockbare"].stop()
