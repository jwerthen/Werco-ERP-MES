"""``app.core.upload_limits`` -- the 413 pre-check and the bounded read behind every AI upload route.

Two properties are the point of the module, and each is pinned here against a real Starlette
``UploadFile`` whose backing file records every read:

* the pre-check refuses an upload whose counted ``size`` is over the cap WITHOUT reading a byte;
* the bounded read never asks for more than ``max_bytes + 1`` bytes, and refuses when ``size`` is
  missing or lies -- it measures what it read, not what it was told.

The route-level twins (every AI upload route answers 413 both ways) live in
``tests/api/test_ai_upload_bounds.py``.
"""

import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.core.upload_limits import (
    ensure_upload_within_limit,
    format_byte_limit,
    read_upload_within_limit,
    upload_too_large_detail,
)

pytestmark = pytest.mark.unit

MAX = 1024


class _RecordingBytesIO(io.BytesIO):
    """A BytesIO that records the size argument of every ``read``."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list = []

    def read(self, size=-1):  # type: ignore[override]
        self.read_sizes.append(size)
        return super().read(size)


def _upload(data: bytes, *, size) -> tuple:
    backing = _RecordingBytesIO(data)
    return UploadFile(file=backing, size=size, filename="doc.pdf"), backing


class TestPreCheck:
    def test_size_over_the_cap_is_refused_without_reading(self):
        upload, backing = _upload(b"x" * (MAX + 1), size=MAX + 1)

        with pytest.raises(HTTPException) as exc_info:
            ensure_upload_within_limit(upload, MAX)

        assert exc_info.value.status_code == 413
        assert exc_info.value.detail == "File too large (max 1 KB)."
        assert backing.read_sizes == []

    @pytest.mark.parametrize("size", [0, MAX, None])
    def test_size_at_or_under_the_cap_or_unknown_passes(self, size):
        upload, backing = _upload(b"x" * 10, size=size)

        ensure_upload_within_limit(upload, MAX)

        assert backing.read_sizes == []

    def test_custom_detail_is_used(self):
        upload, _ = _upload(b"", size=MAX + 1)

        with pytest.raises(HTTPException) as exc_info:
            ensure_upload_within_limit(upload, MAX, detail="Too big for this route.")

        assert exc_info.value.detail == "Too big for this route."


class TestBoundedRead:
    async def test_honest_oversize_is_refused_before_any_read(self):
        upload, backing = _upload(b"x" * (MAX * 5), size=MAX * 5)

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, MAX)

        assert exc_info.value.status_code == 413
        assert backing.read_sizes == []

    @pytest.mark.parametrize("lying_size", [0, 1, None])
    async def test_oversize_body_behind_a_small_or_missing_size_is_refused(self, lying_size):
        """The read bound holds on its own: it asks for MAX + 1 bytes and measures them."""
        upload, backing = _upload(b"x" * (MAX * 5), size=lying_size)

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, MAX)

        assert exc_info.value.status_code == 413
        assert exc_info.value.detail == upload_too_large_detail(MAX)
        assert backing.read_sizes == [MAX + 1]

    async def test_exactly_at_the_cap_is_returned_whole(self):
        data = bytes(range(256)) * (MAX // 256)
        upload, backing = _upload(data, size=len(data))

        assert await read_upload_within_limit(upload, MAX) == data
        assert backing.read_sizes == [MAX + 1]

    async def test_empty_upload_is_returned_empty(self):
        """Empty is the route's own 400, not a size refusal."""
        upload, _ = _upload(b"", size=0)

        assert await read_upload_within_limit(upload, MAX) == b""


@pytest.mark.parametrize(
    ("max_bytes", "expected"),
    [
        (10 * 1024 * 1024, "10 MB"),
        (20 * 1024 * 1024, "20 MB"),
        (1536 * 1024, "1.5 MB"),
        (64 * 1024, "64 KB"),
        (1500, "1.5 KB"),
        (64, "64 bytes"),
    ],
)
def test_format_byte_limit(max_bytes, expected):
    assert format_byte_limit(max_bytes) == expected
