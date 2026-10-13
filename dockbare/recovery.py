"""Recovery mode: scan a remote, compare with the local Docker host, restore."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import archive
from .dockerctl import DockerError, valid_volume_name
from .manager import Manager, ManagerError, fmt_bytes
from .tasks import Task

log = logging.getLogger("dockbare.recovery")


class Recovery:
    def __init__(self, manager: Manager):
        self.m = manager
        self.last_scan: dict | None = None

    # ------------------------------------------------------------------ scan
    def start_scan(self, remote_id: str) -> Task:
        r = self.m.get_remote(remote_id)
        if not r:
            raise ManagerError("Remote not found.")
        return self.m.tasks.submit("recovery-scan", f"Scan remote {r['name']}", lambda t: self.scan(t, remote_id))

    def scan(self, task: Task | None, remote_id: str, read_meta: bool = True) -> dict:
        r = self.m.get_remote(remote_id)
        if not r:
            raise ManagerError("Remote not found.")
        remote = self.m.build_remote(remote_id)
        local_volumes = {}
        docker_ok = True
        try:
            local_volumes = {v["name"]: v for v in self.m.docker.list_volumes()}
        except DockerError as e:
            docker_ok = False
            if task:
                task.add_log(f"Docker not reachable: {e}")
        local_backups: dict[str, dict[str, dict]] = {}
        self.m.scan_backups()
        for b in self.m.list_backups():
            local_backups.setdefault(b["volume"], {})[b["filename"]] = b
        fingerprint = self.m.keyring.fingerprint()

        items = []
        own = self.m.instance_name()
        with remote.session():
            tree = remote.list_tree()
            if task:
                task.add_log(f"Found {len(tree)} volume folder(s) on {r['name']}")
            for entry in sorted(tree, key=lambda e: ((e["instance"] or "").lower(), e["volume"].lower())):
                if task:
                    task.check_cancel()
                volume, instance, files = entry["volume"], entry["instance"], entry["files"]
                names = {f.name: f for f in files}
                backups = []
                for f in files:
                    if not archive.is_backup_filename(f.name):
                        continue
                    ts, encrypted = archive.parse_backup_filename(f.name)
                    backups.append({
                        "filename": f.name, "size": f.size, "created_at": ts.timestamp() if ts else None,
                        "encrypted": encrypted, "has_meta": (f.name + archive.META_EXT) in names,
                        "local": f.name in local_backups.get(volume, {}),
                    })
                backups.sort(key=lambda b: (b["created_at"] or 0), reverse=True)
                if not backups:
                    continue
                meta = None
                if read_meta and backups[0]["has_meta"]:
                    try:
                        raw = remote.read_small_file(volume, backups[0]["filename"] + archive.META_EXT, instance=instance)
                        meta = json.loads(raw.decode("utf-8")) if raw else None
                    except Exception as e:  # noqa: BLE001
                        if task:
                            task.add_log(f"Could not read metadata for {volume}: {e}")
                local = local_volumes.get(volume)
                latest = backups[0]
                key_status = "n/a"
                if latest["encrypted"]:
                    if not self.m.keyring.available:
                        key_status = "missing"
                    elif meta and meta.get("key_fingerprint"):
                        key_status = "match" if meta["key_fingerprint"] == fingerprint else "mismatch"
                    else:
                        key_status = "unknown"
                items.append({
                    "volume": volume,
                    "instance": instance,
                    "own_instance": instance == own,
                    "backups": backups,
                    "latest": latest,
                    "meta": {k: meta.get(k) for k in ("hostname", "containers", "created_at", "size", "raw_bytes", "sha256", "app_version")} if meta else None,
                    "local_volume": {
                        "exists": local is not None,
                        "containers": local["containers"] if local else [],
                        "project": local.get("project") if local else None,
                    },
                    "local_backups": len(local_backups.get(volume, {})),
                    "key_status": key_status,
                    "status": ("ready" if local else "volume-missing"),
                })
        instances = sorted({it["instance"] for it in items if it["instance"]}, key=str.lower)
        configs = []
        if self.m.configsync:
            seen_inst = list(instances) + ([None] if any(it["instance"] is None for it in items) else [])
            if own not in seen_inst:
                seen_inst.append(own)
            for inst in seen_inst:
                try:
                    doc = self.m.configsync.fetch(remote_id, instance=inst)
                except Exception as e:  # noqa: BLE001
                    if task:
                        task.add_log(f"Configuration snapshot of {inst or 'flat layout'} unreadable: {e}")
                    configs.append({"instance": inst, "error": str(e)})
                    continue
                if doc:
                    configs.append({"instance": inst, **self.m.configsync.describe(doc)})
        result = {
            "remote": {"id": r["id"], "name": r["name"], "type": r["type"]},
            "scanned_at": time.time(), "docker_ok": docker_ok, "items": items,
            "instances": instances, "own_instance": own, "flat_layout": any(it["instance"] is None for it in items),
            "configs": configs,
            "encryption": {"configured": self.m.keyring.available, "fingerprint": fingerprint},
            "local_volumes": sorted(local_volumes.keys()),
        }
        self.last_scan = result
        self.m.event("info", "recovery.scanned", f"Recovery scan of {r['name']}: {len(items)} volume(s) with backups", remote_id=remote_id)
        return result

    # --------------------------------------------------------------- execute
    def start_restore(self, remote_id: str, items: list[dict], *, verify: bool = True) -> Task:
        r = self.m.get_remote(remote_id)
        if not r:
            raise ManagerError("Remote not found.")
        if not items:
            raise ManagerError("Nothing selected.")
        clean = []
        for it in items:
            volume = str(it.get("volume") or "")
            filename = os.path.basename(str(it.get("filename") or ""))
            target = str(it.get("target_volume") or volume)
            if not valid_volume_name(volume) or not valid_volume_name(target):
                raise ManagerError(f"Invalid volume name: {volume}")
            if not archive.is_backup_filename(filename):
                raise ManagerError(f"Invalid backup file: {filename}")
            if self.m.tasks.active_for_volume(target):
                raise ManagerError(f"Volume '{target}' is busy.")
            instance = it.get("instance") or None
            if instance is not None and not valid_volume_name(str(instance)):
                raise ManagerError(f"Invalid instance folder: {instance}")
            clean.append({"volume": volume, "filename": filename, "target_volume": target, "instance": instance,
                          "create_volume": bool(it.get("create_volume", True)), "restore": bool(it.get("restore", True))})
        title = f"Recover {len(clean)} volume(s) from {r['name']}"
        return self.m.tasks.submit("recovery", title, lambda t: self._execute(t, remote_id, clean, verify))

    def _execute(self, task: Task, remote_id: str, items: list[dict], verify: bool) -> dict:
        r = self.m.get_remote(remote_id)
        remote = self.m.build_remote(remote_id)
        own = self.m.instance_name()
        results = []
        self.m.event("warning", "recovery.started", f"Recovery from {r['name']} started for {len(items)} volume(s)", remote_id=remote_id)
        self.m.notifier.notify("recovery", "Recovery started", f"Restoring {len(items)} volume(s) from {r['name']}", "warning")
        ok_count = 0
        with remote.session():
            for idx, it in enumerate(items, 1):
                task.check_cancel()
                volume, filename, target, instance = it["volume"], it["filename"], it["target_volume"], it["instance"]
                task.set_progress(phase="download", item=idx, items=len(items), volume=target, bytes=0, total=None)
                task.add_log(f"[{idx}/{len(items)}] {(instance + '/') if instance else ''}{volume}/{filename} → {target}")
                res = {"volume": volume, "filename": filename, "target_volume": target, "status": "pending"}
                try:
                    vdir = self.m.settings.backup_dir / volume
                    vdir.mkdir(parents=True, exist_ok=True)
                    local = vdir / filename
                    rsize = remote.size(volume, filename, instance=instance)
                    if rsize is None:
                        raise ManagerError("File no longer exists on the remote.")
                    if local.is_file() and local.stat().st_size == rsize:
                        task.add_log("Local copy already present, skipping download")
                    else:
                        task.set_progress(total=rsize)
                        remote.download(volume, filename, str(local), progress=lambda n: task.set_progress(bytes=n), instance=instance)
                        task.add_log(f"Downloaded {fmt_bytes(rsize)}")
                    meta_name = filename + archive.META_EXT
                    try:
                        if remote.exists(volume, meta_name, instance=instance):
                            remote.download(volume, meta_name, str(vdir / meta_name), instance=instance)
                    except Exception as e:  # noqa: BLE001
                        task.add_log(f"Metadata not downloaded: {e}")
                    self.m.scan_backups()
                    if instance == own or (instance is None and remote.instance is None):
                        self.m._set_upload_status(str(local), remote_id, "synced")
                    if verify:
                        task.set_progress(phase="verify", bytes=0, total=local.stat().st_size)
                        v = archive.verify_backup(local, self.m.keyring, progress=lambda n: task.set_progress(bytes=n))
                        if not v["ok"]:
                            raise ManagerError(f"Verification failed: {v.get('error')}")
                        task.add_log(f"Verified: {v['files']} files, {fmt_bytes(v['bytes'])}")
                        res["files"] = v["files"]
                    if not it["restore"]:
                        res["status"] = "downloaded"
                        ok_count += 1
                        results.append(res)
                        continue
                    if not self.m.docker.volume_exists(target):
                        if not it["create_volume"]:
                            raise ManagerError(f"Volume '{target}' does not exist.")
                        task.add_log(f"Creating volume {target}")
                        self.m.docker.create_volume(target)
                    task.set_progress(phase="restore", bytes=0, total=local.stat().st_size)
                    self.m._do_restore(task, str(local), target, notify=False)
                    res["status"] = "restored"
                    ok_count += 1
                except Exception as e:  # noqa: BLE001
                    res["status"] = "failed"
                    res["error"] = str(e)
                    task.add_log(f"FAILED: {e}")
                    self.m.event("error", "recovery.item_failed", f"Recovery of {volume} failed: {e}", volume=target, remote_id=remote_id, backup=filename)
                results.append(res)
        failed = len(results) - ok_count
        msg = f"Recovery from {r['name']} finished: {ok_count} succeeded, {failed} failed"
        self.m.event("warning" if failed else "success", "recovery.completed", msg, remote_id=remote_id, data={"results": results})
        self.m.notifier.notify("recovery", "Recovery finished", msg, "error" if failed else "success")
        if failed:
            raise ManagerError(msg)
        return {"results": results, "ok": ok_count, "failed": failed}
