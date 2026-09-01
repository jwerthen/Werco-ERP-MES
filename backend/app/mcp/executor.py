"""Executors: the ONLY way a tool reaches data.

Brief rule 1 -- same backend as the UI. A tool never imports a service, never builds
a user, never opens a session. It builds an HTTP request and hands it to one of two
executors:

- ``InProcessExecutor`` dispatches through ``httpx.ASGITransport`` INTO the real
  FastAPI app object (``app.main:app``) -- the request passes the full middleware
  stack (trusted host, rate limit, body cap, CSRF no-op, CORS, logging) and the
  route's own dependencies (``get_current_user`` / ``get_current_company_id`` /
  ``require_role`` / ``get_audit_service``). Nothing is skipped; the router IS the
  RBAC, tenancy and audit boundary, which is exactly why this is not a service call.
- ``RemoteExecutor`` does the same over real HTTPS to ``WERCO_ERP_URL`` for the stdio
  bridge running on a developer's or an agent host's machine.

Both send the same headers the SPA's Axios client would (bearer token,
``X-Requested-With``), and NEITHER ever sets ``Origin`` or ``Referer``: the CSRF
middleware enforces its browser rules only when one of those is present, and an
in-process dispatch is not a browser.
"""

from __future__ import annotations

import base64
import binascii
import json as jsonlib
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

import httpx

from app.core.config import settings
from app.mcp.auth import AuthContext
from app.mcp.results import ExecResult

DEFAULT_TIMEOUT_SECONDS = 120.0  # a nest import can legitimately run long
USER_AGENT_PREFIX = "werco-mcp"
_SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
_DEFAULT_FILE_CONTENT_TYPE = "application/octet-stream"


@dataclass
class UploadFile:
    """One decoded multipart file part."""

    filename: str
    content: bytes
    content_type: Optional[str] = None


FilesArg = Mapping[str, Union[UploadFile, Sequence[UploadFile]]]


