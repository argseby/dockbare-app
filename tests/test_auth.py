import time

from dockbare.auth import Auth, AuthError
from dockbare.db import Database


def _auth(settings):
    return Auth(settings, Database(settings.data_dir / "t.db"))


def test_session_lifecycle(settings):
    a = _auth(settings)
    token = a.login("admin", "test-password-1234", None, "1.2.3.4", "ua")
    s = a.session_from_token(token)
    assert s and s["ip"] == "1.2.3.4"
    assert a.session_from_token(token[:-1] + ("a" if token[-1] != "a" else "b")) is None  # bad signature
    assert a.session_from_token("garbage") is None
    # idle expiry
    a.db.execute("UPDATE sessions SET last_seen=?", (time.time() - settings.session_idle_minutes * 60 - 1,))
    assert a.session_from_token(token) is None


def test_lockout_escalates(settings):
    a = _auth(settings)
    for _ in range(3):
        try:
            a.login("admin", "x", None, "9.9.9.9", "ua")
        except AuthError as e:
            assert e.status == 401
    failures, locked = a.lock_state("9.9.9.9")
    assert failures == 3 and locked and locked - time.time() <= 60
    try:
        a.login("admin", "test-password-1234", None, "9.9.9.9", "ua")
        raise AssertionError("should be locked")
    except AuthError as e:
        assert e.status == 429 and e.retry_after
    # other IPs unaffected
    assert a.login("admin", "test-password-1234", None, "8.8.8.8", "ua")


def test_unknown_user_same_error(settings):
    a = _auth(settings)
    try:
        a.login("root", "test-password-1234", None, "1.1.1.1", "ua")
        raise AssertionError
    except AuthError as e:
        assert "Invalid" in str(e)


def test_totp_required(settings, monkeypatch):
    import pyotp

    settings.totp_secret = pyotp.random_base32()
    a = _auth(settings)
    try:
        a.login("admin", "test-password-1234", "000000", "1.1.1.1", "ua")
        raise AssertionError
    except AuthError:
        pass
    code = pyotp.TOTP(settings.totp_secret).now()
    assert a.login("admin", "test-password-1234", code, "1.1.1.1", "ua")


def test_password_hash_env(settings):
    from werkzeug.security import generate_password_hash

    settings.admin_password = None
    settings.admin_password_hash = generate_password_hash("hashed-pw", method="scrypt")
    a = _auth(settings)
    assert a.login("admin", "hashed-pw", None, "1.1.1.1", "ua")
