"""Crew-station kiosk lifecycle + station-token fence (``/shop-floor/kiosk-stations``).

Feature surface under test:
- ADMIN/MANAGER manage work-center-bound, PIN-protected kiosk stations
  (create / list / revoke / reset-pin) — the crew twin of the visitor
  sign-in stations.
- ``POST /shop-floor/kiosk-stations/station-login`` (public, rate-limited)
  unlocks a tablet with the shared PIN and mints a scoped ``type="kiosk"``
  JWT + the station identity payload the tablet renders.

Headline invariants (mirrors tests/test_visitor_logs.py):
1. **Scoped-token auth matrix** — a station kiosk token is honored ONLY by
   ``get_kiosk_or_user`` (the roster queue read) and the badge mint; every
   normal ``get_current_user`` endpoint rejects it (the ``verify_token``
   ``type=="access"`` fence). Revoked stations and forged ``cid`` claims are
   401 on their next request — the DB row is the revocation + tenant authority.
2. **PIN handling** — bcrypt-hashed at rest, never echoed; wrong PIN /
   unknown station / revoked station are a uniform 401; failures are audited
   against the station's company.
3. **RBAC fail-closed + tenant isolation** on the management endpoints.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as app_main
from app.models.audit_log import AuditLog
from app.models.kiosk_station import KioskStation
from app.models.user import UserRole
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    STATION_LOGIN_URL,
    STATIONS_URL,
    bearer,
    ensure_company,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

_RATE_LIMITING_ON = getattr(app_main, "AUTH_RATE_LIMITS", None) is not None


def _audit_rows(db: Session, *, action: str, company_id: int = None):
    q = db.query(AuditLog).filter(AuditLog.action == action, AuditLog.resource_type == "kiosk_station")
    if company_id is not None:
        q = q.filter(AuditLog.company_id == company_id)
    return q.all()


# ===========================================================================
# 1. station-login (PIN auth + station identity payload + audit on failure)
# ===========================================================================


def test_station_login_correct_pin_mints_token_and_station_info(client: TestClient, db_session: Session):
    """Correct PIN → 200 with { access_token, station: {...} } — the station
    block carries the bound work-center identity the tablet needs."""
    wc = make_work_center(db_session, company_id=COMPANY_A, name="Weld Bay 1")
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc, pin="4242", label="Weld Kiosk")

    resp = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "4242"})
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 24 * 3600
    assert body["station"] == {
        "id": station.id,
        "label": "Weld Kiosk",
        "work_center_id": wc.id,
        "work_center_code": wc.code,
        "work_center_name": "Weld Bay 1",
    }

    # The minted token actually works on the station's own queue read.
    read = client.get(queue_url(wc.id), headers=bearer(body["access_token"]))
    assert read.status_code == status.HTTP_200_OK, read.text
    assert read.json()["station"]["label"] == "Weld Kiosk"


def test_station_login_wrong_pin_uniform_401_and_audited(client: TestClient, db_session: Session):
    """Wrong PIN → uniform 401, and a LOGIN_FAILED audit row attributed to the
    station's company."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A, pin="1234", label="Dock Kiosk")

    resp = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "9999"})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text
    assert resp.json()["detail"] == "Invalid station or PIN"

    db_session.expire_all()
    failures = _audit_rows(db_session, action="LOGIN_FAILED", company_id=COMPANY_A)
    assert any(r.resource_id == station.id for r in failures), "expected a LOGIN_FAILED audit row for the station"


def test_station_login_revoked_station_401(client: TestClient, db_session: Session):
    """A revoked station cannot be unlocked even with the correct PIN."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A, pin="1234", revoked=True)
    resp = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "1234"})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_station_login_unknown_station_401(client: TestClient, db_session: Session):
    """Unknown station id → 401, indistinguishable from a bad PIN."""
    ensure_company(db_session, COMPANY_A)
    resp = client.post(STATION_LOGIN_URL, json={"station_id": 987654, "pin": "1234"})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


@pytest.mark.parametrize("bad_pin", ["123", "abcd", "123456789", "12 34", ""])
def test_station_login_pin_format_rejected(client: TestClient, db_session: Session, bad_pin):
    """PIN must be 4–8 digits; malformed PINs are a 422 schema rejection."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A, pin="1234")
    resp = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": bad_pin})
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


