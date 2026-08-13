"""Can the kiosk actually RESUME? The path fence, answered.

Making a held operation visible on the kiosk is worthless if the resume control
403s. The kiosk's badge-minted operator token carries ``scope="kiosk"`` and is
path-fenced by ``deps._is_kiosk_scope_allowed_path``, so whether the EXISTING
``PUT /shop-floor/operations/{id}/resume`` is reachable from a crew station is a
fact about that fence, not an opinion -- and the frontend is building a control
against the answer.

The answer is YES, today, with no fence change: the path sits under the
``/api/v1/shop-floor`` allow prefix and matches none of the deny rules
(``kiosk-stations`` / ``dispatch-board`` prefixes, the ``/time-entries/…/approve``
approval suffixes, the ``/work-centers/…/run-order`` planner suffix), and the
endpoint carries no ``require_role``, so an OPERATOR badge may resume. These
tests pin that rather than leaving it to be re-derived, and pin the deny
controls alongside so "resume is allowed" can never be read as "the fence is
open".

Also pinned here, because it is the same shop-floor recovery path and the fence
answer made resume a shop-wide verb rather than one desktop button:

* it still writes its audit row, now as a STATUS_CHANGE carrying before -> after;
* it still returns the still-open blockers it did NOT resolve (BLK-4
  warn-and-record -- resume deliberately does not close the blocker, and that
  divergence is what the warning exists to surface);
* it REFUSES a cancelled (soft-deleted) nest's tombstone operation, 409 -- the
  guard on the WRITE, not just on the kiosk's held read;
* it RESTORES rather than promotes, so one tap can never perform the release that
  ``POST /work-orders/{id}/release`` owns.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.operational_event import OperationalEvent
from app.models.work_order import OperationStatus, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerSeverity,
    WorkOrderBlockerStatus,
)
from app.services.work_order_blocker_service import blocker_default_title
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    COMPANY_B,
    bearer,
    ensure_company,
    make_user,
    make_wo_with_operation,
    make_work_center,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]


def resume_url(operation_id: int) -> str:
    return f"/api/v1/shop-floor/operations/{operation_id}/resume"


def badge_headers(user) -> dict:
    """A 5-minute badge-minted operator token, exactly as POST /auth/kiosk-badge-token mints it."""
    return bearer(
        create_access_token(
            subject=user.id,
            company_id=user.company_id,
            expires_delta=timedelta(minutes=5),
            scope="kiosk",
        )
    )


def test_badge_minted_kiosk_token_may_resume_a_held_operation(client: TestClient, db_session: Session):
    """THE fence answer: the kiosk can drive the existing resume endpoint today."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    # No actual_start -> resumes to READY, so the job returns to the kiosk queue.
    assert resp.json()["status"] == OperationStatus.READY.value
    db_session.expire_all()
    db_session.refresh(held_op)
    assert held_op.status == OperationStatus.READY


def test_the_fence_is_not_open_just_because_resume_is_allowed(client: TestClient, db_session: Session):
    """Control: the same token is still 403 on the deny-listed planner paths."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    headers = badge_headers(operator)

    assert client.get("/api/v1/shop-floor/dispatch-board", headers=headers).status_code == status.HTTP_403_FORBIDDEN
    run_order = client.put(
        f"/api/v1/shop-floor/work-centers/{wc.id}/run-order",
        headers=headers,
        json={"operation_ids": []},
    )
    assert run_order.status_code == status.HTTP_403_FORBIDDEN
    # And a path entirely outside the shop-floor prefix.
    assert client.get("/api/v1/work-orders/", headers=headers).status_code == status.HTTP_403_FORBIDDEN


def test_resume_from_the_kiosk_still_writes_its_audit_row(client: TestClient, db_session: Session):
    """Invariant 2: a status change through a kiosk token audits like any other.

    And it audits AS a status change. Resume used to write a prose ``audit.log``
    row -- named and tenant-tagged, so the invariant held, but carrying no
    before -> after states. That was tolerable while ``ShopFloorSimple`` was the
    only caller; it stopped being tolerable when resume became a shop-wide floor
    verb on both kiosks. ``transition`` carries the verb the generic
    STATUS_CHANGE action no longer names, so "who resumed what" stays one query.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "STATUS_CHANGE",
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == held_op.id,
        )
        .one()
    )
    assert row.company_id == COMPANY_A
    assert row.user_id == operator.id
    assert row.old_values == {"status": OperationStatus.ON_HOLD.value}
    assert row.new_values == {"status": OperationStatus.READY.value}
    assert row.extra_data["transition"] == "resume_operation"


