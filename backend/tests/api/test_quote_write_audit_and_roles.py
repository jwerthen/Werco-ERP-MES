"""Quote write verbs: role gate (``QUOTE_WRITE_ROLES``) and tamper-evident audit rows.

``app/api/endpoints/quotes.py`` used to import no ``AuditService`` at all, so accepting,
rejecting, expiring or re-terming a customer quote, marking it sent, and minting production
work orders from it left nothing on the audit chain (invariant 2) -- and ``PUT`` / ``send``
depended on ``get_current_user`` only, so a Viewer (who can open the Quotes page through
``purchasing:view``) could do all of it.

Pinned here:

* ``PUT``, ``send`` and ``convert`` refuse Viewer / Operator with 403 and change nothing.
* ``send`` writes one STATUS_CHANGE row; an idempotent re-send writes none.
* A ``PUT`` status move writes one STATUS_CHANGE row; a content edit writes one UPDATE row
  (``status`` excluded from its diff) plus a STATUS_CHANGE when it resets PENDING -> DRAFT.
* A refused ``PUT`` writes no row and leaves the quote untouched.
* ``convert`` writes one work-order CREATE row per work order it mints and one row on the
  quote: a STATUS_CHANGE when the status moves, an UPDATE (line -> WO links) when it doesn't.

Audit rows are read COMMITTED-only (roll back, then query): the ``client`` fixture shares
one session with the endpoint, so a merely-flushed row would otherwise look persisted.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.quote import Quote, QuoteLine, QuoteStatus
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

TEST_PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens are minted directly; never used for login
_seq = {"n": 0}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def _user(db: Session, role: UserRole) -> User:
    n = _next()
    user = User(
        email=f"quote-audit-{n}@werco.test",
        employee_id=f"QAUD-{n:05d}",
        first_name="Quote",
        last_name=role.value,
        hashed_password=TEST_PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user: User) -> dict:
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(db: Session) -> Part:
    n = _next()
    part = Part(
        part_number=f"QA-PART-{n:05d}",
        name=f"Quoted part {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _quote(db: Session, parts: list, *, quote_status: QuoteStatus = QuoteStatus.DRAFT) -> Quote:
    """Insert a quote DIRECTLY (no endpoint) -- produces no audit rows of its own."""
    n = _next()
    quote = Quote(
        company_id=1,
        quote_number=f"QA-{n:05d}",
        customer_name="Audit fixture customer",
        payment_terms="Net 30",
        status=quote_status,
        subtotal=10.0 * len(parts),
        total=10.0 * len(parts),
    )
    quote.lines = [
        QuoteLine(
            company_id=1,
            line_number=index,
            part_id=part.id,
            description=f"Line {index}",
            quantity=2 + index,
            unit_price=10,
            line_total=10 * (2 + index),
        )
        for index, part in enumerate(parts, 1)
    ]
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


def _committed_rows(db: Session, *, resource_type: str, resource_id: int = None, action: str = None):
    db.rollback()
    db.expire_all()
    query = db.query(AuditLog).filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.sequence_number).all()


def _reload(db: Session, quote_id: int) -> Quote:
    db.rollback()
    db.expire_all()
    return db.get(Quote, quote_id)


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.QUALITY, UserRole.SHIPPING])
def test_read_only_roles_cannot_edit_send_or_convert(client: TestClient, db_session: Session, role: UserRole):
    part = _part(db_session)
    draft = _quote(db_session, [part])
    sent = _quote(db_session, [part], quote_status=QuoteStatus.SENT)
    headers = _headers(_user(db_session, role))

    edited = client.put(f"/api/v1/quotes/{draft.id}", headers=headers, json={"notes": "Viewer edit"})
    accepted = client.put(f"/api/v1/quotes/{sent.id}", headers=headers, json={"status": "accepted"})
    marked = client.post(f"/api/v1/quotes/{draft.id}/send", headers=headers)
    converted = client.post(f"/api/v1/quotes/{sent.id}/convert", headers=headers, json={})

    for response in (edited, accepted, marked, converted):
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text

    assert _reload(db_session, draft.id).status == QuoteStatus.DRAFT
    assert _reload(db_session, draft.id).notes is None
    assert _reload(db_session, sent.id).status == QuoteStatus.SENT
    assert db_session.query(WorkOrder).count() == 0
    assert _committed_rows(db_session, resource_type="quote") == []
    assert _committed_rows(db_session, resource_type="work_order") == []


def test_reads_stay_open_to_a_viewer(client: TestClient, db_session: Session):
    """Read-broad / write-restricted: the gate is on the verbs, not the page."""
    quote = _quote(db_session, [_part(db_session)])
    headers = _headers(_user(db_session, UserRole.VIEWER))
    assert client.get(f"/api/v1/quotes/{quote.id}", headers=headers).status_code == status.HTTP_200_OK
    assert client.get("/api/v1/quotes/", headers=headers).status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_supervisor_send_writes_one_status_change_and_resend_writes_none(client: TestClient, db_session: Session):
    supervisor = _user(db_session, UserRole.SUPERVISOR)
    quote = _quote(db_session, [_part(db_session)])

    first = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(supervisor))
    assert first.status_code == status.HTTP_200_OK, first.text
    again = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(supervisor))
    assert again.status_code == status.HTTP_200_OK, again.text

    rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "STATUS_CHANGE"
    assert row.old_values == {"status": "draft"} and row.new_values == {"status": "sent"}
    assert row.user_id == supervisor.id and row.company_id == 1
    assert row.resource_identifier == quote.quote_number
    assert row.extra_data["source"] == "send"


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["accepted", "rejected", "expired"])
def test_put_status_transition_writes_one_status_change(client: TestClient, db_session: Session, target: str):
    admin = _user(db_session, UserRole.ADMIN)
    quote = _quote(db_session, [_part(db_session)], quote_status=QuoteStatus.SENT)

    response = client.put(f"/api/v1/quotes/{quote.id}", headers=_headers(admin), json={"status": target})
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert [row.action for row in rows] == ["STATUS_CHANGE"]
    assert rows[0].old_values == {"status": "sent"} and rows[0].new_values == {"status": target}
    assert rows[0].extra_data["source"] == "status_update"
    assert rows[0].user_id == admin.id


def test_content_edit_writes_update_without_status_and_reset_writes_status_change(
    client: TestClient, db_session: Session
):
    admin = _user(db_session, UserRole.ADMIN)
    quote = _quote(db_session, [_part(db_session)], quote_status=QuoteStatus.PENDING)

    response = client.put(
        f"/api/v1/quotes/{quote.id}",
        headers=_headers(admin),
        json={"payment_terms": "Net 45", "customer_po": "PO-77"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert [row.action for row in rows] == ["UPDATE", "STATUS_CHANGE"]
    update, status_change = rows
    changes = update.extra_data["changes"]
    assert changes["payment_terms"] == {"old": "Net 30", "new": "Net 45"}
    assert changes["customer_po"] == {"old": None, "new": "PO-77"}
    assert "status" not in changes and "status" not in update.new_values
    assert update.extra_data["edited_fields"] == ["customer_po", "payment_terms"]
    # The edit reset an issued-for-review quote to DRAFT -- recorded as its own transition.
    assert status_change.old_values == {"status": "pending"} and status_change.new_values == {"status": "draft"}
    assert status_change.extra_data["source"] == "edit"


def test_draft_content_edit_writes_only_the_update_row(client: TestClient, db_session: Session):
    admin = _user(db_session, UserRole.ADMIN)
    quote = _quote(db_session, [_part(db_session)])

    response = client.put(f"/api/v1/quotes/{quote.id}", headers=_headers(admin), json={"notes": "Corrected terms"})
    assert response.status_code == status.HTTP_200_OK, response.text

    rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert [row.action for row in rows] == ["UPDATE"]
    assert rows[0].extra_data["changes"]["notes"] == {"old": None, "new": "Corrected terms"}


@pytest.mark.parametrize(
    "start, payload, expected",
    [
        (QuoteStatus.SENT, {"notes": "Change issued content"}, status.HTTP_409_CONFLICT),
        (QuoteStatus.SENT, {"status": "converted"}, 422),
        (QuoteStatus.CONVERTED, {"status": "accepted"}, status.HTTP_409_CONFLICT),
    ],
)
def test_refused_put_writes_nothing(client: TestClient, db_session: Session, start, payload, expected):
    admin = _user(db_session, UserRole.ADMIN)
    quote = _quote(db_session, [_part(db_session)], quote_status=start)

    response = client.put(f"/api/v1/quotes/{quote.id}", headers=_headers(admin), json=payload)
    assert response.status_code == expected, response.text

    reloaded = _reload(db_session, quote.id)
    assert reloaded.status == start and reloaded.notes is None
    assert _committed_rows(db_session, resource_type="quote", resource_id=quote.id) == []


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def test_convert_writes_one_create_per_work_order_and_a_quote_status_change(client: TestClient, db_session: Session):
    supervisor = _user(db_session, UserRole.SUPERVISOR)
    first, second = _part(db_session), _part(db_session)
    quote = _quote(db_session, [first, second], quote_status=QuoteStatus.SENT)

    response = client.post(f"/api/v1/quotes/{quote.id}/convert", headers=_headers(supervisor), json={})
    assert response.status_code == status.HTTP_200_OK, response.text
    created = response.json()["work_orders"]
    assert len(created) == 2

    wo_rows = _committed_rows(db_session, resource_type="work_order", action="CREATE")
    assert sorted(row.resource_id for row in wo_rows) == sorted(item["work_order_id"] for item in created)
    by_wo = {row.resource_id: row for row in wo_rows}
    for item in created:
        row = by_wo[item["work_order_id"]]
        assert row.resource_identifier == item["work_order_number"]
        assert row.user_id == supervisor.id and row.company_id == 1
        assert row.extra_data["source"] == "quote_conversion"
        assert row.extra_data["quote_id"] == quote.id
        assert row.extra_data["quote_number"] == quote.quote_number
        assert row.extra_data["quote_line_id"] == item["line_id"]
        assert row.new_values["work_order_number"] == item["work_order_number"]

    quote_rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert [row.action for row in quote_rows] == ["STATUS_CHANGE"]
    assert quote_rows[0].old_values == {"status": "sent"}
    assert quote_rows[0].new_values == {"status": "converted"}
    assert [wo["work_order_id"] for wo in quote_rows[0].extra_data["work_orders"]] == [
        item["work_order_id"] for item in created
    ]
    assert quote_rows[0].extra_data["remaining_line_ids"] == []


def test_partial_reconversion_without_status_move_writes_link_update(client: TestClient, db_session: Session):
    admin = _user(db_session, UserRole.ADMIN)
    first, second, third = _part(db_session), _part(db_session), _part(db_session)
    quote = _quote(db_session, [first, second, third], quote_status=QuoteStatus.SENT)
    line_ids = [line.id for line in sorted(quote.lines, key=lambda line: line.line_number)]

    one = client.post(f"/api/v1/quotes/{quote.id}/convert", headers=_headers(admin), json={"line_ids": [line_ids[0]]})
    assert one.status_code == status.HTTP_200_OK, one.text
    two = client.post(f"/api/v1/quotes/{quote.id}/convert", headers=_headers(admin), json={"line_ids": [line_ids[1]]})
    assert two.status_code == status.HTTP_200_OK, two.text
    second_wo = two.json()["work_orders"][0]["work_order_id"]

    quote_rows = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert [row.action for row in quote_rows] == ["STATUS_CHANGE", "UPDATE"]
    assert quote_rows[0].new_values == {"status": "accepted"}
    link_update = quote_rows[1]
    assert link_update.extra_data["changes"]["line_work_orders"]["new"][str(line_ids[1])] == second_wo
    assert link_update.extra_data["changes"]["line_work_orders"]["old"][str(line_ids[1])] is None
    assert link_update.extra_data["remaining_line_ids"] == [line_ids[2]]
    assert len(_committed_rows(db_session, resource_type="work_order", action="CREATE")) == 2


def test_refused_convert_writes_nothing(client: TestClient, db_session: Session):
    admin = _user(db_session, UserRole.ADMIN)
    quote = _quote(db_session, [_part(db_session)])  # DRAFT: must be sent or accepted first

    response = client.post(f"/api/v1/quotes/{quote.id}/convert", headers=_headers(admin), json={})
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert _reload(db_session, quote.id).status == QuoteStatus.DRAFT
    assert db_session.query(WorkOrder).count() == 0
    assert _committed_rows(db_session, resource_type="quote") == []
    assert _committed_rows(db_session, resource_type="work_order") == []


# ---------------------------------------------------------------------------
# Coverage added in review: the full role set, refused/no-op sends and PUTs, tenancy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])
def test_every_quote_write_role_can_send_and_is_named_on_the_row(client: TestClient, db_session: Session, role):
    """Positive half of the role gate. MANAGER appears in no other test here, so dropping it
    from ``QUOTE_WRITE_ROLES`` would otherwise go unnoticed."""
    actor = _user(db_session, role)
    quote = _quote(db_session, [_part(db_session)], quote_status=QuoteStatus.PENDING)

    response = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(actor))
    assert response.status_code == status.HTTP_200_OK, response.text

    (row,) = _committed_rows(db_session, resource_type="quote", resource_id=quote.id)
    assert row.action == "STATUS_CHANGE" and row.user_id == actor.id
    assert row.old_values == {"status": "pending"} and row.new_values == {"status": "sent"}
    assert _reload(db_session, quote.id).status == QuoteStatus.SENT


@pytest.mark.parametrize(
    "start", [QuoteStatus.ACCEPTED, QuoteStatus.REJECTED, QuoteStatus.EXPIRED, QuoteStatus.CONVERTED]
)
def test_refused_send_writes_nothing(client: TestClient, db_session: Session, start):
    quote = _quote(db_session, [_part(db_session)], quote_status=start)

    response = client.post(f"/api/v1/quotes/{quote.id}/send", headers=_headers(_user(db_session, UserRole.ADMIN)))
    assert response.status_code == status.HTTP_409_CONFLICT, response.text

    assert _reload(db_session, quote.id).status == start
    assert _committed_rows(db_session, resource_type="quote", resource_id=quote.id) == []


def test_put_echoing_the_current_status_writes_no_row(client: TestClient, db_session: Session):
    """A client echoing the record back is not a transition: no STATUS_CHANGE, no UPDATE."""
    quote = _quote(db_session, [_part(db_session)], quote_status=QuoteStatus.SENT)

    response = client.put(
        f"/api/v1/quotes/{quote.id}", headers=_headers(_user(db_session, UserRole.ADMIN)), json={"status": "sent"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    assert _reload(db_session, quote.id).status == QuoteStatus.SENT
    assert _committed_rows(db_session, resource_type="quote", resource_id=quote.id) == []


def test_other_tenant_cannot_edit_send_or_convert_and_nothing_is_written(client: TestClient, db_session: Session):
    """Tenant isolation on every write verb: a company-2 Admin addressing company 1's quotes
    by id gets 404, and neither tenant's audit chain nor work-order table moves."""
    from app.models.company import Company

    db_session.add(Company(id=2, name="Quote audit other tenant", slug="quote-audit-other", is_active=True))
    db_session.commit()
    n = _next()
    outsider = User(
        email=f"quote-audit-outsider-{n}@co2.test",
        employee_id=f"QAUD-X{n:05d}",
        first_name="Other",
        last_name="Tenant",
        hashed_password=TEST_PASSWORD_HASH,
        role=UserRole.ADMIN,
        is_active=True,
        company_id=2,
    )
    db_session.add(outsider)
    db_session.commit()
    headers = _headers(outsider)

    part = _part(db_session)
    draft = _quote(db_session, [part])
    sent = _quote(db_session, [part], quote_status=QuoteStatus.SENT)

    responses = [
        client.put(f"/api/v1/quotes/{draft.id}", headers=headers, json={"notes": "cross-tenant edit"}),
        client.put(f"/api/v1/quotes/{sent.id}", headers=headers, json={"status": "accepted"}),
        client.post(f"/api/v1/quotes/{draft.id}/send", headers=headers),
        client.post(f"/api/v1/quotes/{sent.id}/convert", headers=headers, json={}),
    ]
    for response in responses:
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

    reloaded_draft = _reload(db_session, draft.id)
    assert reloaded_draft.status == QuoteStatus.DRAFT and reloaded_draft.notes is None
    assert _reload(db_session, sent.id).status == QuoteStatus.SENT
    assert db_session.query(WorkOrder).count() == 0
    db_session.rollback()
    assert db_session.query(AuditLog).filter(AuditLog.resource_type.in_(["quote", "work_order"])).count() == 0
