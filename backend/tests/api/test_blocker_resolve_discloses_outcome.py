"""Closing a blocker has to SAY what it did to the operation.

THE DEFECT. A shop owner resolved a blocker on a held laser nest, got a green
"Resolved blocker" toast, and the operation was still ON_HOLD -- a second blocker
still named it, so ``_resume_operation_if_no_open_blockers`` withheld the resume.
``POST /work-order-blockers/{id}/resolve`` returned 200 with a body that could
not express which way it went: four distinct situations all collapsed into a bare
``(None, None)`` the response never saw.

THIS IS A DISCLOSURE FIX, NOT A BEHAVIOUR CHANGE, and these tests are written to
hold that line. ``tests/api/test_blocker_resume_sequencing.py`` owns WHEN a resume
happens and where it lands; nothing here re-litigates it. What is pinned here is
that each situation is NAMED, that the two warnable outcomes are distinguishable
from the two harmless ones, and -- invariant 2 -- that the operation's
``log_status_change`` row is still written if and ONLY if the operation actually
moved. A status-change row for a transition that did not happen is a false entry
in a tamper-evident hash chain.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import (
    BlockerResumeWithheldReason,
    WorkOrderBlocker,
    WorkOrderBlockerStatus,
)
from app.schemas.work_order_blocker import WorkOrderBlockerUpdate
from app.services.audit_service import AuditService
from app.services.work_order_blocker_service import WorkOrderBlockerService


@pytest.fixture
def shop(db_session, test_company):
    part = Part(
        company_id=test_company.id,
        part_number="BLK-OUTCOME-1",
        name="Bracket",
        part_type="manufactured",
        unit_of_measure="EA",
    )
    cell = WorkCenter(company_id=test_company.id, code="BLK-O1", name="Laser", work_center_type="laser")
    db_session.add_all([part, cell])
    db_session.flush()
    return part, cell


def _wo(db, company_id, part, *, number, status=WorkOrderStatus.RELEASED):
    wo = WorkOrder(
        company_id=company_id,
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=status,
        sequential_operations=True,
    )
    db.add(wo)
    db.flush()
    return wo


def _op(db, company_id, wo, cell, *, status=OperationStatus.ON_HOLD, sequence=10):
    op = WorkOrderOperation(
        company_id=company_id,
        work_order_id=wo.id,
        work_center_id=cell.id,
        sequence=sequence,
        name=f"OP{sequence}",
        status=status,
    )
    db.add(op)
    db.flush()
    return op


def _blocker(db, company_id, wo, op, *, status=WorkOrderBlockerStatus.OPEN, title="waiting on stock"):
    blocker = WorkOrderBlocker(
        company_id=company_id,
        work_order_id=wo.id,
        operation_id=op.id if op else None,
        category="material_missing",
        severity="high",
        title=title,
        status=status.value,
        reported_at=datetime.utcnow(),
    )
    db.add(blocker)
    db.flush()
    return blocker


OTHER_COMPANY_ID = 8_871


def _foreign_blocker_on(db, *, operation_id: int, title: str) -> WorkOrderBlocker:
    """An OPEN blocker belonging to a DIFFERENT tenant that names this operation id.

    Row ids are global, so nothing stops another company's blocker from carrying
    the same ``operation_id`` -- which is exactly why every read in this service
    filters ``company_id``. Built with its own company, part and work order, so it
    is a legitimate row over there rather than a malformed one that a filter might
    reject for the wrong reason.
    """
    if db.query(Company).filter(Company.id == OTHER_COMPANY_ID).first() is None:
        db.add(Company(id=OTHER_COMPANY_ID, name="Other Shop", slug="blk-outcome-other", is_active=True))
        db.flush()
    part = db.query(Part).filter(Part.company_id == OTHER_COMPANY_ID).first()
    if part is None:
        part = Part(
            company_id=OTHER_COMPANY_ID,
            part_number="BLK-OUTCOME-OTHER",
            name="Their bracket",
            part_type="manufactured",
            unit_of_measure="EA",
        )
        db.add(part)
        db.flush()
    wo = WorkOrder(
        company_id=OTHER_COMPANY_ID,
        work_order_number=f"BLK-O-FOREIGN-{operation_id}",
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
    )
    db.add(wo)
    db.flush()
    blocker = WorkOrderBlocker(
        company_id=OTHER_COMPANY_ID,
        work_order_id=wo.id,
        operation_id=operation_id,
        category="material_missing",
        severity="high",
        title=title,
        status=WorkOrderBlockerStatus.OPEN.value,
        reported_at=datetime.utcnow(),
    )
    db.add(blocker)
    db.flush()
    return blocker


class TestEveryWithheldResumeIsNamed:
    """The four ``(None, None)`` situations now come back as four distinct reasons."""

    def test_a_blocker_that_names_no_operation(self, db_session, test_company, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-NOOP")
        blocker = _blocker(db_session, test_company.id, wo, None)

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.NO_OPERATION
        assert outcome.resumed is False
        # NOTHING was ever held, so this must not read as a withheld resume -- a
        # "still held" warning here would be a new lie, not a fix.
        assert outcome.operation_still_held is False
        assert outcome.operation_id is None

    def test_another_open_blocker_still_names_the_operation(self, db_session, test_company, shop):
        """The reported defect: the resume is OWED and withheld, and the op stays held."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-OTHER")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        other = _blocker(db_session, test_company.id, wo, op, title="fixture on order")

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OTHER_BLOCKERS_OPEN
        assert outcome.resumed is False
        assert outcome.operation_still_held is True
        assert [b.id for b in outcome.other_open_blockers] == [other.id]
        db_session.refresh(op)
        assert op.status == OperationStatus.ON_HOLD

    def test_an_acknowledged_blocker_counts_as_open(self, db_session, test_company, shop):
        """ACKNOWLEDGED is still in the way -- the predicate is unchanged from before."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-ACK")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        _blocker(db_session, test_company.id, wo, op, status=WorkOrderBlockerStatus.ACKNOWLEDGED)

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OTHER_BLOCKERS_OPEN

    def test_the_operation_is_not_on_hold(self, db_session, test_company, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-LIVE")
        op = _op(db_session, test_company.id, wo, cell, status=OperationStatus.IN_PROGRESS)
        blocker = _blocker(db_session, test_company.id, wo, op)

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OPERATION_NOT_HELD
        assert outcome.resumed is False
        assert outcome.operation_still_held is False  # nothing to resume, so nothing withheld
        assert outcome.operation_status == "in_progress"

    def test_the_operation_row_cannot_be_loaded(self, db_session, test_company, shop):
        """The blocker names an operation id, and the id still rides the outcome."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-GONE")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        # Cross-tenant / vanished row: the company-scoped load finds nothing.
        blocker.operation_id = op.id + 9_000

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OPERATION_MISSING
        assert outcome.operation_id == op.id + 9_000
        assert outcome.operation is None
        assert outcome.operation_status is None
        assert outcome.operation_still_held is False

    def test_still_held_is_read_off_the_operation_not_off_the_reason(self, db_session, test_company, shop):
        """``other_blockers_open`` does NOT imply the op is held, and the object says so.

        Somebody can resume an operation from the kiosk without closing its
        blocker -- ``PUT /shop-floor/operations/{id}/resume`` deliberately leaves
        the record open. Warning "still held" there would be false, which is why
        the judgement is the operation's status and never the reason name.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-SPLIT")
        op = _op(db_session, test_company.id, wo, cell, status=OperationStatus.IN_PROGRESS)
        blocker = _blocker(db_session, test_company.id, wo, op)
        _blocker(db_session, test_company.id, wo, op, title="second stop")

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OTHER_BLOCKERS_OPEN
        assert outcome.operation_still_held is False


class TestAResumeThatHappenedIsReportedAsOne:
    def test_a_resume_to_ready_carries_no_withheld_reason(self, db_session, test_company, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-READY")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.withheld_reason is None
        assert outcome.resumed is True
        assert outcome.previous_status == "on_hold"
        assert outcome.operation_status == "ready"
        assert outcome.operation_still_held is False

    def test_the_fifth_outcome_resumed_but_floored_at_pending(self, db_session, test_company, shop):
        """The hold cleared and the job still did not come back to the board.

        PENDING is off the dispatch board and off the kiosk (both surface READY
        only). It is NOT a withheld resume and carries no reason -- the landing
        status is the disclosure, which is what stops "withheld" meaning two
        different things.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-PEND", status=WorkOrderStatus.DRAFT)
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(blocker)

        assert outcome.resumed is True
        assert outcome.withheld_reason is None
        assert outcome.operation_still_held is False
        assert outcome.operation_status == "pending"


