"""Host metrics from /proc and /sys (no dependencies, Linux only).

Inside a container most of /proc is host-wide anyway: ``stat`` (CPU), ``meminfo``,
``loadavg`` and ``diskstats`` are not namespaced, and /sys shows the host's
temperature sensors. Network counters are per network namespace, so host-wide
traffic needs the host's /proc mounted (``/proc:/host/proc:ro``); without it the
network value is left empty and the UI falls back to the containers' sum.
Disk usage is reported for the filesystems holding Dockbare's data and backup
directories (normally the Docker data disk) plus any ``DOCKBARE_HOST_DISKS`` paths.
"""
from __future__ import annotations

import glob
import os
import re

SECTOR = 512
VIRTUAL_DISK = re.compile(r"^(loop|ram|zram|sr|fd|md|dm-|nbd)")
PARTITION = re.compile(r"^((sd|hd|vd|xvd)[a-z]+\d+|(nvme\d+n\d+|mmcblk\d+)p\d+)$")


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


class HostReader:
    def __init__(self, proc: str = "/proc", sys: str = "/sys", disk_paths: list[tuple[str, str]] | None = None):
        self.proc = proc.rstrip("/") or "/proc"
        self.sys = sys.rstrip("/") or "/sys"
        self.host_proc = self.proc != "/proc"
        self.disk_paths = disk_paths or []

    # ------------------------------------------------------------ counters
    def cpu(self) -> dict | None:
        text = _read(f"{self.proc}/stat")
        if not text:
            return None
        total = idle = None
        cpus = 0
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "cpu":
                vals = [int(v) for v in parts[1:]]
                # user nice system idle iowait irq softirq steal (guest/guest_nice are already in user/nice)
                total = sum(vals[:8])
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            elif re.match(r"^cpu\d+$", parts[0]):
                cpus += 1
        if total is None:
            return None
        return {"cpu_total": total, "cpu_idle": idle, "cpus": cpus or 1}

    def load(self) -> tuple[float, float, float] | None:
        text = _read(f"{self.proc}/loadavg")
        try:
            a, b, c = (float(x) for x in text.split()[:3])  # type: ignore[union-attr]
            return a, b, c
        except (AttributeError, ValueError):
            return None

    def memory(self) -> dict | None:
        text = _read(f"{self.proc}/meminfo")
        if not text:
            return None
        kv = {}
        for line in text.splitlines():
            k, _, rest = line.partition(":")
            try:
                kv[k.strip()] = int(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                continue
        total = kv.get("MemTotal")
        if not total:
            return None
        avail = kv.get("MemAvailable", kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0))
        return {"mem_total": total, "mem_used": max(0, total - avail),
                "swap_total": kv.get("SwapTotal", 0), "swap_used": max(0, kv.get("SwapTotal", 0) - kv.get("SwapFree", 0))}

    def _whole_disks(self) -> set[str] | None:
        names = [os.path.basename(p) for p in glob.glob(f"{self.sys}/block/*")]
        return set(names) if names else None

    def diskstats(self) -> dict | None:
        text = _read(f"{self.proc}/diskstats")
        if not text:
            return None
        whole = self._whole_disks()
        r = w = 0
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 10:
                continue
            name = parts[2]
            if VIRTUAL_DISK.match(name):
                continue
            if (name not in whole) if whole is not None else bool(PARTITION.match(name)):
                continue  # partitions would count the same I/O twice
            r += int(parts[5]) * SECTOR
            w += int(parts[9]) * SECTOR
        return {"disk_r": r, "disk_w": w}

    def network(self) -> dict | None:
        if not self.host_proc:
            return None  # /proc/net is the container's own namespace
        text = _read(f"{self.proc}/1/net/dev")
        if not text:
            return None
        rx = tx = 0
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo" or name.startswith(("veth", "docker", "br-", "virbr", "cni", "flannel", "vxlan")):
                continue  # count only physical/uplink traffic, not container bridges (that would double it)
            vals = rest.split()
            if len(vals) >= 9:
                rx += int(vals[0])
                tx += int(vals[8])
        return {"net_rx": rx, "net_tx": tx}

    def temperature(self) -> float | None:
        vals = []
        for p in glob.glob(f"{self.sys}/class/hwmon/hwmon*/temp*_input") + glob.glob(f"{self.sys}/class/thermal/thermal_zone*/temp"):
            t = _read(p)
            try:
                c = int((t or "").strip()) / 1000.0
            except ValueError:
                continue
            if 0 < c < 150:
                vals.append(c)
        return round(max(vals), 1) if vals else None

    def read(self) -> dict:
        """One raw reading: cumulative counters plus current gauges (missing parts are None)."""
        out: dict = {"cpu": self.cpu(), "load": self.load(), "mem": self.memory(), "disk": self.diskstats(),
                     "net": self.network(), "temp": self.temperature()}
        return out

    def disks(self) -> list[dict]:
        """Usage of every distinct filesystem behind the configured paths."""
        seen: dict[int, dict] = {}
        for label, path in self.disk_paths:
            try:
                st = os.stat(path)
                vfs = os.statvfs(path)
            except OSError:
                continue
            if st.st_dev in seen:
                if label not in seen[st.st_dev]["labels"]:
                    seen[st.st_dev]["labels"].append(label)
                continue
            total = vfs.f_blocks * vfs.f_frsize
            free = vfs.f_bavail * vfs.f_frsize
            used = max(0, total - vfs.f_bfree * vfs.f_frsize)
            if total <= 0:
                continue
            # like df: used / (used + available), i.e. blocks reserved for root count as unavailable
            pct = round(used / (used + free) * 100, 1) if used + free else None
            seen[st.st_dev] = {"mount": path, "labels": [label], "total": total, "used": used, "free": free, "pct": pct}
        out = []
        for d in seen.values():
            d["name"] = " + ".join(d.pop("labels"))
            out.append(d)
        return out


def host_rates(prev: dict, cur: dict, dt: float) -> dict | None:
    """Turn two raw readings ``dt`` seconds apart into one host sample, or None to skip."""
    if dt <= 0 or not prev or not cur.get("cpu") or not prev.get("cpu") or not cur.get("mem"):
        return None
    c0, c1 = prev["cpu"], cur["cpu"]
    d_total = c1["cpu_total"] - c0["cpu_total"]
    d_idle = c1["cpu_idle"] - c0["cpu_idle"]
    if d_total <= 0 or d_idle < 0:
        return None  # counter wrap / reboot
    sample = {"cpu": round(max(0.0, min(100.0, (1 - d_idle / d_total) * 100)), 2), "cpus": c1["cpus"]}
    load = cur.get("load") or (None, None, None)
    sample.update({"load1": load[0], "load5": load[1], "load15": load[2]})
    sample.update(cur["mem"])
    for key, fields in (("disk", ("disk_r", "disk_w")), ("net", ("net_rx", "net_tx"))):
        a, b = prev.get(key), cur.get(key)
        for f in fields:
            sample[f] = round((b[f] - a[f]) / dt, 1) if a and b and b[f] >= a[f] else None
    sample["temp"] = cur.get("temp")
    return sample
