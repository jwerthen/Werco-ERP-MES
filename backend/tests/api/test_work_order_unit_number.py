"""``WorkOrder.unit_number`` (083) — the build identity of a one-unit-per-WO job.

The "Unit #" of a weld assembly used to be typed into ``work_orders.notes``, where it
was unsearchable and — because ``notes`` is unbounded free text — unshowable on an
unattended screen. 083 gives it a bounded column of its own.

This file covers the OFFICE half of that column: the write paths, what they audit, and
the two searches that were widened to match on it. The shop-floor read surfaces (kiosk
queue / held / running-job, wallboard, dispatch board) are in
``test_unit_number_shop_floor_surfaces.py``; the duplicate-service omission — the single
most consequential property of the whole feature — lives beside the other
"``_copy_header`` deliberately does not carry this" assertions in
``test_work_order_duplicate.py::TestUnitNumberIsNotCarried``; the DDL is in
``tests/test_migration_083_wo_unit_number.py``.

What is pinned here and why each would plausibly regress:

* **Round-trip on all three office shapes.** ``WorkOrderBase`` carries the field, so
  ``WorkOrderCreate`` and ``WorkOrderResponse`` inherit it for free — but
  ``WorkOrderSummary`` does NOT inherit the base, and the list endpoint builds it
  kwarg-by-kwarg. An omitted kwarg there ships the schema default (``None``) for every
  work order in the shop with no error anywhere, which is exactly how the list card
  would silently lose the badge.

* **Set / change / CLEAR, each with an audit row naming the field** (invariant 2). The
  clear matters on its own: a unit typed onto the wrong work order is on the kiosk and
  the TV wall, so it has to be REMOVABLE, not merely overwritable. ``exclude_unset`` is
  what makes an explicit ``null`` distinguishable from "this PUT is about something
  else" — and the negative case (a PUT that never mentions the field must not blank it)
  is asserted too, because the update handler is a blind ``setattr`` loop.

* **Audit rows are read back COMMITTED**, using the ``db.rollback()``-first guard proven
  in ``test_work_orders_audit_persistence.py``: the ``client`` fixture shares one
  never-closed session with the endpoint, so a plain query would see a flushed-only row
  and pass against an un-committed audit trail.

* **Both searches match on it, and both stay tenant-scoped** (invariant 1). A unit
  number is a customer's numbering scheme, not ours — two tenants building for the same
  customer will legitimately hold the same string, so "search finds it" and "search
  finds only mine" are one requirement, not two.
"""

from datetime import date, timedelta
from typing import Optional

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.llm_client as llm_client
from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.user import UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from tests.lean_phase1_helpers import COMPANY_A, COMPANY_B, headers_for, make_part, make_user, make_wo

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS_URL = "/api/v1/work-orders/"
SEARCH_URL = "/api/v1/search/"
NL_URL = "/api/v1/search/nl"

# A real Miratech-shaped unit number: digits only, so it cannot accidentally match a
# work-order number, a part number or a customer name and make a search test vacuous.
UNIT = "2410048"
OTHER_UNIT = "2410099"


def _wo_url(work_order_id: int) -> str:
    return f"/api/v1/work-orders/{work_order_id}"


def _make_wo(
    db: Session,
    part: Part,
    *,
    unit_number: Optional[str] = None,
    company_id: int = COMPANY_A,
    status_: WorkOrderStatus = WorkOrderStatus.RELEASED,
) -> WorkOrder:
    """A work order carrying (or deliberately not carrying) a unit number.

    Set after construction rather than by widening ``lean_phase1_helpers.make_wo``:
    that helper is shared by a dozen files and none of the others has an opinion about
    this column.
    """
    wo = make_wo(db, part, company_id=company_id, status_=status_)
    wo.unit_number = unit_number
    db.commit()
    db.refresh(wo)
    return wo


