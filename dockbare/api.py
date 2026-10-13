"""JSON API."""
from __future__ import annotations

import logging
import os
import time
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request, send_file, stream_with_context

from . import __version__, archive
from .auth import AuthError, CSRF_HEADER, CSRF_VALUE
from .crypto import CryptoError
from .dockerctl import DockerError, valid_volume_name
from .manager import ManagerError
from .notify import NOTIFY_EVENTS
from .remotes import RemoteError

log = logging.getLogger("dockbare.api")
api = Blueprint("api", __name__)

PUBLIC = {"api.health", "api.login", "api.session", "api.peer_status", "api.uptime_public"}


def core():
    return current_app.extensions["dockbare"]


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def err(message: str, status: int = 400, **extra):
    payload = {"error": message, **extra}
    return jsonify(payload), status


@api.before_request
def _require_auth():
    if request.method == "OPTIONS":
        return None
    if request.endpoint in PUBLIC:
        return None
    if g.get("session") is None:
        return err("Authentication required.", 401)
    if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return err("Missing CSRF header.", 403)
    return None


@api.errorhandler(ManagerError)
@api.errorhandler(CryptoError)
def _bad_request(e):
    return err(str(e), 400)


@api.errorhandler(DockerError)
@api.errorhandler(RemoteError)
def _upstream(e):
    return err(str(e), 502)


@api.errorhandler(AuthError)
def _auth_error(e: AuthError):
    resp = err(str(e), e.status)
    if e.retry_after:
        resp[0].headers["Retry-After"] = str(e.retry_after)
    return resp


@api.errorhandler(Exception)
def _unexpected(e):
    log.exception("Unhandled API error")
    return err("Internal error: " + (str(e) or e.__class__.__name__), 500)


# ---------------------------------------------------------------------- auth
@api.get("/health")
def health():
    return jsonify({"ok": True, "app": "dockbare", "version": __version__})


@api.post("/auth/login")
def login():
    c = core()
    data = body()
    username = str(data.get("username") or "")[:128]
    password = str(data.get("password") or "")[:1024]
    totp = str(data.get("totp") or "")[:16]
    token = c.auth.login(username, password, totp, request.remote_addr or "?", request.user_agent.string)
    resp = jsonify({"ok": True})
    c.set_session_cookie(resp, token)
    return resp


@api.post("/auth/logout")
def logout():
    c = core()
    c.auth.logout(request.cookies.get(c.cookie_name))
    resp = jsonify({"ok": True})
    c.clear_session_cookie(resp)
    return resp


@api.get("/auth/session")
def session():
    c = core()
    s = g.get("session")
    return jsonify({
        "authenticated": s is not None,
        "user": c.settings.admin_user if s else None,
        "auth_configured": c.auth.configured,
        "totp_enabled": c.auth.totp_enabled,
        "version": __version__,
    })


# ------------------------------------------------------------------ overview
@api.get("/overview")
def overview():
    return jsonify(core().manager.overview())


@api.get("/volumes")
def volumes():
    return jsonify(core().manager.volumes_overview())


# ------------------------------------------------------------------- backups
@api.get("/backups")
def backups():
    volume = request.args.get("volume") or None
    return jsonify(core().manager.list_backups(volume))


@api.post("/backups")
def create_backup():
    data = body()
    vols = data.get("volumes") or ([data["volume"]] if data.get("volume") else [])
    if not vols or not all(isinstance(v, str) for v in vols):
        return err("At least one volume is required.")
    remotes = data.get("remotes")
    if remotes is not None and not isinstance(remotes, list):
        return err("remotes must be a list.")
    tasks = core().manager.start_backup(
        vols, remotes=remotes, encrypt=data.get("encrypt"), stop_containers=data.get("stop_containers"))
    return jsonify({"tasks": [t.to_dict() for t in tasks]})


@api.post("/backups/restore")
def restore_backup():
    data = body()
    if not data.get("path") or not data.get("volume"):
        return err("path and volume are required.")
    t = core().manager.start_restore(str(data["path"]), str(data["volume"]))
    return jsonify({"task": t.to_dict()})


@api.post("/backups/verify")
def verify_backup():
    data = body()
    if not data.get("path"):
        return err("path is required.")
    t = core().manager.start_verify(str(data["path"]))
    return jsonify({"task": t.to_dict()})


