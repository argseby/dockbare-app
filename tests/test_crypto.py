import os
import pytest

from dockbare import crypto


@pytest.fixture(scope="module")
def keyring():
    return crypto.Keyring("correct horse battery staple", app_salt=b"\x01" * 16)


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1000, 1024, 1025, 3 * 1024, 3 * 1024 + 1, 10 * 1024 + 7])
def test_roundtrip_sizes(keyring, size):
    data = os.urandom(size)
    enc = crypto.encrypt_bytes(data, keyring, chunk_size=1024)
    assert enc[:4] == b"DKBE"
    assert crypto.decrypt_bytes(enc, keyring) == data


def test_roundtrip_streaming_odd_pieces(keyring):
    data = os.urandom(5000)
    enc = b"".join(crypto.encrypt_stream([data[:7], data[7:700], data[700:]], keyring, chunk_size=1024))
    pieces = [enc[i:i + 13] for i in range(0, len(enc), 13)]
    assert b"".join(crypto.decrypt_stream(pieces, keyring)) == data


def test_wrong_key(keyring):
    enc = crypto.encrypt_bytes(b"secret", keyring)
    other = crypto.Keyring("wrong", app_salt=b"\x01" * 16)
    with pytest.raises(crypto.WrongKeyError):
        crypto.decrypt_bytes(enc, other)


def test_truncation_detected(keyring):
    data = os.urandom(3000)
    enc = crypto.encrypt_bytes(data, keyring, chunk_size=1024)
    # drop the final chunk entirely
    truncated = enc[: crypto.HEADER_LEN + 2 * (1024 + 16)]
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(truncated, keyring)
    # drop a few bytes of the last chunk
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(enc[:-3], keyring)


def test_tamper_detected(keyring):
    data = os.urandom(3000)
    enc = bytearray(crypto.encrypt_bytes(data, keyring, chunk_size=1024))
    enc[crypto.HEADER_LEN + 10] ^= 0xFF
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(bytes(enc), keyring)
    # tamper with header chunk size
    enc2 = bytearray(crypto.encrypt_bytes(data, keyring, chunk_size=1024))
    enc2[40] ^= 0x01
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(bytes(enc2), keyring)


def test_chunk_reorder_detected(keyring):
    data = os.urandom(3 * 1024)
    enc = crypto.encrypt_bytes(data, keyring, chunk_size=1024)
    h = crypto.HEADER_LEN
    c = 1024 + 16
    swapped = enc[:h] + enc[h + c:h + 2 * c] + enc[h:h + c] + enc[h + 2 * c:]
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(swapped, keyring)


def test_fingerprint_stable(keyring):
    fp = keyring.fingerprint()
    assert fp and len(fp) == 12
    assert crypto.Keyring("correct horse battery staple", app_salt=b"\x02" * 16).fingerprint() == fp
    assert crypto.Keyring("other", app_salt=b"\x01" * 16).fingerprint() != fp


def test_secretbox_roundtrip():
    box = crypto.SecretBox("app-secret")
    token = box.encrypt("p@ss")
    assert token.startswith("enc:v1:")
    assert box.decrypt(token) == "p@ss"
    assert box.decrypt("legacy-plain") == "legacy-plain"
    with pytest.raises(crypto.CryptoError):
        crypto.SecretBox("other").decrypt(token)