@pytest.mark.skipif(not _RATE_LIMITING_ON, reason="Rate limiting disabled in this environment")
def test_station_login_rate_limited_after_five_attempts(client: TestClient, db_session: Session):
    """kiosk station-login (shared numeric PIN) allows 5/min per IP, then 429."""
    ensure_company(db_session, COMPANY_A)
    payload = {"station_id": 999999, "pin": "000000"}

    for i in range(5):
        r = client.post(STATION_LOGIN_URL, json=payload)
        assert r.status_code == status.HTTP_401_UNAUTHORIZED, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    blocked = client.post(STATION_LOGIN_URL, json=payload)
    assert blocked.status_code == 429, blocked.text
    assert "Rate limit exceeded" in blocked.json()["detail"]


# ===========================================================================
# 2. Scoped-token auth matrix (the security-critical part)
# ===========================================================================


def test_kiosk_token_rejected_by_normal_user_endpoints(client: TestClient, db_session: Session):
    """THE FENCE: a station kiosk token must be rejected by every normal
    ``get_current_user`` endpoint — it can never act as a user session."""
    station = make_kiosk_station(db_session, company_id=COMPANY_A)
    token = kiosk_token_for(station)

    for path in ("/api/v1/shop-floor/my-active-job", "/api/v1/work-orders/", STATIONS_URL):
        resp = client.get(path, headers=bearer(token))
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED, f"{path}: {resp.status_code} {resp.text}"


def test_kiosk_token_accepted_only_on_own_queue(client: TestClient, db_session: Session):
    """A station kiosk token reads its OWN work center's queue (200) and is
    403 on any other work center — the WC binding comes from the DB row."""
    wc_own = make_work_center(db_session, company_id=COMPANY_A)
    wc_other = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc_own)
    token = kiosk_token_for(station)

    own = client.get(queue_url(wc_own.id), headers=bearer(token))
    assert own.status_code == status.HTTP_200_OK, own.text

    other = client.get(queue_url(wc_other.id), headers=bearer(token))
    assert other.status_code == status.HTTP_403_FORBIDDEN, other.text


def test_revoked_station_401_despite_valid_jwt(client: TestClient, db_session: Session):
    """Revocation is a DB-row check on every request: a still-valid JWT dies
    the moment the row is revoked."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    token = kiosk_token_for(station)

    ok = client.get(queue_url(wc.id), headers=bearer(token))
    assert ok.status_code == status.HTTP_200_OK, ok.text

    station.revoked = True
    db_session.commit()

    resp = client.get(queue_url(wc.id), headers=bearer(token))
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_forged_cid_claim_401(client: TestClient, db_session: Session):
    """A kiosk token whose ``cid`` differs from the station row's company is
    rejected — the client claim can never widen tenant scope."""
    ensure_company(db_session, COMPANY_B)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    forged = kiosk_token_for(station, company_id=COMPANY_B)

    resp = client.get(queue_url(wc.id), headers=bearer(forged))
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_kiosk_token_for_missing_station_401(client: TestClient, db_session: Session):
    """A kiosk token whose ``sid`` points at no station row is rejected."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    from app.core.security import create_kiosk_token

    token = create_kiosk_token(station_id=999999, company_id=COMPANY_A, label="Ghost")
    resp = client.get(queue_url(wc.id), headers=bearer(token))
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_signin_token_rejected_on_queue(client: TestClient, db_session: Session):
    """A visitor ``signin`` station token must NOT satisfy the kiosk queue read
    (type=='kiosk' is required on the station branch)."""
    from app.core.security import create_signin_token

    wc = make_work_center(db_session, company_id=COMPANY_A)
    token = create_signin_token(station_id=1, company_id=COMPANY_A, label="Lobby")
    resp = client.get(queue_url(wc.id), headers=bearer(token))
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


# ===========================================================================
# 3. RBAC (fail-closed) + PIN hygiene + tenant isolation on management
# ===========================================================================