class UploadError(ValueError):
    """A file argument could not be decoded or is over the upload cap (surfaced as a 422/413 tool error)."""

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class Executor(Protocol):
    async def request(
        self,
        *,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        files: Optional[FilesArg] = None,
        form: Optional[Mapping[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> ExecResult: ...

    async def aclose(self) -> None: ...


def default_in_process_host() -> str:
    """A ``Host`` value ``TrustedHostMiddleware`` will accept when the caller's is unknown.

    ``ALLOWED_HOSTS="*"`` (the dev default) accepts anything, so ``localhost`` is fine;
    a production allowlist's FIRST entry is by convention the API's own hostname.
    """
    for host in settings.allowed_hosts_list:
        if host and host != "*":
            return host
    return "localhost"


def safe_host_header(value: Optional[str]) -> Optional[str]:
    """Only forward a ``Host`` that is plausibly a host[:port]; anything else is dropped."""
    if value and _SAFE_HOST.match(value):
        return value
    return None


def decode_upload(value: Any, *, max_bytes: int) -> UploadFile:
    """``{filename, content_base64, content_type?}`` -> ``UploadFile`` (bounded by ``max_bytes``)."""
    if not isinstance(value, Mapping):
        raise UploadError("A file argument must be an object with 'filename' and 'content_base64'.")
    filename = value.get("filename")
    encoded = value.get("content_base64")
    if not isinstance(filename, str) or not filename.strip():
        raise UploadError("File argument is missing 'filename'.")
    if not isinstance(encoded, str) or not encoded:
        raise UploadError(f"File {filename!r} is missing 'content_base64'.")
    # 4 base64 chars encode 3 bytes: refuse before decoding when the payload is clearly over the cap.
    if len(encoded) * 3 // 4 > max_bytes + 3:
        raise UploadError(f"File {filename!r} exceeds the {max_bytes}-byte MCP upload cap.", status=413)
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadError(f"File {filename!r}: content_base64 is not valid base64 ({exc}).") from exc
    if len(content) > max_bytes:
        raise UploadError(f"File {filename!r} exceeds the {max_bytes}-byte MCP upload cap.", status=413)
    content_type = value.get("content_type")
    if content_type is not None and not isinstance(content_type, str):
        raise UploadError(f"File {filename!r}: content_type must be a string.")
    return UploadFile(filename=filename.strip(), content=content, content_type=content_type or None)


def decode_upload_list(value: Any, *, max_bytes: int) -> List[UploadFile]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise UploadError("A multi-file argument must be a list of file objects.")
    return [decode_upload(item, max_bytes=max_bytes) for item in value]


def _scalar(value: Any) -> Any:
    """Serialize a query/form value the way the SPA would put it on the wire."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return jsonlib.dumps(value)
    return value


def encode_query(query: Optional[Mapping[str, Any]]) -> List[Tuple[str, Any]]:
    """Drop ``None`` values; repeat a key for list values (``?types=a&types=b``)."""
    pairs: List[Tuple[str, Any]] = []
    for key, value in (query or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, _scalar(item)) for item in value if item is not None)
        else:
            pairs.append((key, _scalar(value)))
    return pairs


def encode_form(form: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return {key: _scalar(value) for key, value in (form or {}).items() if value is not None}


def encode_files(files: Optional[FilesArg]) -> Optional[List[Tuple[str, Tuple[str, bytes, str]]]]:
    if not files:
        return None
    parts: List[Tuple[str, Tuple[str, bytes, str]]] = []
    for field_name, value in files.items():
        items = list(value) if isinstance(value, Sequence) and not isinstance(value, UploadFile) else [value]
        for item in items:
            parts.append((field_name, (item.filename, item.content, item.content_type or _DEFAULT_FILE_CONTENT_TYPE)))
    return parts


class _HttpxExecutor:
    """Shared request/retry logic; subclasses supply the ``httpx.AsyncClient``."""

    def __init__(self, *, version: str = "0", timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._version = version
        self._timeout = timeout

    def _user_agent(self) -> str:
        return f"{USER_AGENT_PREFIX}/{self._version}"

    def _headers(self, auth: Optional[AuthContext]) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": self._user_agent(),
        }
        if auth is not None:
            headers["Authorization"] = f"Bearer {auth.token}"
        return headers

    def _client_for(self, auth: Optional[AuthContext]) -> Tuple[httpx.AsyncClient, bool]:
        """``(client, dispose_after_use)``."""
        raise NotImplementedError

    async def request(
        self,
        *,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        files: Optional[FilesArg] = None,
        form: Optional[Mapping[str, Any]] = None,
        auth: Optional[AuthContext] = None,
    ) -> ExecResult:
        result = await self._send(method, path, query, json, files, form, auth)
        # One refresh-and-retry, and only for a bridge-side token (brief 3.2/3.3). A door
        # caller's 401 is theirs to act on; an unauthenticated exchange (the refresh /
        # login calls themselves) never retries, which is what keeps this from recursing.
        if result.status == 401 and auth is not None and auth.token_source is not None:
            new_token = await auth.token_source.refresh_after_401(auth.token)
            if new_token and new_token != auth.token:
                result = await self._send(method, path, query, json, files, form, replace(auth, token=new_token))
        return result

    async def _send(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]],
        json: Any,
        files: Optional[FilesArg],
        form: Optional[Mapping[str, Any]],
        auth: Optional[AuthContext],
    ) -> ExecResult:
        client, dispose = self._client_for(auth)
        try:
            kwargs: Dict[str, Any] = {"params": encode_query(query), "headers": self._headers(auth)}
            encoded_files = encode_files(files)
            if encoded_files is not None or form is not None:
                kwargs["data"] = encode_form(form)
                if encoded_files is not None:
                    kwargs["files"] = encoded_files
            elif json is not None:
                kwargs["json"] = json
            response = await client.request(method.upper(), path, **kwargs)
            return ExecResult(
                status=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                content=response.content,
                content_type=response.headers.get("content-type", ""),
            )
        finally:
            if dispose:
                await client.aclose()

    async def aclose(self) -> None:
        return None


class InProcessExecutor(_HttpxExecutor):
    """Dispatch into the FastAPI app object without a socket.

    A fresh ``ASGITransport`` per call is deliberate: the transport's ``client`` tuple
    is what the app's per-IP rate limiter keys on, and it must be THIS caller's
    address (the MCP HTTP caller's, or 127.0.0.1 on stdio), not a process-wide
    constant that would fold every agent into one bucket. Constructing it is cheap --
    there is no connection to pool.
    """

    def __init__(
        self,
        app: Any,
        *,
        default_host: Optional[str] = None,
        version: str = "0",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(version=version, timeout=timeout)
        self._app = app
        self._default_host = default_host or default_in_process_host()

    def _client_for(self, auth: Optional[AuthContext]) -> Tuple[httpx.AsyncClient, bool]:
        host = safe_host_header(auth.host_header if auth else None) or self._default_host
        client_host = auth.client_host if auth and auth.client_host else "127.0.0.1"
        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False, client=(client_host, 0))
        client = httpx.AsyncClient(
            transport=transport,
            base_url=f"http://{host}",
            timeout=self._timeout,
            follow_redirects=False,
        )
        return client, True


class RemoteExecutor(_HttpxExecutor):
    """Real HTTPS to a deployed Werco ERP (``WERCO_ERP_URL``). TLS verified, no redirects."""

    def __init__(
        self,
        base_url: str,
        *,
        version: str = "0",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        super().__init__(version=version, timeout=timeout)
        self.base_url = base_url.strip().rstrip("/")
        client_kwargs: Dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": timeout,
            "follow_redirects": False,
        }
        if transport is not None:
            client_kwargs["transport"] = transport  # tests: httpx.MockTransport
        self._client = httpx.AsyncClient(**client_kwargs)

    def _client_for(self, auth: Optional[AuthContext]) -> Tuple[httpx.AsyncClient, bool]:
        return self._client, False

    async def aclose(self) -> None:
        await self._client.aclose()