def _committed_audit_rows(db: Session, *, resource_id: int, action: str = "UPDATE"):
    """Work-order audit rows that were COMMITTED, not merely flushed.

    ``AuditService.log()`` only flushes — the handler owns the ``commit()`` — and the
    ``client`` fixture hands the endpoint the SAME never-closed session the test reads
    through, so a flushed-only row is visible to a naive query and the assertion passes
    against a broken audit trail. Rolling back first discards exactly the rows that were
    never committed. Lifted from ``test_work_orders_audit_persistence.py``.
    """
    db.rollback()
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == resource_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.sequence_number.desc())
        .all()
    )


def _unit_number_change(row: AuditLog) -> dict:
    """The ``unit_number`` entry of an UPDATE row's change map, or ``{}``."""
    return (row.extra_data or {}).get("changes", {}).get("unit_number", {})


# ---------------------------------------------------------------------------
# 1. Round-trip: create -> detail -> list summary
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_create_persists_it_and_the_detail_read_returns_it(self, client: TestClient, db_session: Session):
        """``WorkOrderCreate``/``WorkOrderResponse`` inherit it from ``WorkOrderBase``,
        and ``create_work_order`` ``model_dump``s the whole schema onto the row."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)

        resp = client.post(
            f"{WORK_ORDERS_URL}?auto_routing=false",
            headers=headers_for(manager),
            json={
                "part_id": part.id,
                "quantity_ordered": 1,
                "customer_name": "Miratech",
                "priority": 5,
                "unit_number": UNIT,
            },
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        assert resp.json()["unit_number"] == UNIT
        new_id = resp.json()["id"]

        # It reached the COLUMN, not just the response model.
        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == new_id).one().unit_number == UNIT

        detail = client.get(_wo_url(new_id), headers=headers_for(manager))
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["unit_number"] == UNIT

    def test_list_summary_carries_it(self, client: TestClient, db_session: Session):
        """``WorkOrderSummary`` does NOT inherit ``WorkOrderBase`` and the list handler
        builds it kwarg-by-kwarg, so a dropped kwarg is a silent ``None`` on every list
        card in the shop rather than an error anywhere."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part, unit_number=UNIT)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = {row["work_order_number"]: row for row in resp.json()}
        assert wo.work_order_number in rows, "fixture work order missing from the active list"
        assert rows[wo.work_order_number]["unit_number"] == UNIT

    def test_a_work_order_without_one_reads_null_on_every_office_shape(self, client: TestClient, db_session: Session):
        """Most work orders never carry a unit, and those must look exactly as they did
        before 083: the key is PRESENT and ``None`` — never absent, never ``""`` — so
        the client renders off one stable shape and ``UnitBadge`` hides it."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)

        created = client.post(
            f"{WORK_ORDERS_URL}?auto_routing=false",
            headers=headers_for(manager),
            json={"part_id": part.id, "quantity_ordered": 4, "priority": 5},
        )
        assert created.status_code == status.HTTP_201_CREATED, created.text
        body = created.json()
        assert "unit_number" in body and body["unit_number"] is None

        detail = client.get(_wo_url(body["id"]), headers=headers_for(manager)).json()
        assert "unit_number" in detail and detail["unit_number"] is None

        listed = client.get(WORK_ORDERS_URL, headers=headers_for(manager)).json()
        row = next(r for r in listed if r["id"] == body["id"])
        assert "unit_number" in row and row["unit_number"] is None


# ---------------------------------------------------------------------------
# 2. PUT /work-orders/{id}: set, change, clear — each audited (invariant 2)
# ---------------------------------------------------------------------------


class TestUpdateAndAudit:
    def test_put_sets_it_and_audits_the_field_by_name(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(manager),
            json={"version": wo.version, "unit_number": UNIT},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["unit_number"] == UNIT

        rows = _committed_audit_rows(db_session, resource_id=wo_id)
        assert len(rows) == 1, "expected exactly one COMMITTED UPDATE audit row"
        assert _unit_number_change(rows[0]) == {"old": None, "new": UNIT}
        assert rows[0].company_id == COMPANY_A
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).one().unit_number == UNIT

    def test_put_changes_an_existing_unit_number_and_audits_both_sides(self, client: TestClient, db_session: Session):
        """A rework or a mis-keyed unit gets corrected in place; the audit row has to
        carry what it WAS, or the record cannot say which unit the earlier labour was
        booked against."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part, unit_number=UNIT)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(manager),
            json={"version": wo.version, "unit_number": OTHER_UNIT},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["unit_number"] == OTHER_UNIT

        rows = _committed_audit_rows(db_session, resource_id=wo_id)
        assert len(rows) == 1
        assert _unit_number_change(rows[0]) == {"old": UNIT, "new": OTHER_UNIT}

    def test_put_clears_it_to_null_and_audits_the_removal(self, client: TestClient, db_session: Session):
        """An explicit ``null`` CLEARS the column.

        Not a nicety: the badge this column drives is on the kiosk hero, the crew
        station and the public TV wall, so a unit typed onto the wrong work order has to
        be removable rather than only overwritable. ``exclude_unset`` is what makes an
        explicit ``null`` reach the ``setattr`` loop at all — see the sibling test for
        the other half of that contract.
        """
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part, unit_number=UNIT)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(manager),
            json={"version": wo.version, "unit_number": None},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["unit_number"] is None

        rows = _committed_audit_rows(db_session, resource_id=wo_id)
        assert len(rows) == 1
        assert _unit_number_change(rows[0]) == {"old": UNIT, "new": None}
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).one().unit_number is None

    def test_a_put_that_never_mentions_the_field_leaves_it_alone(self, client: TestClient, db_session: Session):
        """The other half of the ``exclude_unset`` contract, and the one a blind
        ``setattr`` loop breaks first: editing the due date from any other screen must
        not silently blank the unit on the wall."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part, unit_number=UNIT)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(manager),
            json={"version": wo.version, "due_date": (date.today() + timedelta(days=21)).isoformat()},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["unit_number"] == UNIT

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).one().unit_number == UNIT

    def test_over_length_unit_number_is_refused_at_the_schema(self, client: TestClient, db_session: Session):
        """``max_length=50`` mirrors ``String(50)``. Refused as a 422 before the row is
        touched, rather than as a database error after it."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wo = _make_wo(db_session, part, unit_number=UNIT)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(manager),
            json={"version": wo.version, "unit_number": "X" * 51},
        )
        assert resp.status_code == 422, resp.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).one().unit_number == UNIT

    def test_an_operator_cannot_set_a_unit_number(self, client: TestClient, db_session: Session):
        """No new gate: ``unit_number`` rides the existing ``PUT /work-orders/{id}``
        role tier (ADMIN/MANAGER/SUPERVISOR). Pinned so the badge's provenance is a
        planner decision and not something a shared kiosk badge can rewrite."""
        operator = make_user(db_session, role=UserRole.OPERATOR)
        part = make_part(db_session)
        wo = _make_wo(db_session, part)
        wo_id = wo.id

        resp = client.put(
            _wo_url(wo_id),
            headers=headers_for(operator),
            json={"version": wo.version, "unit_number": UNIT},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN, resp.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == wo_id).one().unit_number is None


