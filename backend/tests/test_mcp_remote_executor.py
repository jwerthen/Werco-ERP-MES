"""``RemoteExecutor`` + ``TokenSource``: the stdio bridge's side, over an ``httpx.MockTransport``.

No app, no database: the transport records every request the bridge would put on the
wire to a deployed Werco ERP and answers with scripted responses. What is pinned:

- a 401 on a bridge-side token triggers ONE refresh (``POST /auth/refresh`` with the
  rotating refresh token) and ONE retry carrying the new token;
- with no refresh token, the password login path (``POST /auth/login`` as a form) is
  used, and it is never entered while a static token still works;
- a spent refresh token falls through to login; when neither works the original 401
  is surfaced -- and the refresh / login exchanges themselves are never retried;
- a DOOR caller's 401 (no token source) passes through untouched;
- no request ever carries ``Origin`` or ``Referer``; the SPA's ``X-Requested-With``
  and a bearer are always present; paths are appended verbatim to ``WERCO_ERP_URL``;
- ``TokenSource.from_env`` reads only the documented variables and never logs values.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import parse_qs

import httpx
import pytest

from app.mcp.auth import (
    ENV_EMAIL,
    ENV_PASSWORD,
    ENV_REFRESH_TOKEN,
    ENV_TOKEN,
    LOGIN_PATH,
    REFRESH_PATH,
    AuthContext,
    TokenSource,
)
from app.mcp.executor import RemoteExecutor

pytestmark = pytest.mark.unit

BASE_URL = "https://erp.example.test/"  # trailing slash on purpose: it must be stripped


class Scripted:
    """A MockTransport handler: records requests, answers by (method, path) with a queue per key."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.answers: Dict[tuple, List[httpx.Response]] = {}

    def on(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.answers.setdefault((method, path), []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.answers.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(599, json={"detail": f"unscripted {request.method} {request.url.path}"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent(self) -> List[tuple]:
        return [(r.method, r.url.path, r.headers.get("authorization")) for r in self.requests]


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _executor(script: Scripted) -> RemoteExecutor:
    return RemoteExecutor(BASE_URL, version="9.9", transport=script.transport())


class TestWireHygiene:
    async def test_paths_are_appended_verbatim_and_no_origin_or_referer_is_ever_sent(self):
        script = Scripted()
        script.on("GET", "/api/v1/work-orders/", _json(200, [{"id": 1}]))
        executor = _executor(script)
        assert executor.base_url == "https://erp.example.test"
        result = await executor.request(
            method="GET",
            path="/api/v1/work-orders/",
            query={"limit": 5, "status": None, "flags": [True, "x"]},
            auth=AuthContext(token="T1"),
        )
        assert result.status == 200 and result.json() == [{"id": 1}]
        [request] = script.requests
        assert str(request.url) == "https://erp.example.test/api/v1/work-orders/?limit=5&flags=true&flags=x"
        assert "origin" not in request.headers and "referer" not in request.headers
        assert request.headers["authorization"] == "Bearer T1"
        assert request.headers["x-requested-with"] == "XMLHttpRequest"
        assert request.headers["user-agent"] == "werco-mcp/9.9"
        assert request.headers["accept"].startswith("application/json")
        await executor.aclose()

    async def test_json_body_and_multipart_are_encoded_like_the_spa_would(self):
        from app.mcp.executor import UploadFile

        script = Scripted()
        script.on("POST", "/api/v1/things", _json(201, {"ok": 1}))
        script.on("POST", "/api/v1/upload", _json(200, {"ok": 2}))
        executor = _executor(script)
        await executor.request(method="POST", path="/api/v1/things", json={"a": 1}, auth=AuthContext(token="T"))
        await executor.request(
            method="POST",
            path="/api/v1/upload",
            form={"rows": [{"n": 1}], "flag": False, "skip": None},
            files={"file": UploadFile(filename="pkg.zip", content=b"PK\x03\x04", content_type="application/zip")},
            auth=AuthContext(token="T"),
        )
        as_json, as_multipart = script.requests
        assert as_json.headers["content-type"] == "application/json"
        assert json.loads(as_json.content) == {"a": 1}
        assert as_multipart.headers["content-type"].startswith("multipart/form-data")
        body = as_multipart.content
        assert b'name="file"; filename="pkg.zip"' in body and b"PK\x03\x04" in body
        assert b'name="rows"' in body and b'[{"n": 1}]' in body
        assert b'name="flag"' in body and b"false" in body
        assert b'name="skip"' not in body, "None form values are dropped"
        await executor.aclose()

    async def test_redirects_are_not_followed(self):
        script = Scripted()
        script.on("GET", "/api/v1/x", httpx.Response(307, headers={"location": "https://elsewhere.test/"}))
        executor = _executor(script)
        result = await executor.request(method="GET", path="/api/v1/x", auth=AuthContext(token="T"))
        assert result.status == 307 and len(script.requests) == 1
        await executor.aclose()


class TestRefreshOn401:
    async def test_401_refreshes_once_and_retries_once_with_the_new_token(self):
        script = Scripted()
        script.on("GET", "/api/v1/parts/", _json(401, {"detail": "Could not validate credentials"}), _json(200, []))
        script.on("POST", REFRESH_PATH, _json(200, {"access_token": "T2", "refresh_token": "R2", "expires_in": 900}))
        executor = _executor(script)
        source = TokenSource(executor, access_token="T1", refresh_token="R1")
        token = await source.get_token()
        assert token == "T1", "a static token is used first, without any exchange"
        assert script.requests == []

        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token=token, token_source=source)
        )
        assert result.status == 200
        assert script.sent() == [
            ("GET", "/api/v1/parts/", "Bearer T1"),
            ("POST", REFRESH_PATH, None),
            ("GET", "/api/v1/parts/", "Bearer T2"),
        ]
        refresh = script.requests[1]
        assert json.loads(refresh.content) == {"refresh_token": "R1"}
        # Rotation is remembered: the NEXT refresh sends R2, and the next call sends T2.
        assert await source.get_token() == "T2"
        assert source._refresh_token == "R2"
        await executor.aclose()

    async def test_a_second_401_after_refresh_is_surfaced_not_looped(self):
        script = Scripted()
        script.on("GET", "/api/v1/parts/", _json(401, {"detail": "still no"}))
        script.on("POST", REFRESH_PATH, _json(200, {"access_token": "T2", "refresh_token": "R2"}))
        executor = _executor(script)
        source = TokenSource(executor, access_token="T1", refresh_token="R1")
        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token="T1", token_source=source)
        )
        assert result.status == 401 and result.json()["detail"] == "still no"
        assert [m for m, _p, _a in script.sent()] == ["GET", "POST", "GET"]
        await executor.aclose()

    async def test_door_callers_401_passes_through_with_no_refresh(self):
        script = Scripted()
        script.on("GET", "/api/v1/parts/", _json(401, {"detail": "expired"}))
        executor = _executor(script)
        result = await executor.request(method="GET", path="/api/v1/parts/", auth=AuthContext(token="T1"))
        assert result.status == 401
        assert script.sent() == [("GET", "/api/v1/parts/", "Bearer T1")]
        await executor.aclose()


