from __future__ import annotations

import posixpath
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..archive import is_backup_filename

ProgressFn = Callable[[int], None]

SECRET_FIELDS = ("password", "private_key", "key_passphrase")
REMOTE_TYPES = ("ftp", "sftp")
OWN = object()  # sentinel: "this instance's own folder"


class RemoteError(Exception):
    pass


@dataclass
class RemoteFile:
    name: str
    size: int | None = None
    mtime: float | None = None


@dataclass
class RemoteInfo:
    id: str
    name: str
    type: str
    enabled: bool = True
    config: dict = field(default_factory=dict)
    instance: str | None = None  # name of this Dockbare instance (for per-instance folders)


class Remote:
    """Base class. Subclasses implement ``_open``/``_close`` and the primitives."""

    type = "base"

    def __init__(self, info: RemoteInfo):
        self.info = info
        self.config = info.config
        self._conn = None
        self._depth = 0

    # --- lifecycle -----------------------------------------------------------
    def _open(self):
        raise NotImplementedError

    def _close(self, conn) -> None:
        pass

    @contextmanager
    def session(self) -> Iterator[None]:
        if self._depth == 0:
            self._conn = self._open()
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._conn is not None:
                try:
                    self._close(self._conn)
                finally:
                    self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise RemoteError("Not connected (use `with remote.session()`).")
        return self._conn

    # --- paths ---------------------------------------------------------------
    @property
    def base_path(self) -> str:
        base = (self.config.get("base_path") or "").strip().replace("\\", "/")
        base = base.rstrip("/")
        if not base or base == "/":
            raise RemoteError("Remote base path must be set and must not be the root directory.")
        return base

    @property
    def instance(self) -> str | None:
        """Folder for this instance below the base path, or None for the flat v2 layout."""
        if self.config.get("instance_dir", False) and self.info.instance:
            return self.info.instance
        return None

    def _inst(self, instance) -> str | None:
        return self.instance if instance is OWN else instance

    def volume_dir(self, volume: str, instance=OWN) -> str:
        inst = self._inst(instance)
        return posixpath.join(self.base_path, inst, volume) if inst else posixpath.join(self.base_path, volume)

    def remote_path(self, volume: str, filename: str, instance=OWN) -> str:
        return posixpath.join(self.volume_dir(volume, instance), filename)

    # --- API (each wraps itself in a session) --------------------------------
    def test(self) -> tuple[bool, str]:
        try:
            with self.session():
                msg = self._test()
            return True, msg
        except Exception as e:  # noqa: BLE001
            return False, f"{e.__class__.__name__}: {e}"

    def _test(self) -> str:
        raise NotImplementedError

    def upload(self, local_path: str, volume: str, filename: str, progress: ProgressFn | None = None) -> None:
        with self.session():
            self._ensure_dir(self.volume_dir(volume))
            self._upload(local_path, self.remote_path(volume, filename), progress)

    def download(self, volume: str, filename: str, local_path: str, progress: ProgressFn | None = None, instance=OWN) -> None:
        with self.session():
            self._download(self.remote_path(volume, filename, instance), local_path, progress)

    def delete(self, volume: str, filename: str, instance=OWN) -> bool:
        with self.session():
            return self._delete(self.remote_path(volume, filename, instance))

    def exists(self, volume: str, filename: str, instance=OWN) -> bool:
        with self.session():
            return self._size(self.remote_path(volume, filename, instance)) is not None

    def size(self, volume: str, filename: str, instance=OWN) -> int | None:
        with self.session():
            return self._size(self.remote_path(volume, filename, instance))

    def list_tree(self) -> list[dict]:
        """Every volume folder under the base path, in both layouts.

        Returns ``[{"instance": str | None, "volume": str, "files": [RemoteFile]}]``.
        ``<base>/<volume>/<backups>`` yields instance ``None`` (flat v2 layout);
        ``<base>/<instance>/<volume>/<backups>`` yields the instance name.
        """
        with self.session():
            out: list[dict] = []
            for name, is_dir, _size in self._listdir(self.base_path):
                if not is_dir:
                    continue
                sub = self._listdir(posixpath.join(self.base_path, name))
                files = [RemoteFile(n, sz) for n, d, sz in sub if not d]
                if any(is_backup_filename(f.name) for f in files):
                    out.append({"instance": None, "volume": name, "files": files})
                for sname, sdir, _ in sub:
                    if not sdir:
                        continue
                    sfiles = [RemoteFile(n, sz) for n, d, sz in self._listdir(posixpath.join(self.base_path, name, sname)) if not d]
                    if any(is_backup_filename(f.name) for f in sfiles):
                        out.append({"instance": name, "volume": sname, "files": sfiles})
            return out

    def read_small_file(self, volume: str, filename: str, limit: int = 256 * 1024, instance=OWN) -> bytes | None:
        with self.session():
            return self._read_small(self.remote_path(volume, filename, instance), limit)

    # --- primitives ----------------------------------------------------------
    def _ensure_dir(self, path: str) -> None:
        raise NotImplementedError

    def _upload(self, local_path: str, remote_path: str, progress: ProgressFn | None) -> None:
        raise NotImplementedError

    def _download(self, remote_path: str, local_path: str, progress: ProgressFn | None) -> None:
        raise NotImplementedError

    def _delete(self, remote_path: str) -> bool:
        raise NotImplementedError

    def _size(self, remote_path: str) -> int | None:
        raise NotImplementedError

    def _listdir(self, path: str) -> list[tuple[str, bool, int | None]]:
        """Return [(name, is_dir, size)]."""
        raise NotImplementedError

    def _read_small(self, remote_path: str, limit: int) -> bytes | None:
        raise NotImplementedError


def build_remote(info: RemoteInfo) -> Remote:
    from .ftp import FTPRemote
    from .sftp import SFTPRemote

    if info.type == "ftp":
        return FTPRemote(info)
    if info.type == "sftp":
        return SFTPRemote(info)
    raise RemoteError(f"Unknown remote type: {info.type}")