def test_resume_returns_the_blocker_it_did_not_resolve(client: TestClient, db_session: Session):
    """BLK-4: resume does NOT resolve the blocker, and says so, verbatim.

    The decoupling is deliberate -- resolution stays owned by the blocker
    resolve/dismiss flow -- so the operation/blocker divergence has to reach the
    operator's screen. The kiosk needs these fields to warn with.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    blocker = WorkOrderBlocker(
        company_id=COMPANY_A,
        work_order_id=held_op.work_order_id,
        operation_id=held_op.id,
        category=WorkOrderBlockerCategory.QUALITY_HOLD.value,
        severity=WorkOrderBlockerSeverity.CRITICAL.value,
        status=WorkOrderBlockerStatus.OPEN.value,
        title="Quality Hold: op 10",
        note="Awaiting CMM",
        reported_by=operator.id,
        reported_at=datetime.utcnow(),
    )
    db_session.add(blocker)
    db_session.commit()

    body = client.put(resume_url(held_op.id), headers=badge_headers(operator)).json()

    assert [b["id"] for b in body["open_blockers"]] == [blocker.id]
    assert body["open_blockers"][0]["category"] == WorkOrderBlockerCategory.QUALITY_HOLD.value
    assert body["open_blockers"][0]["severity"] == WorkOrderBlockerSeverity.CRITICAL.value
    # Still open: resume resolved nothing.
    db_session.refresh(blocker)
    assert blocker.status == WorkOrderBlockerStatus.OPEN.value


# ---------------------------------------------------------------------------
# THE RESPONSE'S FREE TEXT, gated by audience.
#
# ``open_blockers[].title`` is caller-supplied free text
# (``WorkOrderBlockerCreate.title``; ``blocker_default_title`` composes one only
# when the caller supplies none), and the kiosk screen it drives persists until
# somebody taps it away -- bounded only by the 90s idle reset -- on a shared,
# unattended tablet. So the resume WRITE splits by audience exactly as the queue
# READ does: a badge-minted crew-station token (``scope="kiosk"``) does not get
# the title; an identified session does.
# ---------------------------------------------------------------------------


def _office_blocker(db_session: Session, *, operation, reported_by: int, title: str, note=None) -> WorkOrderBlocker:
    """A blocker as the OFFICE files it: free text in the title, empty note."""
    blocker = WorkOrderBlocker(
        company_id=COMPANY_A,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        category=WorkOrderBlockerCategory.QUALITY_HOLD.value,
        severity=WorkOrderBlockerSeverity.HIGH.value,
        status=WorkOrderBlockerStatus.OPEN.value,
        title=title,
        note=note,
        reported_by=reported_by,
        reported_at=datetime.utcnow(),
    )
    db_session.add(blocker)
    db_session.commit()
    return blocker


def test_a_crew_station_resume_never_gets_the_blocker_title(client: TestClient, db_session: Session):
    """The disclosure this endpoint used to leak: an office blocker's free text.

    A title like this is an ordinary office shape -- a customer name and an NCR
    number in the title, nothing in the note -- and it reached a shared tablet on
    a view built to persist. The key is ABSENT, not blanked, so a devtools
    console on the tablet has nothing to read either.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _office_blocker(
        db_session,
        operation=held_op,
        reported_by=operator.id,
        title="NCR-1042 cracked welds - ACME rejected lot",
    )

    body = client.put(resume_url(held_op.id), headers=badge_headers(operator)).json()
    payload = body["open_blockers"][0]

    assert "title" not in payload
    assert "ACME" not in json.dumps(body)
    # The station keeps everything it needs to act: what kind of stop, how bad,
    # and that a human wrote a reason it cannot show.
    assert payload["category"] == WorkOrderBlockerCategory.QUALITY_HOLD.value
    assert payload["severity"] == WorkOrderBlockerSeverity.HIGH.value
    assert payload["free_text_withheld"] is True
    assert payload["has_note"] is True


