"""Gated customer names on the TV wallboard (2026-07-23).

The wallboard job tiles may carry the work order's ``customer_name``, but ONLY
for a principal explicitly authorized to see it:

  * a display token provisioned with ``show_customer_names=True`` (an
    executive-office TV), or
  * a signed-in privileged office role (PLATFORM_ADMIN / ADMIN / MANAGER).

Every public shop-floor TV (an un-flagged display token, or a pre-migration NULL
row) and every non-privileged signed-in role (operator / quality / shipping /
supervisor / viewer) gets the field back as ``None`` — the payload's
long-standing "no customer names on a public screen" (CUI/AS9100D) posture.

Layers exercised here:
  * ``build_wallboard_payload(..., include_customer=...)`` — the service gate.
  * ``GET /api/v1/shop-floor/wallboard`` — the display-token + signed-in-role
    gate wired through ``get_display_or_user`` / ``WallboardPrincipal``.
  * ``issue_display_token(..., show_customer_names=...)`` — the flag persists on
    the row AND lands in the tamper-evident audit ``new_values``.

The redaction (unauthorized → null) contract for a full sensitive payload is
proven in test_wallboard_display_token.py::test_payload_privacy_no_identities_costs_or_customers;
this file proves the authorized POSITIVE paths and the persistence/audit.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import _WALLBOARD_CUSTOMER_ROLES
from app.models.audit_log import AuditLog
from app.models.display_token import DisplayToken
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.wallboard_service import build_wallboard_payload
from tests.lean_phase1_helpers import (
    headers_for,
    make_op,
    make_part,
    make_user,
    make_wo,
    make_work_center,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

DISPLAY_TOKEN_URL = "/api/v1/auth/display-token"
WALLBOARD_URL = "/api/v1/shop-floor/wallboard"

CUSTOMER = "Globex Aerospace Inc"


def _issue_token(client: TestClient, headers: dict, label: str = "Exec office TV", **body) -> dict:
    response = client.post(DISPLAY_TOKEN_URL, json={"label": label, **body}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _display_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _seed_customer_wo(db: Session, *, customer_name: str = CUSTOMER, company_id: int = 1):
    """A company-scoped RELEASED WO on the job wall, with an open op + a set
    customer_name. Returns the WorkOrder."""
    part = make_part(db, company_id=company_id)
    wc = make_work_center(db, company_id=company_id)
    wo = make_wo(
        db,
        part,
        company_id=company_id,
        status_=WorkOrderStatus.IN_PROGRESS,
        customer_name=customer_name,
        quantity_ordered=10,
    )
    make_op(db, wo, wc, company_id=company_id, status_=OperationStatus.READY)
    db.commit()
    return wo


def _tile_customer(payload: dict, wo_number: str):
    tile = next(job for job in payload["jobs"] if job["wo_number"] == wo_number)
    return tile["customer_name"]


# ---------------------------------------------------------------------------
# Service gate: build_wallboard_payload(include_customer=...)
# ---------------------------------------------------------------------------


def test_build_payload_includes_customer_name_when_flagged(db_session: Session):
    """include_customer=True populates the job tile's customer_name from the WO."""
    wo = _seed_customer_wo(db_session)

    payload = build_wallboard_payload(db_session, 1, include_customer=True)
    tile = next(job for job in payload.jobs if job.wo_number == wo.work_order_number)
    assert tile.customer_name == CUSTOMER


def test_build_payload_omits_customer_name_by_default(db_session: Session):
    """The default (include_customer=False) keeps customer_name None — the
    public-safe posture; the WO still tiles on the wall, just redacted."""
    wo = _seed_customer_wo(db_session)

    payload = build_wallboard_payload(db_session, 1)  # default include_customer=False
    tile = next(job for job in payload.jobs if job.wo_number == wo.work_order_number)
    assert tile.customer_name is None
    # It really is on the wall (so the None above is a redaction, not an absence).
    assert tile.part_number is not None


def test_build_payload_empty_customer_collapses_to_none_even_when_flagged(db_session: Session):
    """An empty-string customer_name collapses to None even for an authorized
    principal (the service's ``wo.customer_name or None``) — a blank free-text
    field renders an empty cell, never the empty string."""
    wo = _seed_customer_wo(db_session, customer_name="")

    payload = build_wallboard_payload(db_session, 1, include_customer=True)
    tile = next(job for job in payload.jobs if job.wo_number == wo.work_order_number)
    assert tile.customer_name is None


