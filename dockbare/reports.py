"""Periodic activity reports (daily / weekly / monthly)."""
from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger

from .db import rows_to_dicts
from .manager import Manager, ManagerError, fmt_bytes

log = logging.getLogger("dockbare.reports")

PERIODS = {"daily": 1, "weekly": 7, "monthly": 30}

DEFAULTS = {
    "daily": {"enabled": False, "time": "07:00"},
    "weekly": {"enabled": False, "weekday": 0, "time": "07:00"},
    "monthly": {"enabled": False, "day": 1, "time": "07:00"},
    "channels": ["ntfy", "email"],
    "only_on_problems": False,
}


class Reports:
    def __init__(self, manager: Manager):
        self.m = manager

    # --------------------------------------------------------------- settings
    def settings(self) -> dict:
        s = json.loads(json.dumps(DEFAULTS))
        stored = self.m.db.get_setting("reports", {}) or {}
        for k in ("daily", "weekly", "monthly"):
            if isinstance(stored.get(k), dict):
                s[k].update(stored[k])
        if isinstance(stored.get("channels"), list):
            s["channels"] = [c for c in stored["channels"] if c in ("ntfy", "email", "webhook")]
        s["only_on_problems"] = bool(stored.get("only_on_problems", False))
        return s

    def save_settings(self, data: dict) -> dict:
        s = self.settings()
        for k in ("daily", "weekly", "monthly"):
            if isinstance(data.get(k), dict):
                s[k].update(data[k])
                s[k]["enabled"] = bool(s[k].get("enabled"))
                s[k]["time"] = _norm_time(s[k].get("time"))
        if "weekday" in s["weekly"]:
            s["weekly"]["weekday"] = min(6, max(0, int(s["weekly"]["weekday"] or 0)))
        if "day" in s["monthly"]:
            s["monthly"]["day"] = min(28, max(1, int(s["monthly"]["day"] or 1)))
        if isinstance(data.get("channels"), list):
            s["channels"] = [c for c in data["channels"] if c in ("ntfy", "email", "webhook")]
        if "only_on_problems" in data:
            s["only_on_problems"] = bool(data["only_on_problems"])
        self.m.db.set_setting("reports", s)
        self.install_jobs()
        self.m.event("info", "settings.saved", "Report settings saved")
        return self.settings()

    def install_jobs(self) -> None:
        s = self.settings()
        sched = self.m.scheduler
        for period in PERIODS:
            job_id = f"report:{period}"
            try:
                sched.remove_job(job_id)
            except Exception:  # noqa: BLE001
                pass
            cfg = s[period]
            if not cfg.get("enabled"):
                continue
            hh, mm = cfg["time"].split(":")
            if period == "daily":
                trig = CronTrigger(hour=int(hh), minute=int(mm), timezone=self.m.settings.timezone)
            elif period == "weekly":
                trig = CronTrigger(day_of_week=int(cfg.get("weekday", 0)), hour=int(hh), minute=int(mm), timezone=self.m.settings.timezone)
            else:
                trig = CronTrigger(day=int(cfg.get("day", 1)), hour=int(hh), minute=int(mm), timezone=self.m.settings.timezone)
            sched.add_job(self.send, trig, args=[period, True], id=job_id, replace_existing=True)

    def next_runs(self) -> dict:
        out = {}
        for period in PERIODS:
            job = self.m.scheduler.get_job(f"report:{period}") if self.m.scheduler.running else None
            out[period] = job.next_run_time.timestamp() if job and job.next_run_time else None
        return out

    # ------------------------------------------------------------------ build
    def build(self, period: str, end_ts: float | None = None) -> dict:
        if period not in PERIODS:
            raise ManagerError("Unknown report period.")
        end = end_ts or time.time()
        start = end - PERIODS[period] * 86400
        db = self.m.db
        events = rows_to_dicts(db.query("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)))
        for e in events:
            try:
                e["data"] = json.loads(e["data"]) if e.get("data") else {}
            except json.JSONDecodeError:
                e["data"] = {}

        def of(kind: str) -> list[dict]:
            return [e for e in events if e["kind"] == kind]

        completed = of("backup.completed")
        failed = of("backup.failed")
        uploads_ok = of("upload.completed")
        uploads_failed = of("upload.failed")
        deleted = of("backup.deleted")
        restores = of("restore.completed") + of("restore.failed")
        recoveries = of("recovery.completed")
        verify_failed = of("backup.verify_failed")
        auth_failed = of("auth.login_failed") + of("auth.lockout")
        remote_down = of("remote.unreachable")

        backups_now = self.m.list_backups()
        total_now = sum(b["size"] for b in backups_now)
        first = db.one("SELECT total_bytes, backup_count FROM storage_samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (start,)) \
            or db.one("SELECT total_bytes, backup_count FROM storage_samples WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (start,))
        total_start = int(first["total_bytes"]) if first else total_now

        per_volume: dict[str, dict] = {}
        for b in backups_now:
            pv = per_volume.setdefault(b["volume"], {"volume": b["volume"], "backups_total": 0, "bytes_total": 0, "created": 0, "created_bytes": 0, "failed": 0, "deleted": 0, "last_backup": None})
            pv["backups_total"] += 1
            pv["bytes_total"] += b["size"]
            if pv["last_backup"] is None or b["created_at"] > pv["last_backup"]:
                pv["last_backup"] = b["created_at"]
        for e in completed:
            pv = per_volume.setdefault(e["volume"], {"volume": e["volume"], "backups_total": 0, "bytes_total": 0, "created": 0, "created_bytes": 0, "failed": 0, "deleted": 0, "last_backup": None})
            pv["created"] += 1
            pv["created_bytes"] += int(e["data"].get("size") or 0)
        for e in failed:
            per_volume.setdefault(e["volume"], {"volume": e["volume"], "backups_total": 0, "bytes_total": 0, "created": 0, "created_bytes": 0, "failed": 0, "deleted": 0, "last_backup": None})["failed"] += 1
        for e in deleted:
            if e.get("volume") in per_volume:
                per_volume[e["volume"]]["deleted"] += 1

        try:
            docker_volumes = self.m.docker.volume_names()
        except Exception:  # noqa: BLE001
            docker_volumes = set()
        unprotected = sorted(v for v in docker_volumes if v not in per_volume)
        stale = self.m.overdue_schedules(end)
        peers = self.m.peers.summary() if self.m.peers else []

        remotes = self.m.list_remotes()
        stats = {
            "period": period, "start": start, "end": end,
            "backups_created": len(completed), "backups_created_bytes": sum(int(e["data"].get("size") or 0) for e in completed),
            "backups_failed": len(failed), "uploads_ok": len(uploads_ok), "uploads_failed": len(uploads_failed),
            "deleted": len(deleted), "deleted_bytes": sum(int(e["data"].get("size") or 0) for e in deleted),
            "restores": len(restores), "recoveries": len(recoveries), "verify_failed": len(verify_failed),
            "auth_failed": len(auth_failed), "remote_down": len(remote_down),
            "storage_now": total_now, "storage_start": total_start, "storage_delta": total_now - total_start,
            "backup_count": len(backups_now), "disk": self.m.disk_usage(),
            "per_volume": sorted(per_volume.values(), key=lambda x: x["volume"].lower()),
            "unprotected": unprotected, "stale": stale,
            "remotes": [{"name": r["name"], "type": r["type"], "enabled": r["enabled"], "ok": (r["health"] or {}).get("ok")} for r in remotes],
            "peers": peers,
            "problems": [e for e in events if e["level"] == "error"][-25:],
            "encryption": {"configured": self.m.keyring.available, "encrypted_backups": sum(1 for b in backups_now if b["encrypted"])},
        }
        stats["has_problems"] = bool(stats["backups_failed"] or stats["uploads_failed"] or stats["verify_failed"] or stale or stats["remote_down"]
                                     or any(r["enabled"] and r["ok"] is False for r in stats["remotes"])
                                     or any(p["enabled"] and (p["alert_active"] or p["ok"] is False) for p in peers))
        subject = f"{period.capitalize()} backup report — {stats['backups_created']} backups, {stats['backups_failed']} failed, {fmt_bytes(total_now)} stored"
        if stats["has_problems"]:
            subject = "⚠ " + subject
        return {"subject": subject, "text": _render_text(stats, self.m.hostname), "html": _render_html(stats, self.m.hostname), "stats": stats}

    # ------------------------------------------------------------------- send
    def send(self, period: str, scheduled: bool = False, channels: list[str] | None = None) -> dict:
        s = self.settings()
        rep = self.build(period)
        chans = channels or s["channels"]
        if scheduled and s.get("only_on_problems") and not rep["stats"]["has_problems"]:
            status = "skipped (no problems)"
            results: dict = {}
        else:
            results = self.m.notifier.notify("report", rep["subject"], rep["text"], "warning" if rep["stats"]["has_problems"] else "info",
                                             html=rep["html"], force=True, channels=chans)
            if not results:
                status = "no channel enabled"
            elif all(v == "ok" for v in results.values()):
                status = "sent"
            else:
                status = "partial: " + ", ".join(f"{k}={v}" for k, v in results.items())
        cur = self.m.db.execute(
            "INSERT INTO reports(period, generated_at, start_ts, end_ts, subject, text, html, channels, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (period, time.time(), rep["stats"]["start"], rep["stats"]["end"], rep["subject"], rep["text"], rep["html"], json.dumps(chans), status))
        self.m.db.execute("DELETE FROM reports WHERE id NOT IN (SELECT id FROM reports ORDER BY id DESC LIMIT 200)")
        self.m.event("info", "report.sent", f"{period.capitalize()} report: {status}", data={"channels": results})
        return {"id": cur.lastrowid, "status": status, "results": results, "subject": rep["subject"]}

    def list(self, limit: int = 50) -> list[dict]:
        rows = rows_to_dicts(self.m.db.query("SELECT id, period, generated_at, start_ts, end_ts, subject, channels, status FROM reports ORDER BY id DESC LIMIT ?", (limit,)))
        for r in rows:
            r["channels"] = json.loads(r["channels"] or "[]")
        return rows

    def get(self, report_id: int) -> dict | None:
        row = self.m.db.one("SELECT * FROM reports WHERE id=?", (report_id,))
        if not row:
            return None
        d = dict(row)
        d["channels"] = json.loads(d["channels"] or "[]")
        return d


def _norm_time(t) -> str:
    try:
        hh, mm = [int(x) for x in str(t or "07:00").split(":")]
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
        return f"{hh:02d}:{mm:02d}"
    except Exception as e:
        raise ManagerError("Time must be HH:MM.") from e


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render_text(s: dict, host: str) -> str:
    lines = [
        f"Dockbare {s['period']} report for {host}",
        f"Period: {_fmt_ts(s['start'])} → {_fmt_ts(s['end'])}",
        "",
        f"Backups created: {s['backups_created']} ({fmt_bytes(s['backups_created_bytes'])})",
        f"Backups failed:  {s['backups_failed']}",
        f"Uploads:         {s['uploads_ok']} ok, {s['uploads_failed']} failed",
        f"Deleted/pruned:  {s['deleted']} ({fmt_bytes(s['deleted_bytes'])})",
        f"Restores:        {s['restores']}   Recoveries: {s['recoveries']}",
        f"Storage:         {fmt_bytes(s['storage_now'])} ({'+' if s['storage_delta'] >= 0 else '-'}{fmt_bytes(abs(s['storage_delta']))} in period), {s['backup_count']} files",
    ]
    if s["disk"]:
        lines.append(f"Disk free:       {fmt_bytes(s['disk'].get('free', 0))} of {fmt_bytes(s['disk'].get('total', 0))}")
    lines.append(f"Encryption:      {'on' if s['encryption']['configured'] else 'OFF'} ({s['encryption']['encrypted_backups']} encrypted files)")
    if s["remotes"]:
        lines.append("Remotes:         " + ", ".join(f"{r['name']} ({'ok' if r['ok'] else 'DOWN' if r['ok'] is False else 'n/a'})" for r in s["remotes"]))
    if s.get("peers"):
        lines.append("Peers:           " + ", ".join(f"{p['name']} ({'DOWN' if p['alert_active'] or p['ok'] is False else 'ok' if p['ok'] else 'n/a'})" for p in s["peers"]))
    if s["stale"]:
        lines += ["", "⚠ Overdue schedules: " + ", ".join(s["stale"])]
    if s["unprotected"]:
        lines += ["", "Volumes without backups: " + ", ".join(s["unprotected"])]
    if s["auth_failed"]:
        lines += ["", f"Security: {s['auth_failed']} failed login attempts / lockouts"]
    lines += ["", "Per volume:"]
    for pv in s["per_volume"]:
        lines.append(f"  {pv['volume']}: {pv['created']} new ({fmt_bytes(pv['created_bytes'])}), {pv['failed']} failed, "
                     f"{pv['backups_total']} total ({fmt_bytes(pv['bytes_total'])}), last {_fmt_ts(pv['last_backup'])}")
    if s["problems"]:
        lines += ["", "Problems:"]
        for e in s["problems"]:
            lines.append(f"  {_fmt_ts(e['ts'])} {e['message']}")
    return "\n".join(lines)


def _render_html(s: dict, host: str) -> str:
    h = html.escape
    bad = s["has_problems"]
    rows = "".join(
        f"<tr><td>{h(pv['volume'])}</td><td align=right>{pv['created']}</td><td align=right>{h(fmt_bytes(pv['created_bytes']))}</td>"
        f"<td align=right style='color:{'#b91c1c' if pv['failed'] else 'inherit'}'>{pv['failed']}</td><td align=right>{pv['backups_total']}</td>"
        f"<td align=right>{h(fmt_bytes(pv['bytes_total']))}</td><td>{h(_fmt_ts(pv['last_backup']))}</td></tr>"
        for pv in s["per_volume"])
    stat = lambda label, val, color=None: (  # noqa: E731
        f"<td style='padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px'><div style='font-size:12px;color:#6b7280'>{h(label)}</div>"
        f"<div style='font-size:20px;font-weight:600;color:{color or '#111827'}'>{h(str(val))}</div></td>")
    problems = "".join(f"<li>{h(_fmt_ts(e['ts']))} — {h(e['message'])}</li>" for e in s["problems"])
    remotes = ", ".join(f"{h(r['name'])} <b style='color:{'#15803d' if r['ok'] else '#b91c1c' if r['ok'] is False else '#6b7280'}'>{'ok' if r['ok'] else 'down' if r['ok'] is False else 'n/a'}</b>" for r in s["remotes"]) or "none configured"
    delta = ("+" if s["storage_delta"] >= 0 else "−") + fmt_bytes(abs(s["storage_delta"]))
    return f"""<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827;max-width:760px;margin:0 auto;padding:20px">
<h2 style="margin:0 0 4px">Dockbare {h(s['period'])} report</h2>
<div style="color:#6b7280;font-size:13px">{h(host)} · {h(_fmt_ts(s['start']))} → {h(_fmt_ts(s['end']))}</div>
{"<p style='background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:10px;border-radius:8px'>There were problems in this period. See below.</p>" if bad else "<p style='background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;padding:10px;border-radius:8px'>All good: no failures in this period.</p>"}
<table cellspacing="8"><tr>{stat('Backups created', s['backups_created'])}{stat('Failed', s['backups_failed'], '#b91c1c' if s['backups_failed'] else None)}{stat('Uploads ok / failed', f"{s['uploads_ok']} / {s['uploads_failed']}")}{stat('Pruned', f"{s['deleted']} ({fmt_bytes(s['deleted_bytes'])})")}</tr>
<tr>{stat('Storage used', fmt_bytes(s['storage_now']))}{stat('Change', delta)}{stat('Backup files', s['backup_count'])}{stat('Encrypted files', s['encryption']['encrypted_backups'])}</tr></table>
<p style="font-size:13px"><b>Remotes:</b> {remotes}<br>{('<b>Peers:</b> ' + ', '.join(f"{h(p['name'])} <b style='color:{'#b91c1c' if p['alert_active'] or p['ok'] is False else '#15803d' if p['ok'] else '#6b7280'}'>{'down' if p['alert_active'] or p['ok'] is False else 'ok' if p['ok'] else 'n/a'}</b>" for p in s['peers']) + '<br>') if s.get('peers') else ''}<b>Encryption:</b> {'enabled' if s['encryption']['configured'] else '<span style=color:#b91c1c>disabled</span>'}
{('<br><b>Disk free:</b> ' + h(fmt_bytes(s['disk'].get('free', 0))) + ' of ' + h(fmt_bytes(s['disk'].get('total', 0)))) if s['disk'] else ''}
{('<br><b style=color:#b45309>Overdue schedules:</b> ' + h(', '.join(s['stale']))) if s['stale'] else ''}
{('<br><b>Volumes without backups:</b> ' + h(', '.join(s['unprotected']))) if s['unprotected'] else ''}
{('<br><b style=color:#b91c1c>Security:</b> ' + str(s['auth_failed']) + ' failed logins / lockouts') if s['auth_failed'] else ''}</p>
<h3 style="font-size:15px;margin:18px 0 6px">Per volume</h3>
<table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f3f4f6"><th align=left>Volume</th><th>New</th><th>New size</th><th>Failed</th><th>Total</th><th>Total size</th><th align=left>Last backup</th></tr></thead>
<tbody>{rows or '<tr><td colspan=7>No backups</td></tr>'}</tbody></table>
{('<h3 style="font-size:15px;margin:18px 0 6px">Problems</h3><ul style="font-size:13px">' + problems + '</ul>') if problems else ''}
<p style="color:#9ca3af;font-size:11px;margin-top:24px">Sent by Dockbare</p></body></html>"""