def test_has_note_catches_free_text_that_lives_only_in_the_title(client: TestClient, db_session: Session):
    """The gap that made withholding actively misleading.

    ``has_note`` used to key on ``note`` alone, so an office blocker carrying its
    free text in the TITLE with an empty note reported False -- the station drew
    a bare category, and silence there reads as "no reason was given" over a hold
    somebody deliberately wrote up. It now covers both fields.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _office_blocker(db_session, operation=held_op, reported_by=operator.id, title="Weld porosity, hold for engineering")

    payload = client.put(resume_url(held_op.id), headers=badge_headers(operator)).json()["open_blockers"][0]

    assert payload["has_note"] is True


def test_a_server_composed_title_does_not_claim_somebody_wrote_a_reason(client: TestClient, db_session: Session):
    """``has_note`` describes the RECORD, not the policy.

    A kiosk-placed hold gets its title composed by ``blocker_default_title`` from
    the category and the operation name -- text no human typed, and which the
    station is already showing as the category. Reporting it as a withheld
    written reason would send an operator to find a supervisor over nothing.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _office_blocker(
        db_session,
        operation=held_op,
        reported_by=operator.id,
        # Exactly what blocker_default_title() composes for this category+operation.
        title=blocker_default_title(WorkOrderBlockerCategory.QUALITY_HOLD.value, held_op.work_order, held_op),
    )

    payload = client.put(resume_url(held_op.id), headers=badge_headers(operator)).json()["open_blockers"][0]

    assert payload["has_note"] is False
    assert payload["free_text_withheld"] is True


