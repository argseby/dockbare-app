"""Authentication: admin credentials, sessions, throttling, TOTP."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import string
import time

from werkzeug.security import check_password_hash, generate_password_hash

from .config import Settings
from .db import Database, rows_to_dicts

log = logging.getLogger("dockbare.auth")

COOKIE_NAME = "dockbare_session"
WEAK_PASSWORDS = {"password", "admin", "dockbare", "changeme", "change-me", "secret", "letmein", "backup", "123456", "12345678",
                  "qwerty", "admin123", "password1", "change-me-to-a-long-random-password"}
GLOBAL_FAILURE_LIMIT = 30  # failures from all IPs within the window before every attempt is slowed down
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "Dockbare"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class Auth:
    def __init__(self, settings: Settings, db: Database, on_event=None, notify=None, secretbox=None):
        self.settings = settings
        self.db = db
        self.secretbox = secretbox
        self._event = on_event or (lambda *a, **k: None)
        self._notify = notify or (lambda *a, **k: None)
        self._hash: str | None = None
        self.weak_password: str | None = None
        if settings.admin_password_hash:
            self._hash = settings.admin_password_hash
        elif settings.admin_password:
            self._hash = generate_password_hash(settings.admin_password, method="scrypt")
            pw = settings.admin_password
            if pw.lower() in WEAK_PASSWORDS or pw.lower() == settings.admin_user.lower():
                self.weak_password = "the admin password is a common default"
            elif len(pw) < 12:
                self.weak_password = "the admin password is shorter than 12 characters"
            elif len(set(pw)) < 5:
                self.weak_password = "the admin password has too little variety"
            if self.weak_password:
                log.warning("SECURITY: %s. Set a long random DOCKBARE_ADMIN_PASSWORD (python -m dockbare gen-secret).", self.weak_password)
        self._dummy_hash = generate_password_hash(secrets.token_hex(16), method="scrypt")

    @property
    def configured(self) -> bool:
        return self._hash is not None

    # ------------------------------------------------------------------ TOTP
    def _db_secret(self, key: str) -> str | None:
        val = self.db.get_setting(key)
        if not val:
            return None
        return self.secretbox.decrypt(val) if self.secretbox else val

    def totp_secret(self) -> str | None:
        if self.settings.totp_secret:
            return self.settings.totp_secret.replace(" ", "").upper()
        sec = self._db_secret("totp_secret")
        return sec.replace(" ", "").upper() if sec else None

    @property
    def totp_source(self) -> str | None:
        if self.settings.totp_secret:
            return "env"
        return "ui" if self.db.get_setting("totp_secret") else None

    @property
    def totp_enabled(self) -> bool:
        return self.totp_secret() is not None

    def _totp(self):
        import pyotp

        sec = self.totp_secret()
        return pyotp.TOTP(sec) if sec else None

    def verify_password(self, password: str) -> bool:
        return self.configured and check_password_hash(self._hash, password or "")

    def verify_code(self, code: str, secret: str | None = None, consume_recovery: bool = True) -> str | None:
        """Return 'totp' / 'recovery' when the code is valid for the active (or given) secret, else None."""
        import pyotp

        code = (code or "").replace(" ", "").replace("-", "").strip()
        sec = secret or self.totp_secret()
        if sec and code.isdigit() and len(code) == 6:
            if not pyotp.TOTP(sec).verify(code, valid_window=1):
                return None
            if secret is None:  # replay protection applies to real logins, not to the setup confirmation
                last = self.db.get_setting("totp_last_used") or {}
                if last.get("code") == code and time.time() - float(last.get("ts") or 0) < 120:
                    return None
                self.db.set_setting("totp_last_used", {"code": code, "ts": time.time()})
            return "totp"
        if secret is None and len(code) >= 8:
            hashes = list(self.db.get_setting("totp_recovery") or [])
            digest = hashlib.sha256(code.lower().encode("utf-8")).hexdigest()
            for h in hashes:
                if hmac.compare_digest(h, digest):
                    if consume_recovery:
                        hashes.remove(h)
                        self.db.set_setting("totp_recovery", hashes)
                        self._event("warning", "auth.recovery_code_used", f"A recovery code was used to log in; {len(hashes)} left")
                    return "recovery"
        return None

    def recovery_codes_left(self) -> int:
        return len(self.db.get_setting("totp_recovery") or [])

    def totp_begin_setup(self) -> dict:
        """Generate a pending secret for the GUI wizard (not active until confirmed)."""
        import pyotp

        if self.totp_source == "env":
            raise AuthError("Two-factor is configured through DOCKBARE_TOTP_SECRET; manage it in .env.", 400)
        secret = pyotp.random_base32()
        self.db.set_setting("totp_pending", {"secret": self.secretbox.encrypt(secret) if self.secretbox else secret, "ts": time.time()})
        return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(name=self.settings.admin_user, issuer_name="Dockbare")}

    def _new_recovery_codes(self, n: int = 8) -> list[str]:
        alphabet = string.ascii_lowercase + string.digits
        codes = ["".join(secrets.choice(alphabet) for _ in range(10)) for _ in range(n)]
        self.db.set_setting("totp_recovery", [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in codes])
        return [f"{c[:5]}-{c[5:]}" for c in codes]

    def totp_confirm_setup(self, code: str) -> list[str]:
        pend = self.db.get_setting("totp_pending") or {}
        if not pend.get("secret") or time.time() - float(pend.get("ts") or 0) > 1800:
            raise AuthError("No pending two-factor setup (or it expired); start again.", 400)
        secret = self.secretbox.decrypt(pend["secret"]) if self.secretbox else pend["secret"]
        if self.verify_code(code, secret=secret) is None:
            raise AuthError("The code does not match. Check the time on your phone and try the next code.", 400)
        self.db.set_setting("totp_secret", pend["secret"])
        self.db.set_setting("totp_pending", None)
        codes = self._new_recovery_codes()
        self._event("warning", "auth.totp_enabled", "Two-factor authentication enabled from the UI")
        self._notify("auth", "Two-factor enabled", "TOTP two-factor login was enabled for the Dockbare admin account.", "info")
        return codes

    def totp_disable(self, password: str, code: str) -> None:
        if self.totp_source == "env":
            raise AuthError("Two-factor is configured through DOCKBARE_TOTP_SECRET; remove it from .env.", 400)
        if not self.verify_password(password):
            time.sleep(0.4)
            raise AuthError("Wrong password.", 401)
        if self.verify_code(code, consume_recovery=False) is None:
            time.sleep(0.4)
            raise AuthError("Invalid one-time or recovery code.", 401)
        self.db.set_setting("totp_secret", None)
        self.db.set_setting("totp_recovery", [])
        self._event("warning", "auth.totp_disabled", "Two-factor authentication disabled from the UI")
        self._notify("auth", "Two-factor disabled", "TOTP two-factor login was disabled for the Dockbare admin account.", "warning")

    def totp_regenerate_recovery(self, code: str) -> list[str]:
        if not self.totp_enabled:
            raise AuthError("Two-factor is not enabled.", 400)
        if self.verify_code(code, consume_recovery=False) is None:
            time.sleep(0.4)
            raise AuthError("Invalid one-time or recovery code.", 401)
        codes = self._new_recovery_codes()
        self._event("warning", "auth.recovery_codes_regenerated", "Recovery codes regenerated")
        return codes

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

    def global_failures(self) -> int:
        window = time.time() - self.settings.login_window_minutes * 60
        return int(self.db.one("SELECT COUNT(*) AS n FROM login_attempts WHERE success=0 AND ts>=?", (window,))["n"])

    # ------------------------------------------------------------------- login
    def login(self, username: str, password: str, totp_code: str | None, ip: str, user_agent: str) -> str:
        if not self.configured:
            raise AuthError("Authentication is not configured. Set DOCKBARE_ADMIN_PASSWORD in .env.", 503)
        failures, locked_until = self.lock_state(ip)
        if locked_until:
            wait = int(locked_until - time.time()) + 1
            raise AuthError(f"Too many failed attempts. Try again in {wait} seconds.", 429, retry_after=wait)
        global_fail = self.global_failures()
        if global_fail >= GLOBAL_FAILURE_LIMIT:
            # Distributed guessing: slow every attempt down (no lockout, so the admin can still get in).
            time.sleep(min(5.0, 0.5 * (1 + (global_fail - GLOBAL_FAILURE_LIMIT) // 10)))
        ok_user = hmac.compare_digest(username.encode("utf-8"), self.settings.admin_user.encode("utf-8"))
        # Always run the hash check so timing is identical for unknown users.
        ok_pass = check_password_hash(self._hash if ok_user else self._dummy_hash, password or "")
        ok = ok_user and ok_pass
        if ok and self.totp_enabled:
            how = self.verify_code(totp_code or "")
            if how is None:
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
