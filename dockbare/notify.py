"""Notification channels: ntfy, email (SMTP), webhook."""
from __future__ import annotations

import html as _html
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime

import requests

log = logging.getLogger("dockbare.notify")

LEVEL_PRIORITY = {"info": "default", "success": "high", "warning": "high", "error": "urgent"}

# Event kinds that can be toggled in the UI
NOTIFY_EVENTS = {
    "backup_success": {"label": "Backup completed", "default": True},
    "backup_failure": {"label": "Backup failed", "default": True},
    "upload_failure": {"label": "Upload to remote failed", "default": True},
    "restore": {"label": "Restore started / finished", "default": True},
    "prune": {"label": "Backups pruned", "default": False},
    "sync": {"label": "Remote sync finished", "default": False},
    "recovery": {"label": "Recovery runs", "default": True},
    "auth": {"label": "Failed logins / lockouts", "default": True},
    "peer": {"label": "Peer instance down / recovered / problems", "default": True},
    "uptime": {"label": "Container down / recovered / auto-restarted", "default": True},
    "report": {"label": "Periodic reports", "default": True},
}

DEFAULT_SETTINGS = {
    "ntfy": {"enabled": False, "base_url": "https://ntfy.sh", "topic": "", "token": ""},
    "email": {
        "enabled": False, "host": "", "port": 587, "security": "starttls", "username": "", "password": "",
        "from_addr": "", "to_addrs": "", "timeout": 20,
    },
    "webhook": {"enabled": False, "url": "", "secret": ""},
    "events": {k: v["default"] for k, v in NOTIFY_EVENTS.items()},
}


