"""``POST /auth/kiosk-badge-token`` — badge → 5-minute kiosk-scoped operator token.

The exchange is station-token-gated: the shared crew tablet presents its scoped
``type="kiosk"`` JWT (validated against the ``kiosk_stations`` row: exists, not
revoked, ``cid`` matches) plus a badge scan, and receives a short-lived
``type="access"`` / ``scope="kiosk"`` OPERATOR token.

Headline invariants:
1. **No refresh token, ever** — a shared terminal must never hold a long-lived
   personal credential; the operator token dies ≤ 5 minutes.
2. **Uniform 401 "Invalid badge"** for unknown / inactive / locked /
   foreign-tenant badges — the response can't be used to probe accounts, and
   the badge lookup is fenced to the station's company.
3. **Audit** — KIOSK_BADGE_TOKEN_ISSUED on success (actor = the operator),
   KIOSK_BADGE_TOKEN_FAILED on every refusal, both attributed to the station's
   company.
4. **Rate limit** — 30/minute per IP (station-token-gated, so generous is safe).
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

import app.main as app_main
from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    mint_badge_token,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

_RATE_LIMITING_ON = getattr(app_main, "AUTH_RATE_LIMITS", None) is not None


def _badge_audit_rows(db: Session, *, action: str, company_id: int = None):
    q = db.query(AuditLog).filter(AuditLog.action == action, AuditLog.resource_type == "kiosk_station")
    if company_id is not None:
        q = q.filter(AuditLog.company_id == company_id)
    return q.all()


def test_valid_badge_mints_scoped_short_lived_token(client: TestClient, db_session: Session):
    """A known in-company badge mints a scope='kiosk' access token: correct
    claims (type/scope/cid/sub), ≤ 5-minute expiry, NO refresh_token, the
    operator identity block, and a KIOSK_BADGE_TOKEN_ISSUED audit row."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A, first_name="Alice", last_name="Torres")

    resp = mint_badge_token(client, kiosk_token_for(station), operator.employee_id)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()

    # NO refresh token — the load-bearing posture improvement over employee-login.
    assert "refresh_token" not in body
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 300
    assert body["user"] == {"id": operator.id, "full_name": "Alice Torres", "employee_id": operator.employee_id}

    claims = jwt.decode(body["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert claims["type"] == "access"
    assert claims["scope"] == "kiosk"
    assert claims["cid"] == COMPANY_A
    assert claims["sub"] == str(operator.id)
    # Expiry ≤ 5 minutes out (small clock-skew tolerance).
    expires_at = datetime.utcfromtimestamp(claims["exp"])
    assert expires_at <= datetime.utcnow() + timedelta(minutes=5, seconds=5)

    db_session.expire_all()
    issued = _badge_audit_rows(db_session, action="KIOSK_BADGE_TOKEN_ISSUED", company_id=COMPANY_A)
    assert any(
        r.resource_id == station.id and r.user_id == operator.id for r in issued
    ), "expected a KIOSK_BADGE_TOKEN_ISSUED audit row attributed to the operator"


def test_minted_token_works_on_shop_floor(client: TestClient, db_session: Session):
    """The badge-minted operator token authenticates a shop-floor read as that user."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)

    minted = mint_badge_token(client, kiosk_token_for(station), operator.employee_id)
    assert minted.status_code == status.HTTP_200_OK, minted.text
    operator_token = minted.json()["access_token"]

    resp = client.get("/api/v1/shop-floor/my-active-job", headers=bearer(operator_token))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["active_jobs"] == []


@pytest.mark.parametrize(
    "case",
    ["unknown", "inactive", "locked", "foreign_tenant"],
)
def test_bad_badges_uniform_401_and_audited(client: TestClient, db_session: Session, case):
    """Unknown / inactive / locked / foreign-tenant badges are ALL the same
    401 'Invalid badge' (no account probing), each leaving a
    KIOSK_BADGE_TOKEN_FAILED audit row against the station's company."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)

    if case == "unknown":
        badge = "NO-SUCH-BADGE-99999"
    elif case == "inactive":
        badge = make_user(db_session, company_id=COMPANY_A, is_active=False).employee_id
    elif case == "locked":
        badge = make_user(
            db_session, company_id=COMPANY_A, locked_until=datetime.utcnow() + timedelta(minutes=30)
        ).employee_id
    else:  # foreign_tenant: a perfectly valid badge — in ANOTHER company
        badge = make_user(db_session, company_id=COMPANY_B).employee_id

    resp = mint_badge_token(client, kiosk_token_for(station), badge)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text
    assert resp.json()["detail"] == "Invalid badge"

    db_session.expire_all()
    failures = _badge_audit_rows(db_session, action="KIOSK_BADGE_TOKEN_FAILED", company_id=COMPANY_A)
    assert any(r.resource_id == station.id for r in failures), "expected a KIOSK_BADGE_TOKEN_FAILED audit row"


def test_ambiguous_badge_in_company_409(client: TestClient, db_session: Session):
    """Two same-company badges that NORMALIZE identically ('EMP-42' vs
    'OP-0042' -> '0042') make a fuzzy scan ambiguous: 409 admin-data error
    (mirroring employee-login), and no token is ever minted."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    make_user(db_session, company_id=COMPANY_A, employee_id="EMP-42")
    make_user(db_session, company_id=COMPANY_A, employee_id="OP-0042")

    resp = mint_badge_token(client, kiosk_token_for(station), "42")
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "not unique" in resp.json()["detail"]
    assert "access_token" not in resp.json()


def test_no_station_token_401(client: TestClient, db_session: Session):
    """Without a bearer token the mint is 401 before any badge lookup."""
    make_user(db_session, company_id=COMPANY_A)
    resp = client.post(
        "/api/v1/auth/kiosk-badge-token",
        headers={"X-Requested-With": "XMLHttpRequest"},
        json={"employee_id": "0001"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_user_access_token_cannot_gate_the_mint(client: TestClient, db_session: Session):
    """A normal USER access token is not a station credential — 401. Only the
    scoped type='kiosk' station JWT can gate the badge exchange."""
    admin = make_user(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    user_token = create_access_token(subject=admin.id, company_id=COMPANY_A)

    resp = mint_badge_token(client, user_token, operator.employee_id)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_revoked_station_cannot_mint(client: TestClient, db_session: Session):
    """A revoked station's still-valid JWT can no longer mint badge tokens —
    the DB row is re-checked on every exchange."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    token = kiosk_token_for(station)

    station.revoked = True
    db_session.commit()

    resp = mint_badge_token(client, token, operator.employee_id)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_forged_cid_station_token_cannot_mint(client: TestClient, db_session: Session):
    """A station token whose cid disagrees with the station row is 401 — the
    claim can never redirect the badge lookup to another tenant."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    operator_b = make_user(db_session, company_id=COMPANY_B)
    forged = kiosk_token_for(station, company_id=COMPANY_B)

    resp = mint_badge_token(client, forged, operator_b.employee_id)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


@pytest.mark.skipif(not _RATE_LIMITING_ON, reason="Rate limiting disabled in this environment")
def test_badge_mint_rate_limited_after_thirty(client: TestClient, db_session: Session):
    """The 31st mint attempt within a minute is 429 (30/min per IP)."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    token = kiosk_token_for(station)

    for i in range(30):
        r = mint_badge_token(client, token, "NO-SUCH-BADGE")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    blocked = mint_badge_token(client, token, "NO-SUCH-BADGE")
    assert blocked.status_code == 429, blocked.text
    assert "Rate limit exceeded" in blocked.json()["detail"]
    assert blocked.headers.get("Retry-After") is not None