class TestTheAuditContractIsUnchanged:
    """Invariant 2: an operation status-change row iff the operation actually moved."""

    @staticmethod
    def _operation_status_rows(db, operation_id):
        return (
            db.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == operation_id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )

    def test_no_operation_row_is_written_when_the_resume_is_withheld(self, db_session, test_company, test_user, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-AUD1")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        _blocker(db_session, test_company.id, wo, op, title="second stop")

        _, outcome = WorkOrderBlockerService(db_session).resolve_blocker_with_outcome(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            resolution_note="stock arrived",
            audit=AuditService(db_session, test_user),
        )

        assert outcome is not None and outcome.operation is not None  # populated for DISCLOSURE
        assert outcome.previous_status is None  # ...and that is what gates the row
        assert self._operation_status_rows(db_session, op.id) == []

    def test_exactly_one_operation_row_is_written_when_it_resumes(self, db_session, test_company, test_user, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-AUD2")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        WorkOrderBlockerService(db_session).resolve_blocker(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            resolution_note="stock arrived",
            audit=AuditService(db_session, test_user),
        )

        rows = self._operation_status_rows(db_session, op.id)
        assert len(rows) == 1
        assert rows[0].old_values["status"] == "on_hold"
        assert rows[0].new_values["status"] == "ready"

    @staticmethod
    def _blocker_status_rows(db, blocker_id):
        return (
            db.query(AuditLog)
            .filter(
                AuditLog.resource_type == "work_order_blocker",
                AuditLog.resource_id == blocker_id,
                AuditLog.action == "STATUS_CHANGE",
            )
            .all()
        )

    def test_no_operation_row_when_the_blocker_named_no_operation(self, db_session, test_company, test_user, shop):
        """Withheld situation 3 -- and the BLOCKER's own row still lands.

        The second half is what catches a refactor that goes one step too far.
        The change had to stop claiming a transition that never happened without
        dropping the row for the one that did: the blocker itself moved to
        RESOLVED, and that row is the record of who closed it. Both directions in
        one test, because a suite that only pins the absence would stay green on
        a build that wrote no audit rows at all.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-AUD3")
        held = _op(db_session, test_company.id, wo, cell)  # exists, but this blocker does not name it
        blocker = _blocker(db_session, test_company.id, wo, None)

        _, outcome = WorkOrderBlockerService(db_session).resolve_blocker_with_outcome(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            resolution_note="never held anything",
            audit=AuditService(db_session, test_user),
        )

        assert outcome is not None
        assert outcome.withheld_reason is BlockerResumeWithheldReason.NO_OPERATION
        assert self._operation_status_rows(db_session, held.id) == []
        assert len(self._blocker_status_rows(db_session, blocker.id)) == 1
        db_session.refresh(held)
        assert held.status == OperationStatus.ON_HOLD  # untouched, as it always was

    def test_no_operation_row_when_the_operation_was_never_held(self, db_session, test_company, test_user, shop):
        """Withheld situation 4: the operation is running, so nothing transitioned."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-AUD4")
        op = _op(db_session, test_company.id, wo, cell, status=OperationStatus.IN_PROGRESS)
        blocker = _blocker(db_session, test_company.id, wo, op)

        _, outcome = WorkOrderBlockerService(db_session).resolve_blocker_with_outcome(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            resolution_note="cleared",
            audit=AuditService(db_session, test_user),
        )

        assert outcome is not None
        assert outcome.withheld_reason is BlockerResumeWithheldReason.OPERATION_NOT_HELD
        # The operation row is populated for DISCLOSURE, which is exactly why the
        # audit guard may never key on it alone.
        assert outcome.operation is not None and outcome.previous_status is None
        assert self._operation_status_rows(db_session, op.id) == []
        assert len(self._blocker_status_rows(db_session, blocker.id)) == 1
        db_session.refresh(op)
        assert op.status == OperationStatus.IN_PROGRESS