class Notifier:
    def __init__(self, get_settings, app_name: str = "Dockbare"):
        self._get_settings = get_settings
        self.app_name = app_name

    @property
    def settings(self) -> dict:
        s = json.loads(json.dumps(DEFAULT_SETTINGS))
        stored = self._get_settings() or {}
        for section in ("ntfy", "email", "webhook", "events"):
            if isinstance(stored.get(section), dict):
                s[section].update(stored[section])
        return s

    def event_enabled(self, event: str) -> bool:
        return bool(self.settings["events"].get(event, NOTIFY_EVENTS.get(event, {}).get("default", True)))

    # --- public --------------------------------------------------------------
    def notify(self, event: str, title: str, message: str, level: str = "info", *, html: str | None = None,
               force: bool = False, channels: list[str] | None = None, headline: str | None = None,
               subject_name: str | None = None, facts: list[tuple[str, str]] | None = None) -> dict[str, str]:
        """Send to every enabled channel. Returns {channel: 'ok' | error}.

        ``headline`` (what happened, e.g. "Service down") and ``subject_name`` (what it happened to)
        make up the header of the HTML email; without them they are taken from a "What: name" title.
        ``facts`` are extra label/value rows shown under the message.
        """
        if not force and not self.event_enabled(event):
            return {}
        if html is None:
            html = render_email_html(self.app_name, title, message, level, headline=headline, subject_name=subject_name, facts=facts)
        s = self.settings
        wanted = set(channels) if channels else {"ntfy", "email", "webhook"}
        results: dict[str, str] = {}
        if "ntfy" in wanted and s["ntfy"]["enabled"]:
            results["ntfy"] = self._ntfy(s["ntfy"], title, message, level)
        if "email" in wanted and s["email"]["enabled"]:
            results["email"] = self._email(s["email"], title, message, html)
        if "webhook" in wanted and s["webhook"]["enabled"]:
            results["webhook"] = self._webhook(s["webhook"], event, title, message, level)
        for ch, res in results.items():
            if res != "ok":
                log.warning("Notification via %s failed: %s", ch, res)
        return results

    def test(self, channel: str, override: dict | None = None) -> tuple[bool, str]:
        s = self.settings
        cfg = dict(s.get(channel) or {})
        if override:
            cfg.update({k: v for k, v in override.items() if v is not None})
        title = f"{self.app_name} test notification"
        msg = "If you can read this, the channel works."
        if channel == "ntfy":
            res = self._ntfy(cfg, title, msg, "info")
        elif channel == "email":
            res = self._email(cfg, title, msg, render_email_html(self.app_name, title, msg, "info", headline="Test notification", subject_name="Email channel"))
        elif channel == "webhook":
            res = self._webhook(cfg, "test", title, msg, "info")
        else:
            return False, "Unknown channel"
        return res == "ok", res

    # --- channels ------------------------------------------------------------
    def _ntfy(self, cfg: dict, title: str, message: str, level: str) -> str:
        base = (cfg.get("base_url") or "").rstrip("/")
        topic = (cfg.get("topic") or "").strip()
        if not base or not topic:
            return "ntfy base URL or topic missing"
        headers = {"Title": title, "Priority": LEVEL_PRIORITY.get(level, "default"),
                   "Tags": {"success": "white_check_mark", "error": "rotating_light", "warning": "warning"}.get(level, "package")}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        try:
            r = requests.post(f"{base}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15)
            if r.status_code >= 400:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            return "ok"
        except requests.RequestException as e:
            return str(e)

    def _email(self, cfg: dict, title: str, message: str, html: str | None) -> str:
        host = (cfg.get("host") or "").strip()
        if not host:
            return "SMTP host missing"
        to_addrs = [a.strip() for a in str(cfg.get("to_addrs") or "").replace(";", ",").split(",") if a.strip()]
        if not to_addrs:
            return "No recipient configured"
        from_addr = (cfg.get("from_addr") or cfg.get("username") or "").strip()
        if not from_addr:
            return "Sender address missing"
        port = int(cfg.get("port") or 587)
        security = (cfg.get("security") or "starttls").lower()
        timeout = int(cfg.get("timeout") or 20)

        msg = EmailMessage()
        msg["Subject"] = f"[{self.app_name}] {title}"
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="dockbare")
        msg.set_content(message)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            ctx = ssl.create_default_context()
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
            else:
                server = smtplib.SMTP(host, port, timeout=timeout)
            with server:
                server.ehlo()
                if security == "starttls":
                    server.starttls(context=ctx)
                    server.ehlo()
                if cfg.get("username"):
                    server.login(cfg["username"], cfg.get("password") or "")
                server.send_message(msg)
            return "ok"
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            return f"{e.__class__.__name__}: {e}"

    def _webhook(self, cfg: dict, event: str, title: str, message: str, level: str) -> str:
        url = (cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Webhook URL missing or invalid"
        payload = {"app": self.app_name, "event": event, "title": title, "message": message, "level": level}
        headers = {"Content-Type": "application/json"}
        if cfg.get("secret"):
            import hashlib
            import hmac
            body = json.dumps(payload).encode("utf-8")
            headers["X-Dockbare-Signature"] = hmac.new(cfg["secret"].encode("utf-8"), body, hashlib.sha256).hexdigest()
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code >= 400:
                return f"HTTP {r.status_code}"
            return "ok"
        except requests.RequestException as e:
            return str(e)


LEVEL_COLORS = {  # header background, header text
    "error": ("#b91c1c", "#ffffff"),
    "warning": ("#b45309", "#ffffff"),
    "success": ("#15803d", "#ffffff"),
    "info": ("#1d4ed8", "#ffffff"),
}


def split_title(title: str) -> tuple[str, str | None]:
    """'Backup failed: vol1' -> ('Backup failed', 'vol1'); a title without ': ' has no name part."""
    head, sep, name = title.partition(": ")
    return (head.strip(), name.strip()) if sep and name.strip() else (title.strip(), None)


def render_email_html(app_name: str, title: str, message: str, level: str = "info", *, headline: str | None = None,
                      subject_name: str | None = None, facts: list[tuple[str, str]] | None = None,
                      host: str | None = None) -> str:
    """Single-column notification email: a coloured header with the issue and the affected name, then details."""
    h = _html.escape
    if headline is None:
        headline, auto_name = split_title(title)
        subject_name = subject_name or auto_name
    bg, fg = LEVEL_COLORS.get(level, LEVEL_COLORS["info"])
    paragraphs = "".join(f"<p style='margin:0 0 10px'>{h(line)}</p>" for line in message.split("\n") if line.strip())
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#6b7280;white-space:nowrap;vertical-align:top'>{h(k)}</td><td style='padding:4px 0'>{h(str(v))}</td></tr>"
        for k, v in (facts or []) if v not in (None, ""))
    when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"""<!doctype html><html><body style="margin:0;padding:20px;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb">
<tr><td style="background:{bg};color:{fg};padding:18px 22px">
<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;opacity:.85">{h(headline)}</div>
<div style="font-size:22px;font-weight:600;margin-top:4px">{h(subject_name or app_name)}</div>
</td></tr>
<tr><td style="padding:18px 22px;font-size:14px;line-height:1.5">{paragraphs}
{f"<table cellspacing='0' cellpadding='0' style='font-size:13px;margin-top:6px'>{rows}</table>" if rows else ""}
</td></tr>
<tr><td style="padding:12px 22px;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:11px">Sent by {h(app_name)}{f" on {h(host)}" if host else ""} · {h(when)}</td></tr>
</table></body></html>"""