@api.post("/backups/push")
def push_backup():
    data = body()
    if not data.get("path") or not isinstance(data.get("remotes"), list) or not data["remotes"]:
        return err("path and remotes are required.")
    t = core().manager.start_push(str(data["path"]), [str(r) for r in data["remotes"]])
    return jsonify({"task": t.to_dict()})


@api.delete("/backups")
def delete_backup():
    data = body()
    if not data.get("path"):
        return err("path is required.")
    return jsonify(core().manager.delete_backup(str(data["path"]), delete_remote=bool(data.get("delete_remote"))))


@api.get("/backups/download")
def download_backup():
    c = core()
    p = c.manager.resolve_backup_path(request.args.get("path") or "")
    decrypt = request.args.get("decrypt") in ("1", "true")
    if decrypt and archive.is_encrypted_file(p):
        if not c.keyring.available:
            return err("No encryption key configured.")
        name = p.name[: -len(archive.ENC_EXT)] + archive.PLAIN_EXT
        gen = archive.open_decrypted_stream(p, c.keyring)
        c.manager.event("info", "backup.downloaded", f"Downloaded {p.name} (decrypted)", volume=p.parent.name, backup=p.name)
        return Response(stream_with_context(gen), mimetype="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{p.parent.name}_{name}"'})
    c.manager.event("info", "backup.downloaded", f"Downloaded {p.name}", volume=p.parent.name, backup=p.name)
    return send_file(p, as_attachment=True, download_name=f"{p.parent.name}_{p.name}", mimetype="application/octet-stream")


@api.post("/backups/upload")
def upload_backup():
    volume = request.form.get("volume") or ""
    f = request.files.get("file")
    if not f:
        return err("file is required.")
    return jsonify(core().manager.save_uploaded_backup(volume, f))


@api.get("/backups/events")
def backup_events():
    c = core()
    p = c.manager.resolve_backup_path(request.args.get("path") or "", must_exist=False)
    rows = c.manager.list_events(limit=200, q=p.name)
    return jsonify(rows)


@api.post("/prune")
def prune():
    data = body()
    volume = str(data.get("volume") or "")
    if not valid_volume_name(volume):
        return err("volume is required.")
    res = core().manager.prune(
        volume, keep_seconds=int(data.get("keep_seconds") or 0), keep_max=int(data.get("keep_max") or 0),
        dry_run=bool(data.get("dry_run")), delete_remote=bool(data.get("delete_remote")))
    return jsonify(res)


# ----------------------------------------------------------------- schedules
@api.get("/schedules")
def schedules():
    return jsonify(core().manager.list_schedules())


@api.post("/schedules")
def add_schedule():
    data = body()
    volume = str(data.get("volume") or "")
    if not valid_volume_name(volume):
        return err("volume is required.")
    if not core().docker.volume_exists(volume):
        return err(f"Volume '{volume}' does not exist.")
    return jsonify(core().manager.save_schedule(volume, data))


@api.put("/schedules/<volume>")
def update_schedule(volume):
    if not core().manager.get_schedule(volume):
        return err("Schedule not found.", 404)
    return jsonify(core().manager.save_schedule(volume, body()))


@api.delete("/schedules/<volume>")
def delete_schedule(volume):
    core().manager.delete_schedule(volume)
    return jsonify({"ok": True})


@api.post("/schedules/<volume>/toggle")
def toggle_schedule(volume):
    return jsonify(core().manager.toggle_schedule(volume, bool(body().get("enabled"))))


@api.post("/schedules/<volume>/run")
def run_schedule(volume):
    core().manager.run_schedule_now(volume)
    return jsonify({"ok": True})


# ------------------------------------------------------------------- remotes
@api.get("/remotes")
def remotes():
    return jsonify(core().manager.list_remotes())


@api.post("/remotes")
def add_remote():
    return jsonify(core().manager.save_remote(body()))


@api.put("/remotes/<rid>")
def update_remote(rid):
    return jsonify(core().manager.save_remote(body(), rid))


@api.delete("/remotes/<rid>")
def delete_remote(rid):
    core().manager.delete_remote(rid)
    return jsonify({"ok": True})


@api.post("/remotes/test")
def test_remote():
    data = body()
    rid = data.get("id")
    payload = data if data.get("config") else None
    ok, msg = core().manager.test_remote(payload, rid)
    if rid and ok:
        core().manager.check_remote_health(rid)
    return jsonify({"ok": ok, "message": msg})


@api.post("/remotes/<rid>/check")
def check_remote(rid):
    return jsonify(core().manager.check_remote_health(rid))


@api.post("/remotes/sync")
def sync_remotes():
    data = body()
    ids = data.get("remotes")
    t = core().manager.start_sync([str(r) for r in ids] if isinstance(ids, list) and ids else None)
    return jsonify({"task": t.to_dict()})


# ------------------------------------------------------------------ recovery
@api.get("/recovery/last")
def recovery_last():
    return jsonify(core().recovery.last_scan)


@api.post("/recovery/scan")
def recovery_scan():
    data = body()
    if not data.get("remote"):
        return err("remote is required.")
    t = core().recovery.start_scan(str(data["remote"]))
    return jsonify({"task": t.to_dict()})


@api.post("/recovery/restore")
def recovery_restore():
    data = body()
    if not data.get("remote") or not isinstance(data.get("items"), list):
        return err("remote and items are required.")
    t = core().recovery.start_restore(str(data["remote"]), data["items"], verify=bool(data.get("verify", True)))
    return jsonify({"task": t.to_dict()})


# --------------------------------------------------------------------- tasks
@api.get("/tasks")
def tasks():
    active = request.args.get("active") in ("1", "true")
    return jsonify([t.to_dict() for t in core().tasks.list(active_only=active)])


@api.get("/tasks/<tid>")
def task(tid):
    t = core().tasks.get(tid)
    if not t:
        return err("Task not found.", 404)
    return jsonify(t.to_dict(include_log=True))


@api.post("/tasks/<tid>/cancel")
def cancel_task(tid):
    return jsonify({"ok": core().tasks.cancel(tid)})


# -------------------------------------------------------------------- events
@api.get("/events")
def events():
    a = request.args
    rows = core().manager.list_events(
        limit=int(a.get("limit") or 200), level=a.get("level") or None, kind=a.get("kind") or None,
        volume=a.get("volume") or None, q=a.get("q") or None,
        since=float(a["since"]) if a.get("since") else None,
        before_id=int(a["before"]) if a.get("before") else None)
    return jsonify(rows)


@api.get("/logs/download")
def logs_download():
    c = core()
    path = c.log_file
    if not path or not os.path.isfile(path):
        return err("No log file.", 404)
    return send_file(path, as_attachment=True, download_name="dockbare.log", mimetype="text/plain")


# ------------------------------------------------------------------- reports
@api.get("/reports")
def reports():
    c = core()
    return jsonify({"settings": c.reports.settings(), "next_runs": c.reports.next_runs(), "history": c.reports.list()})


@api.get("/reports/<int:rid>")
def report(rid):
    r = core().reports.get(rid)
    if not r:
        return err("Report not found.", 404)
    return jsonify(r)


@api.get("/reports/preview")
def report_preview():
    period = request.args.get("period") or "daily"
    rep = core().reports.build(period)
    return jsonify({"subject": rep["subject"], "text": rep["text"], "html": rep["html"], "stats": rep["stats"]})


@api.post("/reports/send")
def report_send():
    data = body()
    chans = data.get("channels") if isinstance(data.get("channels"), list) else None
    return jsonify(core().reports.send(str(data.get("period") or "daily"), channels=chans))


@api.put("/reports/settings")
def report_settings():
    return jsonify(core().reports.save_settings(body()))


# ------------------------------------------------------------------ settings
@api.get("/settings")
def settings():
    c = core()
    return jsonify({
        "general": c.manager.general(),
        "notifications": c.manager.notify_settings_public(),
        "notify_events": NOTIFY_EVENTS,
        "reports": c.reports.settings(),
        "env": c.settings.public_dict(),
        "encryption": {"configured": c.keyring.available, "fingerprint": c.keyring.fingerprint()},
        "instance": {"name": c.manager.instance_name(), "from_env": bool(c.settings.instance_name)},
        "docker": c.docker.info(),
        "helper_image": c.docker._helper_image,
        "log_file": c.log_file,
    })


@api.put("/settings/general")
def save_general():
    data = body()
    if "instance_name" in data:
        core().manager.set_instance_name(str(data.pop("instance_name") or ""))
    return jsonify(core().manager.save_general(data))


@api.put("/settings/notifications")
def save_notifications():
    return jsonify(core().manager.save_notify_settings(body()))


@api.post("/settings/notifications/test")
def test_notification():
    data = body()
    channel = str(data.get("channel") or "")
    override = data.get("override") if isinstance(data.get("override"), dict) else None
    if override:
        # blank secret in override → use stored secret
        stored = core().manager.notifier.settings.get(channel, {})
        for k in ("token", "password", "secret"):
            if k in override and override[k] in ("", None):
                override[k] = stored.get(k)
    ok, msg = core().manager.notifier.test(channel, override)
    return jsonify({"ok": ok, "message": msg})


# ------------------------------------------------------------------ security
@api.get("/security")
def security():
    c = core()
    s = g.get("session") or {}
    failures, locked_until = c.auth.lock_state(request.remote_addr or "?")
    return jsonify({
        "sessions": c.auth.list_sessions(s.get("token_id")),
        "attempts": c.auth.recent_attempts(),
        "this_ip": {"ip": request.remote_addr, "failures": failures, "locked_until": locked_until},
        "settings": {k: v for k, v in c.settings.public_dict().items() if k.startswith(("session", "login", "behind", "secure", "admin", "totp", "generated"))},
        "totp": {"enabled": c.auth.totp_enabled},
        "weak_password": c.auth.weak_password,
    })


@api.delete("/security/sessions")
def revoke_sessions():
    c = core()
    s = g.get("session") or {}
    n = c.auth.revoke_all(except_db_id=s.get("token_id"))
    c.manager.event("warning", "auth.sessions_revoked", f"{n} other session(s) revoked")
    return jsonify({"revoked": n})


@api.delete("/security/sessions/<sid>")
def revoke_session(sid):
    return jsonify({"ok": core().auth.revoke(sid)})


# --------------------------------------------------------------------- peers
@api.get("/peer/status")
def peer_status():
    """Status for other Dockbare instances (bearer token, no session)."""
    c = core()
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else None
    if not c.peers.verify_token(token):
        log.warning("Rejected peer status request from %s", request.remote_addr)
        time.sleep(0.5)
        return err("Invalid peer token.", 401)
    return jsonify(c.peers.self_status())


@api.get("/peers")
def peers():
    c = core()
    return jsonify({"peers": c.peers.list(), "token": c.peers.token(), "token_source": c.peers.token_source(),
                    "self": c.peers.self_status()})


@api.post("/peers")
def add_peer():
    return jsonify(core().peers.save(body()))


@api.put("/peers/<pid>")
def update_peer(pid):
    return jsonify(core().peers.save(body(), pid))


@api.delete("/peers/<pid>")
def delete_peer(pid):
    core().peers.delete(pid)
    return jsonify({"ok": True})


@api.post("/peers/test")
def test_peer():
    data = body()
    ok, msg, st = core().peers.test(data, data.get("id"))
    return jsonify({"ok": ok, "message": msg, "status": st})


@api.post("/peers/<pid>/check")
def check_peer(pid):
    p = core().peers.check(pid)
    if not p:
        return err("Peer not found.", 404)
    return jsonify(p)


@api.post("/peers/token/regenerate")
def regenerate_peer_token():
    return jsonify({"token": core().peers.regenerate_token()})


# -------------------------------------------------------------------- uptime
@api.get("/uptime/public")
def uptime_public():
    c = core()
    if not c.uptime.settings().get("public_enabled"):
        return err("Not found.", 404)
    return jsonify(c.uptime.public_payload())


@api.get("/uptime")
def uptime():
    c = core()
    return jsonify({"containers": c.uptime.rows(), "incidents": c.uptime.incidents(limit=100), "settings": c.uptime.settings(),
                    "summary": c.uptime.summary(), "announcements": c.uptime.announcements(limit=50),
                    "defaults": __import__("dockbare.uptime", fromlist=["MONITOR_DEFAULTS"]).MONITOR_DEFAULTS})


@api.post("/uptime/poll")
def uptime_poll():
    core().uptime.poll()
    return jsonify({"ok": True})


@api.put("/uptime/settings")
def uptime_settings():
    return jsonify(core().uptime.save_settings(body()))


@api.put("/uptime/monitors/<name>")
def uptime_monitor(name):
    return jsonify(core().uptime.save_monitor(name, body()))


@api.delete("/uptime/monitors/<name>")
def uptime_monitor_delete(name):
    core().uptime.delete_monitor(name)
    return jsonify({"ok": True})


@api.post("/uptime/containers/<name>/restart")
def uptime_restart(name):
    return jsonify({"ok": core().uptime.restart_now(name)})


# -------------------------------------------------------------------- config
@api.get("/config/export")
def config_export():
    c = core()
    encrypted = request.args.get("plain") not in ("1", "true")
    data = c.configsync.to_bytes(encrypt=encrypted and c.keyring.available)
    name = f"dockbare-config-{c.manager.instance_name()}-{time.strftime('%Y%m%d-%H%M%S')}.json" + (".enc" if encrypted and c.keyring.available else "")
    c.manager.event("info", "config.exported", f"Configuration exported ({'encrypted' if name.endswith('.enc') else 'plain'})")
    return Response(data, mimetype="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@api.post("/config/import")
def config_import():
    c = core()
    f = request.files.get("file")
    if f:
        raw = f.read(16 * 1024 * 1024)
    else:
        raw = request.get_data(cache=False)
    if not raw:
        return err("file is required.")
    doc = c.configsync.from_bytes(raw)
    sections = request.form.getlist("sections") or None
    return jsonify({"summary": c.configsync.apply(doc, sections=sections), "snapshot": c.configsync.describe(doc)})


@api.post("/config/push")
def config_push():
    return jsonify(core().configsync.push())


@api.get("/config/status")
def config_status():
    c = core()
    return jsonify({"last_push": c.configsync.last_push, "autopush": c.manager.general().get("config_autopush", True),
                    "encrypted": c.keyring.available, "snapshot": c.configsync.describe(c.configsync.export(include_secrets=False))})


@api.post("/recovery/config/import")
def recovery_config_import():
    c = core()
    data = body()
    if not data.get("remote"):
        return err("remote is required.")
    inst = data.get("instance")
    doc = c.configsync.fetch(str(data["remote"]), instance=(inst if inst else None) if "instance" in data else c.manager.instance_name())
    if not doc:
        return err("No configuration snapshot found there.", 404)
    sections = data.get("sections") if isinstance(data.get("sections"), list) else None
    return jsonify({"summary": c.configsync.apply(doc, sections=sections), "snapshot": c.configsync.describe(doc)})


@api.get("/uptime/announcements")
def uptime_announcements():
    return jsonify(core().uptime.announcements())


@api.post("/uptime/announcements")
def uptime_announcement_add():
    return jsonify(core().uptime.save_announcement(body()))


@api.put("/uptime/announcements/<int:aid>")
def uptime_announcement_update(aid):
    return jsonify(core().uptime.save_announcement(body(), aid))


@api.delete("/uptime/announcements/<int:aid>")
def uptime_announcement_delete(aid):
    core().uptime.delete_announcement(aid)
    return jsonify({"ok": True})


@api.post("/uptime/announcements/<int:aid>/updates")
def uptime_announcement_update_post(aid):
    data = body()
    return jsonify(core().uptime.post_update(aid, data.get("status") or None, str(data.get("message") or "")))


@api.delete("/uptime/announcements/<int:aid>/updates/<int:uid>")
def uptime_announcement_update_delete(aid, uid):
    return jsonify(core().uptime.delete_update(aid, uid))


@api.put("/uptime/announcements/<int:aid>/updates/<int:uid>")
def uptime_announcement_update_edit(aid, uid):
    data = body()
    return jsonify(core().uptime.edit_update(aid, uid, data.get("message"), data.get("status") or None))


@api.get("/security/totp-qr")
def totp_qr():
    """SVG QR code of the configured TOTP secret (for adding another authenticator device)."""
    c = core()
    if not c.settings.totp_secret:
        return err("TOTP is not configured (DOCKBARE_TOTP_SECRET).", 404)
    import io

    import pyotp
    import qrcode
    import qrcode.image.svg

    uri = pyotp.TOTP(c.settings.totp_secret.replace(" ", "").upper()).provisioning_uri(name=c.settings.admin_user, issuer_name="Dockbare")
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    c.manager.event("warning", "auth.totp_qr", "TOTP QR code displayed")
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})
