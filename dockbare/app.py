"""Flask application factory and wiring of all components."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, build_id, crypto
from .api import api
from .auth import Auth, COOKIE_NAME
from .config import Settings, load_settings
from .db import Database
from .dockerctl import DockerCtl
from .manager import Manager
from .peers import Peers
from .uptime import Uptime
from .configsync import ConfigSync
from .recovery import Recovery
from .reports import Reports
from .tasks import TaskManager

STATIC_DIR = Path(__file__).parent / "static"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _setup_logging(settings: Settings) -> str:
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    log_file = str(settings.log_dir / "dockbare.log")
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.WARNING)
    return log_file


def _load_app_salt(settings: Settings) -> bytes:
    p = settings.data_dir / "encryption_salt"
    try:
        if p.is_file():
            raw = bytes.fromhex(p.read_text(encoding="utf-8").strip())
            if len(raw) == 16:
                return raw
    except (OSError, ValueError):
        pass
    raw = os.urandom(16)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw.hex())
    except OSError as e:
        logging.getLogger("dockbare").warning("Could not persist encryption salt: %s", e)
    return raw


class Core:
    """All long-lived components, attached to ``app.extensions['dockbare']``."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.log_file = _setup_logging(settings)
        self.db = Database(settings.data_dir / "dockbare.db")
        self.docker = DockerCtl(settings.helper_image)
        self.keyring = crypto.Keyring(settings.encryption_key, app_salt=_load_app_salt(settings))
        self.secretbox = crypto.SecretBox(settings.secret_key)
        self.tasks = TaskManager(workers=6)
        self.manager = Manager(settings, self.db, self.docker, self.keyring, self.tasks, self.secretbox)
        self.recovery = Recovery(self.manager)
        self.reports = Reports(self.manager)
        self.peers = Peers(self.manager)
        self.manager.peers = self.peers
        self.uptime = Uptime(self.manager)
        self.manager.uptime = self.uptime
        self.configsync = ConfigSync(self.manager)
        self.manager.configsync = self.configsync
        self.auth = Auth(settings, self.db, on_event=self.manager.event, notify=self.manager.notifier.notify, secretbox=self.secretbox)
        self.manager.auth = self.auth
        self.cookie_name = COOKIE_NAME
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        log = logging.getLogger("dockbare")
        log.info("Dockbare %s build %s starting (data=%s, backups=%s)", __version__, build_id(), self.settings.data_dir, self.settings.backup_dir)
        if not self.auth.configured:
            log.error("No admin password configured! Set DOCKBARE_ADMIN_PASSWORD (or _HASH) in .env. All access is blocked until then.")
        if not self.keyring.available:
            log.warning("No DOCKBARE_ENCRYPTION_KEY set: backups will be stored and uploaded unencrypted.")
        self.manager.start()
        self.reports.install_jobs()
        self.peers.install_job()
        self.uptime.start(background=not self.settings.testing)
        self.configsync.install_job()

    def stop(self) -> None:
        self.configsync.stop()
        self.uptime.stop()
        self.manager.stop()
        self.tasks.shutdown()
        self.db.close()

    # cookies -----------------------------------------------------------------
    def _secure_flag(self) -> bool:
        mode = self.settings.secure_cookies
        if mode == "true":
            return True
        if mode == "false":
            return False
        return bool(request.is_secure)

    def set_session_cookie(self, resp: Response, token: str) -> None:
        resp.set_cookie(self.cookie_name, token, max_age=self.settings.session_max_hours * 3600, httponly=True,
                        secure=self._secure_flag(), samesite="Strict", path="/")

    def clear_session_cookie(self, resp: Response) -> None:
        resp.delete_cookie(self.cookie_name, path="/", httponly=True, secure=self._secure_flag(), samesite="Strict")


def create_app(settings: Settings | None = None, start: bool = True) -> Flask:
    os.umask(0o077)  # database, logs, archives and snapshots are created owner-only
    settings = settings or load_settings()
    app = Flask("dockbare", static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024 * 1024  # 64 GiB uploads
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = settings.secret_key
    if settings.behind_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    core = Core(settings)
    app.extensions["dockbare"] = core

    @app.before_request
    def _load_session():
        g.session = core.auth.session_from_token(request.cookies.get(core.cookie_name))

    _build_prefix = f"/static/{build_id()}/"

    @app.after_request
    def _headers(resp: Response):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.path.startswith("/api/") or request.path in ("/", "/login"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.startswith("/static/") and not request.path.startswith(_build_prefix):
            resp.headers["Cache-Control"] = "no-cache"  # unversioned path: always revalidate
        if request.is_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    app.register_blueprint(api, url_prefix="/api")

    build = build_id()
    static_prefix = f"/static/{build}/"

    def page(name: str, status: int = 200) -> Response:
        """Serve an HTML page with build-versioned asset URLs (immune to proxy/browser caching)."""
        html = (STATIC_DIR / name).read_text(encoding="utf-8").replace('"/static/', '"' + static_prefix)
        resp = Response(html, status=status, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/static/<build_tag>/<path:filename>")
    def versioned_static(build_tag, filename):
        resp = send_from_directory(STATIC_DIR, filename, max_age=31536000)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if build_tag == build else "no-cache"
        return resp

    def status_page(status_ok: int = 200):
        if core.uptime.settings().get("public_enabled"):
            return page("uptime.html", status_ok)
        if g.get("session"):
            return redirect("/admin", code=302)
        return page("status-disabled.html", 404)

    @app.get("/")
    def index():
        """Public status page (or the 'not enabled' note); the app lives under /admin."""
        return status_page()

    @app.get("/uptime")
    def uptime_page():
        return status_page()

    @app.get("/admin")
    def admin():
        return page("index.html" if g.get("session") else "login.html")

    @app.get("/login")
    def login_page():
        if g.get("session"):
            return redirect("/admin", code=302)
        return page("login.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "version": __version__, "build": build_id()})

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        if g.get("session") or request.path.startswith("/admin"):
            return page("index.html" if g.get("session") else "login.html", 404)
        public = core.uptime.settings().get("public_enabled")
        return page("uptime.html" if public else "status-disabled.html", 404)

    @app.errorhandler(413)
    def _413(_e):
        return jsonify({"error": "Upload too large"}), 413

    if start:
        core.start()
    return app
