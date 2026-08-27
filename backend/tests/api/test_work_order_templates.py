"""``/api/v1/work-order-templates`` — a catalog entry that produces a DRAFT, never a release.

WHAT THIS FILE EXISTS TO HOLD DOWN
----------------------------------
A template is a NAME plus a POINTER at the work order whose plan it stands for, and
``POST /{id}/use`` hands that work order to
``work_order_duplicate_service.duplicate_work_order`` — the same copy engine
``POST /work-orders/{id}/duplicate`` uses. So most of what a template *does* is already
pinned by ``tests/api/test_work_order_duplicate.py``, and this file deliberately does
not re-litigate it. What it pins is the part that is new, and the part that is
dangerous.

**The draft guarantee is the highest-consequence property here, and it is not obvious.**
The shop already had a one-click "run it again" door: Import Nest Package. That door
force-sets ``RELEASED`` at three separate sites, so an imported nest job is on the
dispatch board and in the kiosk queue before any planner has looked at it. This feature
is the *other* door — the reviewed one — and the entire argument for it collapses the
moment its output reaches the floor unreviewed.

Two things about how that guarantee is asserted are worth reading before editing a test
here:

* **The dispatch query does NOT exclude DRAFT work orders.**
  ``dispatch_service.queued_operations_query`` filters on OPERATION status
  (``READY``/``IN_PROGRESS``) and on the work order not being terminal and not
  soft-deleted. ``DRAFT`` is nowhere in it. A template's output is invisible on the
  board because its operations are born ``PENDING`` and nothing promotes a DRAFT work
  order — *not* because the board filters DRAFT out. So
  :class:`TestTheDraftGuarantee` asserts the OBSERVABLE (zero rows out of the real
  query) and names the real mechanism, rather than asserting operation statuses and
  calling that a dispatch test. If someone ever adds a promotion path that runs on a
  DRAFT, an operation-status assertion would still pass while the floor got the job.

* **The contrast is asserted too.** :class:`TestImportStillReleases` exercises a
  release-forcing door on purpose. Nothing in this feature should make anyone
  "helpfully" convert import to draft: the release-on-import behavior is what the laser
  cell depends on, and it is the *difference* between the two doors that this feature
  is. Note that the import path releases WITHOUT ``released_by`` / ``released_at`` and
  without a ``log_status_change`` row — it force-sets the column — so the test asserts
  the status and explicitly does not assert a release stamp.

The rest, in one line each:

* **Nothing of the production record leaks.** :class:`TestNoProductionRecordLeakage`
  asserts every field by name, including that the copy carries ZERO ``TimeEntry`` rows
  while the source keeps its own. A copy asserting work happened that never happened is
  a false statement on an AS9100D-auditable record.
* **The source comes through untouched** — after saving AND after using. That is the
  stated acceptance criterion for the feature, not an implementation detail, so
  :class:`TestTheSourceIsUnchanged` snapshots the whole job and diffs it.
* **Quantity resolution is request -> template default -> source**, first POSITIVE wins,
  and a nest-bearing template OVERRULES all of it with the sum of its nests' planned
  runs. The response reports what was STORED; the chain records the overruled request.
* **Refusals propagate untouched.** A template must never mint a work order the
  duplicate path would have refused: a retired produced part, a process-sheet family
  with no released revision. One button is not a licence to route around a gate. A
  DELETED SOURCE is the documented exception and used to be in that list — the owner
  overrode it (*"templates need to stay even if there is no work order present for
  it"*), so a template reads through the tombstone and still produces a DRAFT. That
  is not a hole in the rule: the duplicate service never asked whether the source was
  deleted either. The gate that stays is at the other end — saving a NEW template
  from a deleted work order is still 404, because that is SELECTION.
* **Skips propagate untouched too**, in the SAME envelope the Duplicate dialog renders.
  A skipped material tie means the new job carries no demand for that material — so no
  shortage is raised, the work runs, and stock is never deducted. A skip only the audit
  chain knows about is a job the planner releases believing it carries demand it does
  not have.
* Tenancy (invariant 1), the audit chain (invariant 2), soft delete + name reuse
  (invariant 3) and the role gate each get their own class.
* :class:`TestTheDraftAssertionGuard` unit-tests the safety net itself. It is redundant
  code by design, so nothing else would ever notice it stopped working.

Builders are IMPORTED from ``tests/api/test_work_order_duplicate.py`` rather than
re-declared. They are module-level functions, not fixtures, and sharing them is what
keeps "a template produces what a duplicate produces" a property two suites can compare
rather than two independently-built fictions.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem
from app.models.laser_nest import LaserNestPackage
from app.models.part import Part
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.models.work_order_template import WorkOrderTemplate
from app.services import dispatch_service
from app.services import work_order_template_service as templates
from tests.api.test_work_order_duplicate import (
    COMPANY_A,
    COMPANY_B,
    MEASUREMENT_CONFIG,
    _ensure_company,
    add_operation,
    attach_nest,
    audit_rows,
    headers_for,
    make_document,
    make_package,
    make_part,
    make_process_sheet,
    make_tie,
    make_user,
    make_work_center,
    make_work_order,
    nests_of,
    operations_of,
    snapshot_onto,
    ties_of,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/work-order-templates"


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #


def save_template(client: TestClient, headers: dict, work_order_id: int, name: str, **extra):
    """``POST /work-order-templates`` — point a name at a work order."""
    body = {"source_work_order_id": work_order_id, "name": name}
    body.update(extra)
    return client.post(BASE, headers=headers, json=body)


def use_template(client: TestClient, headers: dict, template_id: int, **body):
    """``POST /work-order-templates/{id}/use``.

    Sent with an explicit ``json={}`` when no keys are given, because that is the
    click-once case the feature is for: BOTH body fields are optional and the planner
    presses the button without typing anything.
    """
    return client.post(f"{BASE}/{template_id}/use", headers=headers, json=dict(body))


def saved(client: TestClient, headers: dict, work_order_id: int, name: str, **extra) -> int:
    """Save a template and return its id, failing loudly on anything but a 201."""
    response = save_template(client, headers, work_order_id, name, **extra)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"]


def created_work_order(db: Session, response) -> WorkOrder:
    """The new WO row behind a 201. The body is the duplicate ENVELOPE, not a bare WO."""
    db.expire_all()
    return db.query(WorkOrder).filter(WorkOrder.id == response.json()["work_order"]["id"]).one()


def dispatch_rows_for(db: Session, work_order: WorkOrder, company_id: int = COMPANY_A) -> list:
    """Rows THIS work order contributes to the real dispatch board query.

    Deliberately the production query (``dispatch_service.queued_operations_query`` —
    what ``/dispatch``, the kiosk queue and the wallboard all read) rather than a
    re-statement of its filters, so a change to the board's definition is felt here.
    """
    return [
        op for op in dispatch_service.queued_operations_query(db, company_id).all() if op.work_order_id == work_order.id
    ]


# --------------------------------------------------------------------------- #
# Source builders — the two shapes the shop actually re-runs
# --------------------------------------------------------------------------- #


def build_nest_source(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    runs: tuple = (3, 2),
    tie_material: bool = True,
    **overrides,
):
    """A finished laser nest job: the headline template case.

    Its ``quantity_ordered`` is the SUM of the nests' planned runs, because that is what
    ``laser_nest_service._recompute_child_quantity_ordered`` defines a laser WO's
    quantity to BE. Building it any other way would make the derived-quantity
    assertions below pass against a source that is already inconsistent.
    """
    work_center = make_work_center(db, company_id=company_id, wc_type="laser")
    work_order = make_work_order(
        db,
        company_id=company_id,
        part=None,
        work_order_type="laser_cutting",
        quantity_ordered=float(sum(runs)),
        status_value=WorkOrderStatus.COMPLETE,
        quantity_complete=float(sum(runs)),
        sequential_operations=False,
        **overrides,
    )
    package = make_package(db, work_order, company_id=company_id)
    document = make_document(db, company_id=company_id, work_order_id=work_order.id)
    sheet = make_part(db, company_id=company_id, part_type="raw_material", uom="sheets")

    operations, nests = [], []
    for index, planned in enumerate(runs, start=1):
        operation = add_operation(
            db,
            work_order,
            work_center,
            company_id=company_id,
            sequence=index * 10,
            operation_number=f"Nest {index}",
            status=OperationStatus.COMPLETE,
            quantity_complete=float(planned),
            component_quantity=float(planned),
        )
        nests.append(
            attach_nest(
                db,
                package,
                operation,
                company_id=company_id,
                planned_runs=planned,
                completed_runs=float(planned),
                document_id=document.id,
            )
        )
        if tie_material:
            make_tie(
                db,
                work_order,
                sheet,
                company_id=company_id,
                operation=operation,
                qty_per_run=1.0,
                qty_planned=float(planned),
                unit_of_measure="sheets",
                qty_consumed=float(planned),
            )
        operations.append(operation)
    db.commit()
    return SimpleNamespace(
        work_order=work_order,
        work_center=work_center,
        operations=operations,
        nests=nests,
        package=package,
        sheet=sheet,
        document=document,
    )


def build_brake_source(
    db: Session,
    *,
    company_id: int = COMPANY_A,
    quantity: float = 20.0,
    sequential: bool = False,
    **overrides,
):
    """A press-brake batch: two operations on ONE work center, run as a dispatch POOL.

    ``sequential_operations=False`` is the shape this feature is most often pointed at
    (the brake/weld-sub batches that promote together), and it is a column the copy
    carries — so it is built in rather than defaulted, and asserted on the way out.
    """
    work_center = make_work_center(db, company_id=company_id, wc_type="press_brake")
    part = make_part(db, company_id=company_id)
    work_order = make_work_order(
        db,
        company_id=company_id,
        part=part,
        quantity_ordered=quantity,
        status_value=WorkOrderStatus.COMPLETE,
        quantity_complete=quantity,
        sequential_operations=sequential,
        **overrides,
    )
    first = add_operation(
        db,
        work_order,
        work_center,
        company_id=company_id,
        sequence=10,
        operation_number="10",
        name="Brake A",
        run_time_hours=4.0,
        status=OperationStatus.COMPLETE,
        quantity_complete=quantity,
    )
    second = add_operation(
        db,
        work_order,
        work_center,
        company_id=company_id,
        sequence=20,
        operation_number="20",
        name="Brake B",
        run_time_hours=2.0,
        status=OperationStatus.COMPLETE,
        quantity_complete=quantity,
    )
    db.commit()
    return SimpleNamespace(
        work_order=work_order,
        work_center=work_center,
        operations=[first, second],
        part=part,
    )


def _every_column(row) -> dict:
    """Every mapped column of one row, by name.

    Column-by-column rather than a hand-picked field list ON PURPOSE. A hand-picked
    list only catches the mutations somebody already thought of: an early draft of this
    file listed seven header fields, and an injected ``source.notes += ...`` walked
    straight through it. ``version`` and ``updated_at`` are in here too and are the most
    useful members — they move whenever the row is flushed dirty at all, so they catch a
    write that happens to set a column back to the value it already had.
    """
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def job_snapshot(db: Session, work_order: WorkOrder) -> dict:
    """Everything about a job that a template must NOT change by pointing at it.

    Compared as a whole rather than field-by-field so a future column that starts getting
    mutated is caught by the diff without anyone remembering to add it — see
    :func:`_every_column`. Covers the header, its operations, its nests and its ties,
    because "the source is unchanged" is a claim about the JOB, not about one row.
    """
    db.expire_all()
    row = db.query(WorkOrder).filter(WorkOrder.id == work_order.id).one()
    return {
        "header": _every_column(row),
        "operations": {op.id: _every_column(op) for op in operations_of(db, row)},
        "nests": {nest.id: _every_column(nest) for nest in nests_of(db, row)},
        "ties": {tie.id: _every_column(tie) for tie in ties_of(db, row)},
    }


def template_row(db: Session, template_id: int) -> WorkOrderTemplate:
    db.expire_all()
    return db.query(WorkOrderTemplate).filter(WorkOrderTemplate.id == template_id).one()


def list_templates(client: TestClient, headers: dict, **params):
    return client.get(BASE, headers=headers, params=params or None)


# --------------------------------------------------------------------------- #
# A. THE DRAFT GUARANTEE — the reason this door exists at all
# --------------------------------------------------------------------------- #
class TestTheDraftGuarantee:
    """A template puts work in front of a PLANNER, never on the floor.

    Import Nest Package force-sets ``RELEASED``; this door does not. If a regression
    ever made a template's output land RELEASED — or its operations land READY — the
    planner's screen would look identical right up to the moment unreviewed work
    appeared at the laser, and the audit chain would explain nothing after the fact.
    """

    def test_a_nest_template_lands_a_draft_with_every_operation_pending(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        # The wire and the row are separate assertions: the planner's screen renders the
        # status badge off the envelope, so a response claiming DRAFT over a RELEASED row
        # (or the reverse) is exactly the failure that must not go unnoticed.
        assert response.json()["work_order"]["status"] == "draft"
        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert draft.id != source.work_order.id

        copied = operations_of(db_session, draft)
        assert len(copied) == 2
        assert [op.status for op in copied] == [OperationStatus.PENDING, OperationStatus.PENDING]
        # Non-vacuity: the SOURCE's laser operations are not PENDING, so "everything is
        # PENDING" is not simply what this fixture builds.
        assert {op.status for op in operations_of(db_session, source.work_order)} == {OperationStatus.COMPLETE}

    def test_it_contributes_zero_rows_to_the_dispatch_board_before_release(
        self, client: TestClient, db_session: Session
    ):
        """Asserted through the REAL queue query, and the mechanism is not what it looks like.

        ``queued_operations_query`` filters OPERATION status in (READY, IN_PROGRESS) and
        the work order not terminal / not deleted. It does **not** exclude DRAFT work
        orders. The zero rows below follow from the operations being born PENDING and
        nothing promoting a DRAFT — so this asserts the observable rather than a status
        list, and would still fail if some future path promoted a draft's operations.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)

        assert dispatch_rows_for(db_session, draft) == []
        # The board query itself is alive — a released job at the same work center DOES
        # come back — so the empty list above is the draft's doing, not a broken query.
        released = make_work_order(
            db_session,
            part=None,
            work_order_type="laser_cutting",
            quantity_ordered=1.0,
            status_value=WorkOrderStatus.RELEASED,
        )
        add_operation(db_session, released, source.work_center, sequence=10, status=OperationStatus.READY)
        db_session.commit()
        assert len(dispatch_rows_for(db_session, released)) == 1

    def test_after_release_the_draft_behaves_like_any_other_draft(self, client: TestClient, db_session: Session):
        """Release is the authorization step, and it is all that was missing.

        The draft is not a crippled work order — it is an unreleased one. So the same
        endpoint a planner uses on any draft promotes its laser nest operations to READY
        and puts them on the board, which is what makes "review, then release" a
        workflow rather than a dead end.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")
        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        assert dispatch_rows_for(db_session, draft) == []

        release = client.post(f"/api/v1/work-orders/{draft.id}/release", headers=headers_for(admin))
        assert release.status_code == status.HTTP_200_OK, release.text

        db_session.expire_all()
        promoted = operations_of(db_session, draft)
        assert [op.status for op in promoted] == [OperationStatus.READY, OperationStatus.READY]
        assert len(dispatch_rows_for(db_session, draft)) == 2
        # Released BY somebody, unlike the import door — the release stamp is the record
        # of who authorized production.
        db_session.refresh(draft)
        assert draft.status == WorkOrderStatus.RELEASED
        assert draft.released_by == admin.id
        assert draft.released_at is not None

    def test_a_press_brake_template_is_off_its_board_until_release_too(self, client: TestClient, db_session: Session):
        """The guarantee is not a laser special case.

        A pooled brake batch is the other shape this feature is pointed at, and its
        operations promote TOGETHER at release — so a draft that leaked would drop the
        whole batch onto one machine's board at once.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Bracket brake set")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)

        assert draft.status == WorkOrderStatus.DRAFT
        assert {op.status for op in operations_of(db_session, draft)} == {OperationStatus.PENDING}
        assert dispatch_rows_for(db_session, draft) == []

        release = client.post(f"/api/v1/work-orders/{draft.id}/release", headers=headers_for(admin))
        assert release.status_code == status.HTTP_200_OK, release.text
        db_session.expire_all()
        # Pooled: both same-work-center operations promote together, so the whole batch
        # lands on the brake board at once — which is precisely why it must not have
        # been there a moment ago.
        assert len(dispatch_rows_for(db_session, draft)) == 2