def test_an_identified_session_keeps_the_blocker_title_on_resume(client: TestClient, db_session: Session):
    """The split is by AUDIENCE, not a blanket redaction.

    The single-operator kiosk runs on the operator's own session (no ``scope``
    claim) and the desktop on a normal one, so neither is an unattended screen
    and neither loses the text.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _office_blocker(
        db_session,
        operation=held_op,
        reported_by=operator.id,
        title="NCR-1042 cracked welds - ACME rejected lot",
    )

    payload = client.put(resume_url(held_op.id), headers=user_headers(operator)).json()["open_blockers"][0]

    assert payload["title"] == "NCR-1042 cracked welds - ACME rejected lot"
    assert payload["free_text_withheld"] is False
    assert payload["has_note"] is True


def test_resume_refuses_an_operation_that_is_not_on_hold(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, ready_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.READY)

    resp = client.put(resume_url(ready_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "Operation is not on hold"


# ---------------------------------------------------------------------------
# THE CANCELLED-NEST TOMBSTONE, on the WRITE.
#
# ``laser_nest_service.soft_delete_laser_nest`` parks a cancelled nest's
# operation in ON_HOLD -- ``OperationStatus`` has no operation-level CANCELLED --
# and never hard-deletes the nest, precisely so traceability survives. Resuming
# one is therefore not "lifting a hold": it undoes a soft delete from the front
# end (invariant 3), and lands a laser operation with no live nest, no CNC file
# and a quantity its parent no longer counts back on the board (invariant 5).
#
# The guard used to live only in ``dispatch_service.held_operations_query``, so
# it protected the kiosk's held READ while resume itself checked nothing. It is
# now the shared ``dispatch_service.cancelled_nest_exists`` predicate, applied
# here on the write.
# ---------------------------------------------------------------------------


def _cancelled_nest_for(db_session: Session, operation) -> LaserNest:
    """Soft-deleted nest bound to ``operation`` -- the tombstone shape."""
    package = LaserNestPackage(
        company_id=operation.company_id,
        child_work_order_id=operation.work_order_id,
        package_name=f"PKG-{operation.id}",
    )
    db_session.add(package)
    db_session.flush()
    nest = LaserNest(
        company_id=operation.company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=f"NEST-{operation.id}",
        planned_runs=4,
        material="0.250 A36",
        is_deleted=True,
    )
    db_session.add(nest)
    db_session.commit()
    db_session.refresh(nest)
    return nest


def test_resume_refuses_a_cancelled_nests_operation(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _cancelled_nest_for(db_session, held_op)

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_409_CONFLICT, resp.text
    assert "cancelled" in resp.json()["detail"].lower()
    # And it refused BEFORE mutating: the tombstone is untouched.
    db_session.expire_all()
    db_session.refresh(held_op)
    assert held_op.status == OperationStatus.ON_HOLD


def test_refused_resume_writes_no_audit_row_and_leaves_the_nest_deleted(client: TestClient, db_session: Session):
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    nest = _cancelled_nest_for(db_session, held_op)
    audit_before = db_session.query(AuditLog).count()

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert db_session.query(AuditLog).count() == audit_before
    db_session.refresh(nest)
    assert nest.is_deleted is True


def test_a_live_nest_on_a_genuinely_held_operation_still_resumes(client: TestClient, db_session: Session):
    """Control: the guard keys on the SOFT DELETE, not on carrying a nest."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    package = LaserNestPackage(
        company_id=COMPANY_A,
        child_work_order_id=held_op.work_order_id,
        package_name=f"PKG-LIVE-{held_op.id}",
    )
    db_session.add(package)
    db_session.flush()
    db_session.add(
        LaserNest(
            company_id=COMPANY_A,
            package_id=package.id,
            work_order_operation_id=held_op.id,
            nest_name=f"NEST-LIVE-{held_op.id}",
            planned_runs=4,
            material="0.250 A36",
            is_deleted=False,
        )
    )
    db_session.commit()

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_another_companys_cancelled_nest_cannot_condemn_our_operation(client: TestClient, db_session: Session):
    """Invariant 1 on the guard itself: the predicate is tenant-scoped both ways."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    ensure_company(db_session, COMPANY_B)
    package = LaserNestPackage(
        company_id=COMPANY_B,
        child_work_order_id=held_op.work_order_id,
        package_name=f"PKG-FOREIGN-{held_op.id}",
    )
    db_session.add(package)
    db_session.flush()
    db_session.add(
        LaserNest(
            company_id=COMPANY_B,
            package_id=package.id,
            work_order_operation_id=held_op.id,
            nest_name=f"NEST-FOREIGN-{held_op.id}",
            planned_runs=4,
            material="0.250 A36",
            is_deleted=True,
        )
    )
    db_session.commit()

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text


def test_the_desktop_operations_list_hides_a_cancelled_nests_operation(client: TestClient, db_session: Session):
    """``GET /shop-floor/operations`` is where ShopFloorSimple offers Resume.

    Listing the tombstone there is what put the one-tap resurrection in front of
    anyone in the first place, so the same predicate applies to the list.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, tombstone_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _, genuine_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    _cancelled_nest_for(db_session, tombstone_op)

    body = client.get(f"/api/v1/shop-floor/operations?work_center_id={wc.id}", headers=user_headers(operator)).json()
    listed = {row["id"] for row in body["operations"]}

    assert genuine_op.id in listed
    assert tombstone_op.id not in listed


