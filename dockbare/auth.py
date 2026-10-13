"""Authentication: admin credentials, sessions, throttling, TOTP."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

from .config import Settings
from .db import Database, rows_to_dicts

log = logging.getLogger("dockbare.auth")

COOKIE_NAME = "dockbare_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "Dockbare"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Auth:
    def __init__(self, settings: Settings, db: Database, on_event=None, notify=None):
        self.settings = settings
        self.db = db
        self._event = on_event or (lambda *a, **k: None)
        self._notify = notify or (lambda *a, **k: None)
        self._hash: str | None = None
        if settings.admin_password_hash:
            self._hash = settings.admin_password_hash
        elif settings.admin_password:
            self._hash = generate_password_hash(settings.admin_password, method="scrypt")
        self._dummy_hash = generate_password_hash(secrets.token_hex(16), method="scrypt")
        self._totp = None
        if settings.totp_secret:
            import pyotp
            self._totp = pyotp.TOTP(settings.totp_secret.replace(" ", "").upper())

    @property
    def configured(self) -> bool:
        return self._hash is not None

    @property
    def totp_enabled(self) -> bool:
        return self._totp is not None

    # ---------------------------------------------------------------- throttle
    def lock_state(self, ip: str) -> tuple[int, float | None]:
        """Return (recent failures, locked_until)."""
        window = time.time() - self.settings.login_window_minutes * 60
        rows = self.db.query("SELECT ts FROM login_attempts WHERE ip=? AND success=0 AND ts>=? ORDER BY ts DESC", (ip, window))
        failures = len(rows)
        if failures < self.settings.login_max_attempts:
            return failures, None
        over = failures - self.settings.login_max_attempts
        lock_minutes = min(60, 2 ** over)
        locked_until = rows[0]["ts"] + lock_minutes * 60
        return failures, (locked_until if locked_until > time.time() else None)

    def _record(self, ip: str, username: str, success: bool) -> None:
        self.db.execute("INSERT INTO login_attempts(ts, ip, username, success) VALUES(?,?,?,?)", (time.time(), ip, username[:64], int(success)))

    # ------------------------------------------------------------------- login
    def login(self, username: str, password: str, totp_code: str | None, ip: str, user_agent: str) -> str:
        if not self.configured:
            raise AuthError("Authentication is not configured. Set DOCKBARE_ADMIN_PASSWORD in .env.", 503)
        failures, locked_until = self.lock_state(ip)
        if locked_until:
            wait = int(locked_until - time.time()) + 1
            raise AuthError(f"Too many failed attempts. Try again in {wait} seconds.", 429, retry_after=wait)
        ok_user = hmac.compare_digest(username.encode("utf-8"), self.settings.admin_user.encode("utf-8"))
        # Always run the hash check so timing is identical for unknown users.
        ok_pass = check_password_hash(self._hash if ok_user else self._dummy_hash, password or "")
        ok = ok_user and ok_pass
        if ok and self._totp is not None:
            code = (totp_code or "").replace(" ", "")
            if not (code.isdigit() and self._totp.verify(code, valid_window=1)):
                ok = False
                reason = "invalid one-time code"
            else:
                reason = ""
        else:
            reason = "invalid credentials"
        if not ok:
            self._record(ip, username, False)
            time.sleep(0.4)
            failures += 1
            self._event("warning", "auth.login_failed", f"Failed login for '{username[:32]}' from {ip} ({reason})", data={"ip": ip})
            if failures >= self.settings.login_max_attempts:
                self._event("error", "auth.lockout", f"IP {ip} locked out after {failures} failed logins", data={"ip": ip})
                self._notify("auth", "Login lockout", f"{ip} was locked out after {failures} failed login attempts for '{username[:32]}'.", "warning")
            raise AuthError("Invalid username, password or one-time code.", 401)
        self._record(ip, username, True)
        token = self._create_session(ip, user_agent)
        self._event("info", "auth.login", f"Admin logged in from {ip}", data={"ip": ip})
        return token

    # ---------------------------------------------------------------- sessions
    def _sign(self, sid: str) -> str:
        return hmac.new(self.settings.secret_key.encode("utf-8"), sid.encode("ascii"), hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _db_id(sid: str) -> str:
        return hashlib.sha256(sid.encode("ascii")).hexdigest()

    def _create_session(self, ip: str, user_agent: str) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("INSERT INTO sessions(id, created_at, last_seen, ip, user_agent) VALUES(?,?,?,?,?)",
                        (self._db_id(sid), now, now, ip, (user_agent or "")[:200]))
        return f"{sid}.{self._sign(sid)}"

    def session_from_token(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        sid, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(sid)):
            return None
        row = self.db.one("SELECT * FROM sessions WHERE id=?", (self._db_id(sid),))
        if not row:
            return None
        now = time.time()
        if now - row["last_seen"] > self.settings.session_idle_minutes * 60 or now - row["created_at"] > self.settings.session_max_hours * 3600:
            self.db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
            return None
        if now - row["last_seen"] > 60:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, row["id"]))
        d = dict(row)
        d["token_id"] = row["id"]
        return d

    def logout(self, token: str | None) -> None:
        if not token or "." not in token:
            return
        sid = token.rsplit(".", 1)[0]
        self.db.execute("DELETE FROM sessions WHERE id=?", (self._db_id(sid),))

    def revoke_all(self, except_db_id: str | None = None) -> int:
        if except_db_id:
            cur = self.db.execute("DELETE FROM sessions WHERE id<>?", (except_db_id,))
        else:
            cur = self.db.execute("DELETE FROM sessions")
        return cur.rowcount

    def revoke(self, db_id: str) -> bool:
        return self.db.execute("DELETE FROM sessions WHERE id=?", (db_id,)).rowcount > 0

    def list_sessions(self, current_db_id: str | None = None) -> list[dict]:
        rows = rows_to_dicts(self.db.query("SELECT id, created_at, last_seen, ip, user_agent FROM sessions ORDER BY last_seen DESC"))
        for r in rows:
            r["current"] = r["id"] == current_db_id
        return rows

    def recent_attempts(self, limit: int = 50) -> list[dict]:
        rows = rows_to_dicts(self.db.query("SELECT ts, ip, username, success FROM login_attempts ORDER BY ts DESC LIMIT ?", (limit,)))
        for r in rows:
            r["success"] = bool(r["success"])
        return rows