class TestNoResumeAttemptedIsItsOwnState:
    def test_acknowledging_a_blocker_reports_no_outcome_at_all(self, db_session, test_company, test_user, shop):
        """Absent, not "withheld" -- an acknowledge must never warn about a hold."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-ACK2")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        _, outcome = WorkOrderBlockerService(db_session).update_blocker_with_outcome(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            data=WorkOrderBlockerUpdate(status=WorkOrderBlockerStatus.ACKNOWLEDGED),
            audit=AuditService(db_session, test_user),
        )

        assert outcome is None
        db_session.refresh(op)
        assert op.status == OperationStatus.ON_HOLD


class TestTheResponseCarriesIt:
    def test_resolve_discloses_the_withheld_resume_and_what_is_still_in_the_way(
        self, client, db_session, test_company, test_user, manager_headers, shop
    ):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        other = _blocker(db_session, test_company.id, wo, op, title="fixture on order")
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "stock arrived"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "resolved"  # the blocker DID close -- not an error
        outcome = body["operation_outcome"]
        assert outcome["operation_id"] == op.id
        assert outcome["operation_status"] == "on_hold"
        assert outcome["operation_resumed"] is False
        assert outcome["resume_withheld_reason"] == "other_blockers_open"
        assert outcome["operation_still_held"] is True
        assert [b["id"] for b in outcome["open_blockers"]] == [other.id]
        # Same shape the shop-floor resume returns, so the client reuses one type.
        assert outcome["open_blockers"][0]["title"] == "fixture on order"
        assert outcome["open_blockers"][0]["category"] == "material_missing"
        assert outcome["open_blockers"][0]["status"] == "open"

    def test_resolve_discloses_a_resume_that_landed_off_the_board(
        self, client, db_session, test_company, manager_headers, shop
    ):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP2", status=WorkOrderStatus.DRAFT)
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "stock arrived"},
            headers=manager_headers,
        )

        outcome = response.json()["operation_outcome"]
        assert outcome["operation_resumed"] is True
        assert outcome["resume_withheld_reason"] is None
        assert outcome["operation_still_held"] is False
        assert outcome["operation_status"] == "pending"

    def test_the_update_verb_carries_it_too(self, client, db_session, test_company, manager_headers, shop):
        """Both verbs share ``update_blocker``, so a caller of either could be misled."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP3")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        _blocker(db_session, test_company.id, wo, op, title="fixture on order")
        db_session.commit()

        response = client.put(
            f"/api/v1/work-order-blockers/{blocker.id}",
            json={"status": "dismissed"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        assert response.json()["operation_outcome"]["resume_withheld_reason"] == "other_blockers_open"

    def test_an_acknowledge_carries_no_outcome(self, client, db_session, test_company, manager_headers, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP4")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        db_session.commit()

        response = client.put(
            f"/api/v1/work-order-blockers/{blocker.id}",
            json={"status": "acknowledged"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        assert response.json()["operation_outcome"] is None

    def test_resolve_discloses_a_clean_resume(self, client, db_session, test_company, manager_headers, shop):
        """The ordinary good case over the wire: the last blocker closed and the job is back.

        Pinned at the HTTP layer, not only in the service, because this is the
        response the page decides between green and amber on. A build that
        reported every resolve as a shortfall would be a new bug of the same
        family -- crying wolf until nobody reads the toast.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP5")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "stock arrived"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        outcome = response.json()["operation_outcome"]
        assert outcome["operation_id"] == op.id
        assert outcome["operation_resumed"] is True
        assert outcome["resume_withheld_reason"] is None
        assert outcome["operation_still_held"] is False
        assert outcome["operation_status"] == "ready"  # on the board again
        assert outcome["open_blockers"] == []
        db_session.refresh(op)
        assert op.status == OperationStatus.READY

    def test_resolve_discloses_a_blocker_that_held_nothing(
        self, client, db_session, test_company, manager_headers, shop
    ):
        """A whole-work-order blocker: named, but NOT as a withheld resume.

        ``no_operation`` is a reason, and reporting it as "still held" would put a
        hold notice on a blocker that never touched an operation -- a new kind of
        false statement rather than a fix. ``operation_still_held`` is what the
        client warns on, and it must be false here.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP6")
        held = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, None)
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "customer confirmed"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        outcome = response.json()["operation_outcome"]
        assert outcome["resume_withheld_reason"] == "no_operation"
        assert outcome["operation_still_held"] is False
        assert outcome["operation_resumed"] is False
        assert outcome["operation_id"] is None
        assert outcome["operation_status"] is None
        db_session.refresh(held)
        assert held.status == OperationStatus.ON_HOLD  # a different operation, untouched

    def test_resolve_discloses_an_operation_that_was_never_held(
        self, client, db_session, test_company, manager_headers, shop
    ):
        """Somebody already lifted the hold from the kiosk; closing the record changes nothing."""
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-HTTP7")
        op = _op(db_session, test_company.id, wo, cell, status=OperationStatus.READY)
        blocker = _blocker(db_session, test_company.id, wo, op)
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "already handled"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        outcome = response.json()["operation_outcome"]
        assert outcome["resume_withheld_reason"] == "operation_not_held"
        assert outcome["operation_resumed"] is False
        assert outcome["operation_still_held"] is False  # nothing to resume, so nothing withheld
        assert outcome["operation_status"] == "ready"

    def test_another_companys_open_blocker_neither_withholds_nor_is_disclosed(
        self, client, db_session, test_company, manager_headers, shop
    ):
        """INVARIANT 1 on the new read. A foreign blocker is not in the way, and is not named.

        Both halves matter and they fail differently. If the ``.all()`` probe lost
        its ``company_id`` filter, another tenant's open blocker would silently
        WITHHOLD this shop's resume -- a job stopped by a row its owner cannot
        see. And the list is now returned to the browser, so a leak here would put
        another company's blocker TITLE, free text an operator typed, on this
        company's screen.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-TEN1")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)
        _foreign_blocker_on(db_session, operation_id=op.id, title="OTHER TENANT CONFIDENTIAL")
        db_session.commit()

        response = client.post(
            f"/api/v1/work-order-blockers/{blocker.id}/resolve",
            json={"resolution_note": "stock arrived"},
            headers=manager_headers,
        )

        assert response.status_code == 200
        body = response.json()
        outcome = body["operation_outcome"]
        assert outcome["operation_resumed"] is True  # the foreign row did not stop it
        assert outcome["resume_withheld_reason"] is None
        assert outcome["operation_still_held"] is False
        assert outcome["open_blockers"] == []
        assert "OTHER TENANT CONFIDENTIAL" not in response.text
        db_session.refresh(op)
        assert op.status == OperationStatus.READY


class TestATenantCannotReachAnotherTenantsOperation:
    """INVARIANT 1 on the operation read the disclosure moved earlier in the function."""

    def test_a_foreign_blocker_can_neither_read_nor_resume_this_operation(
        self, db_session, test_company, test_user, shop
    ):
        """The operation load is scoped to the BLOCKER's company, so it finds nothing.

        This is the mirror of the test above: there, a foreign blocker must not
        block us; here, a foreign blocker must not TOUCH us. It reads
        ``operation_missing`` -- the row exists, but not for this tenant -- and the
        held operation is left exactly where it was.
        """
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-TEN2")
        op = _op(db_session, test_company.id, wo, cell)
        foreign = _foreign_blocker_on(db_session, operation_id=op.id, title="theirs")
        db_session.flush()

        outcome = WorkOrderBlockerService(db_session)._resume_operation_if_no_open_blockers(foreign)

        assert outcome.withheld_reason is BlockerResumeWithheldReason.OPERATION_MISSING
        assert outcome.operation is None
        assert outcome.resumed is False
        db_session.refresh(op)
        assert op.status == OperationStatus.ON_HOLD


class TestTheCompatibilityWrappersStillReturnJustTheBlocker:
    """The non-HTTP caller is untouched by the disclosure refactor.

    ``ai_action_applier`` (escalate_blocker / acknowledge_blocker) is the only
    thing outside this router that calls ``resolve_blocker`` / ``update_blocker``,
    and it uses the result AS A BLOCKER. (``copilot_service`` and
    ``process_sheet_service`` import the service too, but only for
    ``list_blockers`` / ``create_blocker``.) The refactor put the outcome on new
    ``*_with_outcome`` methods precisely so it did not have to change, and this
    pins that seam -- a tuple leaking back through the old names would break it at
    a distance and only sometimes, since a tuple is truthy and sails through an
    ``if`` unharmed before failing on the first attribute read.
    """

    def test_resolve_blocker_returns_the_blocker_row(self, db_session, test_company, test_user, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-WRAP1")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        result = WorkOrderBlockerService(db_session).resolve_blocker(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            resolution_note="stock arrived",
            audit=AuditService(db_session, test_user),
        )

        assert isinstance(result, WorkOrderBlocker)
        assert result.id == blocker.id
        assert result.status == WorkOrderBlockerStatus.RESOLVED.value

    def test_update_blocker_returns_the_blocker_row(self, db_session, test_company, test_user, shop):
        part, cell = shop
        wo = _wo(db_session, test_company.id, part, number="BLK-O-WRAP2")
        op = _op(db_session, test_company.id, wo, cell)
        blocker = _blocker(db_session, test_company.id, wo, op)

        result = WorkOrderBlockerService(db_session).update_blocker(
            company_id=test_company.id,
            user=test_user,
            blocker_id=blocker.id,
            data=WorkOrderBlockerUpdate(status=WorkOrderBlockerStatus.ACKNOWLEDGED),
            audit=AuditService(db_session, test_user),
        )

        assert isinstance(result, WorkOrderBlocker)
        assert result.status == WorkOrderBlockerStatus.ACKNOWLEDGED.value


class TestTheClosedVocabularyIsClosedOnBOTHSIDES:
    """STRUCTURAL GUARD: the TS union must list exactly the members of the enum.

    ``frontend/src/components/kiosk/heldOperations.ts`` switches exhaustively over
    ``BlockerResumeWithheldReason`` and binds the ``default:`` arm to ``never``, so
    a member the client does not word is a COMPILE ERROR -- but only once the
    TypeScript union in ``frontend/src/types/aiForward.ts`` has grown the member.
    Nothing else forces that. Add a reason to the Python enum alone and the client
    still compiles, the new value falls into the ``default:`` arm at runtime, and
    the shop reads a vague sentence carrying a raw internal token: the same
    unhandled-value-renders-something-plausible failure this whole change exists
    to end, one layer down.

    So the parity is asserted here, in Python, against the TS source -- the
    ``notification_links`` guard's pattern (parse the real file; a duplicated
    constant would be the second source that drifts). The "Backend Tests" job
    checks out the whole repo, so the file is present; if it ever is not this
    FAILS rather than skipping, because a guard that evaporates is not a guard.
    """

    TS_TYPES = Path(__file__).resolve().parents[3] / "frontend" / "src" / "types" / "aiForward.ts"

    def test_the_ts_union_lists_exactly_the_enum_members(self):
        assert self.TS_TYPES.is_file(), f"cannot find {self.TS_TYPES} -- the guard cannot run"
        source = self.TS_TYPES.read_text(encoding="utf-8")
        match = re.search(r"export type BlockerResumeWithheldReason\s*=\s*([^;]+);", source)
        assert match, "BlockerResumeWithheldReason is not declared in frontend/src/types/aiForward.ts"
        declared = set(re.findall(r"'([a-z_]+)'", match.group(1)))

        assert declared == {reason.value for reason in BlockerResumeWithheldReason}, (
            "The TS union and the Python enum disagree. Add the member to BOTH, and word it in "
            "heldOperations.ts::stillHeldSentence -- the exhaustive switch is what stops a new "
            "reason rendering a sentence nobody chose."
        )