# ---------------------------------------------------------------------------
# RESTORE, NOT PROMOTE.
#
# ``put_operation_on_hold`` refuses only COMPLETE, so a PENDING operation can be
# held. Resume used to set ``IN_PROGRESS if actual_start else READY``, which made
# one tap on a shop-floor screen perform a RELEASE: an unstarted, unreleased
# operation arrived on the dispatch board and the kiosk queue. Release is the
# authorization step and the record of who authorized production.
#
# Resume now floors at PENDING and delegates the lift to
# ``promote_ready_operations`` -- THE promotion rule, shared with WO release,
# operation completion and the read-path heal -- so it can only ever reach a
# state a board read would have reached anyway.
# ---------------------------------------------------------------------------


def _add_operation(db_session: Session, work_order, work_center, *, sequence: int, op_status: OperationStatus):
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=f"Step {sequence}",
        status=op_status,
        company_id=work_order.company_id,
    )
    db_session.add(operation)
    db_session.commit()
    db_session.refresh(operation)
    return operation


def test_resume_does_not_release_a_held_operation_on_a_draft_work_order(client: TestClient, db_session: Session):
    """The sharp edge: one tap used to put unreleased work on the floor's board."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(
        db_session,
        work_center=wc,
        op_status=OperationStatus.ON_HOLD,
        wo_status=WorkOrderStatus.DRAFT,
    )

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == OperationStatus.PENDING.value
    db_session.expire_all()
    db_session.refresh(held_op)
    assert held_op.status == OperationStatus.PENDING


def test_resume_does_not_promote_past_an_incomplete_cross_work_center_predecessor(
    client: TestClient, db_session: Session
):
    """The promotion gate still applies -- resume does not get to skip it."""
    wc_a = make_work_center(db_session, company_id=COMPANY_A, name="Laser")
    wc_b = make_work_center(db_session, company_id=COMPANY_A, name="Brake")
    operator = make_user(db_session, company_id=COMPANY_A)
    work_order, first_op = make_wo_with_operation(db_session, work_center=wc_a, op_status=OperationStatus.READY)
    second_op = _add_operation(db_session, work_order, wc_b, sequence=20, op_status=OperationStatus.ON_HOLD)

    resp = client.put(resume_url(second_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["status"] == OperationStatus.PENDING.value
    db_session.expire_all()
    db_session.refresh(first_op)
    # The predecessor is untouched -- resume promoted nothing anywhere.
    assert first_op.status == OperationStatus.READY


def test_resume_returns_a_startable_operation_to_ready(client: TestClient, db_session: Session):
    """The common case is unchanged: net zero for work that was READY before."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.json()["status"] == OperationStatus.READY.value