# ---------------------------------------------------------------------------
# 3. Search: both paths match, both stay tenant-scoped (invariant 1)
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_llm(monkeypatch):
    """NL search must fall back to the rule parser / literal fallback — never call out."""

    def raise_not_configured(ctx, **kwargs):
        raise llm_client.LLMNotConfiguredError("api_key")

    monkeypatch.setattr(llm_client, "run_llm_task", raise_not_configured)


class TestSearchMatchesOnUnitNumber:
    def test_work_order_list_search_matches_it(self, client: TestClient, db_session: Session):
        """The reason the column is indexed at all. Before 083 the number lived in
        ``notes``, which no search path reads."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wanted = _make_wo(db_session, part, unit_number=UNIT)
        other = _make_wo(db_session, part, unit_number=OTHER_UNIT)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(manager), params={"search": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        numbers = [row["work_order_number"] for row in resp.json()]
        assert numbers == [wanted.work_order_number]
        assert other.work_order_number not in numbers

    def test_global_search_matches_it(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wanted = _make_wo(db_session, part, unit_number=UNIT)

        resp = client.get(SEARCH_URL, headers=headers_for(manager), params={"q": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        titles = [r["title"] for r in resp.json()["results"] if r["type"] == "work_order"]
        assert titles == [wanted.work_order_number]

    def test_nl_literal_fallback_matches_it(self, client: TestClient, db_session: Session, _no_llm):
        """``search.py``'s own copy of the clause. It is a SECOND literal predicate,
        maintained by hand beside ``search_service``'s — so it needs its own assertion
        or half the global search silently keeps the pre-083 behavior."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        wanted = _make_wo(db_session, part, unit_number=UNIT)

        resp = client.post(NL_URL, headers=headers_for(manager), json={"query": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["used_fallback"] is True
        assert [r["title"] for r in body["results"]] == [wanted.work_order_number]


class TestSearchStaysTenantScoped:
    """Invariant 1, and NOT a theoretical case for this column.

    A unit number belongs to the CUSTOMER's numbering scheme, not to us. Two tenants
    building for the same customer hold the same string legitimately, so an unscoped
    match here would hand one shop the other's work-order numbers off a value their own
    planner typed — the most plausible way this feature leaks.
    """

    @staticmethod
    def _both_companies_use_the_same_unit(db: Session):
        mine = _make_wo(db, make_part(db, company_id=COMPANY_A), unit_number=UNIT, company_id=COMPANY_A)
        theirs = _make_wo(db, make_part(db, company_id=COMPANY_B), unit_number=UNIT, company_id=COMPANY_B)
        return mine, theirs

    def test_work_order_list_search_is_scoped(self, client: TestClient, db_session: Session):
        mine, theirs = self._both_companies_use_the_same_unit(db_session)
        manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)

        resp = client.get(WORK_ORDERS_URL, headers=headers_for(manager_a), params={"search": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        numbers = [row["work_order_number"] for row in resp.json()]
        # Non-vacuity: the match really did fire for company A.
        assert numbers == [mine.work_order_number]
        assert theirs.work_order_number not in resp.text

    def test_global_search_is_scoped(self, client: TestClient, db_session: Session):
        mine, theirs = self._both_companies_use_the_same_unit(db_session)
        manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)

        resp = client.get(SEARCH_URL, headers=headers_for(manager_a), params={"q": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        titles = [r["title"] for r in resp.json()["results"] if r["type"] == "work_order"]
        assert titles == [mine.work_order_number]
        assert theirs.work_order_number not in resp.text

    def test_nl_literal_fallback_is_scoped(self, client: TestClient, db_session: Session, _no_llm):
        mine, theirs = self._both_companies_use_the_same_unit(db_session)
        manager_a = make_user(db_session, role=UserRole.MANAGER, company_id=COMPANY_A)

        resp = client.post(NL_URL, headers=headers_for(manager_a), json={"query": UNIT})
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert [r["title"] for r in resp.json()["results"]] == [mine.work_order_number]
        assert theirs.work_order_number not in resp.text

    def test_a_soft_deleted_work_order_never_matches(self, client: TestClient, db_session: Session):
        """Invariant 3 on the widened clause: the new predicate sits INSIDE the existing
        ``or_``, so the tombstone filter beside it still applies."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        part = make_part(db_session)
        gone = _make_wo(db_session, part, unit_number=UNIT)
        gone.is_deleted = True
        db_session.commit()

        listed = client.get(WORK_ORDERS_URL, headers=headers_for(manager), params={"search": UNIT})
        assert [row["work_order_number"] for row in listed.json()] == []

        searched = client.get(SEARCH_URL, headers=headers_for(manager), params={"q": UNIT})
        assert [r["title"] for r in searched.json()["results"] if r["type"] == "work_order"] == []
        assert gone.work_order_number not in searched.text