def test_create_station_does_not_echo_pin(client: TestClient, db_session: Session):
    """Creating a station never echoes the PIN or pin_hash; the row is stamped
    with the caller's company and stores only a bcrypt hash."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    wc = make_work_center(db_session, company_id=COMPANY_A)

    resp = client.post(
        STATIONS_URL,
        headers=user_headers(admin),
        json={"label": "New Crew Kiosk", "work_center_id": wc.id, "pin": "4321"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert "pin" not in body
    assert "pin_hash" not in body
    assert body["label"] == "New Crew Kiosk"
    assert body["work_center_id"] == wc.id
    assert body["work_center_code"] == wc.code

    row = db_session.query(KioskStation).filter(KioskStation.id == body["id"]).first()
    assert row.company_id == COMPANY_A
    assert row.pin_hash and row.pin_hash != "4321"

    db_session.expire_all()
    creates = _audit_rows(db_session, action="CREATE", company_id=COMPANY_A)
    assert any(r.resource_id == body["id"] for r in creates), "expected a CREATE audit row"


def test_create_station_foreign_work_center_404(client: TestClient, db_session: Session):
    """A station can never be bound to another company's work center."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    foreign_wc = make_work_center(db_session, company_id=COMPANY_B)

    resp = client.post(
        STATIONS_URL,
        headers=user_headers(admin),
        json={"label": "X", "work_center_id": foreign_wc.id, "pin": "1234"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER])
def test_station_management_denied_for_lower_roles(client: TestClient, db_session: Session, role):
    """Create/list/revoke/reset-pin require ADMIN/MANAGER — lower roles are 403."""
    user = make_user(db_session, company_id=COMPANY_A, role=role)
    wc = make_work_center(db_session, company_id=COMPANY_A)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, work_center=wc)
    headers = user_headers(user)

    create = client.post(STATIONS_URL, headers=headers, json={"label": "X", "work_center_id": wc.id, "pin": "1234"})
    assert create.status_code == status.HTTP_403_FORBIDDEN, create.text

    listing = client.get(STATIONS_URL, headers=headers)
    assert listing.status_code == status.HTTP_403_FORBIDDEN, listing.text

    revoke = client.post(f"{STATIONS_URL}/{station.id}/revoke", headers=headers)
    assert revoke.status_code == status.HTTP_403_FORBIDDEN, revoke.text

    reset = client.post(f"{STATIONS_URL}/{station.id}/reset-pin", headers=headers, json={"pin": "5678"})
    assert reset.status_code == status.HTTP_403_FORBIDDEN, reset.text


def test_list_stations_tenant_scoped(client: TestClient, db_session: Session):
    """Company A's admin never sees company B's kiosk stations."""
    a_station = make_kiosk_station(db_session, company_id=COMPANY_A)
    b_station = make_kiosk_station(db_session, company_id=COMPANY_B)
    admin_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get(STATIONS_URL, headers=user_headers(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = {s["id"] for s in resp.json()["stations"]}
    assert a_station.id in ids
    assert b_station.id not in ids


def test_revoke_other_company_station_404(client: TestClient, db_session: Session):
    """A company-A admin cannot revoke company B's station (404); B's row survives."""
    admin_a = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    b_station = make_kiosk_station(db_session, company_id=COMPANY_B)

    resp = client.post(f"{STATIONS_URL}/{b_station.id}/revoke", headers=user_headers(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    assert db_session.query(KioskStation).filter(KioskStation.id == b_station.id).first().revoked is False


def test_revoke_is_idempotent_and_audited(client: TestClient, db_session: Session):
    """Revoking twice is a no-op success; the flip leaves a STATUS_CHANGE row."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    station = make_kiosk_station(db_session, company_id=COMPANY_A)

    first = client.post(f"{STATIONS_URL}/{station.id}/revoke", headers=user_headers(admin))
    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json()["revoked"] is True

    second = client.post(f"{STATIONS_URL}/{station.id}/revoke", headers=user_headers(admin))
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json()["revoked"] is True

    db_session.expire_all()
    changes = _audit_rows(db_session, action="STATUS_CHANGE", company_id=COMPANY_A)
    assert any(r.resource_id == station.id for r in changes), "expected a STATUS_CHANGE audit row"


def test_reset_pin_old_dies_new_works(client: TestClient, db_session: Session):
    """reset-pin re-hashes: the old PIN stops working, the new PIN unlocks."""
    admin = make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    station = make_kiosk_station(db_session, company_id=COMPANY_A, pin="1111")

    pre = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "1111"})
    assert pre.status_code == status.HTTP_200_OK, pre.text

    reset = client.post(f"{STATIONS_URL}/{station.id}/reset-pin", headers=user_headers(admin), json={"pin": "2222"})
    assert reset.status_code == status.HTTP_200_OK, reset.text

    old = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "1111"})
    assert old.status_code == status.HTTP_401_UNAUTHORIZED, old.text

    new = client.post(STATION_LOGIN_URL, json={"station_id": station.id, "pin": "2222"})
    assert new.status_code == status.HTTP_200_OK, new.text
