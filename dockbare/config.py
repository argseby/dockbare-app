"""Configuration from environment / .env.

Every deployment-level setting (paths, credentials, keys) lives in the
environment, loaded from a ``.env`` file when present. Runtime settings that an
admin changes in the UI live in the database instead (see ``db.py``).
"""
from __future__ import annotations

import os
import secrets
import logging
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("dockbare.config")

ENV_PREFIX = "DOCKBARE_"

# Notification channels can be configured from the environment; env values take
# precedence over the UI and are shown as locked there.
NOTIFY_ENV_MAP = {
    "email": {"SMTP_HOST": "host", "SMTP_PORT": "port", "SMTP_SECURITY": "security", "SMTP_USER": "username",
              "SMTP_PASSWORD": "password", "SMTP_FROM": "from_addr", "SMTP_TO": "to_addrs", "SMTP_ENABLED": "enabled"},
    "ntfy": {"NTFY_URL": "base_url", "NTFY_TOPIC": "topic", "NTFY_TOKEN": "token", "NTFY_ENABLED": "enabled"},
    "webhook": {"WEBHOOK_URL": "url", "WEBHOOK_SECRET": "secret", "WEBHOOK_ENABLED": "enabled"},
}


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(ENV_PREFIX + name)
    if val is None or val == "":
        return default
    return val


def _env_bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        log.warning("Invalid integer for %s%s=%r, using %s", ENV_PREFIX, name, val, default)
        return default


@dataclass
class Settings:
    data_dir: Path
    backup_dir: Path
    log_dir: Path

    host: str
    port: int
    behind_proxy: bool
    secure_cookies: str  # auto | true | false

    admin_user: str
    admin_password: str | None
    admin_password_hash: str | None
    totp_secret: str | None
    secret_key: str

    encryption_key: str | None
    peer_token: str | None
    instance_name: str | None

    session_idle_minutes: int
    session_max_hours: int
    login_max_attempts: int
    login_window_minutes: int

    helper_image: str | None
    timezone: str
    log_level: str
    env_file: Path | None = None
    generated_secret: bool = field(default=False)
    notify_env: dict = field(default_factory=dict)
    env_remotes: list = field(default_factory=list)
    env_peers: list = field(default_factory=list)
    testing: bool = False

    # --- derived -----------------------------------------------------------
    @property
    def auth_configured(self) -> bool:
        return bool(self.admin_password or self.admin_password_hash)

    @property
    def encryption_configured(self) -> bool:
        return bool(self.encryption_key)

    @property
    def totp_enabled(self) -> bool:
        return bool(self.totp_secret)

    def public_dict(self) -> dict:
        """Non-secret view used by the API."""
        return {
            "data_dir": str(self.data_dir),
            "backup_dir": str(self.backup_dir),
            "behind_proxy": self.behind_proxy,
            "secure_cookies": self.secure_cookies,
            "admin_user": self.admin_user,
            "totp_enabled": self.totp_enabled,
            "encryption_configured": self.encryption_configured,
            "session_idle_minutes": self.session_idle_minutes,
            "session_max_hours": self.session_max_hours,
            "login_max_attempts": self.login_max_attempts,
            "login_window_minutes": self.login_window_minutes,
            "helper_image": self.helper_image,
            "timezone": self.timezone,
            "env_file": str(self.env_file) if self.env_file else None,
            "generated_secret": self.generated_secret,
            "notify_env_keys": {k: sorted(v.keys()) for k, v in self.notify_env.items()},
            "peer_token_from_env": bool(self.peer_token),
            "instance_name_from_env": bool(self.instance_name),
            "env_remotes": len(self.env_remotes),
            "env_peers": len(self.env_peers),
        }


