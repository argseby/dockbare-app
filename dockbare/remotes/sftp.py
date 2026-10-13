"""SFTP remote (paramiko) with trust-on-first-use host key pinning."""
from __future__ import annotations

import hashlib
import base64
import io
import os
import posixpath
import stat

import paramiko

from .base import ProgressFn, Remote, RemoteError

BLOCK = 256 * 1024


def _fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class SFTPRemote(Remote):
    type = "sftp"

    def __init__(self, info):
        super().__init__(info)
        self.learned_fingerprint: str | None = None

    def _open(self):
        cfg = self.config
        host = (cfg.get("host") or "").strip()
        if not host:
            raise RemoteError("SFTP host is not configured.")
        port = int(cfg.get("port") or 22)
        user = cfg.get("username") or ""
        pwd = cfg.get("password") or None
        key_text = cfg.get("private_key") or None
        key_pass = cfg.get("key_passphrase") or None
        timeout = int(cfg.get("timeout") or 30)
        pinned = (cfg.get("host_key_fingerprint") or "").strip()

        transport = None
        try:
            sock_addr = (host, port)
            transport = paramiko.Transport(sock_addr)
            transport.banner_timeout = timeout
            transport.start_client(timeout=timeout)
            server_key = transport.get_remote_server_key()
            fp = _fingerprint(server_key)
            if pinned:
                if fp != pinned:
                    raise RemoteError(f"Host key mismatch for {host}: got {fp}, pinned {pinned}. "
                                      "Clear the pinned fingerprint if the server key really changed.")
            else:
                self.learned_fingerprint = fp
            pkey = None
            if key_text:
                pkey = self._load_key(key_text, key_pass)
            if pkey is not None:
                transport.auth_publickey(user, pkey)
            elif pwd is not None:
                transport.auth_password(user, pwd)
            else:
                raise RemoteError("SFTP needs a password or a private key.")
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                raise RemoteError("Could not open SFTP channel.")
            sftp.get_channel().settimeout(timeout * 4)
            return sftp
        except (paramiko.SSHException, OSError) as e:
            if transport is not None:
                transport.close()
            raise RemoteError(f"SFTP connection failed: {e}") from e
        except RemoteError:
            if transport is not None:
                transport.close()
            raise

    @staticmethod
    def _load_key(text: str, passphrase: str | None) -> paramiko.PKey:
        last: Exception | None = None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return cls.from_private_key(io.StringIO(text), password=passphrase)
            except paramiko.PasswordRequiredException as e:
                raise RemoteError("Private key requires a passphrase.") from e
            except paramiko.SSHException as e:
                last = e
        raise RemoteError(f"Could not load private key: {last}")

    def _close(self, conn) -> None:
        try:
            transport = conn.get_channel().get_transport()
            conn.close()
            if transport:
                transport.close()
        except Exception:  # noqa: BLE001
            pass

    def _test(self) -> str:
        self._ensure_dir(self.base_path)
        self.conn.listdir(self.base_path)
        fp = self.learned_fingerprint or self.config.get("host_key_fingerprint") or "?"
        return f"Connected; base path reachable. Host key {fp}."

    def _ensure_dir(self, path: str) -> None:
        sftp = self.conn
        parts = [p for p in path.split("/") if p]
        cur = "/" if path.startswith("/") else ""
        for part in parts:
            cur = posixpath.join(cur, part) if cur else part
            try:
                st = sftp.stat(cur)
                if not stat.S_ISDIR(st.st_mode):
                    raise RemoteError(f"{cur} exists and is not a directory.")
            except FileNotFoundError:
                try:
                    sftp.mkdir(cur)
                except OSError as e:
                    raise RemoteError(f"Cannot create remote directory {cur}: {e}") from e

    def _upload(self, local_path: str, remote_path: str, progress: ProgressFn | None) -> None:
        tmp = remote_path + ".part"
        try:
            self.conn.put(local_path, tmp, callback=(lambda sent, _total: progress(sent)) if progress else None)
            try:
                self.conn.remove(remote_path)
            except FileNotFoundError:
                pass
            self.conn.rename(tmp, remote_path)
        except (OSError, paramiko.SSHException) as e:
            raise RemoteError(f"SFTP upload failed: {e}") from e

    def _download(self, remote_path: str, local_path: str, progress: ProgressFn | None) -> None:
        tmp = local_path + ".part"
        try:
            self.conn.get(remote_path, tmp, callback=(lambda got, _total: progress(got)) if progress else None)
            os.replace(tmp, local_path)
        except (OSError, paramiko.SSHException) as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RemoteError(f"SFTP download failed: {e}") from e

    def _delete(self, remote_path: str) -> bool:
        try:
            self.conn.remove(remote_path)
            return True
        except FileNotFoundError:
            return False
        except (OSError, paramiko.SSHException) as e:
            raise RemoteError(f"SFTP delete failed: {e}") from e

    def _size(self, remote_path: str) -> int | None:
        try:
            return int(self.conn.stat(remote_path).st_size or 0)
        except FileNotFoundError:
            return None
        except (OSError, paramiko.SSHException) as e:
            raise RemoteError(f"SFTP stat failed: {e}") from e

    def _listdir(self, path: str) -> list[tuple[str, bool, int | None]]:
        try:
            entries = self.conn.listdir_attr(path)
        except FileNotFoundError:
            return []
        except (OSError, paramiko.SSHException) as e:
            raise RemoteError(f"SFTP list failed: {e}") from e
        return [(e.filename, stat.S_ISDIR(e.st_mode or 0), e.st_size) for e in entries
                if e.filename not in (".", "..")]

    def _read_small(self, remote_path: str, limit: int) -> bytes | None:
        try:
            st = self.conn.stat(remote_path)
            if (st.st_size or 0) > limit:
                return None
            with self.conn.open(remote_path, "rb") as f:
                return f.read(limit)
        except FileNotFoundError:
            return None
        except (OSError, paramiko.SSHException) as e:
            raise RemoteError(f"SFTP read failed: {e}") from e
