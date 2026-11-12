"""Size bounds for multipart file uploads, answered with **413** before the body is buffered.

Starlette's form parser spools every uploaded part to a ``SpooledTemporaryFile`` (1 MB in RAM,
then disk) and counts the bytes it wrote into ``UploadFile.size``. A handler that then calls
``await file.read()`` pulls the WHOLE spool into memory before it can look at the length, so an
oversized upload costs its full size in RAM before the "too large" check runs. The two helpers
here close that gap, in this order:

1. :func:`ensure_upload_within_limit` -- the pre-check. When ``file.size`` is already over the cap
   the request is refused without reading a byte. It is cheap and synchronous, so a handler can run
   it before taking any slot or doing any work (roadmap #9-2a puts the AI slot between the two).
2. :func:`read_upload_within_limit` -- the bounded read. It reads at most ``max_bytes + 1`` bytes
   and refuses the upload when more than ``max_bytes`` came back. This is the bound that holds when
   ``file.size`` is missing (an ``UploadFile`` not built by Starlette's parser) or wrong, so it never
   trusts the pre-check: it runs the pre-check again and then measures what it actually read.

Both raise the same 413 with a plain-language ``detail`` naming the limit. Each route keeps its own
cap as a module-level constant and passes it in at call time, so the constant stays the single
tunable source of truth (and a test can monkeypatch it small).
"""

from typing import Optional

from fastapi import HTTPException, UploadFile, status

_KIB = 1024
_MIB = 1024 * 1024


def format_byte_limit(max_bytes: int) -> str:
    """Render a byte cap for a person: ``10 MB``, ``1.5 MB``, ``64 KB`` or ``200 bytes``."""
    if max_bytes >= _MIB:
        megabytes = max_bytes / _MIB
        return f"{megabytes:g} MB" if megabytes == int(megabytes) else f"{megabytes:.1f} MB"
    if max_bytes >= _KIB:
        kilobytes = max_bytes / _KIB
        return f"{kilobytes:g} KB" if kilobytes == int(kilobytes) else f"{kilobytes:.1f} KB"
    return f"{max_bytes} bytes"


def upload_too_large_detail(max_bytes: int) -> str:
    """The default 413 ``detail``: ``File too large (max 10 MB).``"""
    return f"File too large (max {format_byte_limit(max_bytes)})."


def _too_large(max_bytes: int, detail: Optional[str]) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=detail or upload_too_large_detail(max_bytes),
    )


def ensure_upload_within_limit(file: UploadFile, max_bytes: int, *, detail: Optional[str] = None) -> None:
    """Refuse with 413 when the size Starlette counted for ``file`` is already over ``max_bytes``.

    Reads nothing. ``file.size`` is ``None`` for an ``UploadFile`` built outside Starlette's parser;
    that is not an error here -- :func:`read_upload_within_limit` still bounds the read.
    """
    size = file.size
    if size is not None and size > max_bytes:
        raise _too_large(max_bytes, detail)


async def read_upload_within_limit(file: UploadFile, max_bytes: int, *, detail: Optional[str] = None) -> bytes:
    """Return the upload's bytes, reading at most ``max_bytes + 1`` of them; 413 if it is longer.

    Runs :func:`ensure_upload_within_limit` first, so a caller that only needs the bytes can call
    this alone. Callers that do work between the size check and the read (roadmap #9-2a's AI slot)
    call the pre-check themselves, earlier.
    """
    ensure_upload_within_limit(file, max_bytes, detail=detail)
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise _too_large(max_bytes, detail)
    return content