class TestPasswordLogin:
    async def test_login_is_deferred_until_a_token_is_needed_and_sent_as_a_form(self):
        script = Scripted()
        script.on("POST", LOGIN_PATH, _json(200, {"access_token": "L1", "refresh_token": "LR1", "expires_in": 900}))
        script.on("GET", "/api/v1/parts/", _json(200, []))
        executor = _executor(script)
        source = TokenSource(executor, email="assistant@werco.test", password="s3cret-but-long-enough")
        assert source.configured and source.can_login and not source.can_refresh
        assert source.describe() == "password-login"
        assert script.requests == []

        token = await source.get_token()
        assert token == "L1"
        login = script.requests[0]
        assert login.headers["content-type"] == "application/x-www-form-urlencoded"
        assert "authorization" not in login.headers, "the login exchange is unauthenticated"
        assert parse_qs(login.content.decode()) == {
            "username": ["assistant@werco.test"],
            "password": ["s3cret-but-long-enough"],
        }

        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token=token, token_source=source)
        )
        assert result.status == 200
        assert script.sent()[-1] == ("GET", "/api/v1/parts/", "Bearer L1")
        # Login handed back a refresh token: the next 401 refreshes rather than re-logging in.
        assert source.can_refresh and source._refresh_token == "LR1"
        await executor.aclose()

    async def test_spent_refresh_token_falls_through_to_login(self):
        script = Scripted()
        script.on("GET", "/api/v1/parts/", _json(401, {"detail": "expired"}), _json(200, [{"id": 7}]))
        script.on("POST", REFRESH_PATH, _json(401, {"detail": "Invalid refresh token"}))
        script.on("POST", LOGIN_PATH, _json(200, {"access_token": "L2", "refresh_token": "LR2"}))
        executor = _executor(script)
        source = TokenSource(
            executor, access_token="T1", refresh_token="R-spent", email="a@b.test", password="pw-long-enough"
        )
        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token="T1", token_source=source)
        )
        assert result.status == 200 and result.json() == [{"id": 7}]
        assert [(m, p) for m, p, _a in script.sent()] == [
            ("GET", "/api/v1/parts/"),
            ("POST", REFRESH_PATH),
            ("POST", LOGIN_PATH),
            ("GET", "/api/v1/parts/"),
        ]
        assert script.sent()[-1][2] == "Bearer L2"
        assert source._refresh_token == "LR2", "the spent token was forgotten and the new one kept"
        await executor.aclose()

    async def test_when_nothing_works_the_original_401_is_returned_once(self):
        script = Scripted()
        script.on("GET", "/api/v1/parts/", _json(401, {"detail": "expired"}))
        script.on("POST", LOGIN_PATH, _json(401, {"detail": "Incorrect email or password"}))
        executor = _executor(script)
        source = TokenSource(executor, access_token="T1", email="a@b.test", password="wrong-but-long-enough")
        result = await executor.request(
            method="GET", path="/api/v1/parts/", auth=AuthContext(token="T1", token_source=source)
        )
        assert result.status == 401 and result.json()["detail"] == "expired"
        assert [(m, p) for m, p, _a in script.sent()] == [("GET", "/api/v1/parts/"), ("POST", LOGIN_PATH)]
        assert await source.get_token() is None
        await executor.aclose()


class TestTokenSourceFromEnv:
    def test_nothing_configured_is_none(self):
        assert TokenSource.from_env(_executor(Scripted()), environ={}) is None
        assert TokenSource.from_env(_executor(Scripted()), environ={ENV_TOKEN: "   ", ENV_EMAIL: "x@y"}) is None

    def test_reads_the_documented_variables_and_describes_kinds_only(self):
        source = TokenSource.from_env(
            _executor(Scripted()),
            environ={
                ENV_TOKEN: " eyJ-access-value ",
                ENV_REFRESH_TOKEN: "eyJ-refresh-value",
                ENV_EMAIL: "assistant@werco.test",
                ENV_PASSWORD: "hunter2-but-longer",
            },
        )
        assert source is not None and source.configured
        assert source.describe() == "access-token, refresh-token, password-login"
        assert source._access_token == "eyJ-access-value", "whitespace is trimmed"
        for secret in ("eyJ-access-value", "eyJ-refresh-value", "hunter2", "assistant@werco.test"):
            assert secret not in source.describe()