# --------------------------------------------------------------------------- #
# B. THE OTHER DOOR STILL RELEASES — the guard against a "helpful" alignment
# --------------------------------------------------------------------------- #
class TestImportStillReleases:
    """Import force-sets RELEASED, and that must stay true.

    This test exists to protect the *contrast*. Reading the draft guarantee above, the
    natural next thought is "shouldn't import be a draft too?" — and the laser cell's
    whole flow depends on the answer being no: a nest package is imported *because* the
    sheets are about to be cut, and a draft would strand the job off the board with
    nobody watching for it.

    Three sites in ``work_orders.py`` force ``RELEASED``; the manual-nest door is the
    cheapest to exercise and is the one a planner reaches by hand. Note what is NOT
    asserted: the import path force-sets the column, so ``released_by`` and
    ``released_at`` stay NULL and no ``log_status_change`` row is written. Asserting a
    release stamp here would fail against correct code.
    """

    def test_the_manual_nest_door_still_force_sets_released(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        make_work_center(db_session, wc_type="laser")
        # The subject is a template's own output: the clearest possible statement of the
        # difference between the two doors, on one work order.
        source = build_nest_source(db_session, runs=(3,))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")
        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        assert draft.status == WorkOrderStatus.DRAFT
        assert dispatch_rows_for(db_session, draft) == []

        response = client.post(
            f"/api/v1/work-orders/{draft.id}/laser-nests/manual",
            headers=headers_for(admin),
            json={"cnc_number": "05749", "planned_runs": 4, "material": "A36", "thickness": "0.250"},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text

        db_session.expire_all()
        db_session.refresh(draft)
        assert draft.status == WorkOrderStatus.RELEASED, "the import door must keep releasing"
        # Born READY and on the board immediately — the behavior the laser cell relies on.
        added = [op for op in operations_of(db_session, draft) if op.status == OperationStatus.READY]
        assert len(added) == 1
        assert added[0].id in {op.id for op in dispatch_rows_for(db_session, draft)}
        # NOT asserted as a release: the column is force-set, so there is no actor stamp
        # and no status-change row. Pinned so nobody "fixes" the absence.
        assert draft.released_by is None
        assert draft.released_at is None


# --------------------------------------------------------------------------- #
# C. NO PRODUCTION-RECORD LEAKAGE
# --------------------------------------------------------------------------- #
class TestNoProductionRecordLeakage:
    """Every field here fabricates history if it regresses, so each is asserted by name.

    A template is pointed at a job that RAN. Carrying one line of that run onto the copy
    produces a work order asserting, on an AS9100D-auditable record, that work happened
    which never happened — and unlike a wrong plan, nothing downstream contradicts it.
    """

    @pytest.fixture
    def finished_nest_job(self, db_session: Session):
        """A laser nest job that ran to completion, with a record on every column.

        It carries a ``parent_work_order_id`` on purpose: a laser WO cut for an assembly
        has one, and it is the shape that makes the ``parent_work_order_id is None``
        assertion below non-vacuous rather than true-by-construction.
        """
        admin = make_user(db_session)
        parent = make_work_order(db_session, part=make_part(db_session, part_type="assembly"))
        source = build_nest_source(
            db_session,
            runs=(3, 2),
            parent_work_order_id=parent.id,
            quantity_scrapped=1.0,
            actual_start=datetime(2026, 5, 4, 13, 0),
            actual_end=datetime(2026, 5, 9, 21, 30),
            scheduled_start=datetime(2026, 5, 4, 6, 0),
            scheduled_end=datetime(2026, 5, 10, 6, 0),
            due_date=date(2026, 5, 12),
            must_ship_by=date(2026, 5, 11),
            actual_hours=61.25,
            actual_cost=7420.0,
            lot_number="LOT-2026-0042",
            serial_numbers='["SN-001", "SN-002"]',
            unit_number="2410048",
            released_by=admin.id,
            released_at=datetime(2026, 5, 3, 8, 0),
        )
        source.work_order.current_operation_id = source.operations[0].id
        db_session.add(
            TimeEntry(
                company_id=COMPANY_A,
                user_id=admin.id,
                work_order_id=source.work_order.id,
                operation_id=source.operations[0].id,
                work_center_id=source.work_center.id,
                entry_type=TimeEntryType.RUN,
                clock_in=datetime(2026, 5, 4, 13, 0),
                clock_out=datetime(2026, 5, 4, 21, 0),
                duration_hours=8.0,
                quantity_produced=3.0,
            )
        )
        db_session.commit()
        return admin, source

    def test_the_header_starts_with_no_production_record(
        self, client: TestClient, db_session: Session, finished_nest_job
    ):
        admin, source = finished_nest_job
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)

        assert draft.status == WorkOrderStatus.DRAFT
        assert (draft.quantity_complete or 0) == 0
        assert (draft.quantity_scrapped or 0) == 0
        assert not draft.actual_start
        assert not draft.actual_end
        assert not (draft.actual_hours or 0)
        assert not (draft.actual_cost or 0)
        assert draft.lot_number is None
        assert draft.serial_numbers is None
        # Identity, not history: two live jobs both claiming to build unit 2410048 would
        # appear on the kiosk hero, the crew station and the TV wall at once, with
        # nothing able to say which one the welder is standing at.
        assert draft.unit_number is None
        assert draft.released_by is None
        assert draft.released_at is None
        assert draft.current_operation_id is None
        # SchedulingService OUTPUT for the SOURCE's run — release re-runs scheduling.
        assert draft.scheduled_start is None
        assert draft.scheduled_end is None
        # The original order's promise date, which outranks due_date in OTD/OTIF.
        assert draft.must_ship_by is None
        # An INDEPENDENT job: re-attaching it to the source's assembly parent would put a
        # second child against demand the first child already satisfied.
        assert draft.parent_work_order_id is None
        assert source.work_order.parent_work_order_id is not None, "non-vacuity: the source HAS a parent"

    def test_no_labour_follows_the_copy(self, client: TestClient, db_session: Session, finished_nest_job):
        """Labour is the record of what happened, and it happened on the source."""
        admin, source = finished_nest_job
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        assert db_session.query(TimeEntry).filter(TimeEntry.work_order_id == draft.id).count() == 0
        assert db_session.query(TimeEntry).filter(TimeEntry.work_order_id == source.work_order.id).count() == 1

    def test_the_operations_start_pending_with_no_record_of_their_own(
        self, client: TestClient, db_session: Session, finished_nest_job
    ):
        admin, source = finished_nest_job
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        for copied in operations_of(db_session, draft):
            assert copied.status == OperationStatus.PENDING
            assert (copied.quantity_complete or 0) == 0
            assert (copied.quantity_scrapped or 0) == 0
            assert not copied.actual_start
            assert not copied.actual_end
            assert copied.started_by is None
            assert copied.completed_by is None
            # The manager's rank for ONE machine's board. A pre-ranked batch arriving at
            # release displaces the sequence the manager set.
            assert copied.run_order is None

    def test_a_standalone_nest_source_produces_a_standalone_draft(self, client: TestClient, db_session: Session):
        """A part-less nest WO stays part-less and parent-less. It is a sheet-run job.

        ``ck_work_orders_part_required_unless_laser`` makes the NULL part legal here, and
        inventing a produced part (or a parent) on the copy would be the template
        deciding something no planner asked for.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(4,))
        assert source.work_order.part_id is None and source.work_order.parent_work_order_id is None
        template_id = saved(client, headers_for(admin), source.work_order.id, "Standalone sheets")

        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        assert draft.part_id is None
        assert draft.parent_work_order_id is None
        assert draft.work_order_type == "laser_cutting"


# --------------------------------------------------------------------------- #
# D. THE PLAN CARRIES
# --------------------------------------------------------------------------- #
class TestThePlanCarries:
    """The other half of the asymmetry: everything that describes the WORK comes across."""

    def test_the_nests_carry_their_program_and_run_counts_and_start_unrun(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        copied = nests_of(db_session, draft)
        assert len(copied) == 2
        assert sorted(nest.planned_runs for nest in copied) == [2, 3]
        assert sorted(nest.cnc_number for nest in copied) == sorted(nest.cnc_number for nest in source.nests)
        assert {nest.material for nest in copied} == {"A36"}
        # The plan says how many runs; the record says how many happened, and none have.
        assert all((nest.completed_runs or 0) == 0 for nest in copied)
        assert all(nest.remaining_runs == nest.planned_runs for nest in copied)
        # Fresh nest rows on a fresh package, sharing the SOURCE's drawing rather than
        # re-uploading it.
        assert {nest.id for nest in copied}.isdisjoint({nest.id for nest in source.nests})
        assert {nest.document_id for nest in copied} == {source.document.id}
        assert {nest.package_id for nest in copied} != {source.package.id}

    def test_a_nest_templates_quantity_is_the_sum_of_the_planned_runs(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id)
        draft = created_work_order(db_session, response)

        assert draft.quantity_ordered == 5.0
        assert float(response.json()["work_order"]["quantity_ordered"]) == 5.0

    def test_open_ties_carry_unpinned_and_unconsumed(self, client: TestClient, db_session: Session):
        """The pinned lot the source consumed is the one lot the copy must not point at.

        A pin says "consume from THIS lot", and the source job very likely drew it down to
        zero. Unpinned means FIFO picks at consume time — the right default for work that
        has not started. ``qty_consumed`` resets for the same reason: the ledger is the
        record, not this cache.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3,))
        lot = InventoryItem(
            company_id=COMPANY_A,
            part_id=source.sheet.id,
            location="RACK-N-04",
            lot_number="HEAT-88213",
            quantity_on_hand=0.0,
        )
        db_session.add(lot)
        db_session.commit()
        [source_tie] = ties_of(db_session, source.work_order)
        source_tie.pinned_inventory_item_id = lot.id
        source_tie.pinned_lot_number = "HEAT-88213"
        db_session.commit()
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        [tie] = ties_of(db_session, draft)
        assert tie.part_id == source.sheet.id
        assert tie.status == AllocationStatus.OPEN
        assert (tie.qty_consumed or 0) == 0.0
        assert tie.pinned_inventory_item_id is None
        assert tie.pinned_lot_number is None
        assert tie.work_order_operation_id in {op.id for op in operations_of(db_session, draft)}

    def test_a_cancelled_tie_is_not_copied_and_is_not_counted_in_the_plan(
        self, client: TestClient, db_session: Session
    ):
        """A cancelled tie is a tombstone. Counting it would promise material the copy
        does not carry — and the plan summary must count exactly what the copy copies."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3,))
        scrap = make_part(db_session, part_type="raw_material", uom="sheets")
        make_tie(
            db_session,
            source.work_order,
            scrap,
            operation=source.operations[0],
            qty_per_run=1.0,
            tie_status=AllocationStatus.CANCELLED,
        )
        db_session.commit()

        response = save_template(client, headers_for(admin), source.work_order.id, "Nest group")
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["plan"]["open_material_tie_count"] == 1

        draft = created_work_order(db_session, use_template(client, headers_for(admin), response.json()["id"]))
        assert [tie.part_id for tie in ties_of(db_session, draft)] == [source.sheet.id]

    def test_a_pooled_brake_template_carries_the_pool_flag_and_its_work_centers(
        self, client: TestClient, db_session: Session
    ):
        """``sequential_operations`` is per-work-order and it is PLAN, so it carries.

        Dropping it would silently convert a brake batch — whose operations are meant to
        promote together and be mutually startable — into a sequenced routing, and the
        floor would find the second item unstartable with nothing saying why.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0, sequential=False)

        response = save_template(client, headers_for(admin), source.work_order.id, "Bracket brake set")
        assert response.status_code == status.HTTP_201_CREATED, response.text
        plan = response.json()["plan"]
        assert plan["sequential_operations"] is False
        assert plan["work_order_type"] == "production"
        assert plan["work_centers"] == [source.work_center.name]
        assert plan["operation_count"] == 2
        assert plan["nest_count"] == 0 and plan["planned_runs_total"] == 0

        draft = created_work_order(db_session, use_template(client, headers_for(admin), response.json()["id"]))
        assert draft.sequential_operations is False
        copied = operations_of(db_session, draft)
        assert {op.work_center_id for op in copied} == {source.work_center.id}
        assert [op.name for op in copied] == ["Brake A", "Brake B"]
        assert [op.operation_number for op in copied] == ["10", "20"]

    def test_a_sequenced_routing_carries_its_sequencing_too(self, client: TestClient, db_session: Session):
        """The contrast that makes the previous test a carried VALUE rather than a default."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0, sequential=True)

        response = save_template(client, headers_for(admin), source.work_order.id, "Sequenced set")
        assert response.json()["plan"]["sequential_operations"] is True

        draft = created_work_order(db_session, use_template(client, headers_for(admin), response.json()["id"]))
        assert draft.sequential_operations is True


# --------------------------------------------------------------------------- #
# E. THE SOURCE IS UNCHANGED — a stated acceptance criterion
# --------------------------------------------------------------------------- #
class TestTheSourceIsUnchanged:
    """ "Save as template" reads like an action ON the work order. It is not one.

    Both verbs only READ the exemplar. That matters beyond tidiness: the source is
    usually a COMPLETE job whose quantities, ties and nest run counts are the record of
    what the shop actually built and consumed — so a template that nudged any of them
    would be rewriting a production record from a catalog screen.
    """

    def test_saving_a_template_changes_nothing_on_the_source(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        before = job_snapshot(db_session, source.work_order)

        response = save_template(client, headers_for(admin), source.work_order.id, "Nest group", notes="3 sheets")
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert job_snapshot(db_session, source.work_order) == before

    def test_using_a_template_changes_nothing_on_the_source(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")
        before = job_snapshot(db_session, source.work_order)

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=99)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        after = job_snapshot(db_session, source.work_order)
        assert after == before
        # Spelled out, because these are the four that would be most plausible to touch:
        # a completed job stays complete, its ties stay consumed and pinned, its nests
        # stay run.
        assert after["header"]["status"] == WorkOrderStatus.COMPLETE
        assert after["header"]["quantity_complete"] == 5.0
        assert all(tie["qty_consumed"] > 0 for tie in after["ties"].values())
        assert all(nest["completed_runs"] > 0 for nest in after["nests"].values())

    def test_using_a_template_twice_leaves_two_independent_jobs_and_one_untouched_source(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Bracket brake set")
        before = job_snapshot(db_session, source.work_order)

        first = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        second = created_work_order(db_session, use_template(client, headers_for(admin), template_id))

        assert first.id != second.id
        assert first.work_order_number != second.work_order_number
        assert {op.id for op in operations_of(db_session, first)}.isdisjoint(
            {op.id for op in operations_of(db_session, second)}
        )
        assert job_snapshot(db_session, source.work_order) == before


# --------------------------------------------------------------------------- #
# F. QUANTITY RESOLUTION
# --------------------------------------------------------------------------- #
class TestQuantityResolution:
    """Request -> template default -> source quantity, first POSITIVE wins.

    The fallback chain is what makes the click-once case click-once, and the ORDER is
    what keeps a planner's typed number from being quietly overridden by a saved
    prefill.
    """

    def test_the_requested_quantity_wins_over_both_fallbacks(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set", default_quantity=50)

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=7)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert created_work_order(db_session, response).quantity_ordered == 7.0

    def test_the_saved_default_is_used_when_the_request_carries_none(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set", default_quantity=50)

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        draft = created_work_order(db_session, response)
        assert draft.quantity_ordered == 50.0
        # And the plan numbers scale with it — 20 -> 50 is x2.5 on the stored run hours.
        assert sorted(round(op.run_time_hours, 3) for op in operations_of(db_session, draft)) == [5.0, 10.0]

    def test_the_sources_own_quantity_is_the_last_resort(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        assert template_row(db_session, template_id).default_quantity is None

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert created_work_order(db_session, response).quantity_ordered == 20.0

    @pytest.mark.unit
    def test_no_positive_quantity_anywhere_is_a_422_naming_the_template(self):
        """Refused rather than defaulted to 1 — a fabricated quantity is a plan nobody approved.

        Exercised on the pure resolver rather than through the API on purpose: this DB
        carries ``chk_work_orders_quantity_ordered_positive`` and the column is NOT NULL,
        so a source work order with a non-positive quantity cannot be created here at
        all. The branch is defence for legacy rows that predate the constraint, and the
        function is the only place it is reachable. (Same precedent as the duplicate
        suite's zero-source-quantity test.)
        """
        template = SimpleNamespace(name="Legacy set", default_quantity=None)
        source = SimpleNamespace(work_order_number="WO-LEGACY-1", quantity_ordered=0.0)

        with pytest.raises(Exception) as excinfo:
            templates._resolve_quantity(template, source, None)

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "Legacy set" in excinfo.value.detail
        assert "WO-LEGACY-1" in excinfo.value.detail

    @pytest.mark.unit
    def test_a_non_positive_candidate_never_wins_it_falls_through(self):
        """ "Positive to win", not "present to win" — a zero must not shadow a good fallback."""
        template = SimpleNamespace(name="Set", default_quantity=0.0)
        source = SimpleNamespace(work_order_number="WO-1", quantity_ordered=12.0)

        assert templates._resolve_quantity(template, source, None) == 12.0
        assert templates._resolve_quantity(template, source, -4.0) == 12.0

    def test_a_non_positive_requested_quantity_is_refused_at_the_data_boundary(
        self, client: TestClient, db_session: Session
    ):
        """The schema's ``gt=0`` means a zero never reaches the resolver from the wire."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        before = db_session.query(WorkOrder).count()

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=0)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == before

    def test_a_nest_template_overrules_the_resolved_quantity_with_the_derived_sum(
        self, client: TestClient, db_session: Session
    ):
        """A laser WO's quantity is DEFINED as the sum of its nests' planned runs.

        So the resolved value is sent, overruled, and the RESPONSE reports what was
        stored — which is why the contract says to quote the quantity off the response
        and never off the form. Anything else would make this the one laser work order in
        the system where the definition is false, until the next nest edit "corrects" it
        out from under the planner.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group", default_quantity=40)

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=999)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        draft = created_work_order(db_session, response)
        assert draft.quantity_ordered == 5.0, "derived from the copied nests, not the request"
        assert float(response.json()["work_order"]["quantity_ordered"]) == 5.0
        assert sorted(nest.planned_runs for nest in nests_of(db_session, draft)) == [2, 3]

    def test_the_overruled_request_is_recorded_on_the_chain(self, client: TestClient, db_session: Session):
        """The planner asked for 999 and got 5. The chain is where that is reconcilable."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=999)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "USE_TEMPLATE"]
        assert row.extra_data["quantity"] == 5.0
        assert row.extra_data["requested_quantity"] == 999.0

    def test_requested_quantity_is_absent_when_the_two_agree(self, client: TestClient, db_session: Session):
        """Recorded only on a discrepancy — same key and same condition the duplicate uses."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=5)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "USE_TEMPLATE"]
        assert row.extra_data["quantity"] == 5.0
        assert "requested_quantity" not in row.extra_data


# --------------------------------------------------------------------------- #
# G. DUE DATE
# --------------------------------------------------------------------------- #
class TestDueDate:
    """Never inherited. A stale date reads as "late"; no date reads as "not promised yet"."""

    def test_the_sources_due_date_is_never_inherited(self, client: TestClient, db_session: Session):
        """Carrying it forward would make the new job overdue the moment it exists — red
        on the dispatch board and counted against OTD — for a promise nobody made."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0, due_date=date(2026, 5, 12))
        assert source.work_order.due_date == date(2026, 5, 12)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert created_work_order(db_session, response).due_date is None
        assert response.json()["work_order"]["due_date"] is None

    def test_an_explicitly_supplied_due_date_is_stored(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0, due_date=date(2026, 5, 12))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")

        response = use_template(client, headers_for(admin), template_id, due_date="2026-09-30")
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert created_work_order(db_session, response).due_date == date(2026, 9, 30)
        assert response.json()["work_order"]["due_date"] == "2026-09-30"

    def test_a_due_date_in_the_past_is_accepted(self, client: TestClient, db_session: Session):
        """No "not in the past" validator, matching ``WorkOrderDuplicateRequest``.

        A template is most often reached for to re-run something that is ALREADY late,
        and refusing the honest date would only push planners into typing a fictional
        one.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")

        response = use_template(client, headers_for(admin), template_id, due_date="2020-01-06")
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert created_work_order(db_session, response).due_date == date(2020, 1, 6)


# --------------------------------------------------------------------------- #
# H. REFUSALS PROPAGATE UNTOUCHED
# --------------------------------------------------------------------------- #
class TestRefusalsPropagateUntouched:
    """A template adds a NAME, not authority.

    Every gate the copy engine enforces must still bite through this door, or the
    catalog becomes a one-click way around a refusal a planner would have hit by hand.
    """

    def test_a_retired_produced_part_refuses_409_and_writes_nothing(self, client: TestClient, db_session: Session):
        """Saving is still allowed — the part may be back next month, and the save dialog
        cannot show the planner the cause. The refusal lands at USE, where it can."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        source.part.is_deleted = True
        db_session.commit()
        work_orders_before = db_session.query(WorkOrder).count()
        audit_before = db_session.query(AuditLog).count()

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert source.part.part_number in response.json()["detail"]

        db_session.rollback()
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == work_orders_before
        assert db_session.query(AuditLog).count() == audit_before, "no chain row for a job that does not exist"

    def test_a_process_sheet_family_with_no_released_revision_refuses_409(
        self, client: TestClient, db_session: Session
    ):
        """Structured ``PROCESS_SHEET_UNAVAILABLE``, byte-identical to the duplicate's.

        A refusal rather than a skip, and this is the highest-consequence refusal in the
        set: ``missing_required_steps`` returns "complete freely" for an operation with
        zero snapshot steps, so a copy that silently carried none would disarm the
        completion gate on a job whose entire premise is "same plan as last time" — no
        measurement, no SPC point, no gauge attribution, no OOT->NCR path, and no record
        that it happened.
        """
        admin = make_user(db_session)
        work_center = make_work_center(db_session, wc_type="machining")
        part = make_part(db_session)
        source = make_work_order(db_session, part=part, quantity_ordered=10.0)
        operation = add_operation(db_session, source, work_center, sequence=10, operation_number="OP10")
        sheet = make_process_sheet(
            db_session,
            revision="A",
            steps=[{"label": "Bore dia", "step_type": "measurement", "config": dict(MEASUREMENT_CONFIG)}],
        )
        snapshot_onto(db_session, operation, sheet)
        template_id = saved(client, headers_for(admin), source.id, "Machined set")
        # The shop obsoleted Rev A and has not released a replacement.
        sheet.status = "obsolete"
        db_session.commit()
        work_orders_before = db_session.query(WorkOrder).count()

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "PROCESS_SHEET_UNAVAILABLE"
        assert detail["sheet_number"] == sheet.sheet_number
        assert detail["operation"] == "OP10"

        db_session.rollback()
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == work_orders_before

    # THE ONE DOCUMENTED EXCEPTION TO THIS CLASS'S RULE.
    #
    # A deleted source used to be a refusal that propagated like the others, and the
    # owner overrode it (2026-08-27): "templates need to stay even if there is no work
    # order present for it". The two tests below stay in this class deliberately —
    # this is where anyone looking for that refusal comes to find it, and finding it
    # gone WITH the reason beats finding nothing. It is not a hole in the "a template
    # adds a name, not authority" rule: nothing the duplicate path would refuse is
    # admitted, because ``duplicate_work_order`` never asked whether the source was
    # deleted in the first place. What is refused instead is the other end — saving a
    # NEW template from a deleted work order, still 404 below.

    def test_a_deleted_source_is_disclosed_on_the_summary_and_does_not_disable_the_template(
        self, client: TestClient, db_session: Session
    ):
        """Deleting the job a template was saved from changes exactly ONE field.

        The summary is still computed in full off the tombstoned source — the read
        deliberately carries no ``is_deleted`` predicate, the historical-record
        exception to invariant 3 argued in the service module docstring — so the whole
        plan the planner picks by is still there.

        Asserted as a DIFF against the live reading rather than by restating the
        counts. A summary that silently went blank over a tombstone would still satisfy
        "available is true", and the planner would discover it by pressing Use and
        getting an empty draft.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Doomed set", notes="keep the name")

        alive = client.get(f"{BASE}/{template_id}", headers=headers_for(admin)).json()["plan"]
        assert alive["available"] is True and alive["source_work_order_deleted"] is False
        assert alive["operation_count"] > 0, "the diff below is only meaningful against a populated summary"

        source.work_order.soft_delete(admin.id)
        db_session.commit()

        listing = list_templates(client, headers_for(admin))
        assert listing.status_code == status.HTTP_200_OK, listing.text
        [row] = [entry for entry in listing.json()["templates"] if entry["id"] == template_id]
        assert row["name"] == "Doomed set", "the name and note survive — they are the curated part"
        assert row["notes"] == "keep the name"

        plan = row["plan"]
        assert plan["available"] is True, "a deleted source is a disclosure, not a refusal"
        assert plan["unavailable_reason"] is None
        assert plan["source_work_order_deleted"] is True
        assert plan == {**alive, "source_work_order_deleted": True}, "only the disclosure flag moved"

        detail = client.get(f"{BASE}/{template_id}", headers=headers_for(admin))
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["plan"] == plan, "one resolver, so the detail read and the list read agree"

    def test_a_template_whose_source_is_deleted_still_produces_a_draft(self, client: TestClient, db_session: Session):
        """The catalog entry keeps working. This used to pin a 409; the owner removed it.

        The list flag and the use path still come from ONE resolver, so they still
        cannot disagree — the resolver now reads through the tombstone for both
        answers. What this pins is that the copy is genuinely unaffected (the duplicate
        service copies the object it is handed and never inspected the source's
        tombstone), that the DRAFT guarantee holds over a deleted source, and that
        using a template is not a back-door restore of the job it points at.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Doomed set")
        source.work_order.soft_delete(admin.id)
        db_session.commit()

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert draft.is_deleted is False, "the copy is a live job, not an inherited tombstone"
        assert len(operations_of(db_session, draft)) == len(operations_of(db_session, source.work_order))

        db_session.refresh(source.work_order)
        assert source.work_order.is_deleted is True, "using a template does not restore the source"

    def test_saving_a_template_from_a_deleted_work_order_is_404(self, client: TestClient, db_session: Session):
        """A tombstoned source is not a catalogable job — and this is now the ASYMMETRY test.

        An already-saved template reads THROUGH a soft-deleted source (the two tests
        above); creating a new one over it is refused, because picking a work order to
        catalogue is SELECTION and invariant 3 gates selection. The filter lives in
        ``resolve_catalogable_work_order``, deliberately not in the read-through
        ``resolve_source_work_order`` — ``tenant_query`` scopes ``company_id`` and
        nothing else, so it is explicit there.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        source.work_order.soft_delete(admin.id)
        db_session.commit()

        response = save_template(client, headers_for(admin), source.work_order.id, "Never saved")
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
        assert db_session.query(WorkOrderTemplate).count() == 0


# --------------------------------------------------------------------------- #
# I. THE SKIP ENVELOPE
# --------------------------------------------------------------------------- #
class TestTheSkipEnvelope:
    """A draft that is VALID but missing something the source had.

    This is the case that is dangerous precisely because it succeeds. A skipped material
    tie means the new job carries NO demand for that material: no shortage is raised,
    the work runs, and stock is never deducted. So the omission has to reach the planner
    on the same result view the Duplicate dialog renders — the same envelope, the same
    keys — and not only the audit chain.
    """

    def _source_with_one_dead_tie(self, db: Session):
        source = build_nest_source(db, runs=(3,))
        gone = make_part(db, part_type="raw_material", uom="sheets", is_deleted=True)
        orphaned = make_tie(
            db,
            source.work_order,
            gone,
            operation=source.operations[0],
            qty_per_run=1.0,
            unit_of_measure="sheets",
        )
        db.commit()
        return source, gone, orphaned

    def test_a_tie_on_a_deleted_part_is_reported_and_the_draft_is_still_created(
        self, client: TestClient, db_session: Session
    ):
        admin = make_user(db_session)
        source, gone, orphaned = self._source_with_one_dead_tie(db_session)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        # The SAME envelope keys the duplicate endpoint returns.
        assert set(body) == {"work_order", "skipped_operations", "skipped_material_allocations"}
        assert body["skipped_operations"] == []
        assert body["skipped_material_allocations"] == [
            {
                "source_allocation_id": orphaned.id,
                "part_id": gone.id,
                "source_work_order_operation_id": source.operations[0].id,
                "reason": "part_not_available",
            }
        ]

        # The job exists and is otherwise whole — that is what makes the notice load-bearing.
        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert [tie.part_id for tie in ties_of(db_session, draft)] == [source.sheet.id]
        assert len(nests_of(db_session, draft)) == 1

    def test_the_skip_reaches_the_audit_chain_with_the_same_shape(self, client: TestClient, db_session: Session):
        """``model_dump()`` of the very objects the endpoint returns, so the chain and the
        planner's screen cannot describe an omission differently."""
        admin = make_user(db_session)
        source, gone, orphaned = self._source_with_one_dead_tie(db_session)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "USE_TEMPLATE"]
        assert row.extra_data["skipped_material_allocations"] == response.json()["skipped_material_allocations"]
        assert row.extra_data["skipped_operations"] == []
        assert row.extra_data["material_allocation_count"] == 1

    def test_an_operation_whose_nest_is_dead_is_skipped_and_named(self, client: TestClient, db_session: Session):
        """Copying it would put a nest task with no nest on the kiosk queue."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2), tie_material=False)
        source.nests[1].is_deleted = True
        db_session.commit()
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert [entry["source_operation_id"] for entry in body["skipped_operations"]] == [source.operations[1].id]
        assert body["skipped_operations"][0]["reason"] == "laser_nest_deleted"
        draft = created_work_order(db_session, response)
        assert len(operations_of(db_session, draft)) == 1
        # The dead nest is excluded from the derived quantity too, not merely uncopied.
        assert draft.quantity_ordered == 3.0


# --------------------------------------------------------------------------- #
# J. Invariant 1 — tenant isolation
# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    """404 on every verb, never 403 — a 403 confirms the id exists somewhere."""

    def _template_in_company_a(self, client: TestClient, db: Session) -> int:
        owner = make_user(db, company_id=COMPANY_A)
        source = build_brake_source(db, company_id=COMPANY_A, quantity=10.0)
        return saved(client, headers_for(owner), source.work_order.id, "A's brake set")

    def test_another_companys_template_is_absent_from_the_list(self, client: TestClient, db_session: Session):
        template_id = self._template_in_company_a(client, db_session)
        intruder = make_user(db_session, company_id=COMPANY_B)

        listing = list_templates(client, headers_for(intruder))
        assert listing.status_code == status.HTTP_200_OK, listing.text
        assert listing.json()["total"] == 0
        assert listing.json()["templates"] == []
        # The row is really there — company A still sees it.
        assert db_session.query(WorkOrderTemplate).filter(WorkOrderTemplate.id == template_id).one().company_id == 1

    @pytest.mark.parametrize(
        "verb,path_suffix,body",
        [
            ("get", "", None),
            ("put", "", {"name": "hijacked"}),
            ("delete", "", None),
            ("post", "/use", {}),
        ],
    )
    def test_every_verb_answers_404_across_the_tenant_boundary(
        self, client: TestClient, db_session: Session, verb: str, path_suffix: str, body
    ):
        template_id = self._template_in_company_a(client, db_session)
        intruder = make_user(db_session, company_id=COMPANY_B)
        work_orders_before = db_session.query(WorkOrder).count()

        response = client.request(
            verb.upper(),
            f"{BASE}/{template_id}{path_suffix}",
            headers=headers_for(intruder),
            json=body,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

        db_session.expire_all()
        row = db_session.query(WorkOrderTemplate).filter(WorkOrderTemplate.id == template_id).one()
        assert row.name == "A's brake set", "no cross-tenant write may land"
        assert row.is_deleted is False
        assert db_session.query(WorkOrder).count() == work_orders_before

    def test_naming_another_companys_work_order_on_create_is_404(self, client: TestClient, db_session: Session):
        """The save path resolves the source company-scoped, so a foreign id is simply absent."""
        _ensure_company(db_session, COMPANY_B)
        foreign = build_brake_source(db_session, company_id=COMPANY_A, quantity=10.0)
        intruder = make_user(db_session, company_id=COMPANY_B)

        response = save_template(client, headers_for(intruder), foreign.work_order.id, "Borrowed plan")
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
        assert response.json()["detail"] == "Work order not found"
        assert db_session.query(WorkOrderTemplate).count() == 0

    def test_every_row_a_use_creates_is_tagged_with_the_active_company(self, client: TestClient, db_session: Session):
        caller = make_user(db_session, company_id=COMPANY_B)
        source = build_nest_source(db_session, company_id=COMPANY_B, runs=(3,))
        template_id = saved(client, headers_for(caller), source.work_order.id, "B's nest group")
        assert template_row(db_session, template_id).company_id == COMPANY_B

        response = use_template(client, headers_for(caller), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)

        assert draft.company_id == COMPANY_B
        assert {op.company_id for op in operations_of(db_session, draft)} == {COMPANY_B}
        assert {nest.company_id for nest in nests_of(db_session, draft)} == {COMPANY_B}
        assert {tie.company_id for tie in ties_of(db_session, draft)} == {COMPANY_B}
        assert all(row.company_id == COMPANY_B for row in audit_rows(db_session, "work_order_template", template_id))


# --------------------------------------------------------------------------- #
# K. RBAC
# --------------------------------------------------------------------------- #
class TestRoleGate:
    """The duplicate endpoint's own trio, and nothing wider.

    A template must not admit anyone the create path would not — the frontend gates every
    control on ``work_orders:edit``, which maps to exactly this set, so a hidden button
    and a refused call agree.

    PLATFORM_ADMIN and ``is_superuser`` are deliberately absent from the refused set:
    ``require_role`` short-circuits True for both before it ever looks at the allowed
    list, so parametrizing them in would assert a refusal the dependency cannot produce.
    """

    ALLOWED = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
    REFUSED = [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY, UserRole.SHIPPING]

    @pytest.mark.parametrize("role", ALLOWED)
    def test_a_planning_role_may_save_and_use(self, client: TestClient, db_session: Session, role: UserRole):
        user = make_user(db_session, role=role)
        source = build_brake_source(db_session, quantity=10.0)

        template_id = saved(client, headers_for(user), source.work_order.id, f"Set for {role.value}")
        response = use_template(client, headers_for(user), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert created_work_order(db_session, response).status == WorkOrderStatus.DRAFT

    @pytest.mark.parametrize("role", REFUSED)
    def test_every_other_role_is_refused_on_every_verb(self, client: TestClient, db_session: Session, role: UserRole):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        refused = make_user(db_session, role=role)
        headers = headers_for(refused)
        work_orders_before = db_session.query(WorkOrder).count()

        calls = [
            client.get(BASE, headers=headers),
            client.get(f"{BASE}/{template_id}", headers=headers),
            save_template(client, headers, source.work_order.id, "Sneaky set"),
            client.put(f"{BASE}/{template_id}", headers=headers, json={"name": "Renamed"}),
            client.delete(f"{BASE}/{template_id}", headers=headers),
            use_template(client, headers, template_id),
        ]
        assert [response.status_code for response in calls] == [status.HTTP_403_FORBIDDEN] * 6, [
            response.text for response in calls
        ]

        db_session.expire_all()
        row = template_row(db_session, template_id)
        assert row.name == "Brake set" and row.is_deleted is False
        assert db_session.query(WorkOrderTemplate).count() == 1
        assert db_session.query(WorkOrder).count() == work_orders_before

    def test_an_unauthenticated_caller_is_refused(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")

        for response in (
            client.get(BASE),
            client.post(f"{BASE}/{template_id}/use", json={}),
        ):
            assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


# --------------------------------------------------------------------------- #
# L. Invariant 2 — the audit chain
# --------------------------------------------------------------------------- #
class TestAuditChain:
    """The row in ``work_order_templates`` is a convenience index; the chain is the record.

    USE in particular writes a SECOND row, against the TEMPLATE rather than the work
    order: the duplicate service already wrote the work order's own CREATE row naming its
    source work order, and this one is the only place the fact that a CATALOG ENTRY (not
    a planner browsing the list) produced this job exists at all.
    """

    def test_saving_writes_a_create_row_naming_the_source(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)

        response = save_template(
            client, headers_for(admin), source.work_order.id, "Bracket brake set", default_quantity=50
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        template_id = response.json()["id"]

        [row] = audit_rows(db_session, "work_order_template", template_id)
        assert row.action == "CREATE"
        assert row.user_id == admin.id
        assert row.company_id == COMPANY_A
        assert row.resource_identifier == "Bracket brake set"
        assert source.work_order.work_order_number in row.description
        assert row.extra_data["source"] == "work_order_template_save"
        assert row.extra_data["source_work_order_id"] == source.work_order.id
        assert row.extra_data["source_work_order_number"] == source.work_order.work_order_number
        assert row.extra_data["default_quantity"] == 50.0

    def test_using_writes_a_use_template_row_naming_the_work_order_it_made(
        self, client: TestClient, db_session: Session
    ):
        """ "Which template made this job" and "how often do we run this template" are both
        answerable from the chain, or from nothing."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "USE_TEMPLATE"]
        assert row.resource_id == template_id
        assert row.resource_identifier == "Miratech nest group"
        assert row.user_id == admin.id
        assert row.extra_data["source"] == "work_order_template_use"
        assert row.extra_data["template_id"] == template_id
        assert row.extra_data["template_name"] == "Miratech nest group"
        assert row.extra_data["created_work_order_id"] == draft.id
        assert row.extra_data["created_work_order_number"] == draft.work_order_number
        # The DRAFT promise, stamped on the chain as well as on the row.
        assert row.extra_data["created_work_order_status"] == "draft"
        assert row.extra_data["source_work_order_id"] == source.work_order.id
        assert row.extra_data["operation_count"] == 2
        assert row.extra_data["laser_nest_count"] == 2
        assert row.extra_data["material_allocation_count"] == 2
        assert draft.work_order_number in row.description

        # The duplicate service's own work-order CREATE row is still written — the two
        # rows answer different questions and neither replaces the other.
        [work_order_row] = audit_rows(db_session, "work_order", draft.id)
        assert work_order_row.action == "CREATE"
        assert work_order_row.extra_data["source"] == "work_order_duplicate"

    def test_using_twice_leaves_two_use_rows(self, client: TestClient, db_session: Session):
        """The run count is the point — a single row would answer "was it ever used", not
        "how often"."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")

        use_template(client, headers_for(admin), template_id)
        use_template(client, headers_for(admin), template_id)

        rows = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "USE_TEMPLATE"]
        assert len(rows) == 2
        assert len({r.extra_data["created_work_order_id"] for r in rows}) == 2

    def test_renaming_writes_an_update_row_carrying_both_values(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Old name", notes="original")

        response = client.put(
            f"{BASE}/{template_id}", headers=headers_for(admin), json={"name": "New name", "notes": "revised"}
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "UPDATE"]
        assert row.old_values["name"] == "Old name"
        assert row.new_values["name"] == "New name"
        assert row.old_values["notes"] == "original"
        assert row.new_values["notes"] == "revised"
        assert row.resource_identifier == "New name"

    def test_deleting_writes_a_soft_delete_row_that_says_the_name_is_free(
        self, client: TestClient, db_session: Session
    ):
        """Recorded so a chain reader can tell a LATER template of the same name from this one."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Retired set")

        response = client.delete(f"{BASE}/{template_id}", headers=headers_for(admin))
        assert response.status_code == status.HTTP_200_OK, response.text

        [row] = [r for r in audit_rows(db_session, "work_order_template", template_id) if r.action == "DELETE"]
        assert row.resource_identifier == "Retired set"
        assert row.extra_data["soft_delete"] is True
        assert row.extra_data["name_released_for_reuse"] is True
        assert row.extra_data["source_work_order_id"] == source.work_order.id

    def test_every_template_row_goes_through_the_hash_chain(self, client: TestClient, db_session: Session):
        """A direct INSERT could not produce a sequence number or a chain hash.

        ``AuditService`` is the only writer that allocates ``sequence_number`` and computes
        ``integrity_hash``/``previous_hash``, so a row lacking them did not go through it.
        (The DB-level 008/060 triggers already refuse UPDATE/DELETE; this pins the INSERT
        side for the rows this feature adds.)
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Chained set")
        client.put(f"{BASE}/{template_id}", headers=headers_for(admin), json={"name": "Chained set v2"})
        use_template(client, headers_for(admin), template_id)
        client.delete(f"{BASE}/{template_id}", headers=headers_for(admin))

        rows = audit_rows(db_session, "work_order_template", template_id)
        assert {row.action for row in rows} == {"CREATE", "UPDATE", "USE_TEMPLATE", "DELETE"}
        for row in rows:
            assert row.sequence_number is not None
            assert row.integrity_hash and len(row.integrity_hash) == 64
            assert row.company_id == COMPANY_A
            assert row.user_id == admin.id


# --------------------------------------------------------------------------- #
# M. Invariant 3 — soft delete, and the partial index that frees the name
# --------------------------------------------------------------------------- #
class TestSoftDeleteAndNameReuse:
    """Deleting a template removes a name from a picker and nothing else.

    The unique index is PARTIAL (``WHERE NOT is_deleted``), declared for BOTH dialects so
    this SQLite suite enforces exactly what Postgres does. That is what frees the name
    immediately instead of forcing the planner to invent "Miratech nest group 2" — and it
    is why the duplicate-name probe reads LIVE rows only, the one place in this system
    where a duplicate probe deliberately does NOT match tombstones.
    """

    def _source(self, db: Session):
        return build_brake_source(db, quantity=10.0)

    def test_deleting_frees_the_name_immediately(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = self._source(db_session)
        first = saved(client, headers_for(admin), source.work_order.id, "Reusable")

        assert client.delete(f"{BASE}/{first}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
        again = save_template(client, headers_for(admin), source.work_order.id, "Reusable")

        assert again.status_code == status.HTTP_201_CREATED, again.text
        assert again.json()["id"] != first
        # Nothing was physically destroyed — the tombstone is still there, which is what
        # keeps a template_id on an old audit row resolving to a name.
        assert template_row(db_session, first).is_deleted is True

    def test_a_second_delete_is_404(self, client: TestClient, db_session: Session):
        """What makes the tombstone filter observable rather than a claim."""
        admin = make_user(db_session)
        source = self._source(db_session)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Once")

        assert client.delete(f"{BASE}/{template_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK
        second = client.delete(f"{BASE}/{template_id}", headers=headers_for(admin))
        assert second.status_code == status.HTTP_404_NOT_FOUND, second.text

    def test_a_deleted_template_is_absent_from_the_list_and_from_get(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = self._source(db_session)
        kept = saved(client, headers_for(admin), source.work_order.id, "Kept")
        removed = saved(client, headers_for(admin), source.work_order.id, "Removed")
        client.delete(f"{BASE}/{removed}", headers=headers_for(admin))

        listing = list_templates(client, headers_for(admin))
        assert listing.json()["total"] == 1
        assert [entry["id"] for entry in listing.json()["templates"]] == [kept]
        assert client.get(f"{BASE}/{removed}", headers=headers_for(admin)).status_code == status.HTTP_404_NOT_FOUND

    def test_a_deleted_template_cannot_still_be_used(self, client: TestClient, db_session: Session):
        """The verb that actually creates work must honour the tombstone, not just the reads."""
        admin = make_user(db_session)
        source = self._source(db_session)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Removed")
        client.delete(f"{BASE}/{template_id}", headers=headers_for(admin))
        work_orders_before = db_session.query(WorkOrder).count()

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == work_orders_before

    def test_a_duplicate_live_name_is_409(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = self._source(db_session)
        saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = save_template(client, headers_for(admin), source.work_order.id, "Miratech nest group")
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert "Miratech nest group" in response.json()["detail"]
        assert db_session.query(WorkOrderTemplate).count() == 1

    def test_the_duplicate_probe_is_case_insensitive(self, client: TestClient, db_session: Session):
        """Deliberately STRICTER than the index, which is over the stored bytes.

        Two templates whose names differ only in case are indistinguishable in a picker,
        and the picker is the entire feature.
        """
        admin = make_user(db_session)
        source = self._source(db_session)
        saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = save_template(client, headers_for(admin), source.work_order.id, "MIRATECH NEST GROUP")
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert db_session.query(WorkOrderTemplate).count() == 1

    def test_the_same_name_in_another_company_is_fine(self, client: TestClient, db_session: Session):
        """Uniqueness is per company — the index leads with ``company_id``."""
        admin_a = make_user(db_session, company_id=COMPANY_A)
        admin_b = make_user(db_session, company_id=COMPANY_B)
        source_a = build_brake_source(db_session, company_id=COMPANY_A, quantity=10.0)
        source_b = build_brake_source(db_session, company_id=COMPANY_B, quantity=10.0)

        saved(client, headers_for(admin_a), source_a.work_order.id, "Bracket brake set")
        response = save_template(client, headers_for(admin_b), source_b.work_order.id, "Bracket brake set")

        assert response.status_code == status.HTTP_201_CREATED, response.text

    def test_renaming_onto_a_live_name_is_409_and_onto_its_own_is_not(self, client: TestClient, db_session: Session):
        """The rename probe excludes the row being renamed, or re-saving a template with
        its own name unchanged would refuse itself."""
        admin = make_user(db_session)
        source = self._source(db_session)
        first = saved(client, headers_for(admin), source.work_order.id, "Alpha")
        second = saved(client, headers_for(admin), source.work_order.id, "Beta")

        clash = client.put(f"{BASE}/{second}", headers=headers_for(admin), json={"name": "alpha"})
        assert clash.status_code == status.HTTP_409_CONFLICT, clash.text

        itself = client.put(f"{BASE}/{second}", headers=headers_for(admin), json={"name": "Beta", "notes": "n"})
        assert itself.status_code == status.HTTP_200_OK, itself.text
        assert template_row(db_session, first).name == "Alpha"

    def test_the_name_is_whitespace_collapsed_on_store(self, client: TestClient, db_session: Session):
        """Uniqueness is a database index over the stored bytes, so without collapsing,
        "Miratech  nest" and "Miratech nest" are two rows that look identical in a list.

        Spelling only, never meaning: the planner's capitalisation is theirs, and nothing
        is stripped or escaped (this system does no ingest-time sanitisation).
        """
        admin = make_user(db_session)
        source = self._source(db_session)

        response = save_template(client, headers_for(admin), source.work_order.id, "  Miratech   Nest  Group ")
        assert response.status_code == status.HTTP_201_CREATED, response.text

        assert response.json()["name"] == "Miratech Nest Group"
        assert template_row(db_session, response.json()["id"]).name == "Miratech Nest Group"
        # And the collapsed form is what the uniqueness probe compares against.
        clash = save_template(client, headers_for(admin), source.work_order.id, "Miratech Nest Group")
        assert clash.status_code == status.HTTP_409_CONFLICT, clash.text

    def test_a_blank_name_is_refused_at_the_data_boundary(self, client: TestClient, db_session: Session):
        """``min_length=1`` alone admits "   ", which collapses to an unnameable row."""
        admin = make_user(db_session)
        source = self._source(db_session)

        response = save_template(client, headers_for(admin), source.work_order.id, "   ")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert db_session.query(WorkOrderTemplate).count() == 0


# --------------------------------------------------------------------------- #
# The catalog read — a POINTER, not a frozen plan
# --------------------------------------------------------------------------- #
class TestTheCatalogRead:
    """The plan summary is computed LIVE off the source on every read.

    Nothing about the plan is stored, which is the load-bearing design choice: a stored
    ``nest_count`` goes stale the first time somebody soft-deletes a nest on the source,
    and the planner picks a template believing it carries 21 nests and gets 20.
    """

    def test_the_summary_describes_what_the_use_will_actually_produce(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))

        response = save_template(client, headers_for(admin), source.work_order.id, "Nest group", notes="3 sheets")
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert body["source_work_order_id"] == source.work_order.id
        assert body["notes"] == "3 sheets"
        assert body["created_by"] == admin.id
        plan = body["plan"]
        assert plan["available"] is True and plan["unavailable_reason"] is None
        assert plan["source_work_order_number"] == source.work_order.work_order_number
        assert plan["source_status"] == "complete"
        assert plan["work_order_type"] == "laser_cutting"
        assert plan["sequential_operations"] is False
        assert plan["operation_count"] == 2
        assert plan["nest_count"] == 2
        assert plan["planned_runs_total"] == 5
        assert plan["open_material_tie_count"] == 2
        assert plan["work_centers"] == [source.work_center.name]
        assert plan["source_quantity_ordered"] == 5.0

        # And the use delivers exactly that.
        draft = created_work_order(db_session, use_template(client, headers_for(admin), body["id"]))
        assert len(operations_of(db_session, draft)) == plan["operation_count"]
        assert len(nests_of(db_session, draft)) == plan["nest_count"]
        assert len(ties_of(db_session, draft)) == plan["open_material_tie_count"]
        assert draft.quantity_ordered == float(plan["planned_runs_total"])

    def test_editing_the_source_moves_the_summary_because_a_template_is_a_pointer(
        self, client: TestClient, db_session: Session
    ):
        """Intended, not a bug: the exemplar IS the plan, and a planner improving the
        master job expects the improvement to carry."""
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")
        assert client.get(f"{BASE}/{template_id}", headers=headers_for(admin)).json()["plan"]["nest_count"] == 2

        source.nests[1].is_deleted = True
        db_session.commit()

        plan = client.get(f"{BASE}/{template_id}", headers=headers_for(admin)).json()["plan"]
        assert plan["nest_count"] == 1
        assert plan["planned_runs_total"] == 3

        # ...and so do the counts the copy would SKIP for the same reason. This is the
        # assertion this test was missing when it first shipped: the summary reported
        # `operation_count == 2` and `open_material_tie_count == 2` while the copy
        # carried one of each, because it counted the source's rows instead of the
        # rows the copy keeps. On a laser job that renders as "2 ops - 1 nest", which is
        # self-contradictory; the tie half is the one that matters, since a tie the copy
        # drops means the new job carries no demand for that material.
        assert plan["operation_count"] == 1, "an operation whose only nest is dead is not copied"
        assert plan["open_material_tie_count"] == 1, "nor is a tie scoped to it"

        # And the use delivers exactly the reduced numbers, which is the property the
        # whole pointer-not-frozen-plan design rests on.
        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        assert len(operations_of(db_session, draft)) == plan["operation_count"]
        assert len(nests_of(db_session, draft)) == plan["nest_count"]
        assert len(ties_of(db_session, draft)) == plan["open_material_tie_count"]

    def test_a_tie_whose_part_the_copy_would_skip_is_not_counted(self, client: TestClient, db_session: Session):
        """The summary applies the copy's OWN part predicates, not a second opinion.

        ``_copy_material_allocations`` skips a tie whose part has been soft-deleted
        (``part_not_available``) or is one the shop PRODUCES (``part_not_tieable``).
        A summary that counted those would promise material demand the draft will not
        carry -- no shortage raised, the job runs, stock never deducted.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        operation = operations_of(db_session, source.work_order)[0]

        good = make_part(db_session, part_type="raw_material", uom="pounds")
        retired = make_part(db_session, part_type="raw_material", uom="pounds")
        produced = make_part(db_session, part_type="manufactured")
        for part in (good, retired, produced):
            make_tie(db_session, source.work_order, part, operation=operation, qty_planned=5.0)
        retired.is_deleted = True
        db_session.commit()

        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        plan = client.get(f"{BASE}/{template_id}", headers=headers_for(admin)).json()["plan"]
        assert plan["open_material_tie_count"] == 1, "only the raw-material, live-part tie is copyable"

        response = use_template(client, headers_for(admin), template_id)
        draft = created_work_order(db_session, response)
        assert len(ties_of(db_session, draft)) == plan["open_material_tie_count"]
        # ...and the two it dropped still reach the planner through the envelope.
        assert {entry["reason"] for entry in response.json()["skipped_material_allocations"]} == {
            "part_not_available",
            "part_not_tieable",
        }

    def test_search_matches_name_and_notes_case_insensitively(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        by_name = saved(client, headers_for(admin), source.work_order.id, "Miratech housing set")
        by_note = saved(client, headers_for(admin), source.work_order.id, "Bracket set", notes="for Miratech, 3 up")
        saved(client, headers_for(admin), source.work_order.id, "Unrelated set", notes="Acme")

        found = list_templates(client, headers_for(admin), search="miratech")
        assert found.status_code == status.HTTP_200_OK, found.text
        assert {entry["id"] for entry in found.json()["templates"]} == {by_name, by_note}
        assert found.json()["total"] == 2

        assert (
            list_templates(client, headers_for(admin), search="   ").json()["total"] == 3
        ), "blank search is no filter"
        assert list_templates(client, headers_for(admin), search="nothing here").json()["total"] == 0

    def test_the_list_is_name_ordered(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        for name in ("Zeta set", "Alpha set", "Mid set"):
            saved(client, headers_for(admin), source.work_order.id, name)

        names = [entry["name"] for entry in list_templates(client, headers_for(admin)).json()["templates"]]
        assert names == ["Alpha set", "Mid set", "Zeta set"]


# --------------------------------------------------------------------------- #
# Editing the LABEL, not the plan
# --------------------------------------------------------------------------- #
class TestEditingTheLabel:
    """``null`` is a MEANINGFUL value on both nullable fields — it means "clear it".

    A partial update that could not express clearing would leave a planner unable to undo
    a typo, so the router distinguishes an omitted key from an explicit ``null`` via
    ``model_fields_set``.
    """

    def test_an_explicit_null_clears_the_note_and_the_default_quantity(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(
            client, headers_for(admin), source.work_order.id, "Brake set", notes="typo", default_quantity=50
        )

        response = client.put(
            f"{BASE}/{template_id}", headers=headers_for(admin), json={"notes": None, "default_quantity": None}
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        assert response.json()["notes"] is None
        assert response.json()["default_quantity"] is None
        row = template_row(db_session, template_id)
        assert row.notes is None and row.default_quantity is None
        assert row.name == "Brake set", "an omitted key leaves its field alone"
        # And the cleared default really is gone: the use falls through to the source.
        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        assert draft.quantity_ordered == 20.0

    def test_an_omitted_key_leaves_its_field_untouched(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(
            client, headers_for(admin), source.work_order.id, "Brake set", notes="keep me", default_quantity=50
        )

        response = client.put(f"{BASE}/{template_id}", headers=headers_for(admin), json={"name": "Brake set v2"})
        assert response.status_code == status.HTTP_200_OK, response.text

        row = template_row(db_session, template_id)
        assert row.name == "Brake set v2"
        assert row.notes == "keep me"
        assert row.default_quantity == 50.0

    def test_the_source_pointer_is_not_editable(self, client: TestClient, db_session: Session):
        """``extra="forbid"``. Re-pointing a template under the same name silently changes
        what every future click produces, with the only thing anyone reads unchanged."""
        admin = make_user(db_session)
        first = build_brake_source(db_session, quantity=20.0)
        second = build_brake_source(db_session, quantity=99.0)
        template_id = saved(client, headers_for(admin), first.work_order.id, "Brake set")

        response = client.put(
            f"{BASE}/{template_id}",
            headers=headers_for(admin),
            json={"source_work_order_id": second.work_order.id},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert template_row(db_session, template_id).source_work_order_id == first.work_order.id

    def test_the_use_body_rejects_unknown_keys_too(self, client: TestClient, db_session: Session):
        """A misspelled ``quantity`` silently ignored would run the job at the wrong count."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        before = db_session.query(WorkOrder).count()

        response = use_template(client, headers_for(admin), template_id, quantity=7)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == before


# --------------------------------------------------------------------------- #
# N. THE SAFETY NET ITSELF
# --------------------------------------------------------------------------- #
class TestTheDraftAssertionGuard:
    """``_assert_landed_as_draft`` is redundant today, and that is why it needs a test.

    ``_copy_header`` hard-codes ``DRAFT`` and nothing between there and here changes it,
    so no API path can currently make this function raise. It is kept anyway because of
    the failure mode it guards: if a future change to ``_copy_header`` — or a new keyword
    threaded through it — ever made the copy land RELEASED, a template would silently
    become the third release-forcing door in this system and would look identical to the
    planner right up to the moment unreviewed work appeared on the floor.

    Redundant code with no test is how a safety net stops working without anyone
    noticing, so the net is exercised directly.
    """

    @pytest.mark.unit
    def test_a_non_draft_copy_raises_rather_than_being_handed_back(self):
        work_order = WorkOrder(work_order_number="WO-20260825-001", status=WorkOrderStatus.RELEASED)
        template = SimpleNamespace(name="Miratech nest group")

        with pytest.raises(RuntimeError) as excinfo:
            templates._assert_landed_as_draft(work_order, template)

        message = str(excinfo.value)
        assert "WO-20260825-001" in message
        assert "Miratech nest group" in message
        assert "released" in message
        assert "DRAFT" in message

    @pytest.mark.unit
    @pytest.mark.parametrize("draft_status", [WorkOrderStatus.DRAFT, WorkOrderStatus.DRAFT.value])
    def test_a_draft_passes_whether_the_status_is_an_enum_or_its_value(self, draft_status):
        """Both shapes are accepted on purpose — a row read back before a refresh can
        carry the raw string, and a guard that rejected it would fail every use."""
        work_order = WorkOrder(work_order_number="WO-20260825-002", status=draft_status)

        templates._assert_landed_as_draft(work_order, SimpleNamespace(name="Any"))

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "bad_status",
        [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.COMPLETE, "released"],
    )
    def test_every_non_draft_status_is_refused(self, bad_status):
        work_order = WorkOrder(work_order_number="WO-20260825-003", status=bad_status)

        with pytest.raises(RuntimeError):
            templates._assert_landed_as_draft(work_order, SimpleNamespace(name="Any"))

    def test_the_guard_runs_inside_the_transaction_that_would_be_rolled_back(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        """Failing LOUDLY has to mean nothing survives — otherwise it is just a log line.

        The guard is provoked the only way it can be (the copy engine cannot produce a
        non-DRAFT today), and what is asserted is the consequence: the request fails and
        the work order that broke the promise is not in the database.
        """
        admin = make_user(db_session)
        # A NEST source, so the rolled-back copy is one that had already flushed a header,
        # two operations, a nest package, two nests and two ties by the time the guard
        # fired. Against a bare production source the "nothing survives" counts below
        # would be zero either way and would prove nothing.
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")
        before = {
            WorkOrder: db_session.query(WorkOrder).count(),
            AuditLog: db_session.query(AuditLog).count(),
            LaserNestPackage: db_session.query(LaserNestPackage).count(),
            WorkOrderMaterialAllocation: db_session.query(WorkOrderMaterialAllocation).count(),
        }
        assert all(count > 0 for count in before.values()), "non-vacuity: the source really built all four"

        def _pretend_it_landed_released(work_order, template):
            raise RuntimeError(f"Work order template {template.name!r} produced {work_order.work_order_number}")

        monkeypatch.setattr(templates, "_assert_landed_as_draft", _pretend_it_landed_released)

        with pytest.raises(RuntimeError):
            use_template(client, headers_for(admin), template_id)

        db_session.rollback()
        db_session.expire_all()
        for model, count in before.items():
            assert db_session.query(model).count() == count, f"{model.__name__} survived a refused use"


# --------------------------------------------------------------------------- #
# The model's own shape
# --------------------------------------------------------------------------- #
class TestTheTemplateRowItself:
    def test_the_row_stores_a_pointer_and_nothing_about_the_plan(self, client: TestClient, db_session: Session):
        """No operations, no nests, no allocations, no status — by design.

        A frozen plan would need a SECOND copy service re-deciding everything the
        1,300-line duplicate service already decides, and every future fix would have to
        be made twice or the two would drift.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group", notes="n")

        row = template_row(db_session, template_id)
        columns = {column.name for column in row.__table__.columns}
        assert columns == {
            "id",
            "company_id",
            "name",
            "notes",
            "source_work_order_id",
            "default_quantity",
            "created_at",
            "updated_at",
            "created_by",
            "is_deleted",
            "deleted_at",
            "deleted_by",
        }
        assert row.source_work_order_id == source.work_order.id
        assert row.created_by == admin.id
        assert row.company_id == COMPANY_A

    def test_the_parts_and_work_orders_a_template_names_are_never_touched_by_deleting_it(
        self, client: TestClient, db_session: Session
    ):
        """No cascade of any kind: deleting a template must not be able to reach the work
        order it names, nor any job it has already produced."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=10.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake set")
        draft = created_work_order(db_session, use_template(client, headers_for(admin), template_id))
        before = job_snapshot(db_session, source.work_order)

        assert client.delete(f"{BASE}/{template_id}", headers=headers_for(admin)).status_code == status.HTTP_200_OK

        db_session.expire_all()
        assert job_snapshot(db_session, source.work_order) == before
        surviving = db_session.query(WorkOrder).filter(WorkOrder.id == draft.id).one()
        assert surviving.is_deleted is False
        assert surviving.status == WorkOrderStatus.DRAFT
        assert db_session.query(Part).filter(Part.id == source.part.id).one().is_deleted is False
