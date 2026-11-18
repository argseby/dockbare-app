"""Cleanup of unused Docker images, volumes and build cache, built so nothing is deleted by accident.

Safety layers, each on its own:

1. ``scan()`` only lists candidates. Deletion needs an explicit list of items (the UI
   pre-selects nothing) and a typed confirmation in the UI.
2. "Unused" means no container at all uses it: running *or stopped*, including
   Dockbare's own helper containers (so a volume being backed up is never unused).
3. Protected items are never deleted even when requested: Dockbare's own volumes and
   images (its image is also the backup helper image), volumes with a backup schedule or
   a running task, and images other images are built on.
4. ``run()`` scans again right before deleting and only deletes what is still a
   candidate with the same fingerprint (a volume recreated under the same name since the
   scan has a different creation time and is skipped).
5. Volumes get a final backup first by default (local archive plus every remote the
   volume would normally upload to); a volume is only deleted when that backup and all
   uploads succeeded. Dockbare never prunes the newest backup of a volume, so it stays.
6. Docker removals are never forced: the daemon itself refuses anything that came into
   use in the meantime. Every removal is written to the event log.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from .dockerctl import INTERNAL_LABEL, DockerError
from .manager import Manager, ManagerError, fmt_bytes
from .tasks import Task

log = logging.getLogger("dockbare.cleanup")

ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")
STOPPED_STATES = ("exited", "created", "dead")
HELPER_LABEL = "dev.dockbare.helper"


def _ts(iso: str | None) -> float | None:
    """Docker timestamp (RFC 3339 with nanoseconds) → unix seconds; None for the zero time."""
    if not iso or iso.startswith("0001-"):
        return None
    from datetime import datetime

    s = iso.rstrip("Z")
    s = s.split(".")[0] + ("." + s.split(".")[1][:6] if "." in s else "")
    try:
        return datetime.fromisoformat(s + "+00:00").timestamp()
    except ValueError:
        return None


def _tags(img: dict) -> list[str]:
    return [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]


class Cleanup:
    def __init__(self, manager: Manager):
        self.m = manager

    # ------------------------------------------------------------------ scan
    def scan(self, with_sizes: bool = True) -> dict[str, Any]:
        m = self.m
        inv = m.docker.cleanup_inventory(with_sizes=with_sizes)
        containers, images, volumes, df = inv["containers"], inv["images"], inv["volumes"], inv["df"] or {}

        used_images: dict[str, list[str]] = {}
        used_volumes: dict[str, list[str]] = {}
        for c in containers:
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            if c.get("ImageID"):
                used_images.setdefault(c["ImageID"], []).append(name)
            for mt in c.get("Mounts") or []:
                if mt.get("Type") == "volume" and mt.get("Name"):
                    used_volumes.setdefault(mt["Name"], []).append(name)

        try:
            keep_images = m.docker.protected_image_ids()
        except Exception:  # noqa: BLE001
            keep_images = set()
        parents = {i.get("ParentId") for i in images if i.get("ParentId")}
        img_sizes = {i.get("Id"): i for i in (df.get("Images") or [])}

        img_out = []
        for i in images:
            iid = i.get("Id")
            if not iid or iid in used_images:
                continue
            tags = _tags(i)
            protected = ("Dockbare needs this image for backups and restores" if iid in keep_images
                         else "other images are built on it" if iid in parents else None)
            d = img_sizes.get(iid) or {}
            img_out.append({
                "id": iid, "short_id": iid.split(":")[-1][:12], "tags": tags, "dangling": not tags,
                "size": int(i.get("Size") or d.get("Size") or 0),
                "shared_size": max(0, int(d.get("SharedSize") or i.get("SharedSize") or 0)),  # layers other images use too
                "created": i.get("Created"),
                "local_only": bool(tags) and not (i.get("RepoDigests") or []),
                "protected": protected,
            })
        img_out.sort(key=lambda x: (x["protected"] is not None, not x["dangling"], -(x["size"] or 0)))

        scheduled = {s["volume"] for s in m.list_schedules()}
        vol_sizes = {v.get("Name"): ((v.get("UsageData") or {}).get("Size")) for v in (df.get("Volumes") or [])}
        backups = {r["volume"]: (r["n"], r["last"]) for r in m.db.query(
            "SELECT volume, COUNT(*) AS n, MAX(created_at) AS last FROM backups GROUP BY volume")}
        vol_out = []
        for v in volumes:
            name = v.get("Name")
            if not name or name in used_volumes:
                continue
            labels = v.get("Labels") or {}
            if labels.get(INTERNAL_LABEL):
                continue  # Dockbare's own data/backups volumes are never even listed
            protected = None
            if name in scheduled:
                protected = "has a backup schedule (delete the schedule first)"
            elif m.tasks.active_for_volume(name):
                protected = "a backup or restore is running"
            n, last = backups.get(name, (0, None))
            size = vol_sizes.get(name)
            vol_out.append({
                "name": name, "created": v.get("CreatedAt") or "", "driver": v.get("Driver"),
                "project": labels.get("com.docker.compose.project"), "anonymous": bool(ANON_VOLUME.match(name)),
                "size": size if isinstance(size, int) and size >= 0 else None,
                "backups": n, "last_backup": last, "protected": protected,
            })
        vol_out.sort(key=lambda x: (x["protected"] is not None, x["name"].lower()))

        # stopped containers --------------------------------------------------
        own_id = ""
        try:
            own_id = m.docker.own_container_id() or ""
        except Exception:  # noqa: BLE001
            pass
        monitored = {n for n, cfg in (m.uptime.monitors().items() if m.uptime else []) if cfg.get("enabled")}
        rw = {c.get("Id"): c.get("SizeRw") for c in (df.get("Containers") or [])}
        con_out = []
        for c in containers:
            labels = c.get("Labels") or {}
            if c.get("State") not in STOPPED_STATES or labels.get(HELPER_LABEL) or c.get("Id") == own_id:
                continue
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            raw = m.docker.container_raw(c["Id"]) or {}
            st = raw.get("State") or {}
            protected = "has an Uptime monitor (turn the monitor off first)" if name in monitored else None
            con_out.append({
                "id": c["Id"], "short_id": c["Id"][:12], "name": name, "image": c.get("Image"), "state": c.get("State"),
                "status": c.get("Status"), "exit_code": st.get("ExitCode"), "created": c.get("Created"),
                "finished_at": _ts(st.get("FinishedAt")), "project": labels.get("com.docker.compose.project"),
                "size": rw.get(c["Id"]) if isinstance(rw.get(c["Id"]), int) else None,
                "volumes": [mt.get("Name") for mt in c.get("Mounts") or [] if mt.get("Type") == "volume" and mt.get("Name")],
                "protected": protected,
            })
        con_out.sort(key=lambda x: (x["protected"] is not None, x["finished_at"] or x["created"] or 0))

        cache = df.get("BuildCache") or []
        cache_unused = [b for b in cache if not b.get("InUse")]
        vol_total = sum(int((v.get("UsageData") or {}).get("Size") or 0) for v in (df.get("Volumes") or []) if ((v.get("UsageData") or {}).get("Size") or 0) > 0)
        disk = {
            "images": int(df.get("LayersSize") or sum(int(i.get("Size") or 0) for i in images)),
            "images_unused": sum(i["size"] for i in img_out if not i["protected"]),
            "containers": sum(int(c.get("SizeRw") or 0) for c in (df.get("Containers") or [])),
            "containers_unused": sum(c["size"] or 0 for c in con_out if not c["protected"]),
            "volumes": vol_total, "volumes_unused": sum(v["size"] or 0 for v in vol_out if not v["protected"]),
            "build_cache": sum(int(b.get("Size") or 0) for b in cache),
            "build_cache_unused": sum(int(b.get("Size") or 0) for b in cache_unused if not b.get("Shared")),
        } if inv["df"] else None
        return {
            "containers": con_out, "disk": disk,
            "scanned_at": time.time(), "sizes": bool(inv["df"]),
            "images": img_out, "volumes": vol_out,
            "build_cache": {"size": sum(int(b.get("Size") or 0) for b in cache), "reclaimable": sum(int(b.get("Size") or 0) for b in cache_unused if not b.get("Shared")),
                            "entries": len(cache)},
            "totals": {
                "containers": len(containers), "images": len(images), "volumes": len([v for v in volumes if not (v.get("Labels") or {}).get(INTERNAL_LABEL)]),
                "images_in_use": len([i for i in images if i.get("Id") in used_images]), "volumes_in_use": len(used_volumes),
                "images_size": sum(int(i.get("Size") or 0) for i in images), "layers_size": int(df.get("LayersSize") or 0),
            },
        }

    # ------------------------------------------------------------------- run
    def start(self, images: list[str], volumes: list[dict], *, backup_first: bool = True, build_cache: bool = False,
              containers: list[str] | None = None) -> Task:
        containers = containers if containers is not None else []
        if not isinstance(images, list) or not isinstance(volumes, list) or not isinstance(containers, list) \
                or not all(isinstance(x, str) for x in images + containers) \
                or not all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in volumes):
            raise ManagerError("images and containers must be lists of ids, volumes a list of {name, created}.")
        if not images and not volumes and not containers and not build_cache:
            raise ManagerError("Nothing selected.")
        if any(t.kind == "cleanup" for t in self.m.tasks.list(active_only=True)):
            raise ManagerError("A cleanup is already running.")
        n = len(images) + len(volumes) + len(containers)
        title = f"Cleanup: {n} item{'s' if n != 1 else ''}" + (" + build cache" if build_cache else "")
        return self.m.tasks.submit("cleanup", title, lambda t: self._run(t, images, volumes, bool(backup_first), bool(build_cache), containers),
                                   meta={"images": len(images), "volumes": len(volumes), "containers": len(containers), "backup_first": bool(backup_first)})

    def _run(self, task: Task, image_ids: list[str], vols: list[dict], backup_first: bool, build_cache: bool,
             container_ids: list[str] | None = None) -> dict:
        m = self.m
        task.add_log("Checking every selected item again before deleting anything")
        fresh = self.scan(with_sizes=False)
        cand_img = {i["id"]: i for i in fresh["images"]}
        cand_vol = {v["name"]: v for v in fresh["volumes"]}
        cand_con = {c["id"]: c for c in fresh["containers"]}
        removed_containers: list[str] = []
        removed_images: list[str] = []
        removed_volumes: list[str] = []
        skipped: list[dict] = []
        freed = 0

        def skip(kind: str, item: str, reason: str) -> None:
            skipped.append({"kind": kind, "item": item, "reason": reason})
            task.add_log(f"SKIPPED {kind} {item}: {reason}")

        # containers (their volumes are kept) -----------------------------------
        for cid in container_ids or []:
            task.check_cancel()
            c = cand_con.get(cid)
            if c is None:
                skip("container", cid[:12], "no longer stopped (running again, already gone or not found)")
                continue
            if c["protected"]:
                skip("container", c["name"], c["protected"])
                continue
            try:
                m.docker.remove_container(cid)
            except DockerError as e:
                skip("container", c["name"], f"Docker refused: {e}")
                continue
            removed_containers.append(c["name"])
            freed += c["size"] or 0
            task.add_log(f"Removed stopped container {c['name']} (its volumes are kept)")
            m.event("warning", "cleanup.container_removed", f"Removed stopped container {c['name']} ({c['image']})", data={"id": cid, "name": c["name"]})

        # images ---------------------------------------------------------------
        for iid in image_ids:
            task.check_cancel()
            img = cand_img.get(iid)
            label = iid.split(":")[-1][:12]
            if img is None:
                skip("image", label, "no longer unused (in use, already gone or not found)")
                continue
            label = ", ".join(img["tags"]) or f"untagged {img['short_id']}"
            if img["protected"]:
                skip("image", label, img["protected"])
                continue
            try:
                # untag one by one (removing a multi-tag image by id would need force); the last tag removes the image
                for ref in (img["tags"] or [iid]):
                    m.docker.remove_image(ref)
            except DockerError as e:
                skip("image", label, f"Docker refused: {e}")
                continue
            removed_images.append(label)
            freed += img["size"]
            task.add_log(f"Removed image {label} ({fmt_bytes(img['size'])})")
            m.event("warning", "cleanup.image_removed", f"Removed unused image {label} ({fmt_bytes(img['size'])})", data={"id": iid, "tags": img["tags"]})

        # volumes --------------------------------------------------------------
        for req in vols:
            task.check_cancel()
            name = req["name"]
            v = cand_vol.get(name)
            if v is None:
                skip("volume", name, "no longer unused (mounted by a container, already gone or not found)")
                continue
            if v["protected"]:
                skip("volume", name, v["protected"])
                continue
            if str(req.get("created") or "") != str(v["created"] or ""):
                skip("volume", name, "changed since the scan (recreated?); scan again")
                continue
            if backup_first:
                ok, why = self._final_backup(task, name)
                if not ok:
                    skip("volume", name, f"kept because the final backup failed: {why}")
                    continue
            users = m.docker.volume_users(name)
            if users:
                skip("volume", name, f"now used by {', '.join(users)}")
                continue
            try:
                m.docker.remove_volume(name)
            except DockerError as e:
                skip("volume", name, f"Docker refused: {e}")
                continue
            removed_volumes.append(name)
            if v["size"]:
                freed += v["size"]
            task.add_log(f"Removed volume {name}")
            m.event("warning", "cleanup.volume_removed", f"Removed unused volume {name}" + (" after a final backup" if backup_first else " (no final backup)"),
                    volume=name, data={"backup_first": backup_first, "size": v["size"]})

        # build cache ----------------------------------------------------------
        cache_freed = 0
        if build_cache:
            try:
                cache_freed = m.docker.prune_build_cache()
                freed += cache_freed
                task.add_log(f"Cleared unused build cache ({fmt_bytes(cache_freed)})")
            except DockerError as e:
                skip("build cache", "build cache", f"Docker refused: {e}")

        summary = (f"Cleanup: removed {len(removed_containers)} container(s), {len(removed_images)} image(s) and {len(removed_volumes)} volume(s)"
                   + (f", cleared {fmt_bytes(cache_freed)} build cache" if build_cache else "")
                   + f"; about {fmt_bytes(freed)} freed" + (f"; {len(skipped)} skipped" if skipped else ""))
        task.add_log(summary)
        m.event("warning" if skipped else "success", "cleanup.finished", summary,
                data={"containers": removed_containers, "images": removed_images, "volumes": removed_volumes, "skipped": skipped, "freed": freed})
        return {"removed_containers": removed_containers, "removed_images": removed_images, "removed_volumes": removed_volumes, "skipped": skipped, "freed": freed}

    def _final_backup(self, task: Task, volume: str) -> tuple[bool, str]:
        """Back up the volume (local + its remotes); True only when the archive and every upload succeeded."""
        m = self.m
        try:
            opts = m._resolve_backup_options(volume, remotes=None, encrypt=None, stop_containers=False, notify=False)
            task.add_log(f"Final backup of {volume}" + (f" (uploading to {len(opts['remotes'])} remote(s))" if opts["remotes"] else " (local only, no remotes configured)"))
            res = m._do_backup(task, volume, opts, "cleanup")
        except Exception as e:  # noqa: BLE001
            return False, str(e) or e.__class__.__name__
        # "synced" = uploaded; "skipped" = remote was disabled in the meantime; anything else = failed
        failed = {rid: r for rid, r in (res.get("uploads") or {}).items() if r not in ("synced", "skipped")}
        if failed:
            return False, "upload failed: " + ", ".join(f"{rid}: {r}" for rid, r in failed.items())
        if not res.get("size"):
            return False, "the backup archive is empty"
        task.add_log(f"Final backup of {volume}: {res['filename']} ({fmt_bytes(res['size'])})")
        return True, ""