def test_resume_returns_a_started_operation_to_in_progress(client: TestClient, db_session: Session):
    """Labor evidence wins outright -- a running job goes back to running."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    held_op.actual_start = datetime.utcnow()
    db_session.commit()

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.json()["status"] == OperationStatus.IN_PROGRESS.value


def test_a_pending_resume_audits_the_states_it_actually_moved_between(client: TestClient, db_session: Session):
    """log_status_change is what makes the restore rule reviewable after the fact."""
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(
        db_session,
        work_center=wc,
        op_status=OperationStatus.ON_HOLD,
        wo_status=WorkOrderStatus.DRAFT,
    )

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "STATUS_CHANGE",
            AuditLog.resource_type == "work_order_operation",
            AuditLog.resource_id == held_op.id,
        )
        .one()
    )
    assert row.old_values == {"status": OperationStatus.ON_HOLD.value}
    assert row.new_values == {"status": OperationStatus.PENDING.value}


# ---------------------------------------------------------------------------
# THE PROMOTION EMITS, like every other promotion seam.
#
# Resume passes ``db`` to ``promote_ready_operations``, so each PENDING -> READY
# flip emits its ``operation_ready`` event. It used to pass ``db=None`` to avoid
# "restarting the Lean queue-time clock" -- a reason that does not exist
# (``flow_metrics_service`` reads ``func.min(occurred_at)``) and whose real cost
# fell on the SIBLINGS this promotion flips: for them the suppressed event was
# their FIRST, and the read-path heal only emits when it flips a PENDING row, so
# nothing would ever emit it and their queue time was permanently unmeasurable.
# ---------------------------------------------------------------------------


def _ready_events(db: Session, operation_id: int):
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "operation_ready", OperationalEvent.operation_id == operation_id)
        .order_by(OperationalEvent.occurred_at.asc())
        .all()
    )


def test_a_sibling_promoted_by_a_resume_gets_its_first_ready_event(client: TestClient, db_session: Session):
    """The metrics loss the old ``db=None`` caused, pinned.

    Two operations share a work center, so the predecessor gate leaves them
    mutually unordered and the promotion flips BOTH. The sibling's flip is the
    only ``PENDING -> READY`` it will ever have -- the read-path heal skips a row
    that is already READY -- so an unemitted event here is not a duplicate
    avoided, it is a measurement that never happens.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    work_order, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    sibling = _add_operation(db_session, work_order, wc, sequence=20, op_status=OperationStatus.PENDING)
    assert _ready_events(db_session, sibling.id) == []

    resp = client.put(resume_url(held_op.id), headers=badge_headers(operator))

    assert resp.status_code == status.HTTP_200_OK, resp.text
    db_session.expire_all()
    db_session.refresh(sibling)
    assert sibling.status == OperationStatus.READY
    events = _ready_events(db_session, sibling.id)
    assert len(events) == 1
    # Rule-driven, not authorized by the operator who tapped RESUME: release is
    # the authorization step, and this flip is not one.
    assert events[0].user_id is None
    assert events[0].work_order_id == work_order.id


def test_the_resumed_ops_duplicate_ready_event_does_not_move_its_queue_anchor(client: TestClient, db_session: Session):
    """The cost of emitting, measured -- and it is not a metric change.

    ``flow_metrics_service`` anchors queue time on the EARLIEST ``operation_ready``
    per operation, so a second event on an op that already has one is ignored.
    This pins the consumer, not the comment: if it ever became last-wins, a
    hold/resume cycle would start erasing queue time and this fails.
    """
    from app.services.flow_metrics_service import _ready_times_by_operation

    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    work_order, held_op = make_wo_with_operation(db_session, work_center=wc, op_status=OperationStatus.ON_HOLD)
    original_ready = datetime.utcnow() - timedelta(hours=6)
    db_session.add(
        OperationalEvent(
            company_id=COMPANY_A,
            event_type="operation_ready",
            source_module="work_order_state",
            entity_type="work_order_operation",
            entity_id=held_op.id,
            work_order_id=work_order.id,
            operation_id=held_op.id,
            severity="info",
            event_payload={},
            occurred_at=original_ready,
        )
    )
    db_session.commit()

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    db_session.expire_all()
    assert len(_ready_events(db_session, held_op.id)) == 2, "the resume added one"
    anchor = _ready_times_by_operation(db_session, COMPANY_A, [held_op.id])[held_op.id]
    assert abs((anchor.replace(tzinfo=None) - original_ready).total_seconds()) < 1


def test_a_resume_that_promotes_nothing_emits_nothing(client: TestClient, db_session: Session):
    """A DRAFT parent is refused promotion, so there is no flip and no event.

    The emit rides the PROMOTION, not the resume -- an operation that lands
    PENDING never became ready and must not claim it did.
    """
    wc = make_work_center(db_session, company_id=COMPANY_A)
    operator = make_user(db_session, company_id=COMPANY_A)
    _, held_op = make_wo_with_operation(
        db_session,
        work_center=wc,
        op_status=OperationStatus.ON_HOLD,
        wo_status=WorkOrderStatus.DRAFT,
    )

    client.put(resume_url(held_op.id), headers=badge_headers(operator))

    db_session.expire_all()
    assert _ready_events(db_session, held_op.id) == []
