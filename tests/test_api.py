import io
import os
import time

from tests.conftest import make_tar, wait_task


def test_requires_auth(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert b"Sign in" in client.get("/").data


def test_login_logout_and_csrf(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    assert r.status_code == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert client.get("/api/auth/session").get_json()["authenticated"] is True
    # mutating call without CSRF header
    assert client.post("/api/backups", json={"volumes": ["vol1"]}).status_code == 403
    client.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/overview").status_code == 401


def test_lockout(client):
    for _ in range(3):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    assert r.status_code == 429 and r.headers.get("Retry-After")


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"


def test_volume_validation(auth_client):
    for bad in ("/etc", "../x", "a b", "", "x" * 300):
        r = auth_client.post("/api/backups", json={"volumes": [bad]})
        assert r.status_code == 400, bad
    r = auth_client.post("/api/schedules", json={"volume": "nope", "mode": "daily"})
    assert r.status_code == 400


def test_path_confinement(auth_client, app):
    core = app.extensions["dockbare"]
    outside = core.settings.data_dir / "secret.tar.gz"
    outside.write_bytes(b"x")
    for p in ("/etc/passwd", str(outside), "../data/secret.tar.gz", str(core.settings.backup_dir) + "/../data/secret.tar.gz", "vol1/../../data/secret.tar.gz"):
        assert auth_client.get(f"/api/backups/download?path={p}").status_code == 400, p
        assert auth_client.delete("/api/backups", json={"path": p}).status_code == 400, p
        assert auth_client.post("/api/backups/restore", json={"path": p, "volume": "vol1"}).status_code == 400, p
    assert outside.exists()


def test_backup_verify_restore_delete_flow(auth_client, app, fake_docker):
    core = app.extensions["dockbare"]
    r = auth_client.post("/api/backups", json={"volumes": ["vol1"], "remotes": []})
    assert r.status_code == 200, r.get_json()
    t = wait_task(auth_client, r.get_json()["tasks"][0]["id"])
    assert t["status"] == "success", t
    path = t["result"]["path"]
    assert path.endswith(".tar.gz.enc") and os.path.isfile(path) and os.path.isfile(path + ".meta.json")
    backups = auth_client.get("/api/backups").get_json()
    assert len(backups) == 1 and backups[0]["encrypted"] is True and backups[0]["volume"] == "vol1"
    # verify
    t = wait_task(auth_client, auth_client.post("/api/backups/verify", json={"path": path}).get_json()["task"]["id"])
    assert t["status"] == "success" and t["result"]["files"] == 2
    # download decrypted
    r = auth_client.get(f"/api/backups/download?path={path}&decrypt=1")
    assert r.status_code == 200 and r.data[:2] == b"\x1f\x8b"
    # restore into vol2
    t = wait_task(auth_client, auth_client.post("/api/backups/restore", json={"path": path, "volume": "vol2"}).get_json()["task"]["id"])
    assert t["status"] == "success", t
    assert fake_docker.restored["vol2"] == fake_docker.tar
    # overview counts
    o = auth_client.get("/api/overview").get_json()
    assert o["backups"]["count"] == 1 and o["backups"]["encrypted"] == 1 and o["volumes"]["protected"] == 1
    # events and backup history
    ev = auth_client.get("/api/backups/events?path=" + path).get_json()
    assert any(e["kind"] == "restore.completed" for e in ev)
    # delete
    assert auth_client.delete("/api/backups", json={"path": path}).status_code == 200
    assert not os.path.exists(path) and not os.path.exists(path + ".meta.json")
    assert auth_client.get("/api/backups").get_json() == []


def test_plain_backup_when_encrypt_false(auth_client):
    r = auth_client.post("/api/backups", json={"volumes": ["vol1"], "encrypt": False})
    t = wait_task(auth_client, r.get_json()["tasks"][0]["id"])
    assert t["status"] == "success" and t["result"]["path"].endswith(".tar.gz")


def test_upload_backup_file(auth_client, app):
    tar = make_tar()
    import gzip
    data = gzip.compress(tar)
    r = auth_client.post("/api/backups/upload", data={"volume": "vol1", "file": (io.BytesIO(data), "2024-01-01_00-00-00.tar.gz")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    r = auth_client.post("/api/backups/upload", data={"volume": "../evil", "file": (io.BytesIO(data), "2024-01-01_00-00-00.tar.gz")}, content_type="multipart/form-data")
    assert r.status_code == 400
    r = auth_client.post("/api/backups/upload", data={"volume": "vol1", "file": (io.BytesIO(b"x"), "evil.sh")}, content_type="multipart/form-data")
    assert r.status_code == 400
    b = auth_client.get("/api/backups").get_json()
    assert len(b) == 1 and b[0]["filename"] == "2024-01-01_00-00-00.tar.gz"


def test_schedules_crud(auth_client):
    r = auth_client.post("/api/schedules", json={"volume": "vol1", "mode": "daily", "time": "02:30", "weekdays": [0, 2], "keep_seconds": 86400 * 7, "keep_max": 3})
    assert r.status_code == 200, r.get_json()
    s = r.get_json()
    assert s["interval_str"] == "Mon, Wed at 02:30" and s["next_run"]
    assert auth_client.post("/api/schedules", json={"volume": "vol1", "mode": "interval", "interval_seconds": 10}).status_code == 400
    r = auth_client.put("/api/schedules/vol1", json={"mode": "interval", "interval_seconds": 3600})
    assert r.get_json()["interval_str"] == "every 1h"
    r = auth_client.post("/api/schedules/vol1/toggle", json={"enabled": False})
    assert r.get_json()["enabled"] is False and r.get_json()["next_run"] is None
    assert auth_client.delete("/api/schedules/vol1").status_code == 200
    assert auth_client.get("/api/schedules").get_json() == []


def test_remote_secrets_encrypted_at_rest(auth_client, app):
    core = app.extensions["dockbare"]
    r = auth_client.post("/api/remotes", json={"name": "nas", "type": "ftp", "config": {"host": "nas.local", "port": 21, "username": "u", "password": "hunter2", "base_path": "/b"}})
    assert r.status_code == 200, r.get_json()
    pub = r.get_json()
    assert "password" not in pub["config"] and pub["config"]["password_set"] is True
    raw = core.db.one("SELECT config FROM remotes").__getitem__("config")
    assert "hunter2" not in raw and "enc:v1:" in raw
    assert core.manager.get_remote(pub["id"], with_secrets=True)["config"]["password"] == "hunter2"
    # blank password on update keeps the stored one
    r = auth_client.put(f"/api/remotes/{pub['id']}", json={"name": "nas2", "type": "ftp", "config": {"host": "nas.local", "port": 21, "username": "u", "password": "", "base_path": "/b"}})
    assert r.status_code == 200
    assert core.manager.get_remote(pub["id"], with_secrets=True)["config"]["password"] == "hunter2"
    assert auth_client.post("/api/remotes", json={"name": "bad", "type": "ftp", "config": {"host": "h", "base_path": "/"}}).status_code == 400


def test_notification_secrets_and_settings(auth_client, app):
    core = app.extensions["dockbare"]
    r = auth_client.put("/api/settings/notifications", json={"email": {"enabled": True, "host": "smtp", "password": "pw"}})
    assert r.get_json()["email"]["password_set"] is True and r.get_json()["email"]["password"] == ""
    assert "pw" not in core.db.one("SELECT value FROM settings WHERE key='notifications'")["value"]
    assert core.manager.notifier.settings["email"]["password"] == "pw"
    r = auth_client.put("/api/settings/general", json={"compression_level": 42, "encrypt_default": False})
    assert r.get_json()["compression_level"] == 9 and r.get_json()["encrypt_default"] is False


def test_prune(auth_client, app):
    core = app.extensions["dockbare"]
    vdir = core.settings.backup_dir / "vol1"
    vdir.mkdir()
    import gzip
    for i, name in enumerate(["2024-01-01_00-00-00", "2024-02-01_00-00-00", "2024-03-01_00-00-00", "2024-04-01_00-00-00"]):
        (vdir / f"{name}.tar.gz").write_bytes(gzip.compress(make_tar()))
    core.manager.scan_backups()
    r = auth_client.post("/api/prune", json={"volume": "vol1", "keep_max": 2, "dry_run": True}).get_json()
    assert [d["filename"][:7] for d in r["deleted"]] == ["2024-02", "2024-01"] and r["kept"] == 2
    r = auth_client.post("/api/prune", json={"volume": "vol1", "keep_seconds": 1}).get_json()  # everything is older than 1s …
    assert len(r["deleted"]) == 3  # … but the newest is always kept
    assert [b["filename"][:7] for b in auth_client.get("/api/backups").get_json()] == ["2024-04"]


def test_reports_preview_and_send(auth_client):
    r = auth_client.get("/api/reports/preview?period=daily")
    assert r.status_code == 200 and "Dockbare daily report" in r.get_json()["text"]
    r = auth_client.post("/api/reports/send", json={"period": "weekly"})
    assert r.get_json()["status"] == "no channel enabled"
    assert len(auth_client.get("/api/reports").get_json()["history"]) == 1
    r = auth_client.put("/api/reports/settings", json={"daily": {"enabled": True, "time": "08:15"}, "channels": ["email"]})
    assert r.get_json()["daily"]["time"] == "08:15"
    assert auth_client.get("/api/reports").get_json()["next_runs"]["daily"]


def test_sessions_listing_and_revoke(auth_client, app):
    sec = auth_client.get("/api/security").get_json()
    assert len(sec["sessions"]) == 1 and sec["sessions"][0]["current"]
    other = app.test_client()
    other.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
    assert len(auth_client.get("/api/security").get_json()["sessions"]) == 2
    assert auth_client.delete("/api/security/sessions").get_json()["revoked"] == 1
    assert other.get("/api/overview").status_code == 401


def test_notification_settings_from_env(settings, fake_docker, monkeypatch):
    import dockbare.app as appmod
    from dockbare.config import load_settings

    monkeypatch.setenv("DOCKBARE_SMTP_HOST", "mail.example")
    monkeypatch.setenv("DOCKBARE_SMTP_PORT", "465")
    monkeypatch.setenv("DOCKBARE_SMTP_SECURITY", "ssl")
    monkeypatch.setenv("DOCKBARE_SMTP_PASSWORD", "env-secret")
    monkeypatch.setenv("DOCKBARE_SMTP_TO", "a@example,b@example")
    monkeypatch.setenv("DOCKBARE_NTFY_TOPIC", "env-topic")
    monkeypatch.setenv("DOCKBARE_NTFY_ENABLED", "false")
    s2 = load_settings()
    monkeypatch.setattr(appmod, "DockerCtl", lambda *a, **k: fake_docker)
    app = appmod.create_app(s2, start=True)
    try:
        c = app.test_client()
        c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"})
        c.environ_base["HTTP_X_REQUESTED_WITH"] = "Dockbare"
        n = c.get("/api/settings").get_json()["notifications"]
        assert n["email"]["enabled"] is True and n["email"]["host"] == "mail.example" and n["email"]["port"] == 465
        assert n["email"]["password_set"] is True and n["email"]["password"] == ""
        assert n["ntfy"]["enabled"] is False and n["ntfy"]["topic"] == "env-topic"
        assert "host" in n["env_locked"]["email"] and "topic" in n["env_locked"]["ntfy"]
        # UI cannot override env-locked values, but can still change the rest
        r = c.put("/api/settings/notifications", json={"email": {"host": "other", "from_addr": "me@example"}, "ntfy": {"enabled": True}})
        n = r.get_json()
        assert n["email"]["host"] == "mail.example" and n["email"]["from_addr"] == "me@example" and n["ntfy"]["enabled"] is False
        core = app.extensions["dockbare"]
        assert core.manager.notifier.settings["email"]["password"] == "env-secret"
        assert "env-secret" not in (core.db.get_setting("notifications") and str(core.db.get_setting("notifications")) or "")
    finally:
        app.extensions["dockbare"].stop()


def test_totp_qr_endpoint(auth_client, app):
    assert auth_client.get("/api/security/totp-qr").status_code == 404
    app.extensions["dockbare"].settings.totp_secret = "JBSWY3DPEHPK3PXP"
    r = auth_client.get("/api/security/totp-qr")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.data
    assert app.test_client().get("/api/security/totp-qr").status_code == 401


def test_totp_gui_enable_login_recovery_disable(auth_client, app):
    import pyotp

    core = app.extensions["dockbare"]
    sec = auth_client.get("/api/security").get_json()["totp"]
    assert sec == {"enabled": False, "source": None, "recovery_left": 0}
    d = auth_client.post("/api/security/totp/setup").get_json()
    assert d["secret"] and d["uri"].startswith("otpauth://") and "<svg" in d["qr_svg"]
    assert auth_client.post("/api/security/totp/enable", json={"code": "000000"}).status_code == 400
    assert not core.auth.totp_enabled
    r = auth_client.post("/api/security/totp/enable", json={"code": pyotp.TOTP(d["secret"]).now()})
    assert r.status_code == 200
    codes = r.get_json()["recovery_codes"]
    assert len(codes) == 8 and all(len(c) == 11 and c[5] == "-" for c in codes)
    assert core.auth.totp_enabled and core.auth.totp_source == "ui"
    assert "enc:v1:" in core.db.one("SELECT value FROM settings WHERE key='totp_secret'")["value"]
    # login now needs a code
    c2 = app.test_client()
    assert c2.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"}).status_code == 401
    assert c2.get("/api/auth/session").get_json()["totp_enabled"] is True
    import time
    time.sleep(0)
    assert c2.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234", "totp": pyotp.TOTP(d["secret"]).now()}).status_code == 200
    # recovery code works once
    c3 = app.test_client()
    assert c3.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234", "totp": codes[0]}).status_code == 200
    assert auth_client.get("/api/security").get_json()["totp"]["recovery_left"] == 7
    c4 = app.test_client()
    assert c4.post("/api/auth/login", json={"username": "admin", "password": "test-password-1234", "totp": codes[0]}).status_code == 401
    # regenerate recovery codes, then disable (needs password + code)
    r = auth_client.post("/api/security/totp/recovery", json={"code": codes[1]})
    assert r.status_code == 200 and len(r.get_json()["recovery_codes"]) == 8
    assert auth_client.post("/api/security/totp/disable", json={"password": "wrong", "code": codes[2]}).status_code == 401
    assert auth_client.post("/api/security/totp/disable", json={"password": "test-password-1234", "code": "123456"}).status_code == 401
    new_codes = r.get_json()["recovery_codes"]
    assert auth_client.post("/api/security/totp/disable", json={"password": "test-password-1234", "code": new_codes[0]}).status_code == 200
    assert not core.auth.totp_enabled
    assert app.test_client().post("/api/auth/login", json={"username": "admin", "password": "test-password-1234"}).status_code == 200


def test_totp_env_locks_gui(auth_client, app):
    app.extensions["dockbare"].settings.totp_secret = "JBSWY3DPEHPK3PXP"
    assert auth_client.get("/api/security").get_json()["totp"]["source"] == "env"
    assert auth_client.post("/api/security/totp/setup").status_code == 400
    assert auth_client.post("/api/security/totp/disable", json={"password": "test-password-1234", "code": "x"}).status_code == 400


def test_versioned_static_assets(client, auth_client, app):
    from dockbare import build_id

    b = build_id()
    html = app.test_client().get("/").data.decode()
    assert f'"/static/{b}/app.js"' not in html and f'"/static/{b}/login.js"' in html  # login page for anonymous
    html = auth_client.get("/").data.decode()
    assert f'"/static/{b}/app.js"' in html and f'"/static/{b}/styles.css"' in html
    r = client.get(f"/static/{b}/app.js")
    assert r.status_code == 200 and "immutable" in r.headers["Cache-Control"]
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache"
    assert client.get(f"/static/{b}/../../etc/passwd").status_code in (404, 400)
