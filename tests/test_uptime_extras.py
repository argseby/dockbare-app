"""Uptime extras: missing-container removal, peer group names, public item fields, badge, external checks, services."""
import time

from tests.test_peers_remotes import uptime_app  # noqa: F401  (fixture)


def _quiet(core, monkeypatch):
    monkeypatch.setattr(core.manager.notifier, "notify", lambda *a, **k: None)


def test_missing_container_can_be_removed(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    c.put("/api/uptime/monitors/db", json={"enabled": True, "grace_seconds": 0})
    del d.containers["db"]
    core.uptime.poll()
    rows = {r["name"]: r for r in c.get("/api/uptime").get_json()["containers"]}
    assert rows["db"]["missing"] and rows["db"]["monitor"]["explicit"] is True
    assert c.delete("/api/uptime/monitors/db").status_code == 200
    rows = {r["name"]: r for r in c.get("/api/uptime").get_json()["containers"]}
    assert "db" not in rows
    inc = core.uptime.incidents(container="db")
    assert inc and inc[0]["ended_at"] is not None


def test_peer_group_names(uptime_app, monkeypatch):
    from dockbare import peers as peers_mod

    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    anon = app.test_client()
    # an instance with an auto-generated name reports no group name to its peers
    core.settings.instance_name = None
    core.db.set_setting("instance_name", "dockbare-ab12")
    c.put("/api/uptime/settings", json={"public_enabled": True, "public_group_name": ""})
    st = c.get("/api/peer/status", headers={"Authorization": f"Bearer {core.peers.token()}"}).get_json()
    assert st["uptime"]["public_group_name"] == ""
    assert anon.get("/api/uptime/public").get_json()["groups"][0]["name"] == "This site"
    # a peer's self-reported name beats the peer name; a local display name beats both
    remote = {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": [], "uptime": {"public_group_name": "", "public_items": [], "announcements": []}}
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: dict(remote))
    p = c.post("/api/peers", json={"name": "Server B", "url": "https://b.example", "token": "t"}).get_json()
    names = lambda: [g["name"] for g in anon.get("/api/uptime/public").get_json()["groups"]]  # noqa: E731
    c.post(f"/api/peers/{p['id']}/check")
    assert names() == ["This site", "Server B"]
    remote["uptime"]["public_group_name"] = "Garage rack"
    c.post(f"/api/peers/{p['id']}/check")
    assert names() == ["This site", "Garage rack"]
    p = c.put(f"/api/peers/{p['id']}", json={"name": "Server B", "url": "https://b.example", "display_name": "Basement"}).get_json()
    assert p["display_name"] == "Basement"
    assert names() == ["This site", "Basement"]


def test_public_fields_and_badge(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    anon = app.test_client()
    assert anon.get("/badge.svg").status_code == 404 and anon.get("/badge.json").status_code == 404
    c.put("/api/uptime/settings", json={"public_enabled": True})
    r = c.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website", "public_url": "https://example.com", "public_icon": "🌐", "public_description": "Main site"})
    assert r.status_code == 200, r.get_json()
    assert c.put("/api/uptime/monitors/web", json={"public_url": "ftp://x"}).status_code == 400
    assert c.put("/api/uptime/monitors/web", json={"public_icon": "not an icon at all"}).status_code == 400
    item = anon.get("/api/uptime/public").get_json()["groups"][0]["items"][0]
    assert item["name"] == "Website" and item["url"] == "https://example.com" and item["icon"] == "🌐" and item["description"] == "Main site"
    r = anon.get("/badge.svg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"All systems operational" in r.data and b"#111a2e" in r.data
    assert b"#ffffff" in anon.get("/badge.svg?theme=light").data and b"Homelab" in anon.get("/badge.svg?label=Homelab").data
    assert b"all systems operational" in anon.get("/badge.svg?style=flat").data and b"linearGradient" in anon.get("/badge.svg?style=flat").data
    assert r.headers["Access-Control-Allow-Origin"] == "*" and "max-age=60" in r.headers["Cache-Control"]
    assert anon.get("/badge.json?label=shop").get_json() == {"schemaVersion": 1, "label": "shop", "message": "all systems operational", "color": "#2da44e"}
    d.containers["web"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1, "health": None})
    core.uptime._failing_since["web"] = time.time() - 100
    core.uptime.poll()
    assert b"Major outage" in anon.get("/badge.svg").data


def test_external_checks(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    up = core.uptime
    results = {"ok": True, "status_code": 200, "latency_ms": 12}
    monkeypatch.setattr(type(up), "_probe", lambda self, chk: dict(results))
    assert c.post("/api/uptime/checks", json={"name": "x", "url": "ftp://x"}).status_code == 400
    assert c.post("/api/uptime/checks", json={"name": "", "url": "https://example.com"}).status_code == 400
    r = c.post("/api/uptime/checks", json={"name": "Example", "url": "https://example.com/health", "grace_seconds": 0, "interval_seconds": 5})
    assert r.status_code == 200, r.get_json()
    chk = r.get_json()
    assert chk["interval_seconds"] == 10  # clamped
    tcp = c.post("/api/uptime/checks", json={"name": "DB", "url": "TCP://Db.Example.com:5432/", "public": False}).get_json()
    assert tcp["url"] == "tcp://db.example.com:5432"
    st = c.post(f"/api/uptime/checks/{chk['id']}/run").get_json()
    assert st["ok"] and st["latency_ms"] == 12 and st["incident"] is None
    rows = {x["id"]: x for x in c.get("/api/uptime").get_json()["checks"]}
    assert rows[chk["id"]]["state"]["ok"] and rows[chk["id"]]["uptime"]["d1"] == 100.0
    # failure -> incident (grace 0), notification, public page and peers see it
    sent = []
    monkeypatch.setattr(core.manager.notifier, "notify", lambda event, title, msg, level="info", **kw: sent.append((title, level)))
    results.update({"ok": False, "kind": "http-error", "error": "HTTP 503", "status_code": 503})
    st = c.post(f"/api/uptime/checks/{chk['id']}/run").get_json()
    assert st["incident"] and st["incident"]["kind"] == "http-error" and sent[-1] == ("DOWN: Example", "error")
    c.put("/api/uptime/settings", json={"public_enabled": True})
    anon = app.test_client()
    pl = anon.get("/api/uptime/public").get_json()
    assert [i["name"] for i in pl["groups"][0]["items"]] == ["Example"]  # DB is not public
    assert pl["groups"][0]["items"][0]["status"] == "down" and pl["groups"][0]["items"][0]["kind"] == "http-error"
    assert pl["incidents"][0]["name"] == "Example" and pl["overall"] == "outage"
    assert c.get("/api/uptime").get_json()["summary"]["checks_down"] == ["Example"]
    pst = c.get("/api/peer/status", headers={"Authorization": f"Bearer {core.peers.token()}"}).get_json()
    assert "Checks down: Example" in pst["problems"]
    up.poll()  # container polls leave check incidents alone
    assert [i["container"] for i in up.incidents(open_only=True)] == [f"check:{chk['id']}"]
    # recovery
    results.update({"ok": True, "kind": None, "error": None, "status_code": 200})
    st = c.post(f"/api/uptime/checks/{chk['id']}/run").get_json()
    assert st["incident"] is None and sent[-1] == ("UP: Example", "success") and up.incidents(open_only=True) == []
    # deleting closes its incident and forgets it
    results.update({"ok": False, "kind": "unreachable", "error": "ConnectionError: boom"})
    c.post(f"/api/uptime/checks/{chk['id']}/run")
    assert up.incidents(open_only=True)
    assert c.delete(f"/api/uptime/checks/{chk['id']}").status_code == 200
    assert up.incidents(open_only=True) == [] and c.delete(f"/api/uptime/checks/{chk['id']}").status_code == 400
    # the loop only runs checks that are due
    up.check_state.pop(tcp["id"], None)
    up._check_next[tcp["id"]] = time.time() + 999
    up.run_checks()
    assert tcp["id"] not in up.check_state
    up._check_next[tcp["id"]] = 0
    up.run_checks()
    assert tcp["id"] in up.check_state


def test_services_group_members(uptime_app, monkeypatch):
    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    up = core.uptime
    results = {"ok": True, "latency_ms": 1}
    monkeypatch.setattr(type(up), "_probe", lambda self, chk: dict(results))
    chk = c.post("/api/uptime/checks", json={"name": "API", "url": "https://api.example.com", "grace_seconds": 0}).get_json()
    key = f"check:{chk['id']}"
    c.post(f"/api/uptime/checks/{chk['id']}/run")
    c.put("/api/uptime/settings", json={"public_enabled": True})
    c.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website"})
    assert c.post("/api/uptime/services", json={"name": "Shop", "members": []}).status_code == 400
    assert c.post("/api/uptime/services", json={"name": "Shop", "members": ["check:nope"]}).status_code == 400
    r = c.post("/api/uptime/services", json={"name": "Shop", "members": ["web", "db", key, "web"], "public_description": "Store front", "public_url": "https://shop.example.com", "public_icon": "🛒"})
    assert r.status_code == 200, r.get_json()
    svc = r.get_json()
    assert svc["members"] == ["web", "db", key] and svc["down_if_any"] is True
    anon = app.test_client()
    items = anon.get("/api/uptime/public").get_json()["groups"][0]["items"]
    assert [i["name"] for i in items] == ["Shop"]  # members are not listed on their own
    shop = items[0]
    assert shop["service"] and shop["status"] == "up" and shop["url"] == "https://shop.example.com" and shop["icon"] == "🛒" and shop["description"] == "Store front"
    assert [(x["name"], x["status"]) for x in shop["components"]] == [("Website", "up"), ("db", "up"), ("API", "up")]
    # db goes down -> the whole service is down (down_if_any) and its downtime counts
    d.containers["db"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1})
    up._failing_since["db"] = time.time() - 100
    up.poll()
    pl = anon.get("/api/uptime/public").get_json()
    shop = pl["groups"][0]["items"][0]
    assert shop["status"] == "down" and shop["kind"] == "down" and pl["overall"] == "outage" and shop["uptime"]["d1"] < 100.0
    assert pl["incidents"][0]["name"] == "Shop · db"
    # "all members" rule: partial outage, no downtime counted
    c.put(f"/api/uptime/services/{svc['id']}", json={"down_if_any": False})
    pl = anon.get("/api/uptime/public").get_json()
    shop = pl["groups"][0]["items"][0]
    assert shop["status"] == "degraded" and pl["overall"] == "degraded" and shop["uptime"]["d1"] == 100.0
    # everything down -> down; only the overlap counts as downtime
    results.update({"ok": False, "kind": "unreachable", "error": "boom"})
    up._failing_since[key] = time.time() - 50
    c.post(f"/api/uptime/checks/{chk['id']}/run")
    d.containers["web"].update({"state": "exited", "status": "Exited (1)", "exit_code": 1, "health": None})
    up._failing_since["web"] = time.time() - 100
    up.poll()
    shop = anon.get("/api/uptime/public").get_json()["groups"][0]["items"][0]
    assert shop["status"] == "down" and 99.9 < shop["uptime"]["d1"] < 100.0
    # members can be renamed for the page; names of removed members are dropped
    svc = c.put(f"/api/uptime/services/{svc['id']}", json={"member_names": {"db": "Database", key: "Payments", "nope": "x"}}).get_json()
    assert svc["member_names"] == {"db": "Database", key: "Payments"}
    pl = anon.get("/api/uptime/public").get_json()
    assert [x["name"] for x in pl["groups"][0]["items"][0]["components"]] == ["Website", "Database", "Payments"]
    assert {i["name"] for i in pl["incidents"]} >= {"Shop · Database", "Shop · Payments"}
    # deleting a check prunes it from the service; deleting the service leaves the members alone
    assert c.delete(f"/api/uptime/checks/{chk['id']}").status_code == 200
    assert c.get("/api/uptime").get_json()["services"][0]["members"] == ["web", "db"]
    assert c.delete(f"/api/uptime/services/{svc['id']}").status_code == 200
    items = anon.get("/api/uptime/public").get_json()["groups"][0]["items"]
    assert [i["name"] for i in items] == ["Website"]


def test_public_page_merges_peers(uptime_app, monkeypatch):
    from dockbare import peers as peers_mod

    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    anon = app.test_client()
    c.put("/api/uptime/settings", json={"public_enabled": True, "public_group_name": "Home"})
    c.put("/api/uptime/monitors/web", json={"public": True, "display_name": "Website"})
    item = {"name": "Garage API", "status": "down", "since": 1.0, "kind": "unreachable", "uptime": {"d1": 90.0, "d7": 99.0, "d30": 99.9}, "history": []}
    remote = {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": [],
              "uptime": {"public_group_name": "Garage", "public_items": [item], "announcements": [{"id": 1, "type": "maintenance", "title": "Fan swap", "status": "scheduled", "starts_at": 1.0, "ends_at": 2.0, "active": False, "upcoming": True, "updates": []}]}}
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: dict(remote))
    p = c.post("/api/peers", json={"name": "Server B", "url": "https://b.example", "token": "t"}).get_json()
    c.post(f"/api/peers/{p['id']}/check")
    pl = anon.get("/api/uptime/public").get_json()
    assert [g["name"] for g in pl["groups"]] == ["Home", "Garage"] and pl["unreachable"] == []
    c.put("/api/uptime/settings", json={"public_merge_peers": True})
    pl = anon.get("/api/uptime/public").get_json()
    assert len(pl["groups"]) == 1 and pl["groups"][0]["merged"] and pl["groups"][0]["name"] == "Home"
    assert [(i["name"], i.get("site")) for i in pl["groups"][0]["items"]] == [("Garage API", "Garage"), ("Website", None)]
    assert pl["groups"][0]["announcements"][0]["site"] == "Garage" and pl["overall"] == "degraded"
    # an unreachable peer is still reported
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: (_ for _ in ()).throw(peers_mod.PeerError("down")))
    for _ in range(3):
        c.post(f"/api/peers/{p['id']}/check")
    pl = anon.get("/api/uptime/public").get_json()
    assert pl["unreachable"] == ["Garage"] and len(pl["groups"]) == 1


