"""Backend coverage for the visitor sign-in tablet + admin visitor-log API.

Feature surface under test (``/api/v1/visitor-logs``):
- A PIN-unlocked entrance tablet ("station") mints a scoped ``type="signin"``
  JWT and may perform exactly TWO writes: sign-in and sign-out.
- Staff (ADMIN/MANAGER/SUPERVISOR) view, export, and soft-delete the log and
  manage stations via normal RBAC.

The headline invariants proved here mirror the compliance posture in CLAUDE.md
and the build spec:

1. **Scoped-token auth matrix** — the security-critical part. A station
   ``signin`` token is accepted ONLY on the two visitor-write endpoints and
   ONLY through ``get_signin_principal`` (the DB row is the authoritative
   company-binding + revocation anchor). It is rejected by every normal
   ``get_current_user`` endpoint (the ``verify_token`` ``type=="access"``
   fence). Expired/garbage tokens, revoked stations, and ``cid`` ≠ row
   mismatches are all 401.
2. **station-login** — correct PIN mints a token; wrong PIN / revoked / bad
   format / missing station all fail; a failed attempt writes a LOGIN_FAILED
   audit row attributed to the station's company.
3. **sign-in** — creates a tenant-scoped VisitorLog, emits ``CREATE`` audit,
   enforces purpose_note-when-OTHER and the safety acknowledgment, persists the
   ack, matches the host to an in-company active user, and never blows up when
   the best-effort host email cannot be enqueued (no Redis in tests).
4. **sign-out** — by name (1 match → out, >1 → 409 disambiguation, 0 → 404), by
   id, double sign-out guarded, ``STATUS_CHANGE`` audit emitted.
5. **Tenant isolation** — a principal scoped to company A can never read /
   sign-out / delete company B's rows; cross-tenant host lookup yields no match
   (404 territory, never another tenant's data).
6. **RBAC (fail-closed)** — view roles vs manage roles enforced server-side;
   export writes an EXPORT audit; soft-delete drops the row from later lists;
   revoke is idempotent; reset-pin re-hashes (old PIN dies, new PIN works).

The multi-company fixture shape mirrors tests/api/test_qms_standards_tenant_isolation.py.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    create_signin_token,
    get_password_hash,
)
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.signin_station import SigninStation
from app.models.user import User, UserRole
from app.models.visitor_log import VisitorLog, VisitorPurpose, VisitorStatus

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_A = 1
COMPANY_B = 2

# Module-level counter so every fixture row gets a globally unique natural key,
# even across companies and across tests sharing a worker DB under -n auto.
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _ensure_company(db: Session, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        company = Company(
            id=company_id,
            name=f"Company {company_id}",
            slug=f"company-{company_id}",
            is_active=True,
        )
        db.add(company)
        db.commit()
    return company


def _make_user(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    role: UserRole = UserRole.ADMIN,
    first_name: str = None,
    last_name: str = None,
    email: str = None,
    is_active: bool = True,
) -> User:
    _ensure_company(db, company_id)
    n = _next()
    user = User(
        email=email or f"vlog-user-{n}@co{company_id}.test",
        employee_id=f"VLOG-{n:05d}",
        first_name=first_name or "Vlog",
        last_name=last_name or f"C{company_id}",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",  # tokens minted directly; never used for login
        role=role,
        is_active=is_active,
        is_superuser=False,
        company_id=company_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers_for(user: User, *, active_company_id: int = None) -> dict:
    cid = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=cid)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _station_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _make_station(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    pin: str = "1234",
    label: str = None,
    revoked: bool = False,
) -> SigninStation:
    _ensure_company(db, company_id)
    n = _next()
    station = SigninStation(
        label=label or f"Lobby {n}",
        pin_hash=get_password_hash(pin),
        revoked=revoked,
        company_id=company_id,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


def _signin_token_for(station: SigninStation, *, company_id: int = None) -> str:
    """Mint a station signin token directly (bypassing station-login)."""
    return create_signin_token(
        station_id=station.id,
        company_id=company_id if company_id is not None else station.company_id,
        label=station.label,
    )


def _make_visitor(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    visitor_name: str = None,
    status_: VisitorStatus = VisitorStatus.SIGNED_IN,
    visitor_company: str = None,
    is_deleted: bool = False,
) -> VisitorLog:
    from datetime import datetime

    _ensure_company(db, company_id)
    n = _next()
    row = VisitorLog(
        company_id=company_id,
        visitor_name=visitor_name or f"Visitor {n}",
        visitor_company=visitor_company,
        purpose=VisitorPurpose.MEETING,
        safety_acknowledged=True,
        status=status_,
        signed_in_at=datetime.utcnow(),
        is_deleted=is_deleted,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _valid_signin_payload(**overrides) -> dict:
    payload = {
        "visitor_name": "Jane Caller",
        "visitor_company": "Acme Supply",
        "visitor_phone": "555-0100",
        "host_name": None,
        "purpose": "meeting",
        "purpose_note": None,
        "safety_acknowledged": True,
    }
    payload.update(overrides)
    return payload


def _audit_rows(db: Session, *, action: str, resource_type: str = "visitor_log", company_id: int = None):
    q = db.query(AuditLog).filter(AuditLog.action == action, AuditLog.resource_type == resource_type)
    if company_id is not None:
        q = q.filter(AuditLog.company_id == company_id)
    return q.all()


# ===========================================================================
# 1. Scoped-token auth matrix (the security-critical part)
# ===========================================================================


def test_signin_station_token_accepted_company_from_db_row(client: TestClient, db_session: Session):
    """A valid station signin token is accepted on /sign-in, the row is created,
    and the company comes from the DB station row (kind=='station')."""
    station = _make_station(db_session, company_id=COMPANY_A, label="Lobby A")
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]

    row = db_session.query(VisitorLog).filter(VisitorLog.id == new_id).first()
    assert row is not None
    # company_id comes from the authoritative station row, not the client.
    assert row.company_id == COMPANY_A
    # The station is recorded as the actor (id + denormalized label).
    assert row.signin_station_id == station.id
    assert row.station_label == "Lobby A"


def test_staff_access_token_accepted_on_sign_in(client: TestClient, db_session: Session):
    """A normal staff access token is accepted on /sign-in (kind=='user');
    no station id/label is recorded."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_headers_for(admin),
        json=_valid_signin_payload(visitor_name="Staff Walkin"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.company_id == COMPANY_A
    assert row.signin_station_id is None
    assert row.station_label is None


def test_display_token_rejected_on_visitor_write(client: TestClient, db_session: Session):
    """A wallboard ``display`` token must NOT authenticate a visitor write.

    The fence is ``verify_token`` (type=='access') + ``verify_signin_token``
    (type=='signin'); a display token satisfies neither, so /sign-in is 401."""
    from datetime import datetime, timedelta

    from app.core.security import create_display_token

    _ensure_company(db_session, COMPANY_A)
    display_token = create_display_token(
        jti="fake-jti",
        company_id=COMPANY_A,
        label="TV",
        expires_at=datetime.utcnow() + timedelta(days=1),
    )

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(display_token),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_garbage_signin_token_is_401(client: TestClient, db_session: Session):
    """A structurally-invalid bearer token is rejected on the visitor write."""
    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers("not-a-real-jwt"),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_expired_signin_token_is_401(client: TestClient, db_session: Session):
    """An expired signin token (negative TTL) fails JWT expiry and is 401."""
    station = _make_station(db_session, company_id=COMPANY_A)
    expired = create_signin_token(
        station_id=station.id,
        company_id=station.company_id,
        label=station.label,
        ttl_hours=-1,  # already expired
    )
    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(expired),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_revoked_station_token_is_401(client: TestClient, db_session: Session):
    """A token for a station that has since been revoked is rejected on the next
    request — the DB ``revoked`` flag is re-checked every call."""
    station = _make_station(db_session, company_id=COMPANY_A, revoked=False)
    token = _signin_token_for(station)

    # Revoke the station out-of-band.
    station.revoked = True
    db_session.commit()

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_signin_token_cid_mismatch_rejected(client: TestClient, db_session: Session):
    """A signin token whose ``cid`` claim differs from the station row's
    company_id is rejected — the client claim can never widen tenant scope."""
    station = _make_station(db_session, company_id=COMPANY_A)
    # Forge a token claiming company B while the row is company A.
    forged = _signin_token_for(station, company_id=COMPANY_B)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(forged),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_signin_token_for_missing_station_is_401(client: TestClient, db_session: Session):
    """A signin token whose ``sid`` points at no station row is rejected."""
    _ensure_company(db_session, COMPANY_A)
    token = create_signin_token(station_id=999999, company_id=COMPANY_A, label="Ghost")
    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_signin_token_rejected_by_normal_user_endpoint(client: TestClient, db_session: Session):
    """THE FENCE: a station signin token must be rejected by every normal
    ``get_current_user`` endpoint. Presenting it to the staff-only list view
    (which flows through ``verify_token``'s type=='access' check) is 401, never
    a 200 that would leak the log to an unattended tablet."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.get("/api/v1/visitor-logs/", headers=_station_headers(token))
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text

    # And against a completely unrelated authenticated endpoint.
    resp2 = client.get("/api/v1/visitor-logs/stations", headers=_station_headers(token))
    assert resp2.status_code == status.HTTP_401_UNAUTHORIZED, resp2.text


# ===========================================================================
# 2. station-login (PIN auth + audit on failure)
# ===========================================================================


def test_station_login_correct_pin_mints_token(client: TestClient, db_session: Session):
    """Correct PIN → 200 with a usable token; the token actually authenticates
    a downstream visitor write."""
    station = _make_station(db_session, company_id=COMPANY_A, pin="4242", label="Front Desk")

    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": station.id, "pin": "4242"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["station_label"] == "Front Desk"
    assert body["expires_in"] == 24 * 3600
    token = body["token"]
    assert token

    # The minted token works on /sign-in.
    write = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(),
    )
    assert write.status_code == status.HTTP_201_CREATED, write.text


def test_station_login_wrong_pin_is_401_and_audited(client: TestClient, db_session: Session):
    """Wrong PIN → 401, and a LOGIN_FAILED audit row is written, attributed to
    the station's company (so the trail stays tenant-scoped)."""
    station = _make_station(db_session, company_id=COMPANY_A, pin="1234", label="Dock")

    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": station.id, "pin": "9999"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text

    db_session.expire_all()
    failures = _audit_rows(db_session, action="LOGIN_FAILED", resource_type="signin_station", company_id=COMPANY_A)
    assert any(r.resource_id == station.id for r in failures), "expected a LOGIN_FAILED audit row for the station"


def test_station_login_revoked_station_is_401(client: TestClient, db_session: Session):
    """A revoked station cannot be unlocked even with the correct PIN."""
    station = _make_station(db_session, company_id=COMPANY_A, pin="1234", revoked=True)
    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": station.id, "pin": "1234"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


def test_station_login_unknown_station_is_401(client: TestClient, db_session: Session):
    """An unknown station id → 401 (indistinguishable from a bad PIN)."""
    _ensure_company(db_session, COMPANY_A)
    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": 987654, "pin": "1234"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, resp.text


@pytest.mark.parametrize("bad_pin", ["123", "abcd", "123456789", "12 34", ""])
def test_station_login_pin_format_rejected(client: TestClient, db_session: Session, bad_pin):
    """PIN must be 4–8 digits; malformed PINs are a 422 schema rejection."""
    station = _make_station(db_session, company_id=COMPANY_A, pin="1234")
    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": station.id, "pin": bad_pin},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


@pytest.mark.parametrize("good_pin", ["1234", "12345678"])
def test_station_login_pin_format_boundaries_accepted(client: TestClient, db_session: Session, good_pin):
    """4-digit and 8-digit PINs are the accepted boundaries."""
    station = _make_station(db_session, company_id=COMPANY_A, pin=good_pin)
    resp = client.post(
        "/api/v1/visitor-logs/station-login",
        json={"station_id": station.id, "pin": good_pin},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text


# ===========================================================================
# 3. sign-in
# ===========================================================================


def test_sign_in_creates_log_scoped_and_audited(client: TestClient, db_session: Session):
    """Sign-in via staff creates a VisitorLog scoped to the active company and
    emits a CREATE audit row."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_headers_for(admin),
        json=_valid_signin_payload(visitor_name="Audited Visitor"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    new_id = resp.json()["id"]
    assert resp.json()["status"] == VisitorStatus.SIGNED_IN.value

    row = db_session.query(VisitorLog).filter(VisitorLog.id == new_id).first()
    assert row.company_id == COMPANY_A

    db_session.expire_all()
    creates = _audit_rows(db_session, action="CREATE", company_id=COMPANY_A)
    assert any(r.resource_id == new_id for r in creates), "expected a CREATE audit row for the sign-in"


def test_sign_in_purpose_other_requires_note(client: TestClient, db_session: Session):
    """purpose=OTHER with no purpose_note is a 422 schema rejection."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(purpose="other", purpose_note=None),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_sign_in_purpose_other_with_note_ok(client: TestClient, db_session: Session):
    """purpose=OTHER WITH a note is accepted and the note persists."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(purpose="other", purpose_note="Delivering calibration gauges"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.purpose == VisitorPurpose.OTHER
    assert row.purpose_note == "Delivering calibration gauges"


def test_sign_in_requires_safety_acknowledgment(client: TestClient, db_session: Session):
    """safety_acknowledged must be true to sign in (422 otherwise)."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(safety_acknowledged=False),
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text


def test_sign_in_persists_safety_acknowledged(client: TestClient, db_session: Session):
    """The safety/NDA acknowledgment is persisted on the row."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(safety_acknowledged=True),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.safety_acknowledged is True


def test_sign_in_matches_host_user_in_company(client: TestClient, db_session: Session):
    """A host name that uniquely matches an active in-company user sets
    host_user_id; the best-effort email enqueue does NOT raise even with no
    Redis (it is swallowed by enqueue_job_best_effort)."""
    host = _make_user(
        db_session,
        company_id=COMPANY_A,
        role=UserRole.MANAGER,
        first_name="Dana",
        last_name="Host",
        email="dana.host@co1.test",
    )
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(host_name="dana host"),  # case-insensitive match
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.host_user_id == host.id


def test_sign_in_ambiguous_host_leaves_user_unmatched(client: TestClient, db_session: Session):
    """Two active users with the same name → no host match (>1 match → None)."""
    _make_user(db_session, company_id=COMPANY_A, first_name="Sam", last_name="Twin", email="sam1@co1.test")
    _make_user(db_session, company_id=COMPANY_A, first_name="Sam", last_name="Twin", email="sam2@co1.test")
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(host_name="Sam Twin"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.host_user_id is None
    assert row.host_name == "Sam Twin"  # free-text host still stored


def test_sign_in_host_in_other_company_not_matched(client: TestClient, db_session: Session):
    """A host name matching a user in ANOTHER company is NOT matched — host
    matching is company-scoped (CUI: never cross-tenant)."""
    # Same name lives in company B.
    _make_user(db_session, company_id=COMPANY_B, first_name="Cross", last_name="Tenant", email="cross@co2.test")
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(host_name="Cross Tenant"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.host_user_id is None  # the company-B user must not be matched


# ===========================================================================
# 4. sign-out
# ===========================================================================


def test_sign_out_by_name_single_match(client: TestClient, db_session: Session):
    """Exactly one open match by name → signs out + STATUS_CHANGE audit."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    visitor = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="Solo Visitor")

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_station_headers(token),
        json={"name": "Solo Visitor"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == VisitorStatus.SIGNED_OUT.value

    db_session.expire_all()
    row = db_session.query(VisitorLog).filter(VisitorLog.id == visitor.id).first()
    assert row.status == VisitorStatus.SIGNED_OUT
    assert row.signed_out_at is not None

    changes = _audit_rows(db_session, action="STATUS_CHANGE", company_id=COMPANY_A)
    assert any(r.resource_id == visitor.id for r in changes), "expected a STATUS_CHANGE audit row for sign-out"


def test_sign_in_response_signed_in_at_ends_with_z(client: TestClient, db_session: Session):
    """The sign-in response ``signed_in_at`` is UTC ISO-8601 with a trailing 'Z'.

    Timezone-consistency regression guard: the visitor timestamps flow through
    ``VisitorLogResponse`` (a ``UTCModel`` subclass that re-declares its own
    ``model_config``). The inherited ``json_encoders={datetime: to_utc_iso}``
    must still apply so the frontend parses the value as UTC (not viewer-local).
    """
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token),
        json=_valid_signin_payload(),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["signed_in_at"].endswith("Z"), body["signed_in_at"]
    # No offset was smuggled in place of 'Z'.
    assert "+" not in body["signed_in_at"]


def test_sign_out_response_signed_out_at_ends_with_z(client: TestClient, db_session: Session):
    """The sign-out response ``signed_out_at`` is UTC ISO-8601 with a trailing 'Z'.

    This is the exact trigger for the fix: a sign-out timestamp serialized
    without the 'Z' rendered as viewer-local time on the frontend instead of the
    shop's Central time.
    """
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    _make_visitor(db_session, company_id=COMPANY_A, visitor_name="Zulu Visitor")

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_station_headers(token),
        json={"name": "Zulu Visitor"},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["status"] == VisitorStatus.SIGNED_OUT.value
    assert body["signed_out_at"] is not None
    assert body["signed_out_at"].endswith("Z"), body["signed_out_at"]
    assert "+" not in body["signed_out_at"]
    # signed_in_at is likewise UTC-Z on the same response.
    assert body["signed_in_at"].endswith("Z"), body["signed_in_at"]


def test_sign_out_by_visitor_log_id(client: TestClient, db_session: Session):
    """Sign-out by explicit visitor_log_id signs out that exact row."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    visitor = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="By Id")

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_headers_for(admin),
        json={"visitor_log_id": visitor.id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    assert db_session.query(VisitorLog).filter(VisitorLog.id == visitor.id).first().status == VisitorStatus.SIGNED_OUT


def test_sign_out_by_name_multiple_matches_409(client: TestClient, db_session: Session):
    """>1 open visitors under the same name → 409 with a disambiguation list."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    v1 = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="Pat Dup", visitor_company="Acme")
    v2 = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="Pat Dup", visitor_company="Globex")

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_station_headers(token),
        json={"name": "Pat Dup"},
    )
    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    detail = resp.json()["detail"]
    match_ids = {m["id"] for m in detail["matches"]}
    assert match_ids == {v1.id, v2.id}
    # The disambiguation list carries only company + signed_in_at (minimal PII).
    for m in detail["matches"]:
        assert set(m.keys()) == {"id", "visitor_company", "signed_in_at"}

    # Neither row was signed out (the conflict is fail-safe).
    db_session.expire_all()
    assert db_session.query(VisitorLog).filter(VisitorLog.id == v1.id).first().status == VisitorStatus.SIGNED_IN
    assert db_session.query(VisitorLog).filter(VisitorLog.id == v2.id).first().status == VisitorStatus.SIGNED_IN


