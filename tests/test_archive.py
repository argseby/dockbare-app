import gzip
import io
import tarfile
from datetime import datetime, timezone

from dockbare import archive, crypto
from tests.conftest import FakeDocker, make_tar


def test_filename_roundtrip():
    ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert archive.make_filename(ts, False) == "2026-01-02_03-04-05.tar.gz"
    assert archive.make_filename(ts, True) == "2026-01-02_03-04-05.tar.gz.enc"
    assert archive.parse_backup_filename("2026-01-02_03-04-05.tar.gz") == (ts, False)
    assert archive.parse_backup_filename("2026-01-02_03-04-05.tar.gz.enc") == (ts, True)
    assert archive.parse_backup_filename("junk.tar.gz") == (None, False)
    assert archive.parse_backup_filename("notes.txt") == (None, False)
    assert archive.is_backup_filename("x.tar.gz.enc") and not archive.is_backup_filename("x.tar.gz.enc.meta.json")


def _roundtrip(tmp_path, encrypt):
    tar = make_tar({"./one.txt": b"1" * 100, "./dir/two.bin": bytes(range(256)) * 300})
    d = FakeDocker(volumes=("v",), tar=tar)
    kr = crypto.Keyring("k", app_salt=b"\x05" * 16)
    res = archive.write_backup(d, kr, "v", tmp_path / "v", encrypt=encrypt, compression_level=3)
    assert res.raw_bytes == len(tar)
    assert res.filename.endswith(archive.ENC_EXT if encrypt else archive.PLAIN_EXT)
    assert archive.is_encrypted_file(res.path) == encrypt
    assert archive.sha256_file(res.path) == res.sha256
    # restore stream equals original tar
    out = b"".join(archive.open_backup_stream(res.path, kr))
    assert out == tar
    # decrypted stream is a valid gzip of the tar
    gz = b"".join(archive.open_decrypted_stream(res.path, kr))
    assert gzip.decompress(gz) == tar
    v = archive.verify_backup(res.path, kr)
    assert v["ok"] and v["files"] == 2 and v["bytes"] == 100 + 256 * 300
    # meta sidecar
    archive.write_meta(res, {"extra": 1})
    meta = archive.read_meta(res.path)
    assert meta["sha256"] == res.sha256 and meta["extra"] == 1
    # restore through docker
    archive.restore_backup(d, kr, "v", res.path)
    assert d.restored["v"] == tar and d.cleared == ["v"]
    return res


def test_plain_roundtrip(tmp_path):
    _roundtrip(tmp_path, encrypt=False)


def test_encrypted_roundtrip(tmp_path):
    _roundtrip(tmp_path, encrypt=True)


def test_wrong_key_verify_fails(tmp_path):
    res = _roundtrip(tmp_path, encrypt=True)
    other = crypto.Keyring("other", app_salt=b"\x05" * 16)
    v = archive.verify_backup(res.path, other)
    assert not v["ok"] and "key" in v["error"].lower()


def test_corrupt_archive_detected(tmp_path):
    res = _roundtrip(tmp_path, encrypt=False)
    with open(res.path, "r+b") as f:
        f.seek(40)
        f.write(b"\xff\xff\xff\xff")
    v = archive.verify_backup(res.path, crypto.Keyring(None))
    assert not v["ok"]


def test_failed_backup_leaves_no_partial_file(tmp_path):
    class Broken(FakeDocker):
        def volume_archive_stream(self, volume, chunk_size=65536):
            yield b"x" * 10
            raise RuntimeError("boom")

    d = Broken(volumes=("v",))
    try:
        archive.write_backup(d, crypto.Keyring(None), "v", tmp_path / "v", encrypt=False)
    except RuntimeError:
        pass
    assert list((tmp_path / "v").iterdir()) == []
