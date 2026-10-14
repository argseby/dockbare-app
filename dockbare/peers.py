"""Peer instances: mutual health monitoring between Dockbare installations.

Every instance exposes ``GET /api/peer/status`` (bearer token) with a short
self-assessment. Other instances poll it on a schedule; after a number of
consecutive failures they raise an alert, and again when the peer comes back.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

import requests

from . import __version__
from .db import row_to_dict, rows_to_dicts
from .manager import Manager, ManagerError, fmt_bytes

log = logging.getLogger("dockbare.peers")

DEFAULTS = {"interval_minutes": 5, "failure_threshold": 3, "alert_on_problems": True, "verify_tls": True, "timeout": 15, "display_name": ""}


class PeerError(Exception):
    pass


class Peers:
    def __init__(self, manager: Manager):
        self.m = manager
        self._lock = threading.Lock()

    # ------------------------------------------------------------- token
    def token(self) -> str:
        env = self.m.settings.peer_token
        if env:
            return env
        tok = self.m.db.get_setting("peer_token")
        if not tok:
            tok = secrets.token_urlsafe(32)
            self.m.db.set_setting("peer_token", tok)
        return tok

    def token_source(self) -> str:
        return "env" if self.m.settings.peer_token else "generated"

    def regenerate_token(self) -> str:
        if self.m.settings.peer_token:
            raise ManagerError("The peer token comes from DOCKBARE_PEER_TOKEN in .env; change it there.")
        tok = secrets.token_urlsafe(32)
        self.m.db.set_setting("peer_token", tok)
        self.m.event("warning", "peer.token_regenerated", "Peer token regenerated; update the other instances")
        return tok

    def verify_token(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self.token().encode("utf-8"))

    # ------------------------------------------------------- self status
    def self_status(self) -> dict:
        m = self.m
        now = time.time()
        backups = m.list_backups()
        last_at = max((b["created_at"] for b in backups), default=None)
        since = now - 86400
        failed = m.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='backup.failed' AND ts>=?", (since,))["n"]
        upload_failed = m.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='upload.failed' AND ts>=?", (since,))["n"]
        overdue = m.overdue_schedules()
        remotes = [{"name": r["name"], "ok": (r["health"] or {}).get("ok")} for r in m.list_remotes() if r["enabled"]]
        docker_ok = m.docker.ping()
        disk = m.disk_usage()
        problems: list[str] = []
        if not docker_ok:
            problems.append("Docker engine unreachable")
        if overdue:
            problems.append("Overdue schedules: " + ", ".join(overdue))
        if failed:
            problems.append(f"{failed} failed backup(s) in the last 24h")
        if upload_failed:
            problems.append(f"{upload_failed} failed upload(s) in the last 24h")
        down = [r["name"] for r in remotes if r["ok"] is False]
        if down:
            problems.append("Remotes unreachable: " + ", ".join(down))
        if disk.get("total") and (disk["free"] < 1024 ** 3 or disk["free"] / disk["total"] < 0.05):
            problems.append(f"Low disk space: {fmt_bytes(disk['free'])} free")
        uptime = m.uptime.summary() if m.uptime else None
        if uptime and uptime.get("down"):
            problems.append("Containers down: " + ", ".join(uptime["down"]))
        if uptime and uptime.get("checks_down"):
            problems.append("Checks down: " + ", ".join(uptime["checks_down"]))
        return {
            "ok": True, "app": "dockbare", "version": __version__, "hostname": m.hostname, "time": now,
            "uptime": now - m.started_at, "docker_ok": docker_ok,
            "backups": {"count": len(backups), "bytes": sum(b["size"] for b in backups), "last_at": last_at},
            "schedules": {"active": sum(1 for s in m.list_schedules() if s["enabled"]), "overdue": overdue},
            "failed_24h": int(failed), "upload_failed_24h": int(upload_failed), "remotes": remotes,
            "encryption": m.keyring.available, "disk": disk,
            "active_tasks": len(m.tasks.list(active_only=True)), "problems": problems, "uptime": uptime,
        }

    # --------------------------------------------------------------- crud
    def _row(self, row, with_token: bool = False) -> dict:
        d = row_to_dict(row)
        cfg = json.loads(d.pop("config") or "{}")
        d.update({k: cfg.get(k, v) for k, v in DEFAULTS.items()})
        d["token_set"] = bool(d.get("token"))
        if with_token:
            d["token"] = self.m.secretbox.decrypt(d["token"]) if d.get("token") else ""
        else:
            d.pop("token", None)
        d["enabled"] = bool(d["enabled"])
        d["alert_active"] = bool(d["alert_active"])
        d["status"] = json.loads(d["status"]) if d.get("status") else None
        d["source"] = "env" if d["id"].startswith("env-") else "ui"
        return d

    def list(self) -> list[dict]:
        return [self._row(r) for r in self.m.db.query("SELECT * FROM peers ORDER BY name COLLATE NOCASE")]

    def get(self, peer_id: str, with_token: bool = False) -> dict | None:
        row = self.m.db.one("SELECT * FROM peers WHERE id=?", (peer_id,))
        return self._row(row, with_token) if row else None

    def _normalize(self, data: dict, existing: dict | None) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise ManagerError("Name is required.")
        url = (data.get("url") or "").strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ManagerError("URL must start with http:// or https://")
        url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        token = data.get("token")
        if token in ("", None) and existing:
            token = existing.get("token", "")
        cfg = {}
        for k, default in DEFAULTS.items():
            v = data.get(k, existing.get(k) if existing else default)
            if k in ("interval_minutes", "failure_threshold", "timeout"):
                v = int(v or default)
            elif isinstance(default, bool):
                v = bool(v)
            else:
                v = str(v or "").strip()[:80]
            cfg[k] = v
        cfg["interval_minutes"] = max(1, min(1440, cfg["interval_minutes"]))
        cfg["failure_threshold"] = max(1, min(100, cfg["failure_threshold"]))
        cfg["timeout"] = max(3, min(120, cfg["timeout"]))
        return {"name": name, "url": url, "token": token or "", "enabled": bool(data.get("enabled", existing["enabled"] if existing else True)), "config": cfg}

    def save(self, data: dict, peer_id: str | None = None, *, allow_env: bool = False) -> dict:
        if peer_id and peer_id.startswith("env-") and not allow_env:
            raise ManagerError("This peer is configured in .env (DOCKBARE_PEER_*); change it there and restart.")
        existing = self.get(peer_id, with_token=True) if peer_id else None
        if peer_id and not existing and not allow_env:
            raise ManagerError("Peer not found.")
        p = self._normalize(data, existing)
        now = time.time()
        enc = self.m.secretbox.encrypt(p["token"]) if p["token"] else ""
        if existing:
            self.m.db.execute("UPDATE peers SET name=?, url=?, token=?, enabled=?, config=?, updated_at=? WHERE id=?",
                              (p["name"], p["url"], enc, int(p["enabled"]), json.dumps(p["config"]), now, peer_id))
            if not allow_env:
                self.m.event("info", "peer.updated", f"Peer '{p['name']}' updated")
        else:
            peer_id = peer_id or uuid.uuid4().hex[:12]
            self.m.db.execute(
                "INSERT INTO peers(id, name, url, token, enabled, config, created_at, updated_at, consecutive_failures, alert_active) VALUES(?,?,?,?,?,?,?,?,0,0)",
                (peer_id, p["name"], p["url"], enc, int(p["enabled"]), json.dumps(p["config"]), now, now))
            self.m.event("info", "peer.created", f"Peer '{p['name']}' added ({p['url']}{', from .env' if peer_id.startswith('env-') else ''})")
        threading.Thread(target=self.check, args=(peer_id,), daemon=True).start()
        return self.get(peer_id)

    def delete(self, peer_id: str, *, allow_env: bool = False) -> None:
        p = self.get(peer_id)
        if not p:
            raise ManagerError("Peer not found.")
        if p["source"] == "env" and not allow_env:
            raise ManagerError("This peer is configured in .env (DOCKBARE_PEER_*); remove it there and restart.")
        self.m.db.execute("DELETE FROM peers WHERE id=?", (peer_id,))
        self.m.event("warning", "peer.deleted", f"Peer '{p['name']}' removed")

    def sync_env_peers(self) -> None:
        """Mirror DOCKBARE_PEER_<n>_* blocks into the peers table (ids ``env-<n>``)."""
        wanted = {f"env-{p['index']}": p for p in self.m.settings.env_peers}
        for pid, p in wanted.items():
            try:
                self.save(p, pid, allow_env=True)
            except ManagerError as e:
                log.error("Invalid env peer %s: %s", pid, e)
        for row in self.m.db.query("SELECT id FROM peers WHERE id LIKE 'env-%'"):
            if row["id"] not in wanted:
                self.delete(row["id"], allow_env=True)

    # ------------------------------------------------------------- checks
    def _fetch(self, url: str, token: str, verify: bool, timeout: int) -> dict:
        try:
            r = requests.get(url + "/api/peer/status", headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                               "User-Agent": f"Dockbare/{__version__} peer-check"},
                             timeout=timeout, verify=verify, allow_redirects=False)
        except requests.RequestException as e:
            raise PeerError(f"{e.__class__.__name__}: {str(e)[:200]}") from e
        if r.status_code == 401:
            raise PeerError("Rejected: peer token is wrong")
        if r.status_code != 200:
            raise PeerError(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise PeerError("Response is not JSON (is this a Dockbare instance?)") from e
        if not isinstance(data, dict) or data.get("app") != "dockbare":
            raise PeerError("Not a Dockbare peer status response")
        return data

    def test(self, data: dict, peer_id: str | None = None) -> tuple[bool, str, dict | None]:
        existing = self.get(peer_id, with_token=True) if peer_id else None
        p = self._normalize(data, existing)
        try:
            st = self._fetch(p["url"], p["token"], p["config"]["verify_tls"], p["config"]["timeout"])
        except PeerError as e:
            return False, str(e), None
        return True, f"Reachable: {st.get('hostname')} running Dockbare {st.get('version')}, {len(st.get('problems') or [])} problem(s)", st

    def check(self, peer_id: str) -> dict | None:
        with self._lock:
            p = self.get(peer_id, with_token=True)
            if not p:
                return None
            if not p["enabled"]:
                return p
            now = time.time()
            prev = p.get("status") or {}
            try:
                st = self._fetch(p["url"], p["token"], p["verify_tls"], p["timeout"])
                ok, error = True, None
            except PeerError as e:
                st, ok, error = None, False, str(e)
            consecutive = 0 if ok else p["consecutive_failures"] + 1
            alert_active = p["alert_active"]
            if ok:
                if alert_active:
                    alert_active = False
                    msg = f"Peer '{p['name']}' ({p['url']}) is reachable again after {p['consecutive_failures']} failed checks"
                    self.m.event("success", "peer.up", msg)
                    self.m.notifier.notify("peer", f"Peer back online: {p['name']}", msg, "success")
                problems = list(st.get("problems") or [])
                prev_problems = list(prev.get("problems") or [])
                if p["alert_on_problems"] and problems and problems != prev_problems:
                    msg = f"Peer '{p['name']}' reports problems:\n- " + "\n- ".join(problems)
                    self.m.event("warning", "peer.problems", msg.replace("\n- ", "; ").replace(":;", ":"))
                    self.m.notifier.notify("peer", f"Peer problems: {p['name']}", msg, "warning")
                elif prev_problems and not problems:
                    self.m.event("info", "peer.problems_cleared", f"Peer '{p['name']}' no longer reports problems")
            else:
                if consecutive >= p["failure_threshold"] and not alert_active:
                    alert_active = True
                    msg = f"PEER DOWN: '{p['name']}' ({p['url']}) failed {consecutive} consecutive checks. Last error: {error}"
                    self.m.event("error", "peer.down", msg)
                    self.m.notifier.notify("peer", f"Peer down: {p['name']}", msg, "error")
                elif consecutive == 1:
                    self.m.event("warning", "peer.check_failed", f"Peer '{p['name']}' check failed: {error}")
            # keep the last known remote report while the peer is unreachable (names stay stable on the status page)
            status = {"ok": ok, "checked_at": now, "error": error, "remote": st if ok else prev.get("remote"),
                      "refreshed_at": now if ok else prev.get("refreshed_at"), "problems": list((st or {}).get("problems") or [])}
            self.m.db.execute(
                "UPDATE peers SET last_checked_at=?, last_ok_at=COALESCE(?, last_ok_at), consecutive_failures=?, alert_active=?, status=? WHERE id=?",
                (now, now if ok else None, consecutive, int(alert_active), json.dumps(status), peer_id))
        return self.get(peer_id)

    def _page_shows_peers(self) -> bool:
        up = getattr(self.m, "uptime", None)
        s = up.settings() if up else {}
        return bool(s.get("public_enabled") and s.get("public_show_peers"))

    def check_due(self) -> None:
        """Scheduled once a minute: full checks at each peer's interval, plus a report refresh every minute
        while the public status page shows peers (so changes on a peer appear within about two minutes)."""
        now = time.time()
        live = self._page_shows_peers()
        for p in self.list():
            if not p["enabled"]:
                continue
            st = p.get("status") or {}
            try:
                if (p.get("last_checked_at") or 0) + p["interval_minutes"] * 60 <= now:
                    self.check(p["id"])
                elif st.get("ok") is False and (st.get("retried_at") or st.get("checked_at") or 0) + 55 <= now:
                    self.retry_down(p["id"])  # a peer that was rebuilt comes back within a minute
                elif live and st.get("ok") and (st.get("refreshed_at") or st.get("checked_at") or 0) + 55 <= now:
                    self.refresh_remote(p["id"])
            except Exception:  # noqa: BLE001
                log.exception("Peer check failed for %s", p["id"])

    def retry_down(self, peer_id: str) -> bool:
        """Probe an unreachable peer between scheduled checks. Only a success is recorded (as a full check,
        which clears the alert and notifies); a failure is not counted, so alert timing stays as configured."""
        with self._lock:
            p = self.get(peer_id, with_token=True)
            if not p or not p["enabled"]:
                return False
            try:
                self._fetch(p["url"], p["token"], p["verify_tls"], p["timeout"])
            except PeerError:
                status = {**(p.get("status") or {}), "retried_at": time.time()}
                self.m.db.execute("UPDATE peers SET status=? WHERE id=?", (json.dumps(status), peer_id))
                return False
        self.check(peer_id)
        return True

    def refresh_remote(self, peer_id: str) -> bool:
        """Re-fetch a healthy peer's report for the status page. Failure counting, alerts and problem
        notifications are left to the scheduled check; a failed refresh changes nothing."""
        with self._lock:
            p = self.get(peer_id, with_token=True)
            if not p or not p["enabled"] or not (p.get("status") or {}).get("ok"):
                return False
            try:
                st = self._fetch(p["url"], p["token"], p["verify_tls"], p["timeout"])
            except PeerError:
                return False
            status = {**p["status"], "remote": st, "refreshed_at": time.time()}
            self.m.db.execute("UPDATE peers SET status=? WHERE id=?", (json.dumps(status), peer_id))
        return True

    def check_all(self) -> None:
        for p in self.list():
            if p["enabled"]:
                self.check(p["id"])

    def install_job(self) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        self.sync_env_peers()
        self.m.scheduler.add_job(self.check_due, IntervalTrigger(seconds=60), id="peers", replace_existing=True)
        # verify every peer right after a (re)start instead of waiting for the first due interval
        self.m.scheduler.add_job(self.verify_on_start, "date", id="peers-initial", replace_existing=True)

    def verify_on_start(self, attempt: int = 1) -> None:
        """Check every peer right after a (re)start. Peers that fail, e.g. because they are booting alongside
        us, are probed again after 15 s (up to three more times, success-only so alerts are not hurried)."""
        if attempt == 1:
            self.check_all()
        else:
            for p in self.list():
                if p["enabled"] and (p.get("status") or {}).get("ok") is False:
                    try:
                        self.retry_down(p["id"])
                    except Exception:  # noqa: BLE001
                        log.exception("Peer retry failed for %s", p["id"])
        failed = [p["name"] for p in self.list() if p["enabled"] and (p.get("status") or {}).get("ok") is False]
        if failed and attempt < 4:
            from datetime import datetime, timedelta

            self.m.scheduler.add_job(self.verify_on_start, "date", run_date=datetime.now(self.m.scheduler.timezone) + timedelta(seconds=15),
                                     args=[attempt + 1], id="peers-initial", replace_existing=True)

    def summary(self) -> list[dict]:
        out = []
        for p in self.list():
            st = p.get("status") or {}
            out.append({"id": p["id"], "name": p["name"], "url": p["url"], "enabled": p["enabled"], "ok": st.get("ok"), "source": p["source"],
                        "alert_active": p["alert_active"], "checked_at": st.get("checked_at"), "error": st.get("error"),
                        "problems": st.get("problems") or [], "hostname": (st.get("remote") or {}).get("hostname")})
        return out
