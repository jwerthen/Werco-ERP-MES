"""Laser-package zip-bomb caps (``laser_nest_service.check_laser_zip_limits``) -- roadmap #9-3b.

The caps read the archive's central directory and refuse BEFORE anything is extracted, so these
tests assert both the refusal and that the destination holds no file afterwards. The last test pins
the assumption the size cap rests on: CPython never writes more of a member than its declared
``file_size``, so a header that under-declares a member cannot smuggle bytes past the sum.

The endpoint mapping (413 on the preview) is in ``tests/api/test_ai_upload_bounds.py``.
"""

import io
import struct
import zipfile
from pathlib import Path

import pytest

from app.services import laser_nest_service
from app.services.laser_nest_service import (
    LaserZipLimitError,
    check_laser_zip_limits,
    extract_laser_nest_zip_within_limits,
)

pytestmark = pytest.mark.unit


def _zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_zip(tmp_path: Path, members: dict) -> str:
    path = tmp_path / "package.zip"
    path.write_bytes(_zip_bytes(members))
    return str(path)


def _files_under(root: Path) -> list:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_design_defaults():
    """The caps the design fixed (§1.4): 200 MB unpacked, 200 members. The PDF cap stays 50."""
    assert laser_nest_service.LASER_ZIP_MAX_UNCOMPRESSED_BYTES == 200 * 1024 * 1024
    assert laser_nest_service.LASER_ZIP_MAX_MEMBERS == 200
    assert laser_nest_service.LASER_PDF_PACKAGE_MAX == 50


def test_member_count_over_the_default_cap_is_refused_and_nothing_is_written(tmp_path):
    """201 empty members: cheap to build, over the real (unpatched) 200-member cap."""
    zip_path = _write_zip(tmp_path, {f"nest-{n:03d}.cnc": b"" for n in range(201)})
    dest = tmp_path / "out"

    with pytest.raises(LaserZipLimitError) as exc_info:
        extract_laser_nest_zip_within_limits(zip_path, str(dest))

    message = str(exc_info.value)
    assert "201 files and folders" in message
    assert "the limit is 200" in message
    assert "Split the package" in message
    assert _files_under(dest) == []


def test_uncompressed_total_over_the_cap_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    """A deflated run of zeros: a few hundred bytes compressed, 64 KB declared -- the bomb shape."""
    monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_UNCOMPRESSED_BYTES", 32 * 1024)
    zip_path = _write_zip(tmp_path, {"a.pdf": b"\0" * (32 * 1024), "b.pdf": b"\0" * (32 * 1024)})
    assert Path(zip_path).stat().st_size < 2048  # compressed: the upload cap alone would never see it
    dest = tmp_path / "out"

    with pytest.raises(LaserZipLimitError) as exc_info:
        extract_laser_nest_zip_within_limits(zip_path, str(dest))

    assert "too large once unpacked (over the 32 KB limit)" in str(exc_info.value)
    assert _files_under(dest) == []


def test_refusal_leaves_an_existing_destination_untouched(tmp_path, monkeypatch):
    """The check runs before the destination is cleared, not just before members are written."""
    monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_MEMBERS", 1)
    zip_path = _write_zip(tmp_path, {"a.cnc": b"M30", "b.cnc": b"M30"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("still here")

    with pytest.raises(LaserZipLimitError):
        extract_laser_nest_zip_within_limits(zip_path, str(dest))

    assert (dest / "keep.txt").read_text() == "still here"


def test_at_the_caps_extracts_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_MEMBERS", 2)
    monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_UNCOMPRESSED_BYTES", 6)
    zip_path = _write_zip(tmp_path, {"a.cnc": b"M30", "b.cnc": b"M30"})
    dest = tmp_path / "out"

    extract_laser_nest_zip_within_limits(zip_path, str(dest))

    assert sorted(p.name for p in _files_under(dest)) == ["a.cnc", "b.cnc"]


def test_limit_error_is_a_value_error():
    """A caller that has not mapped it to 413 yet still answers the package 400, never a 500."""
    assert issubclass(LaserZipLimitError, ValueError)


def test_check_reads_only_the_central_directory(tmp_path, monkeypatch):
    """``check_laser_zip_limits`` refuses without opening a single member."""
    monkeypatch.setattr(laser_nest_service, "LASER_ZIP_MAX_MEMBERS", 1)
    with zipfile.ZipFile(_write_zip(tmp_path, {"a.cnc": b"M30", "b.cnc": b"M30"})) as archive:
        monkeypatch.setattr(archive, "open", lambda *a, **k: pytest.fail("a member was opened"))
        with pytest.raises(LaserZipLimitError):
            check_laser_zip_limits(archive)


def _under_declare_first_member(zip_bytes: bytes, declared: int) -> bytes:
    """Rewrite the first central-directory entry's uncompressed-size field to ``declared``."""
    data = bytearray(zip_bytes)
    offset = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, offset + 24, declared)
    return bytes(data)


def test_an_under_declared_member_cannot_write_past_its_declared_size(tmp_path):
    """Why summing ``ZipInfo.file_size`` is a sound bound: the extractor stops at the declared size.

    A 1 MB member declared as 10 bytes passes the size cap (10 bytes counted). Extraction must then
    write at most those 10 bytes -- CPython truncates to the declared size and fails the CRC check.
    """
    lying = _under_declare_first_member(_zip_bytes({"bomb.pdf": b"\0" * (1024 * 1024)}), declared=10)
    zip_path = tmp_path / "lying.zip"
    zip_path.write_bytes(lying)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.infolist()[0].file_size == 10
        check_laser_zip_limits(archive)  # 10 declared bytes: under every cap
    dest = tmp_path / "out"

    with pytest.raises(zipfile.BadZipFile):
        extract_laser_nest_zip_within_limits(str(zip_path), str(dest))

    assert sum(p.stat().st_size for p in _files_under(dest)) <= 10
