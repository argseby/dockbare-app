"""FTP / FTPS (explicit and implicit TLS) remote."""
from __future__ import annotations

import os
import socket
import ssl
from ftplib import FTP, FTP_TLS, error_perm

from .base import ProgressFn, Remote, RemoteError

BLOCK = 256 * 1024


class _ImplicitFTP_TLS(FTP_TLS):
    """FTP over implicit TLS (usually port 990): wrap the control socket right away."""

    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.pop("context", None) or ssl.create_default_context()
        super().__init__(*args, context=self._ctx, **kwargs)

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.af = sock.family
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class FTPRemote(Remote):
    type = "ftp"

    def _open(self):
        cfg = self.config
        host = (cfg.get("host") or "").strip()
        if not host:
            raise RemoteError("FTP host is not configured.")
        port = int(cfg.get("port") or 21)
        user = cfg.get("username") or "anonymous"
        pwd = cfg.get("password") or ""
        secure = bool(cfg.get("secure", False))
        mode = (cfg.get("tls_mode") or "explicit").lower()
        passive = bool(cfg.get("passive", True))
        nat_fix = bool(cfg.get("nat_pasv_fix", False))
        verify = bool(cfg.get("verify_tls", True))
        timeout = int(cfg.get("timeout") or 30)

        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            if secure and mode == "implicit":
                ftp = _ImplicitFTP_TLS(context=ctx, timeout=timeout)
                ftp.connect(host, port)
                ftp.login(user=user, passwd=pwd)
                ftp.prot_p()
            elif secure:
                ftp = FTP_TLS(context=ctx, timeout=timeout)
                ftp.connect(host, port)
                ftp.auth()
                ftp.login(user=user, passwd=pwd)
                ftp.prot_p()
            else:
                ftp = FTP(timeout=timeout)
                ftp.connect(host, port)
                ftp.login(user=user, passwd=pwd)
        except (OSError, ssl.SSLError, error_perm) as e:
            raise RemoteError(f"FTP connection failed: {e}") from e
        ftp.set_pasv(passive)
        if passive and nat_fix:
            orig = ftp.makepasv

            def _makepasv():
                _h, p = orig()
                return host, p

            ftp.makepasv = _makepasv  # type: ignore[assignment]
        return ftp

    def _close(self, conn) -> None:
        try:
            conn.quit()
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _test(self) -> str:
        cwd = self.conn.pwd()
        self._ensure_dir(self.base_path)
        self.conn.cwd(self.base_path)
        return f"Connected; base path reachable (server cwd was {cwd})."

    def _ensure_dir(self, path: str) -> None:
        ftp = self.conn
        parts = [p for p in path.split("/") if p]
        absolute = path.startswith("/")
        cur = "/" if absolute else ""
        for part in parts:
            cur = (cur.rstrip("/") + "/" + part) if cur else part
            try:
                ftp.cwd(cur)
            except error_perm:
                try:
                    ftp.mkd(cur)
                except error_perm as e:
                    raise RemoteError(f"Cannot create remote directory {cur}: {e}") from e

    def _upload(self, local_path: str, remote_path: str, progress: ProgressFn | None) -> None:
        sent = 0

        def cb(block: bytes) -> None:
            nonlocal sent
            sent += len(block)
            if progress:
                progress(sent)

        tmp = remote_path + ".part"
        try:
            with open(local_path, "rb") as f:
                self.conn.storbinary(f"STOR {tmp}", f, blocksize=BLOCK, callback=cb)
            try:
                self.conn.delete(remote_path)
            except error_perm:
                pass
            self.conn.rename(tmp, remote_path)
        except (OSError, error_perm) as e:
            raise RemoteError(f"FTP upload failed: {e}") from e

    def _download(self, remote_path: str, local_path: str, progress: ProgressFn | None) -> None:
        got = 0
        tmp = local_path + ".part"
        try:
            with open(tmp, "wb") as f:
                def cb(block: bytes) -> None:
                    nonlocal got
                    f.write(block)
                    got += len(block)
                    if progress:
                        progress(got)

                self.conn.retrbinary(f"RETR {remote_path}", cb, blocksize=BLOCK)
            os.replace(tmp, local_path)
        except (OSError, error_perm) as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RemoteError(f"FTP download failed: {e}") from e

    def _delete(self, remote_path: str) -> bool:
        try:
            self.conn.delete(remote_path)
            return True
        except error_perm as e:
            if "550" in str(e):
                return False
            raise RemoteError(f"FTP delete failed: {e}") from e

    def _size(self, remote_path: str) -> int | None:
        try:
            self.conn.voidcmd("TYPE I")
            return self.conn.size(remote_path)
        except error_perm:
            return None
        except OSError as e:
            raise RemoteError(f"FTP error: {e}") from e

    def _listdir(self, path: str) -> list[tuple[str, bool, int | None]]:
        ftp = self.conn
        out: list[tuple[str, bool, int | None]] = []
        try:
            for name, facts in ftp.mlsd(path, facts=["type", "size"]):
                if name in (".", ".."):
                    continue
                typ = facts.get("type", "file")
                if typ in ("cdir", "pdir"):
                    continue
                size = int(facts["size"]) if facts.get("size", "").isdigit() else None
                out.append((name, typ == "dir", size))
            return out
        except error_perm:
            pass  # MLSD unsupported → fallback
        except OSError as e:
            raise RemoteError(f"FTP list failed: {e}") from e
        try:
            names = ftp.nlst(path)
        except error_perm as e:
            if "550" in str(e):
                return []
            raise RemoteError(f"FTP list failed: {e}") from e
        for full in names:
            name = full.rsplit("/", 1)[-1]
            if name in (".", ".."):
                continue
            full_path = path.rstrip("/") + "/" + name
            is_dir = False
            size = None
            try:
                ftp.voidcmd("TYPE I")
                size = ftp.size(full_path)
            except error_perm:
                # SIZE fails on directories on most servers
                try:
                    ftp.cwd(full_path)
                    is_dir = True
                except error_perm:
                    pass
            out.append((name, is_dir, size))
        return out

    def _read_small(self, remote_path: str, limit: int) -> bytes | None:
        chunks: list[bytes] = []
        total = 0

        def cb(block: bytes) -> None:
            nonlocal total
            total += len(block)
            if total <= limit:
                chunks.append(block)

        try:
            self.conn.retrbinary(f"RETR {remote_path}", cb)
        except error_perm:
            return None
        except OSError as e:
            raise RemoteError(f"FTP read failed: {e}") from e
        return b"".join(chunks) if total <= limit else None