def test_peer_reports_refresh_while_page_shows_them(uptime_app, monkeypatch):
    from dockbare import peers as peers_mod

    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    anon = app.test_client()
    remote = {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": [], "uptime": {"public_group_name": "Garage", "public_items": [], "announcements": []}}
    calls = []
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: (calls.append(1), dict(remote))[1])
    p = c.post("/api/peers", json={"name": "Server B", "url": "https://b.example", "token": "t", "interval_minutes": 60}).get_json()
    c.post(f"/api/peers/{p['id']}/check")
    n = len(calls)
    core.peers.check_due()  # not due, page off -> nothing
    assert len(calls) == n
    c.put("/api/uptime/settings", json={"public_enabled": True})
    core.peers.check_due()  # fresh enough (just checked) -> nothing
    assert len(calls) == n
    core.db.execute("UPDATE peers SET status=json_set(status, '$.refreshed_at', ?) WHERE id=?", (time.time() - 120, p["id"]))
    remote["uptime"]["public_group_name"] = "Garage rack"
    core.peers.check_due()
    assert len(calls) == n + 1
    pl = anon.get("/api/uptime/public").get_json()
    assert pl["groups"][1]["name"] == "Garage rack" and time.time() - pl["groups"][1]["checked_at"] < 5
    p = c.get("/api/peers").get_json()["peers"][0]
    assert p["consecutive_failures"] == 0 and p["alert_active"] is False and p["status"]["ok"] is True
    # a failing refresh changes nothing; the scheduled check still owns alerting
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: (_ for _ in ()).throw(peers_mod.PeerError("down")))
    core.db.execute("UPDATE peers SET status=json_set(status, '$.refreshed_at', ?) WHERE id=?", (time.time() - 120, p["id"]))
    core.peers.check_due()
    p2 = c.get("/api/peers").get_json()["peers"][0]
    assert p2["consecutive_failures"] == 0 and p2["status"]["ok"] is True and p2["status"]["remote"]["uptime"]["public_group_name"] == "Garage rack"