REMOTE_ENV_FIELDS = {
    "NAME": "name", "TYPE": "type", "HOST": "host", "PORT": "port", "USER": "username", "PASSWORD": "password",
    "PATH": "base_path", "TLS": "tls", "PASSIVE": "passive", "NAT_FIX": "nat_pasv_fix", "VERIFY_TLS": "verify_tls",
    "PRIVATE_KEY": "private_key", "PRIVATE_KEY_FILE": "private_key_file", "KEY_PASSPHRASE": "key_passphrase",
    "HOST_KEY": "host_key_fingerprint", "INSTANCE_DIR": "instance_dir", "ENABLED": "enabled",
}
_BOOL_FIELDS = {"passive", "nat_pasv_fix", "verify_tls", "instance_dir", "enabled"}


def _remotes_from_env() -> list[dict]:
    """``DOCKBARE_REMOTE_<n>_<FIELD>`` blocks (n = 1..9) → remote definitions."""
    out = []
    for n in range(1, 10):
        raw: dict = {}
        for env_key, field_name in REMOTE_ENV_FIELDS.items():
            val = _env(f"REMOTE_{n}_{env_key}")
            if val is not None:
                raw[field_name] = val
        if not raw.get("host"):
            continue
        rtype = (raw.get("type") or "ftp").lower()
        cfg: dict = {"host": raw["host"].strip(), "base_path": raw.get("base_path") or "/backups",
                     "username": raw.get("username") or "", "password": raw.get("password") or ""}
        try:
            cfg["port"] = int(raw.get("port") or (21 if rtype == "ftp" else 22))
        except ValueError:
            cfg["port"] = 21 if rtype == "ftp" else 22
        for f in _BOOL_FIELDS:
            if f in raw:
                cfg[f] = raw[f].strip().lower() in ("1", "true", "yes", "on")
        cfg.setdefault("instance_dir", True)
        if rtype == "ftp":
            tls = (raw.get("tls") or "explicit").lower()
            cfg["secure"] = tls != "none"
            cfg["tls_mode"] = "implicit" if tls == "implicit" else "explicit"
            cfg.setdefault("passive", True)
            cfg.setdefault("verify_tls", True)
        else:
            key = raw.get("private_key") or ""
            if raw.get("private_key_file"):
                try:
                    key = Path(raw["private_key_file"]).read_text(encoding="utf-8")
                except OSError as e:
                    log.warning("Cannot read DOCKBARE_REMOTE_%d_PRIVATE_KEY_FILE: %s", n, e)
            cfg["private_key"] = key.replace("\\n", "\n")
            cfg["key_passphrase"] = raw.get("key_passphrase") or ""
            cfg["host_key_fingerprint"] = raw.get("host_key_fingerprint") or ""
        out.append({"index": n, "name": raw.get("name") or f"Remote {n} ({cfg['host']})", "type": rtype,
                    "enabled": cfg.pop("enabled", True), "config": cfg})
    return out


PEER_ENV_FIELDS = {"NAME": "name", "URL": "url", "TOKEN": "token", "INTERVAL": "interval_minutes", "THRESHOLD": "failure_threshold",
                   "ALERT_ON_PROBLEMS": "alert_on_problems", "VERIFY_TLS": "verify_tls", "ENABLED": "enabled", "TIMEOUT": "timeout"}


def _peers_from_env() -> list[dict]:
    """``DOCKBARE_PEER_<n>_<FIELD>`` blocks (n = 1..9) → peer definitions."""
    out = []
    for n in range(1, 10):
        raw: dict = {}
        for env_key, field_name in PEER_ENV_FIELDS.items():
            val = _env(f"PEER_{n}_{env_key}")
            if val is not None:
                raw[field_name] = val
        if not raw.get("url"):
            continue
        d: dict = {"index": n, "name": raw.get("name") or f"Peer {n}", "url": raw["url"].strip(), "token": raw.get("token") or ""}
        for k in ("interval_minutes", "failure_threshold", "timeout"):
            if raw.get(k):
                try:
                    d[k] = int(raw[k])
                except ValueError:
                    pass
        for k in ("alert_on_problems", "verify_tls", "enabled"):
            if k in raw:
                d[k] = raw[k].strip().lower() in ("1", "true", "yes", "on")
        out.append(d)
    return out


