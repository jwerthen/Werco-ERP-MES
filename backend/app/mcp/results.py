"""HTTP response -> ``CallToolResult``.

Pure: stdlib + the MCP wire types only, so the shaping rules are testable without an
app or a database.

The shape is deliberately boring and lossless where it matters: a route's own error
``detail`` is passed through verbatim (brief rule 2 -- the server's 401/403/409 is
the answer, the MCP layer never rewrites it), JSON bodies come back as
``structured_content`` so a client can read fields without re-parsing text, and the
two caps (characters of text, bytes of blob) exist because an MCP client is a
context window, not a browser: a 40 MB export must be fetched from the UI, not
pasted into a prompt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import mcp.types as types

TRUNCATION_NOTE = "\n[truncated: {shown} of {total} chars — narrow with limit/skip or filters]"
# Server ``detail`` payloads are quoted into error results; keep a pathological one bounded.
MAX_ERROR_DETAIL_CHARS = 8000
_TEXT_LIKE_MEDIA = ("text/",)
_JSON_MEDIA_SUFFIX = "+json"
_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


@dataclass
class ExecResult:
    """What an executor hands back: the raw HTTP outcome, nothing interpreted yet."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)  # lower-cased keys
    content: bytes = b""
    content_type: str = ""

    @property
    def media_type(self) -> str:
        return self.content_type.split(";", 1)[0].strip().lower()

    @property
    def is_json(self) -> bool:
        media = self.media_type
        return media == "application/json" or media.endswith(_JSON_MEDIA_SUFFIX)

    @property
    def is_text(self) -> bool:
        return self.media_type.startswith(_TEXT_LIKE_MEDIA)

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def filename(self) -> Optional[str]:
        disposition = self.headers.get("content-disposition", "")
        match = _FILENAME_RE.search(disposition)
        return match.group(1) if match else None


def _dumps(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False, default=str)


def _as_object(value: Any) -> Dict[str, Any]:
    """``structured_content`` must be a JSON object; wrap anything else."""
    return value if isinstance(value, dict) else {"result": value}


def extract_detail(result: ExecResult) -> Any:
    """The server's ``detail`` (FastAPI's error envelope) or, failing that, the body text.

    Never rewrites it: a 422 ``detail`` is a list of pydantic errors, a 409 is the
    domain's own sentence, and both are exactly what the caller should see.
    """
    if result.content:
        if result.is_json:
            try:
                payload = result.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and "detail" in payload:
                return payload["detail"]
            if payload is not None:
                return payload
        text = result.text()
        if len(text) > MAX_ERROR_DETAIL_CHARS:
            text = text[:MAX_ERROR_DETAIL_CHARS] + "…"
        return text
    return f"HTTP {result.status}"


def error_result(
    *,
    status: int,
    detail: Any,
    method: Optional[str] = None,
    path: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> types.CallToolResult:
    """An ``is_error`` result carrying ``status`` + ``detail`` (+ method/path when known)."""
    payload: Dict[str, Any] = {"status": status, "detail": detail}
    if method:
        payload["method"] = method
    if path:
        payload["path"] = path
    if extra:
        payload.update(dict(extra))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_dumps(payload))],
        structured_content=payload,
        is_error=True,
    )


def validation_error_result(tool_name: str, messages: List[str]) -> types.CallToolResult:
    """422-shaped result for arguments that failed the tool's own input schema."""
    return error_result(
        status=422,
        detail=[f"{tool_name}: {message}" for message in messages],
        extra={"hint": "Fix the arguments to match the tool's inputSchema and call again."},
    )


def transport_error_result(
    exc: BaseException, *, method: Optional[str] = None, path: Optional[str] = None
) -> types.CallToolResult:
    """The request never produced an HTTP status (in-process exception, connection refused)."""
    return error_result(status=0, detail=f"{type(exc).__name__}: {exc}", method=method, path=path)


def build_tool_result(
    *,
    tool_name: str,
    method: str,
    path: str,
    result: ExecResult,
    max_chars: int,
    max_blob_bytes: int,
) -> types.CallToolResult:
    """Shape an HTTP outcome into the MCP result the client sees.

    - >= 400: ``is_error`` with the server's ``status`` and verbatim ``detail``.
    - 204 / empty 2xx: ``{"ok": true, "status": N}``.
    - 2xx JSON: parsed into ``structured_content`` (non-objects wrapped as
      ``{"result": ...}``) and pretty-printed text, capped at ``max_chars`` with a
      trailing note; when capped, ``structured_content`` is replaced by a marker so the
      client is never handed the payload twice.
    - 2xx text/*: the text, same cap.
    - 2xx anything else: an ``EmbeddedResource`` blob (base64) up to ``max_blob_bytes``,
      or ``is_error`` telling the caller to fetch it from the UI.
    """
    if result.status >= 400:
        return error_result(status=result.status, detail=extract_detail(result), method=method, path=path)

    if result.status == 204 or not result.content:
        payload = {"ok": True, "status": result.status}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_dumps(payload))],
            structured_content=payload,
        )

    if result.is_json:
        try:
            parsed = result.json()
        except ValueError:
            return _text_result(result.text(), status=result.status, max_chars=max_chars)
        return _json_result(parsed, status=result.status, max_chars=max_chars)

    if result.is_text:
        return _text_result(result.text(), status=result.status, max_chars=max_chars)

    return _blob_result(tool_name, result, max_blob_bytes=max_blob_bytes)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    shown = max(0, max_chars)
    return text[:shown] + TRUNCATION_NOTE.format(shown=shown, total=len(text)), True


def _json_result(parsed: Any, *, status: int, max_chars: int) -> types.CallToolResult:
    text = _dumps(parsed)
    text, truncated = _truncate(text, max_chars)
    structured: Dict[str, Any]
    if truncated:
        structured = {"truncated": True, "chars": len(_dumps(parsed)), "status": status}
    else:
        structured = _as_object(parsed)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=structured,
    )


def _text_result(text: str, *, status: int, max_chars: int) -> types.CallToolResult:
    total = len(text)
    text, truncated = _truncate(text, max_chars)
    structured: Dict[str, Any] = {"status": status, "text": text}
    if truncated:
        structured = {"truncated": True, "chars": total, "status": status}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=structured,
    )


def _blob_result(tool_name: str, result: ExecResult, *, max_blob_bytes: int) -> types.CallToolResult:
    size = len(result.content)
    if size > max_blob_bytes:
        return error_result(
            status=result.status,
            detail=(
                f"The response is a {size}-byte {result.media_type or 'binary'} file, over the "
                f"{max_blob_bytes}-byte MCP blob cap. Download it from the Werco ERP UI instead."
            ),
            extra={"bytes": size, "content_type": result.media_type, "filename": result.filename()},
        )
    digest = hashlib.sha256(result.content).hexdigest()[:12]
    uri = f"werco://{tool_name}/{digest}"
    resource = types.BlobResourceContents(
        uri=uri,
        mime_type=result.media_type or "application/octet-stream",
        blob=base64.b64encode(result.content).decode("ascii"),
    )
    structured: Dict[str, Any] = {
        "status": result.status,
        "content_type": result.media_type,
        "bytes": size,
        "uri": uri,
        "filename": result.filename(),
    }
    return types.CallToolResult(
        content=[types.EmbeddedResource(type="resource", resource=resource)],
        structured_content=structured,
    )