def test_unreachable_peer_recovers_within_a_minute(uptime_app, monkeypatch):
    from dockbare import peers as peers_mod

    app, c, d = uptime_app
    core = app.extensions["dockbare"]
    _quiet(core, monkeypatch)
    boom = lambda self, url, token, verify, timeout: (_ for _ in ()).throw(peers_mod.PeerError("connection refused"))  # noqa: E731
    monkeypatch.setattr(peers_mod.Peers, "_fetch", boom)
    p = c.post("/api/peers", json={"name": "Server B", "url": "https://b.example", "token": "t", "interval_minutes": 60, "failure_threshold": 2}).get_json()
    for _ in range(50):  # the background check after save
        if c.get("/api/peers").get_json()["peers"][0].get("last_checked_at"):
            break
        time.sleep(0.05)
    for _ in range(2):
        p = c.post(f"/api/peers/{p['id']}/check").get_json()
    assert p["alert_active"] is True and p["status"]["ok"] is False
    failures = p["consecutive_failures"]
    core.db.execute("UPDATE peers SET status=json_set(status, '$.checked_at', ?) WHERE id=?", (time.time() - 120, p["id"]))
    core.peers.check_due()  # still down: retried, but not counted as another failure
    p = c.get("/api/peers").get_json()["peers"][0]
    assert p["consecutive_failures"] == failures and p["status"].get("retried_at")
    core.peers.check_due()  # retried less than a minute ago -> no probe
    # the peer is back (e.g. rebuilt): the next minute tick records it without waiting for the 60 min interval
    monkeypatch.setattr(peers_mod.Peers, "_fetch", lambda self, url, token, verify, timeout: {"app": "dockbare", "hostname": "b", "version": "3.0.0", "problems": []})
    core.db.execute("UPDATE peers SET status=json_set(status, '$.retried_at', ?) WHERE id=?", (time.time() - 120, p["id"]))
    core.peers.check_due()
    p = c.get("/api/peers").get_json()["peers"][0]
    assert p["alert_active"] is False and p["consecutive_failures"] == 0 and p["status"]["ok"] is True
    assert "peer.up" in [e["kind"] for e in c.get("/api/events").get_json()]
