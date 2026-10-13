"""Backup archive pipeline: tar stream → gzip → (encrypt) → file, and back."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import crypto
from .dockerctl import DockerCtl

TS_FORMAT = "%Y-%m-%d_%H-%M-%S"
PLAIN_EXT = ".tar.gz"
ENC_EXT = ".tar.gz.enc"
META_EXT = ".meta.json"
CHUNK = 1024 * 1024

ProgressFn = Callable[[int], None]


class ArchiveError(Exception):
    pass


def is_backup_filename(name: str) -> bool:
    return name.endswith(PLAIN_EXT) or name.endswith(ENC_EXT)


def parse_backup_filename(name: str) -> tuple[datetime | None, bool]:
    """Return (timestamp UTC, encrypted) for a backup filename."""
    encrypted = name.endswith(ENC_EXT)
    stem = name[: -len(ENC_EXT)] if encrypted else (name[: -len(PLAIN_EXT)] if name.endswith(PLAIN_EXT) else None)
    if stem is None:
        return None, False
    try:
        return datetime.strptime(stem, TS_FORMAT).replace(tzinfo=timezone.utc), encrypted
    except ValueError:
        return None, encrypted


def make_filename(ts: datetime | None = None, encrypted: bool = False) -> str:
    ts = ts or datetime.now(timezone.utc)
    return ts.strftime(TS_FORMAT) + (ENC_EXT if encrypted else PLAIN_EXT)


def meta_path_for(path: str | Path) -> str:
    return str(path) + META_EXT


@dataclass
class BackupResult:
    path: str
    filename: str
    volume: str
    size: int
    sha256: str
    raw_bytes: int
    encrypted: bool
    created_at: float
    duration: float


def _gzip_stream(source: Iterable[bytes], level: int) -> Iterator[bytes]:
    comp = zlib.compressobj(level, zlib.DEFLATED, 31)
    for chunk in source:
        if not chunk:
            continue
        out = comp.compress(chunk)
        if out:
            yield out
    out = comp.flush()
    if out:
        yield out


def _gunzip_stream(source: Iterable[bytes]) -> Iterator[bytes]:
    dec = zlib.decompressobj(31)
    for chunk in source:
        if not chunk:
            continue
        try:
            out = dec.decompress(chunk)
        except zlib.error as e:
            raise ArchiveError(f"Corrupt gzip data: {e}") from e
        if out:
            yield out
    try:
        out = dec.flush()
    except zlib.error as e:
        raise ArchiveError(f"Corrupt gzip data: {e}") from e
    if out:
        yield out


def _counting(source: Iterable[bytes], counter: dict, key: str, progress: ProgressFn | None = None) -> Iterator[bytes]:
    for chunk in source:
        counter[key] = counter.get(key, 0) + len(chunk)
        if progress:
            progress(counter[key])
        yield chunk


def _file_chunks(path: str | Path, chunk: int = CHUNK) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            yield data


def write_backup(dockerctl: DockerCtl, keyring: crypto.Keyring, volume: str, dest_dir: Path, *,
                 encrypt: bool, compression_level: int = 6, progress: ProgressFn | None = None,
                 ts: datetime | None = None) -> BackupResult:
    if encrypt and not keyring.available:
        raise ArchiveError("Encryption requested but no encryption key is configured.")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = ts or datetime.now(timezone.utc)
    filename = make_filename(ts, encrypt)
    final_path = dest_dir / filename
    tmp_path = dest_dir / (filename + ".part")
    counters: dict[str, int] = {}
    started = time.time()

    stream: Iterable[bytes] = _counting(dockerctl.volume_archive_stream(volume), counters, "raw")
    stream = _gzip_stream(stream, compression_level)
    if encrypt:
        stream = crypto.encrypt_stream(stream, keyring)

    sha = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as f:
            for chunk in stream:
                f.write(chunk)
                sha.update(chunk)
                size += len(chunk)
                if progress:
                    progress(size)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return BackupResult(
        path=str(final_path), filename=filename, volume=volume, size=size, sha256=sha.hexdigest(),
        raw_bytes=counters.get("raw", 0), encrypted=encrypt, created_at=ts.timestamp(),
        duration=time.time() - started,
    )


def is_encrypted_file(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            return crypto.is_encrypted_header(f.read(4))
    except OSError:
        return False


def open_backup_stream(path: str | Path, keyring: crypto.Keyring, progress: ProgressFn | None = None) -> Iterator[bytes]:
    """Yield raw tar bytes from a (possibly encrypted) backup file."""
    counters: dict[str, int] = {}
    src: Iterable[bytes] = _counting(_file_chunks(path), counters, "read", progress)
    if is_encrypted_file(path):
        src = crypto.decrypt_stream(src, keyring)
    return _gunzip_stream(src)


def open_decrypted_stream(path: str | Path, keyring: crypto.Keyring) -> Iterator[bytes]:
    """Yield the plain ``.tar.gz`` bytes of a backup file (decrypting if needed)."""
    src: Iterable[bytes] = _file_chunks(path)
    if is_encrypted_file(path):
        src = crypto.decrypt_stream(src, keyring)
    return iter(src)


class _StreamReader(io.RawIOBase):
    def __init__(self, it: Iterable[bytes]):
        self._it = iter(it)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = len(b)
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        data, self._buf = self._buf[:n], self._buf[n:]
        b[: len(data)] = data
        return len(data)


def verify_backup(path: str | Path, keyring: crypto.Keyring, progress: ProgressFn | None = None) -> dict:
    """Decrypt, decompress and walk the tar; returns file statistics."""
    files = dirs = links = 0
    total = 0
    reader = _StreamReader(open_backup_stream(path, keyring, progress))
    try:
        with tarfile.open(fileobj=io.BufferedReader(reader, CHUNK), mode="r|") as tar:
            for m in tar:
                if m.isdir():
                    dirs += 1
                elif m.issym() or m.islnk():
                    links += 1
                else:
                    files += 1
                    total += m.size
    except (tarfile.TarError, crypto.CryptoError, ArchiveError, OSError) as e:
        return {"ok": False, "error": str(e), "files": files, "dirs": dirs, "links": links, "bytes": total}
    return {"ok": True, "files": files, "dirs": dirs, "links": links, "bytes": total}


def restore_backup(dockerctl: DockerCtl, keyring: crypto.Keyring, volume: str, path: str | Path, *,
                   clear: bool = True, progress: ProgressFn | None = None) -> None:
    if not Path(path).is_file():
        raise ArchiveError(f"Backup file not found: {path}")
    dockerctl.restore_archive(volume, open_backup_stream(path, keyring, progress), clear=clear)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    for chunk in _file_chunks(path):
        h.update(chunk)
    return h.hexdigest()


def write_meta(result: BackupResult, extra: dict | None = None) -> str:
    data = {"format": "dockbare-backup-meta", "version": 1, "app": "dockbare", **asdict(result)}
    if extra:
        data.update(extra)
    p = meta_path_for(result.path)
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)
    return p


def read_meta(path: str | Path) -> dict | None:
    p = meta_path_for(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
