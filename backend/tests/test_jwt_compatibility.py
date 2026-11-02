"""Keep existing HS256 credentials valid when replacing python-jose with PyJWT."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from app.core import security
from app.core.config import settings

pytestmark = pytest.mark.unit


def legacy_token(claims, key, algorithm="HS256"):
    """Encode the legacy compact JWS wire format independently of either library."""

    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    message = segment({"alg": algorithm, "typ": "JWT"}) + b"." + segment(claims)
    signature = hmac.new(key.encode(), message, hashlib.sha256).digest()
    return (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


TOKEN_CASES = [
    (security.verify_token, "access", {"sub": "7", "cid": 3, "scope": "kiosk"}, {"user_id": "7", "company_id": 3}),
    (
        security.verify_refresh_token,
        "refresh",
        {"sub": "7", "cid": 3, "session_id": "old-session"},
        {"user_id": "7", "session_id": "old-session"},
    ),
    (
        security.verify_display_token,
        "display",
        {"sub": "display:old", "jti": "old", "cid": 3},
        {"jti": "old", "company_id": 3},
    ),
    (
        security.verify_signin_token,
        "signin",
        {"sub": "signin:9", "sid": 9, "cid": 3},
        {"station_id": 9, "company_id": 3},
    ),
    (security.verify_kiosk_token, "kiosk", {"sub": "kiosk:9", "sid": 9, "cid": 3}, {"station_id": 9, "company_id": 3}),
    (
        security.verify_api_token,
        "api",
        {"sub": "api:old", "jti": "old", "user_id": 7, "company_id": 3},
        {"jti": "old", "user_id": 7},
    ),
]


@pytest.mark.parametrize("verify,kind,context,expected", TOKEN_CASES)
def test_accepts_existing_hs256_credentials(verify, kind, context, expected):
    now = int(time.time())
    claims = {**context, "type": kind, "iat": now - 60}
    # Standing API credentials intentionally have no exp; database revocation
    # and optional row expiry are still enforced by the API-token service.
    if kind != "api":
        claims["exp"] = now + 600
    key = settings.REFRESH_TOKEN_SECRET_KEY if kind == "refresh" else settings.SECRET_KEY
    result = verify(legacy_token(claims, key))
    assert result is not None
    assert expected.items() <= result.items()
    if kind == "access":
        assert result["scope"] == "kiosk"
    for other, _, _, _ in TOKEN_CASES:
        if other is not verify:
            assert other(legacy_token(claims, key)) is None


@pytest.mark.parametrize("verify,kind,context,expected", TOKEN_CASES)
@pytest.mark.parametrize(
    "invalid", ["signature", "expired", "not-yet-valid", "future-issued", "algorithm", "audience", "malformed"]
)
def test_rejects_invalid_credentials(verify, kind, context, expected, invalid):
    now = int(time.time())
    claims = {**context, "type": kind, "iat": now - 60, "exp": now + 600}
    key = settings.REFRESH_TOKEN_SECRET_KEY if kind == "refresh" else settings.SECRET_KEY
    algorithm = "HS256"
    if invalid == "signature":
        key = "different-signing-secret-at-least-32-characters"
    elif invalid == "expired":
        claims["exp"] = now - 60
    elif invalid == "not-yet-valid":
        claims["nbf"] = now + 600
    elif invalid == "future-issued":
        claims["iat"] = now + 600
    elif invalid == "algorithm":
        algorithm = "none"
    elif invalid == "audience":
        claims["aud"] = "another-service"
    token = "not.a.jwt" if invalid == "malformed" else legacy_token(claims, key, algorithm)
    assert verify(token) is None


def test_unpatched_jose_and_ecdsa_are_absent_from_both_dependency_locks():
    backend = Path(__file__).resolve().parents[1]
    for name in ("requirements.lock", "requirements-dev.lock"):
        locked = (backend / name).read_text()
        assert "\npython-jose==" not in locked
        assert "\necdsa==" not in locked
        assert "\npyjwt==" in locked
    assert settings.ALGORITHM == "HS256"