# ---------------------------------------------------------------------------
# Endpoint gate: display tokens
# ---------------------------------------------------------------------------


def test_display_token_with_flag_sees_customer_names(client: TestClient, admin_headers: dict, db_session: Session):
    wo = _seed_customer_wo(db_session)
    token = _issue_token(client, admin_headers, show_customer_names=True)["token"]

    payload = client.get(WALLBOARD_URL, headers=_display_headers(token)).json()
    assert _tile_customer(payload, wo.work_order_number) == CUSTOMER


def test_display_token_without_flag_redacts_customer_names(
    client: TestClient, admin_headers: dict, db_session: Session
):
    wo = _seed_customer_wo(db_session)
    # Flag omitted entirely — defaults to False (public-safe).
    token = _issue_token(client, admin_headers)["token"]

    payload = client.get(WALLBOARD_URL, headers=_display_headers(token)).json()
    assert _tile_customer(payload, wo.work_order_number) is None


def test_display_token_explicit_false_redacts_customer_names(
    client: TestClient, admin_headers: dict, db_session: Session
):
    wo = _seed_customer_wo(db_session)
    token = _issue_token(client, admin_headers, show_customer_names=False)["token"]

    payload = client.get(WALLBOARD_URL, headers=_display_headers(token)).json()
    assert _tile_customer(payload, wo.work_order_number) is None


# NOTE: the deps layer coerces a NULL ``show_customer_names`` to False via
# ``bool(record.show_customer_names)`` for defense against a pre-migration row.
# That NULL state can't be reproduced here — the column is NOT NULL with a
# server_default of false, so neither the ORM nor raw SQL can persist a NULL on
# the SQLite test DB (IntegrityError). The omitted-flag / explicit-False tests
# above already prove the redacted display-token path.


# ---------------------------------------------------------------------------
# Endpoint gate: signed-in roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
def test_privileged_signed_in_roles_see_customer_names(client: TestClient, db_session: Session, role: UserRole):
    wo = _seed_customer_wo(db_session)
    viewer = make_user(db_session, role=role)

    payload = client.get(WALLBOARD_URL, headers=headers_for(viewer)).json()
    assert _tile_customer(payload, wo.work_order_number) == CUSTOMER


@pytest.mark.parametrize(
    "role",
    [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY, UserRole.SHIPPING, UserRole.SUPERVISOR],
)
def test_nonprivileged_signed_in_roles_are_redacted(client: TestClient, db_session: Session, role: UserRole):
    wo = _seed_customer_wo(db_session)
    viewer = make_user(db_session, role=role)

    payload = client.get(WALLBOARD_URL, headers=headers_for(viewer)).json()
    assert _tile_customer(payload, wo.work_order_number) is None


def test_wallboard_customer_roles_constant_is_the_office_trio():
    """The signed-in allow-list is exactly the privileged office roles that also
    provision displays — a change here is a deliberate policy change."""
    assert set(_WALLBOARD_CUSTOMER_ROLES) == {UserRole.PLATFORM_ADMIN, UserRole.ADMIN, UserRole.MANAGER}


# ---------------------------------------------------------------------------
# issue_display_token: persistence + audit
# ---------------------------------------------------------------------------


def _latest_display_create_audit(db: Session, resource_id: int) -> AuditLog:
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "display_token",
            AuditLog.action == "CREATE",
            AuditLog.resource_id == resource_id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )


def test_issue_display_token_persists_flag_and_audits_it(client: TestClient, admin_headers: dict, db_session: Session):
    data = _issue_token(client, admin_headers, label="Exec TV", show_customer_names=True)
    assert data["show_customer_names"] is True  # surfaced on the create response

    record = db_session.query(DisplayToken).filter(DisplayToken.id == data["id"]).first()
    assert record.show_customer_names is True

    audit = _latest_display_create_audit(db_session, data["id"])
    assert audit is not None
    assert audit.new_values.get("show_customer_names") is True


def test_issue_display_token_defaults_flag_false_and_audits_it(
    client: TestClient, admin_headers: dict, db_session: Session
):
    data = _issue_token(client, admin_headers, label="Public TV")  # flag omitted
    assert data["show_customer_names"] is False

    record = db_session.query(DisplayToken).filter(DisplayToken.id == data["id"]).first()
    assert record.show_customer_names is False

    audit = _latest_display_create_audit(db_session, data["id"])
    assert audit is not None
    assert audit.new_values.get("show_customer_names") is False
