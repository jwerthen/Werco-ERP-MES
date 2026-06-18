"""Async ProxyBox Zero (pbxz.io) print-bridge client.

ProxyBox Zero tunnels HTTPS print submissions to a locally-attached printer (here a
Westinghouse WHTP203e direct-thermal). We submit a base64-encoded PDF and (optionally)
poll the returned job id until it terminates.

Mirrors ``app.services.carriers.easypost_adapter`` deliberately: a raw
``httpx.AsyncClient`` (no vendor SDK dependency) and a ``_handle_response`` that
normalizes failures into ``ProxyBoxError`` and NEVER includes the API key in any
message or log line.

REQUEST SHAPE (per pbxz.io API docs -- confirmed: POST /print/{target}, X-API-Key
auth, body {"content": <base64>, "contentType": "pdf_base64"}). The docs do not
publish the exact field names for copies / paper size / the job-status response, so
every wire field name is a module-level constant here -- adjust in ONE place if the
provider's contract differs. The job-status parse is defensive (tries the common
keys) so a slightly different response shape still resolves a terminal state.

SECURITY: the API key lives only in the in-memory client headers; it is never
logged, echoed in an error, or returned. The egress kill switch is enforced one
layer up in ``PrintService`` -- this client performs raw provider I/O only.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_MAX_WAIT = 30.0

# Auth header.
API_KEY_HEADER = "X-API-Key"

# Submit-print wire contract (centralized so it is trivial to retune against the
# provider's exact field names). contentType is fixed: we always send a PDF.
SUBMIT_PATH = "/print/{target}"
FIELD_CONTENT = "content"
FIELD_CONTENT_TYPE = "contentType"
FIELD_COPIES = "copies"
FIELD_PAPER_SIZE = "paperSize"
CONTENT_TYPE_PDF_BASE64 = "pdf_base64"

# Job-status wire contract. The job id may come back under any of several keys on
# submit; status under any of several keys on GET. We probe them in order.
JOB_PATH = "/jobs/{job_id}"
_JOB_ID_KEYS = ("jobId", "job_id", "id")
_JOB_STATUS_KEYS = ("status", "state")

# Normalized terminal job states. The provider's vocabulary is probed
# case-insensitively against these sets.
_TERMINAL_SUCCESS = {"done", "completed", "complete", "printed", "success", "succeeded", "finished", "ok"}
_TERMINAL_FAILURE = {"failed", "error", "errored", "cancelled", "canceled", "rejected"}


class ProxyBoxError(Exception):
    """Normalized ProxyBox failure. The message NEVER contains the API key."""


class ProxyBoxClient:
    """Talks to one company's ProxyBox bridge. Construct from a DECRYPTED key.

    SECURITY: ``api_key`` is held only in the client headers for the lifetime of
    this instance and is NEVER logged or serialized.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        target: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not base_url:
            raise ProxyBoxError("ProxyBox base URL is not configured")
        if not target:
            raise ProxyBoxError("ProxyBox target printer is not configured")
        if not api_key:
            raise ProxyBoxError("ProxyBox API key is not configured")
        # Strip a single trailing slash so path joins are predictable.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._target = target
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={API_KEY_HEADER: self._api_key},
            timeout=self._timeout,
        )

    @staticmethod
    def _handle_response(resp: httpx.Response) -> Dict[str, Any]:
        """Raise a normalized ``ProxyBoxError`` on failure; never echo the API key.

        Only the provider's own (key-free) error body / status code is surfaced.
        """
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = str(body.get("error") or body.get("message") or body)
                else:
                    detail = str(body)
            except Exception:  # noqa: BLE001 - response may not be JSON
                detail = resp.text[:500]
            raise ProxyBoxError(f"ProxyBox API error {resp.status_code}: {detail}")
        # A successful submit/status may legitimately return an empty body.
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ProxyBoxError(f"ProxyBox returned a non-JSON response: {exc}") from exc
        return data if isinstance(data, dict) else {"result": data}

    async def submit_pdf(
        self,
        pdf_bytes: bytes,
        *,
        copies: int = 1,
        paper_size: str = "4x6",
    ) -> Optional[str]:
        """POST a base64 PDF to ``/print/{target}``; return the job id (or ``None``).

        The job id is parsed from the submit response under the first matching of
        ``jobId`` / ``job_id`` / ``id``; some bridges return no id (fire-and-forget),
        in which case ``None`` is returned and ``print_and_wait`` skips polling.
        """
        if not pdf_bytes:
            raise ProxyBoxError("Refusing to submit an empty PDF to the printer")
        payload = {
            FIELD_CONTENT: base64.b64encode(pdf_bytes).decode("ascii"),
            FIELD_CONTENT_TYPE: CONTENT_TYPE_PDF_BASE64,
            FIELD_COPIES: int(copies) if copies else 1,
            FIELD_PAPER_SIZE: paper_size or "4x6",
        }
        path = SUBMIT_PATH.format(target=self._target)
        async with self._client() as client:
            resp = await client.post(path, json=payload)
            data = self._handle_response(resp)
        return self._extract_job_id(data)

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        """GET ``/jobs/{job_id}`` and return the raw (key-free) status dict."""
        if not job_id:
            raise ProxyBoxError("get_job requires a job id")
        path = JOB_PATH.format(job_id=job_id)
        async with self._client() as client:
            resp = await client.get(path)
            return self._handle_response(resp)

    async def print_and_wait(
        self,
        pdf_bytes: bytes,
        *,
        copies: int = 1,
        paper_size: str = "4x6",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> Dict[str, Any]:
        """Submit the PDF and poll the job to a terminal state (best-effort wait).

        Returns a normalized result dict: ``{"job_id", "status", "terminal",
        "succeeded", "raw"}``. When the bridge returns no job id (fire-and-forget),
        the submission is treated as accepted (``submitted``) and no polling occurs.
        A terminal FAILURE status raises ``ProxyBoxError`` so the caller can surface
        the print failure (the rendered label is still persisted by the service).
        A timeout before any terminal state returns a non-failed, non-terminal
        ``timeout`` result rather than raising -- the job may still print.
        """
        import asyncio

        job_id = await self.submit_pdf(pdf_bytes, copies=copies, paper_size=paper_size)
        if not job_id:
            return {"job_id": None, "status": "submitted", "terminal": True, "succeeded": True, "raw": {}}

        deadline = asyncio.get_event_loop().time() + max(0.0, max_wait)
        last: Dict[str, Any] = {}
        while True:
            last = await self.get_job(job_id)
            status = self._extract_status(last)
            normalized = (status or "").strip().lower()
            if normalized in _TERMINAL_FAILURE:
                raise ProxyBoxError(f"ProxyBox print job {job_id} failed (status: {status})")
            if normalized in _TERMINAL_SUCCESS:
                return {"job_id": job_id, "status": status, "terminal": True, "succeeded": True, "raw": last}
            if asyncio.get_event_loop().time() >= deadline:
                # Don't raise on timeout: the job may still complete on the device.
                return {
                    "job_id": job_id,
                    "status": status or "pending",
                    "terminal": False,
                    "succeeded": False,
                    "raw": last,
                }
            await asyncio.sleep(max(0.05, poll_interval))

    @staticmethod
    def _extract_job_id(data: Dict[str, Any]) -> Optional[str]:
        for key in _JOB_ID_KEYS:
            value = data.get(key)
            if value:
                return str(value)
        # Some APIs nest the job under a "job" object.
        job = data.get("job")
        if isinstance(job, dict):
            for key in _JOB_ID_KEYS:
                value = job.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _extract_status(data: Dict[str, Any]) -> Optional[str]:
        for key in _JOB_STATUS_KEYS:
            value = data.get(key)
            if value:
                return str(value)
        job = data.get("job")
        if isinstance(job, dict):
            for key in _JOB_STATUS_KEYS:
                value = job.get(key)
                if value:
                    return str(value)
        return None