def _notify_from_env() -> dict:
    out: dict[str, dict] = {}
    for section, keys in NOTIFY_ENV_MAP.items():
        sec: dict = {}
        for env_key, field_name in keys.items():
            val = _env(env_key)
            if val is None:
                continue
            if field_name == "enabled":
                sec[field_name] = val.strip().lower() in ("1", "true", "yes", "on")
            elif field_name == "port":
                try:
                    sec[field_name] = int(val)
                except ValueError:
                    log.warning("Invalid port in %s%s=%r", ENV_PREFIX, env_key, val)
            else:
                sec[field_name] = val
        if sec:
            sec.setdefault("enabled", True)  # configured through env → on unless *_ENABLED=false
            out[section] = sec
    return out


def _find_env_file() -> Path | None:
    explicit = os.environ.get(ENV_PREFIX + "ENV_FILE")
    if explicit is not None:  # explicit (possibly empty) path disables auto-discovery
        p = Path(explicit) if explicit else None
        return p if p and p.is_file() else None
    for c in (Path.cwd() / ".env", Path("/app/.env"), Path("/data/.env")):
        if c.is_file():
            return c
    return None


def _load_or_create_secret(data_dir: Path) -> tuple[str, bool]:
    """Return (secret, generated)."""
    key = _env("SECRET_KEY")
    if key:
        if len(key) < 32:
            log.warning("DOCKBARE_SECRET_KEY is short (<32 chars); use `python -m dockbare gen-secret`.")
        return key, False
    path = data_dir / "secret_key"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing, True
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
    generated = secrets.token_urlsafe(48)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(generated)
        log.warning("No DOCKBARE_SECRET_KEY set; generated one at %s. Put it into .env to keep sessions across reinstalls.", path)
    except OSError as e:
        log.warning("Could not persist generated secret key (%s); sessions will reset on restart.", e)
    return generated, True


def load_settings() -> Settings:
    env_file = _find_env_file()
    if env_file:
        # Real environment variables win over the .env file.
        load_dotenv(env_file, override=False)

    data_dir = Path(_env("DATA_DIR", "/data")).expanduser().resolve()
    backup_dir = Path(_env("BACKUP_DIR", "/backups")).expanduser().resolve()
    log_dir = Path(_env("LOG_DIR", str(data_dir / "logs"))).expanduser().resolve()
    for d in (data_dir, backup_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    secret, generated = _load_or_create_secret(data_dir)

    timezone = _env("TIMEZONE") or os.environ.get("TZ") or "UTC"

    return Settings(
        data_dir=data_dir,
        backup_dir=backup_dir,
        log_dir=log_dir,
        host=_env("LISTEN", "0.0.0.0"),  # DOCKBARE_HOST is the public host name (compose/Traefik), not the bind address
        port=_env_int("PORT", 5000),
        behind_proxy=_env_bool("BEHIND_PROXY", False),
        secure_cookies=(_env("SECURE_COOKIES", "auto") or "auto").lower(),
        admin_user=_env("ADMIN_USER", "admin"),
        admin_password=_env("ADMIN_PASSWORD"),
        admin_password_hash=_env("ADMIN_PASSWORD_HASH"),
        totp_secret=_env("TOTP_SECRET"),
        secret_key=secret,
        encryption_key=_env("ENCRYPTION_KEY"),
        peer_token=_env("PEER_TOKEN"),
        instance_name=_env("INSTANCE_NAME"),
        session_idle_minutes=_env_int("SESSION_IDLE_MINUTES", 12 * 60),
        session_max_hours=_env_int("SESSION_MAX_HOURS", 7 * 24),
        login_max_attempts=_env_int("LOGIN_MAX_ATTEMPTS", 5),
        login_window_minutes=_env_int("LOGIN_WINDOW_MINUTES", 15),
        helper_image=_env("HELPER_IMAGE"),
        timezone=timezone,
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        env_file=env_file,
        generated_secret=generated,
        notify_env=_notify_from_env(),
        env_remotes=_remotes_from_env(),
        env_peers=_peers_from_env(),
        testing=_env_bool("TESTING", False),
    )