def test_sign_out_no_open_match_404(client: TestClient, db_session: Session):
    """No open visitor with that name → 404."""
    station = _make_station(db_session, company_id=COMPANY_A)
    token = _signin_token_for(station)
    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_station_headers(token),
        json={"name": "Nobody Here"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_double_sign_out_guarded(client: TestClient, db_session: Session):
    """An already-signed-out visitor is no longer an OPEN row — signing out
    again by id returns 404 (the guard is status==SIGNED_IN)."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    visitor = _make_visitor(db_session, company_id=COMPANY_A, status_=VisitorStatus.SIGNED_OUT)

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_headers_for(admin),
        json={"visitor_log_id": visitor.id},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


# ===========================================================================
# 5. Tenant isolation
# ===========================================================================


def test_list_only_returns_own_company(client: TestClient, db_session: Session):
    """The list endpoint returns only the caller's company's visitor rows."""
    a = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="A Visitor")
    b = _make_visitor(db_session, company_id=COMPANY_B, visitor_name="B Visitor")
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get("/api/v1/visitor-logs/", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = {row["id"] for row in resp.json()["items"]}
    assert a.id in ids
    assert b.id not in ids


def test_station_sign_out_cannot_reach_other_company_by_name(client: TestClient, db_session: Session):
    """A company-A station signing out by a name that exists ONLY in company B
    gets 404 — it can never reach another tenant's row."""
    station_a = _make_station(db_session, company_id=COMPANY_A)
    token_a = _signin_token_for(station_a)
    b_visitor = _make_visitor(db_session, company_id=COMPANY_B, visitor_name="B Only")

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_station_headers(token_a),
        json={"name": "B Only"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    # B's row is untouched.
    db_session.expire_all()
    assert db_session.query(VisitorLog).filter(VisitorLog.id == b_visitor.id).first().status == VisitorStatus.SIGNED_IN


def test_sign_out_other_company_by_id_404(client: TestClient, db_session: Session):
    """A company-A staffer cannot sign out company B's row by id (404)."""
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    b_visitor = _make_visitor(db_session, company_id=COMPANY_B)

    resp = client.post(
        "/api/v1/visitor-logs/sign-out",
        headers=_headers_for(admin_a),
        json={"visitor_log_id": b_visitor.id},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text


def test_delete_other_company_visitor_404(client: TestClient, db_session: Session):
    """A company-A admin cannot soft-delete company B's visitor row (404), and
    B's row survives undeleted."""
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    b_visitor = _make_visitor(db_session, company_id=COMPANY_B)

    resp = client.delete(f"/api/v1/visitor-logs/{b_visitor.id}", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    refreshed = db_session.query(VisitorLog).filter(VisitorLog.id == b_visitor.id).first()
    assert refreshed.is_deleted is False


def test_cross_tenant_host_lookup_no_leak(client: TestClient, db_session: Session):
    """Cross-tenant host lookup never leaks: a company-A sign-in naming a
    company-B user matches nobody (covered also above) — re-asserted here as a
    tenant-isolation control alongside the cross-company sign-out path."""
    _make_user(db_session, company_id=COMPANY_B, first_name="Bee", last_name="Host", email="bee@co2.test")
    station_a = _make_station(db_session, company_id=COMPANY_A)
    token_a = _signin_token_for(station_a)

    resp = client.post(
        "/api/v1/visitor-logs/sign-in",
        headers=_station_headers(token_a),
        json=_valid_signin_payload(host_name="Bee Host"),
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    row = db_session.query(VisitorLog).filter(VisitorLog.id == resp.json()["id"]).first()
    assert row.company_id == COMPANY_A
    assert row.host_user_id is None


def test_list_stations_only_own_company(client: TestClient, db_session: Session):
    """Station listing is tenant-scoped: company A never sees company B's stations."""
    a_station = _make_station(db_session, company_id=COMPANY_A, label="A station")
    b_station = _make_station(db_session, company_id=COMPANY_B, label="B station")
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)

    resp = client.get("/api/v1/visitor-logs/stations", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    ids = {s["id"] for s in resp.json()["stations"]}
    assert a_station.id in ids
    assert b_station.id not in ids


def test_revoke_other_company_station_404(client: TestClient, db_session: Session):
    """A company-A admin cannot revoke company B's station (404)."""
    admin_a = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    b_station = _make_station(db_session, company_id=COMPANY_B)

    resp = client.post(f"/api/v1/visitor-logs/stations/{b_station.id}/revoke", headers=_headers_for(admin_a))
    assert resp.status_code == status.HTTP_404_NOT_FOUND, resp.text
    db_session.expire_all()
    assert db_session.query(SigninStation).filter(SigninStation.id == b_station.id).first().revoked is False


# ===========================================================================
# 6. RBAC (fail-closed)
# ===========================================================================


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_list_allowed_for_view_roles(client: TestClient, db_session: Session, role):
    """GET / is allowed for ADMIN/MANAGER/SUPERVISOR."""
    user = _make_user(db_session, company_id=COMPANY_A, role=role)
    resp = client.get("/api/v1/visitor-logs/", headers=_headers_for(user))
    assert resp.status_code == status.HTTP_200_OK, resp.text


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING, UserRole.VIEWER])
def test_list_denied_for_lower_roles(client: TestClient, db_session: Session, role):
    """GET / is 403 for roles below SUPERVISOR (fail-closed)."""
    user = _make_user(db_session, company_id=COMPANY_A, role=role)
    resp = client.get("/api/v1/visitor-logs/", headers=_headers_for(user))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_supervisor_cannot_export(client: TestClient, db_session: Session):
    """export.csv is ADMIN/MANAGER only — a SUPERVISOR (a VIEW role) is 403."""
    supervisor = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    resp = client.get("/api/v1/visitor-logs/export.csv", headers=_headers_for(supervisor))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_supervisor_cannot_delete(client: TestClient, db_session: Session):
    """DELETE /{id} is ADMIN/MANAGER only — a SUPERVISOR is 403."""
    supervisor = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    visitor = _make_visitor(db_session, company_id=COMPANY_A)
    resp = client.delete(f"/api/v1/visitor-logs/{visitor.id}", headers=_headers_for(supervisor))
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("post", "/stations"),
        ("get", "/stations"),
    ],
)
def test_station_management_denied_for_supervisor(client: TestClient, db_session: Session, method, path_suffix):
    """Station create/list require ADMIN/MANAGER — a SUPERVISOR is 403."""
    supervisor = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    fn = getattr(client, method)
    kwargs = {"headers": _headers_for(supervisor)}
    if method == "post":
        kwargs["json"] = {"label": "X", "pin": "1234"}
    resp = fn(f"/api/v1/visitor-logs{path_suffix}", **kwargs)
    assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text


def test_revoke_and_reset_denied_for_supervisor(client: TestClient, db_session: Session):
    """Station revoke + reset-pin require ADMIN/MANAGER — a SUPERVISOR is 403."""
    supervisor = _make_user(db_session, company_id=COMPANY_A, role=UserRole.SUPERVISOR)
    station = _make_station(db_session, company_id=COMPANY_A)

    revoke = client.post(f"/api/v1/visitor-logs/stations/{station.id}/revoke", headers=_headers_for(supervisor))
    assert revoke.status_code == status.HTTP_403_FORBIDDEN, revoke.text

    reset = client.post(
        f"/api/v1/visitor-logs/stations/{station.id}/reset-pin",
        headers=_headers_for(supervisor),
        json={"pin": "5678"},
    )
    assert reset.status_code == status.HTTP_403_FORBIDDEN, reset.text


def test_export_writes_audit_export_action(client: TestClient, db_session: Session):
    """export.csv (ADMIN/MANAGER) streams CSV AND writes an EXPORT audit row."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    _make_visitor(db_session, company_id=COMPANY_A, visitor_name="Exported One")

    resp = client.get("/api/v1/visitor-logs/export.csv", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Exported One" in resp.text

    db_session.expire_all()
    exports = _audit_rows(db_session, action="EXPORT", company_id=COMPANY_A)
    assert len(exports) >= 1, "expected an EXPORT audit row"


def test_soft_delete_drops_row_from_list(client: TestClient, db_session: Session):
    """Soft-delete sets is_deleted and the row drops out of subsequent lists
    (no physical delete)."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    visitor = _make_visitor(db_session, company_id=COMPANY_A, visitor_name="To Be Deleted")

    # Present before delete.
    before = client.get("/api/v1/visitor-logs/", headers=_headers_for(admin))
    assert visitor.id in {r["id"] for r in before.json()["items"]}

    resp = client.delete(f"/api/v1/visitor-logs/{visitor.id}", headers=_headers_for(admin))
    assert resp.status_code == status.HTTP_204_NO_CONTENT, resp.text

    # Row still exists physically but is soft-deleted.
    db_session.expire_all()
    row = db_session.query(VisitorLog).filter(VisitorLog.id == visitor.id).first()
    assert row is not None
    assert row.is_deleted is True

    # Dropped from the list.
    after = client.get("/api/v1/visitor-logs/", headers=_headers_for(admin))
    assert visitor.id not in {r["id"] for r in after.json()["items"]}

    # A DELETE audit row was written.
    deletes = _audit_rows(db_session, action="DELETE", company_id=COMPANY_A)
    assert any(r.resource_id == visitor.id for r in deletes), "expected a DELETE audit row"


def test_revoke_is_idempotent(client: TestClient, db_session: Session):
    """Revoking an already-revoked station is a no-op success (idempotent)."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    station = _make_station(db_session, company_id=COMPANY_A)

    first = client.post(f"/api/v1/visitor-logs/stations/{station.id}/revoke", headers=_headers_for(admin))
    assert first.status_code == status.HTTP_200_OK, first.text
    assert first.json()["revoked"] is True

    second = client.post(f"/api/v1/visitor-logs/stations/{station.id}/revoke", headers=_headers_for(admin))
    assert second.status_code == status.HTTP_200_OK, second.text
    assert second.json()["revoked"] is True


def test_reset_pin_rehashes_old_pin_dies_new_pin_works(client: TestClient, db_session: Session):
    """reset-pin re-hashes: the old PIN stops working, the new PIN logs in."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    station = _make_station(db_session, company_id=COMPANY_A, pin="1111")

    # Old PIN works before reset.
    pre = client.post("/api/v1/visitor-logs/station-login", json={"station_id": station.id, "pin": "1111"})
    assert pre.status_code == status.HTTP_200_OK, pre.text

    reset = client.post(
        f"/api/v1/visitor-logs/stations/{station.id}/reset-pin",
        headers=_headers_for(admin),
        json={"pin": "2222"},
    )
    assert reset.status_code == status.HTTP_200_OK, reset.text

    # Old PIN now fails.
    old = client.post("/api/v1/visitor-logs/station-login", json={"station_id": station.id, "pin": "1111"})
    assert old.status_code == status.HTTP_401_UNAUTHORIZED, old.text

    # New PIN works.
    new = client.post("/api/v1/visitor-logs/station-login", json={"station_id": station.id, "pin": "2222"})
    assert new.status_code == status.HTTP_200_OK, new.text


def test_create_station_does_not_echo_pin(client: TestClient, db_session: Session):
    """Creating a station never echoes the PIN or pin_hash in the response."""
    admin = _make_user(db_session, company_id=COMPANY_A, role=UserRole.ADMIN)
    resp = client.post(
        "/api/v1/visitor-logs/stations",
        headers=_headers_for(admin),
        json={"label": "New Lobby", "pin": "4321"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert "pin" not in body
    assert "pin_hash" not in body
    assert body["label"] == "New Lobby"

    # Stamped with the caller's company and a real hash stored.
    row = db_session.query(SigninStation).filter(SigninStation.id == body["id"]).first()
    assert row.company_id == COMPANY_A
    assert row.pin_hash and row.pin_hash != "4321"
