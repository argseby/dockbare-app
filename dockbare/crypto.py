"""DKBE — Dockbare encrypted file format (streaming, authenticated).

Layout::

    header (58 bytes, authenticated as AAD of every chunk):
        magic      4   b"DKBE"
        version    1   0x01
        kdf        1   0x01 = scrypt(n=2**16, r=8, p=1, len=32)
        kdf_salt   16  salt for deriving the master key from the passphrase
        file_salt  16  HKDF salt for the per-file key
        chunk_size 4   plaintext bytes per chunk (big endian)
        key_check  16  HMAC-SHA256(master_key, b"dockbare-key-check")[:16]
    chunks:
        AES-256-GCM(key=file_key, nonce=counter(11 bytes BE) || last_flag(1 byte))
        ciphertext || tag(16)

All chunks except the last carry exactly ``chunk_size`` plaintext bytes; the
last chunk is always strictly shorter (it may be empty), which is how the
decoder recognises it without a length prefix.

The master key is derived once per ``kdf_salt`` and cached (scrypt is slow by
design). Each file gets its own key through HKDF so that nonce reuse across
files is impossible. The last-chunk flag prevents truncation attacks, and the
header being AAD prevents header tampering.

Secrets stored at rest (remote passwords) use ``SecretBox`` which wraps the same
primitives with a key derived from the app secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import threading
from typing import Iterable, Iterator

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"DKBE"
VERSION = 1
KDF_SCRYPT = 1
HEADER_LEN = 58
DEFAULT_CHUNK = 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
TAG_LEN = 16
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1
KEY_CHECK_INFO = b"dockbare-key-check"
FINGERPRINT_INFO = b"dockbare-key-fingerprint"
FINGERPRINT_SALT = b"dockbare-fingerprint-v1\x00"


class CryptoError(Exception):
    pass


class WrongKeyError(CryptoError):
    pass


def derive_master_key(passphrase: str, kdf_salt: bytes) -> bytes:
    kdf = Scrypt(salt=kdf_salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _file_key(master: bytes, file_salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=file_salt, info=b"dockbare-file-key").derive(master)


def _key_check(master: bytes) -> bytes:
    return hmac.new(master, KEY_CHECK_INFO, hashlib.sha256).digest()[:16]


def _nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


class Keyring:
    """Holds the passphrase and caches derived master keys per kdf salt."""

    def __init__(self, passphrase: str | None, app_salt: bytes | None = None):
        self.passphrase = passphrase or None
        self._cache: dict[bytes, bytes] = {}
        self._lock = threading.Lock()
        self._fingerprint: str | None = None
        self.app_salt = app_salt or os.urandom(16)

    @property
    def available(self) -> bool:
        return bool(self.passphrase)

    def master_key(self, kdf_salt: bytes) -> bytes:
        if not self.passphrase:
            raise CryptoError("No encryption key configured (DOCKBARE_ENCRYPTION_KEY).")
        with self._lock:
            key = self._cache.get(kdf_salt)
        if key is None:
            key = derive_master_key(self.passphrase, kdf_salt)
            with self._lock:
                self._cache[kdf_salt] = key
        return key

    def fingerprint(self) -> str | None:
        """Short public identifier of the passphrase, identical on every installation.

        Derived with a fixed salt so that a fresh install can tell whether its
        key matches the one used for remote backups (the sidecar metadata
        stores this value). Anyone holding a ciphertext can mount the same
        offline guessing attack, so this leaks nothing new.
        """
        if not self.passphrase:
            return None
        with self._lock:
            cached = self._fingerprint
        if cached:
            return cached
        master = derive_master_key(self.passphrase, FINGERPRINT_SALT)
        fp = hmac.new(master, FINGERPRINT_INFO, hashlib.sha256).hexdigest()[:12]
        with self._lock:
            self._fingerprint = fp
        return fp

    def key_check_for_salt(self, kdf_salt: bytes) -> bytes:
        return _key_check(self.master_key(kdf_salt))


def parse_header(header: bytes) -> dict:
    if len(header) < HEADER_LEN or header[:4] != MAGIC:
        raise CryptoError("Not a Dockbare encrypted file.")
    version, kdf = header[4], header[5]
    if version != VERSION:
        raise CryptoError(f"Unsupported DKBE version {version}.")
    if kdf != KDF_SCRYPT:
        raise CryptoError(f"Unsupported KDF {kdf}.")
    chunk_size = struct.unpack(">I", header[38:42])[0]
    if chunk_size <= 0 or chunk_size > MAX_CHUNK:
        raise CryptoError("Invalid chunk size in header.")
    return {
        "kdf_salt": header[6:22],
        "file_salt": header[22:38],
        "chunk_size": chunk_size,
        "key_check": header[42:58],
    }


def build_header(kdf_salt: bytes, file_salt: bytes, chunk_size: int, key_check: bytes) -> bytes:
    h = MAGIC + bytes([VERSION, KDF_SCRYPT]) + kdf_salt + file_salt + struct.pack(">I", chunk_size) + key_check
    assert len(h) == HEADER_LEN
    return h


def is_encrypted_header(first_bytes: bytes) -> bool:
    return first_bytes[:4] == MAGIC


def _rechunk(source: Iterable[bytes], size: int) -> Iterator[bytes]:
    """Yield exactly ``size``-byte chunks, then one final shorter (possibly empty) chunk."""
    buf = bytearray()
    for piece in source:
        if not piece:
            continue
        buf.extend(piece)
        while len(buf) >= size:
            yield bytes(buf[:size])
            del buf[:size]
    yield bytes(buf)


def encrypt_stream(source: Iterable[bytes], keyring: Keyring, chunk_size: int = DEFAULT_CHUNK) -> Iterator[bytes]:
    if chunk_size <= 0 or chunk_size > MAX_CHUNK:
        raise CryptoError("Invalid chunk size.")
    kdf_salt = keyring.app_salt
    master = keyring.master_key(kdf_salt)
    file_salt = os.urandom(16)
    header = build_header(kdf_salt, file_salt, chunk_size, _key_check(master))
    aead = AESGCM(_file_key(master, file_salt))
    yield header

    counter = 0
    for chunk in _rechunk(source, chunk_size):
        last = len(chunk) < chunk_size
        yield aead.encrypt(_nonce(counter, last), chunk, header)
        counter += 1


def decrypt_stream(source: Iterable[bytes], keyring: Keyring) -> Iterator[bytes]:
    it = iter(source)
    buf = bytearray()
    for piece in it:
        buf.extend(piece)
        if len(buf) >= HEADER_LEN:
            break
    if len(buf) < HEADER_LEN:
        raise CryptoError("File too short to be a DKBE archive.")
    header = bytes(buf[:HEADER_LEN])
    del buf[:HEADER_LEN]
    info = parse_header(header)
    master = keyring.master_key(info["kdf_salt"])
    if not hmac.compare_digest(_key_check(master), info["key_check"]):
        raise WrongKeyError("The configured encryption key does not match this file.")
    aead = AESGCM(_file_key(master, info["file_salt"]))
    ct_size = info["chunk_size"] + TAG_LEN
    counter = 0

    def dec(ct: bytes, last: bool) -> bytes:
        nonlocal counter
        try:
            pt = aead.decrypt(_nonce(counter, last), ct, header)
        except Exception as e:
            raise CryptoError("Archive is corrupted or truncated (authentication failed).") from e
        counter += 1
        return pt

    for piece in it:
        buf.extend(piece)
        while len(buf) >= ct_size:
            yield dec(bytes(buf[:ct_size]), False)
            del buf[:ct_size]
    while len(buf) >= ct_size:
        yield dec(bytes(buf[:ct_size]), False)
        del buf[:ct_size]
    if len(buf) < TAG_LEN:
        raise CryptoError("Archive is truncated (missing final chunk).")
    yield dec(bytes(buf), True)


def encrypt_bytes(data: bytes, keyring: Keyring, chunk_size: int = DEFAULT_CHUNK) -> bytes:
    return b"".join(encrypt_stream([data], keyring, chunk_size))


def decrypt_bytes(data: bytes, keyring: Keyring) -> bytes:
    return b"".join(decrypt_stream([data], keyring))


def read_header(path: str) -> dict | None:
    """Return parsed header of an encrypted file, or None when not encrypted."""
    with open(path, "rb") as f:
        head = f.read(HEADER_LEN)
    if not is_encrypted_header(head):
        return None
    return parse_header(head)


class SecretBox:
    """Encrypts small secrets (remote passwords) at rest with the app secret."""

    PREFIX = "enc:v1:"

    def __init__(self, app_secret: str):
        self._key = hashlib.sha256(b"dockbare-secretbox:" + app_secret.encode("utf-8")).digest()

    def encrypt(self, plaintext: str | None) -> str:
        if not plaintext:
            return ""
        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), b"secretbox")
        return self.PREFIX + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        if not token.startswith(self.PREFIX):
            return token  # legacy plaintext value
        raw = base64.urlsafe_b64decode(token[len(self.PREFIX):])
        nonce, ct = raw[:12], raw[12:]
        try:
            return AESGCM(self._key).decrypt(nonce, ct, b"secretbox").decode("utf-8")
        except Exception as e:
            raise CryptoError("Stored secret cannot be decrypted; did DOCKBARE_SECRET_KEY change?") from e

    def is_encrypted(self, token: str | None) -> bool:
        return bool(token) and token.startswith(self.PREFIX)
