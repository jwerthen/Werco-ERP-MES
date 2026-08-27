"""A template outlives the job it was saved from — the owner's rule, pinned end to end.

*"Templates need to stay even if there is no work order present for it."* (owner,
2026-08-27, overriding the original refuse-on-deleted design.)

WHY THIS IS A SEPARATE FILE FROM ``test_work_order_templates.py``
----------------------------------------------------------------
That file pins what a template *is* — the DRAFT guarantee, the copy engine, the skip
envelope — and it carries the two tests that used to assert the old 409, rewritten in
place so anyone hunting the removed refusal finds it there with its reason. What lives
here is the read-through itself: the six failure modes that only exist BECAUSE the
``is_deleted`` predicate came off ``resolve_source_work_order`` and
``plan_summaries_for``, and the one FK guard that had to be built to make the removal
safe. They share the ``build_*_source`` builders, so the two suites describe the same
jobs.

THE FACT THE WHOLE FEATURE RESTS ON
-----------------------------------
``work_order_templates.source_work_order_id`` is ``NOT NULL``, a plain
``ForeignKey("work_orders.id")``, with **no** ``ON DELETE`` — in the model and in
migration ``087``. So "no work order present" can only ever mean **soft-deleted**: the
row itself cannot physically vanish while a template names it. Every test below is a
consequence of that, and :class:`TestTheForeignKeyThatGuaranteesIt` is the one that
holds the fact itself up, because it stopped being an accident the moment the read
started depending on it.

THE ONE THING THIS SUITE CANNOT EXECUTE
---------------------------------------
The FK is not enforced here. This repo's tests run on in-memory SQLite (a deliberate
owner decision — see ``CLAUDE.md``), which does not enable ``PRAGMA foreign_keys``, so
a physical ``DELETE`` of a still-referenced work order simply succeeds in this process
and would raise ``ForeignKeyViolation`` on prod Postgres. That asymmetry is exactly why
:class:`TestTheForeignKeyThatGuaranteesIt` asserts the **application-layer refusal**
(which runs in Python before any SQL and therefore behaves identically on both engines)
rather than asserting the database's reaction to an unguarded delete — an assertion
that would pass here for the wrong reason and tell nobody anything about prod.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from app.models.work_order_template import WorkOrderTemplate
from app.services import work_order_template_service as templates
from tests.api.test_work_order_duplicate import (
    COMPANY_A,
    COMPANY_B,
    _ensure_company,
    add_operation,
    headers_for,
    make_part,
    make_user,
    make_work_center,
    make_work_order,
    nests_of,
    operations_of,
    ties_of,
)
from tests.api.test_work_order_templates import (
    BASE,
    _every_column,
    build_brake_source,
    build_nest_source,
    created_work_order,
    job_snapshot,
    list_templates,
    save_template,
    saved,
    template_row,
    use_template,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

WORK_ORDERS = "/api/v1/work-orders"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def plan_from_list(client: TestClient, headers: dict, template_id: int) -> dict:
    """The plan summary as the CATALOG renders it — the list read, not the detail read.

    The list is where ``plan_summaries_for``'s batched source read lives, and that read
    carries its own tombstone predicate separate from ``resolve_source_work_order``'s.
    Reading through the list is what makes a lockstep failure visible: if only one of
    the two filters were dropped, the use path would happily run a template whose row
    in the catalog rendered blank.
    """
    response = list_templates(client, headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    [row] = [entry for entry in response.json()["templates"] if entry["id"] == template_id]
    return row["plan"]


def comparable_plan(work_order: WorkOrder, operations: list, nests: list, ties: list) -> dict:
    """The part of a produced DRAFT that must not depend on the source's tombstone.

    Field-by-field rather than a whole-row diff on purpose: ids, numbers and timestamps
    legitimately differ between two copies, so a raw ``_every_column`` comparison would
    be noise. What must match is the PLAN — the routing, the nests, and the derived
    quantity — which is what the planner picked the template for.
    """
    return {
        "status": work_order.status,
        "work_order_type": work_order.work_order_type,
        "sequential_operations": work_order.sequential_operations,
        "quantity_ordered": float(work_order.quantity_ordered),
        # ``name`` is off a global counter in the builders, so it differs between two
        # independently built twins the way ``nest_name`` does. ``operation_number`` is
        # the deterministic identifier and is the one that matters — it is what the
        # traveler and the kiosk read.
        "operations": sorted((op.sequence, op.operation_number, op.status, op.component_quantity) for op in operations),
        # ``nest_name`` / ``cnc_number`` are per-row unique in the builders and the
        # ``document_id`` points at a per-source Document, so neither is comparable
        # between two independently built twins. What IS comparable is the sheet the
        # nest cuts and how many times it runs — the part of a nest that decides the
        # derived quantity and the material demand.
        "nests": sorted((nest.planned_runs, nest.material, nest.thickness, nest.sheet_size) for nest in nests),
        "tie_count": len(ties),
    }


def produced_plan(db: Session, response) -> dict:
    draft = created_work_order(db, response)
    return comparable_plan(draft, operations_of(db, draft), nests_of(db, draft), ties_of(db, draft))


def point_template_at(db: Session, *, work_order_id: int, company_id: int, name: str) -> WorkOrderTemplate:
    """A template row written DIRECTLY, bypassing the save endpoint.

    Needed only for the cross-tenant case in :class:`TestTenantIsolationSurvivedTheChange`:
    ``POST /work-order-templates`` resolves the source company-scoped and answers 404,
    so the API cannot produce this row. Writing it by hand is the only way to reach the
    ``available=False`` branch and prove the resolver — not the save gate — is what
    refuses to read across the boundary.
    """
    template = WorkOrderTemplate(
        company_id=company_id,
        name=name,
        source_work_order_id=work_order_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def soft_delete_via_api(client: TestClient, headers: dict, work_order_id: int) -> None:
    """Delete the work order the way a planner actually does — the VERB, not the model.

    ``WorkOrder.soft_delete()`` sets three columns; ``DELETE /work-orders/{id}`` does
    that AND auto-cancels every open material tie. That difference is not cosmetic: it
    changes what a template over the tombstone produces (see
    :meth:`TestUsingATemplateOverATombstone.test_the_ties_the_delete_cancelled_are_not_promised_and_not_copied`),
    so every test in this file goes through the endpoint. A suite that soft-deleted the
    model directly would describe a state the app cannot actually be in and would miss
    the tie consequence entirely.
    """
    response = client.delete(f"{WORK_ORDERS}/{work_order_id}", headers=headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text


def make_draft_job(db: Session, *, quantity: float = 20.0):
    """A DRAFT press-brake job — the only status a hard delete is allowed on.

    Built here rather than through ``build_brake_source``, which pins
    ``status_value=COMPLETE`` (correctly: a template's headline case is re-running a
    FINISHED job). Flipping that builder's status afterwards would leave COMPLETE
    operations on a DRAFT header — a state no verb produces, and the hard-delete guard
    is not the place to assert against fiction.
    """
    part = make_part(db)
    work_order = make_work_order(db, part=part, quantity_ordered=quantity, status_value=WorkOrderStatus.DRAFT)
    work_center = make_work_center(db, wc_type="press_brake")
    add_operation(
        db,
        work_order,
        work_center,
        sequence=10,
        operation_number="10",
        name="Brake A",
        status=OperationStatus.PENDING,
    )
    db.commit()
    return work_order


# --------------------------------------------------------------------------- #
# 1. USING a template whose source is gone — the owner's actual requirement
# --------------------------------------------------------------------------- #
class TestUsingATemplateOverATombstone:
    """The catalog entry keeps producing the SAME job, not merely some job.

    A 201 alone would not settle this. The failure the owner is guarding against is a
    template that still "works" but produces less than it used to, and the shape most
    at risk is the laser nest job — its quantity is DERIVED from the nests the copy
    carries, so a nest dropped on the way across silently halves the run and nothing
    refuses. So both tests below compare against a LIVE TWIN: two identical sources,
    two templates, one source deleted, both used, plans diffed.
    """

    def test_a_nest_bearing_template_produces_the_same_plan_over_a_deleted_source(
        self, client: TestClient, db_session: Session
    ):
        """Twin laser jobs, one tombstoned. The two DRAFTs must be plan-identical.

        This is the headline case: two nests, five planned runs between them, ties on
        every operation. If the read-through ever regains an ``is_deleted`` predicate
        somewhere below ``duplicate_work_order``, the tombstoned twin comes back with
        fewer nests and a smaller derived quantity — and this diff is what says so,
        where an ``assert response.status_code == 201`` would not.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        # Untied twins, so the comparison is TOTAL — ties included — rather than a
        # comparison with the one field that legitimately moves quietly excluded. What
        # the delete verb does to ties is asserted on its own in the next test.
        live = build_nest_source(db_session, runs=(3, 2), tie_material=False)
        doomed = build_nest_source(db_session, runs=(3, 2), tie_material=False)
        live_id = saved(client, headers_for(admin), live.work_order.id, "Nest set (control)")
        doomed_id = saved(client, headers_for(admin), doomed.work_order.id, "Nest set (deleted source)")

        soft_delete_via_api(client, headers_for(admin), doomed.work_order.id)

        control = use_template(client, headers_for(admin), live_id)
        assert control.status_code == status.HTTP_201_CREATED, control.text
        over_tombstone = use_template(client, headers_for(admin), doomed_id)
        assert over_tombstone.status_code == status.HTTP_201_CREATED, over_tombstone.text

        expected = produced_plan(db_session, control)
        actual = produced_plan(db_session, over_tombstone)
        assert actual == expected, "a deleted source must not change what the template produces"

        # Named explicitly as well as diffed, so the failure message says WHAT went
        # missing rather than dumping two dicts.
        assert actual["status"] == WorkOrderStatus.DRAFT
        assert len(actual["nests"]) == 2, "both nests came across the tombstone"
        assert actual["quantity_ordered"] == 5.0, "quantity is DERIVED from the copied nests' planned runs"

        db_session.refresh(doomed.work_order)
        assert doomed.work_order.is_deleted is True, "using a template is not a back-door restore"

    def test_the_ties_the_delete_cancelled_are_not_promised_and_not_copied(
        self, client: TestClient, db_session: Session
    ):
        """The one thing a tombstoned source really does change — and it is DISCLOSED.

        ``DELETE /work-orders/{id}`` auto-cancels every OPEN material tie on its way
        out, so a template used over that tombstone produces a job carrying no material
        demand. That is not the read-through leaking: the summary counts OPEN ties only,
        so the catalog says **0** and the copy carries **0**, which is the promise this
        feature rests on ("what the planner is shown is what they will get") holding at
        the one point where the number moved.

        It matters because a dropped tie is the omission that succeeds silently — no
        shortage is raised, the work runs, stock is never deducted, and the count is
        where anyone finds out. Restoring the source puts the ties back
        (:class:`TestRestoringTheSource`), which is the reason this is a disclosure and
        not a defect.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_nest_source(db_session, runs=(3, 2), tie_material=True)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Tied nest set")
        assert plan_from_list(client, headers_for(admin), template_id)["open_material_tie_count"] == 2

        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        promised = plan_from_list(client, headers_for(admin), template_id)["open_material_tie_count"]
        assert promised == 0, "the delete cancelled them, so the catalog stops promising them"

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)
        assert len(ties_of(db_session, draft)) == promised, "the summary and the copy agree — that is the promise"
        # Nothing was SKIPPED: a cancelled tie is a tombstone the copy never considers,
        # which is a different thing from a tie the copy tried and refused to carry.
        assert response.json()["skipped_material_allocations"] == []

    def test_a_pooled_brake_template_keeps_its_sequencing_flag_over_a_tombstone(
        self, client: TestClient, db_session: Session
    ):
        """``sequential_operations`` is a column the copy CARRIES, and it gates the floor.

        Pooled (``False``) is the press-brake batch shape this feature is most often
        pointed at. Getting the flag wrong on a copy is not cosmetic — it decides
        whether the operations promote together or block each other — so it is asserted
        over the tombstone rather than assumed to ride along with the rest of the header.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_brake_source(db_session, quantity=20.0, sequential=False)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Brake batch")
        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert bool(draft.sequential_operations) is False, "the pool stays a pool"
        assert len(operations_of(db_session, draft)) == 2


# --------------------------------------------------------------------------- #
# 2. The CATALOG read — available, populated, and honest about the tombstone
# --------------------------------------------------------------------------- #
class TestTheCatalogStillDescribesThePlan:
    """The list must show what the planner will get, over a tombstone as over a live job.

    ``plan_summaries_for`` carries its own batched source read with its own tombstone
    predicate, separate from ``resolve_source_work_order``'s. They must move in
    lockstep: drop one and keep the other and you get a template the use path happily
    runs whose catalog row reads "0 ops · 0 nests" — the "summary must match what the
    copy produces" promise broken in the direction that matters, discovered by a
    planner pressing Use.
    """

    def test_a_deleted_nest_source_is_summarised_in_full_with_the_flag_set(
        self, client: TestClient, db_session: Session
    ):
        """Positive assertions on every count, not just ``available is True``.

        A summary that silently went blank over a tombstone still satisfies
        "available is true" and "unavailable_reason is None". Only the numbers catch it,
        so the numbers are named — and they are the numbers the twin test above proves
        the copy actually produces.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        # Untied: the tie count over a tombstone is the previous class's subject, and
        # what this test is about is that every OTHER count survives in full.
        source = build_nest_source(db_session, runs=(3, 2), tie_material=False)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest set")
        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        plan = plan_from_list(client, headers_for(admin), template_id)

        assert plan["available"] is True, "a deleted source is a disclosure, not a refusal"
        assert plan["unavailable_reason"] is None
        assert plan["source_work_order_deleted"] is True, "the planner is told the exemplar is in the archive"

        assert plan["operation_count"] == 2
        assert plan["nest_count"] == 2
        assert plan["planned_runs_total"] == 5
        assert plan["source_quantity_ordered"] == 5.0
        assert plan["work_centers"], "the work-center join reads through the tombstone too"
        assert plan["source_work_order_number"] == source.work_order.work_order_number
        assert plan["work_order_type"] == "laser_cutting"

    def test_the_deleted_template_is_listed_rather_than_hidden(self, client: TestClient, db_session: Session):
        """Dropping the row from the list is the mask trap invariant 3 documents.

        A planner's curated template silently vanishing — with nothing saying why — is
        the failure the owner reported in the first place, one layer down. Counted, not
        just present, so a listing that returns the row while reporting ``total`` from a
        differently-filtered query fails here.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Kept in the catalog")
        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        listing = list_templates(client, headers_for(admin))
        assert listing.status_code == status.HTTP_200_OK, listing.text
        body = listing.json()
        assert body["total"] == 1
        assert [row["id"] for row in body["templates"]] == [template_id]


# --------------------------------------------------------------------------- #
# 3. TENANCY — the predicate that came off was is_deleted, and only is_deleted
# --------------------------------------------------------------------------- #
class TestTenantIsolationSurvivedTheChange:
    """Invariant 1, asserted at the resolver rather than at the save gate.

    ``resolve_source_work_order`` dropped its tombstone filter and kept ``tenant_query``.
    The save endpoint's own 404 would mask a regression here — it refuses a foreign id
    before the resolver is ever asked — so these tests write the template row directly
    and then exercise BOTH answers the one resolver drives: the catalog's
    ``available`` flag and the use path's 409.

    Both polarities of the deleted flag are covered, because the whole change was about
    that flag: a resolver that reads through a tombstone must not also read through a
    company.
    """

    @pytest.mark.parametrize("foreign_source_deleted", [False, True], ids=["live", "soft_deleted"])
    def test_a_source_in_another_company_never_resolves(
        self, client: TestClient, db_session: Session, foreign_source_deleted: bool
    ):
        _ensure_company(db_session, COMPANY_B)
        intruder = make_user(db_session, company_id=COMPANY_A)
        foreign_owner = make_user(db_session, company_id=COMPANY_B)
        foreign = build_nest_source(db_session, company_id=COMPANY_B, runs=(3, 2))
        if foreign_source_deleted:
            foreign.work_order.soft_delete(foreign_owner.id)
            db_session.commit()

        template = point_template_at(
            db_session,
            work_order_id=foreign.work_order.id,
            company_id=COMPANY_A,
            name="Pointing across the boundary",
        )

        # The resolver itself, asked directly: one function, and it is the one both
        # answers below come from.
        assert templates.resolve_source_work_order(db_session, foreign.work_order.id, COMPANY_A) is None

        plan = plan_from_list(client, headers_for(intruder), template.id)
        assert plan["available"] is False
        assert plan["unavailable_reason"] == templates.UNAVAILABLE_SOURCE_MISSING
        # Not one field of the other company's job leaks through the summary.
        assert plan["source_work_order_number"] is None
        assert plan["source_status"] is None
        assert plan["work_order_type"] is None
        assert plan["work_centers"] == []
        assert plan["operation_count"] == 0
        assert plan["nest_count"] == 0
        assert plan["planned_runs_total"] == 0
        assert plan["source_quantity_ordered"] is None
        assert plan["source_work_order_deleted"] is False, "unresolvable is not the same claim as deleted"

        work_orders_before = db_session.query(WorkOrder).count()
        response = use_template(client, headers_for(intruder), template.id)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        db_session.expire_all()
        assert db_session.query(WorkOrder).count() == work_orders_before, "no job may be minted from a foreign plan"

    def test_saving_from_another_companys_deleted_work_order_is_404(self, client: TestClient, db_session: Session):
        """Both gates at once: the tenant scope AND the selection-side tombstone filter.

        ``resolve_catalogable_work_order`` is a wrapper over the read-through resolver,
        so a bug that collapsed the two would show up here as a 201.
        """
        _ensure_company(db_session, COMPANY_B)
        foreign_owner = make_user(db_session, company_id=COMPANY_B)
        foreign = build_brake_source(db_session, company_id=COMPANY_B, quantity=10.0)
        foreign.work_order.soft_delete(foreign_owner.id)
        db_session.commit()
        intruder = make_user(db_session, company_id=COMPANY_A)

        response = save_template(client, headers_for(intruder), foreign.work_order.id, "Borrowed and buried")
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
        assert db_session.query(WorkOrderTemplate).count() == 0


# --------------------------------------------------------------------------- #
# 4. RESTORING the source
# --------------------------------------------------------------------------- #
class TestRestoringTheSource:
    """Undoing the delete puts the catalog back exactly where it was.

    The flag is INFORMATIONAL, so it has to clear on restore or it becomes a permanent
    scar on a template whose source is right there — a planner reading "the exemplar is
    in the archive" about a live job would go looking for something to fix.
    """

    def test_restore_clears_the_disclosure_flag_and_leaves_the_plan_identical(
        self, client: TestClient, db_session: Session
    ):
        """Diffed against the pre-delete reading, so a partial restore cannot pass.

        Delete -> restore is a round trip, and the summary is computed live off the
        source on every read, so the plan must come back byte-identical. Anything that
        did not is a column the delete mutated and the restore did not put back.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        # Untied, so the delete has nothing to cancel and the ONLY field that may move
        # is the disclosure flag. The tied case moves ``open_material_tie_count`` too,
        # by design, and the round trip that puts it back is the next test.
        source = build_nest_source(db_session, runs=(3, 2), tie_material=False)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Round trip")

        before = plan_from_list(client, headers_for(admin), template_id)
        assert before["source_work_order_deleted"] is False

        soft_delete_via_api(client, headers_for(admin), source.work_order.id)
        during = plan_from_list(client, headers_for(admin), template_id)
        assert during == {**before, "source_work_order_deleted": True}, "only the disclosure flag moved"

        restored = client.post(f"{WORK_ORDERS}/{source.work_order.id}/restore", headers=headers_for(admin))
        assert restored.status_code == status.HTTP_200_OK, restored.text

        after = plan_from_list(client, headers_for(admin), template_id)
        assert after["source_work_order_deleted"] is False, "the flag is a disclosure, not a scar"
        assert after == before, "the round trip changed nothing the catalog can see"

    def test_the_template_still_produces_a_draft_after_the_round_trip(self, client: TestClient, db_session: Session):
        """Use it on the far side. The delete/restore pair must leave no residue.

        Worth its own test rather than folding into the read above: the WO restore verb
        re-opens material ties that the delete cancelled, and a tie left CANCELLED is
        precisely the omission that succeeds silently — the copy carries no demand, the
        work runs, and stock is never deducted.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_nest_source(db_session, runs=(3, 2), tie_material=True)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Round trip, then run")

        assert plan_from_list(client, headers_for(admin), template_id)["open_material_tie_count"] == 2
        soft_delete_via_api(client, headers_for(admin), source.work_order.id)
        assert (
            plan_from_list(client, headers_for(admin), template_id)["open_material_tie_count"] == 0
        ), "the delete cancelled them"
        assert (
            client.post(f"{WORK_ORDERS}/{source.work_order.id}/restore", headers=headers_for(admin)).status_code
            == status.HTTP_200_OK
        )
        assert (
            plan_from_list(client, headers_for(admin), template_id)["open_material_tie_count"] == 2
        ), "and the restore put them back — the catalog promises them again"

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["skipped_material_allocations"] == [], "the restore put the ties back"
        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert len(ties_of(db_session, draft)) == 2
        assert float(draft.quantity_ordered) == 5.0


# --------------------------------------------------------------------------- #
# 5. The FK that makes "deleted can only mean soft-deleted" true
# --------------------------------------------------------------------------- #
class TestTheForeignKeyThatGuaranteesIt:
    """``DELETE /work-orders/{id}?hard_delete=true`` must refuse while a template names it.

    The read-through is only safe because the source ROW cannot physically vanish. That
    used to be an accident of a ``NOT NULL`` FK with no ``ON DELETE`` — the hard delete
    walked into a ``ForeignKeyViolation`` and surfaced as a **500**. Now it is
    load-bearing, so it refuses legibly, names what is in the way, and does it BEFORE
    the first mutation.

    **What this class deliberately does not do:** it never asserts that an unguarded
    hard delete raises. SQLite runs here with foreign-key enforcement off, so an
    unguarded delete would succeed in this process and the assertion would say nothing
    about Postgres. The application-layer refusal runs in Python before any SQL and is
    therefore engine-identical — that is the thing worth pinning, and it is what the
    tests below read.
    """

    def _draft_with_a_template(self, client: TestClient, db_session: Session, name: str):
        admin = make_user(db_session, role=UserRole.ADMIN)
        work_order = make_draft_job(db_session)
        template_id = saved(client, headers_for(admin), work_order.id, name)
        return admin, work_order, template_id

    def test_hard_deleting_a_job_a_template_points_at_is_a_clean_409_naming_the_template(
        self, client: TestClient, db_session: Session
    ):
        """409 with the template's NAME in it — not a 500, and not a bare "conflict".

        The name is the whole point of the message: the operator is looking at a work
        order and the thing blocking them lives on a different screen, so a refusal that
        does not say which template is a refusal they cannot act on.
        """
        admin, work_order, template_id = self._draft_with_a_template(client, db_session, "Blocks the hard delete")

        response = client.delete(
            f"{WORK_ORDERS}/{work_order.id}",
            headers=headers_for(admin),
            params={"hard_delete": "true"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert "Blocks the hard delete" in detail, "name the template that is in the way"
        assert "Soft delete instead" in detail, "name a remedy that exists"

    def test_the_refused_hard_delete_leaves_the_job_and_the_template_byte_untouched(
        self, client: TestClient, db_session: Session
    ):
        """The refusal is raised before the first mutation, so nothing moved at all.

        Snapshotted column-by-column (``_every_column`` / ``job_snapshot``) rather than
        by checking a handful of fields: ``version`` and ``updated_at`` are in the
        snapshot and move whenever a row is flushed dirty at all, so a probe that
        happened to touch a row on its way to refusing is caught even if it set every
        column back to the value it already had.
        """
        admin, work_order, template_id = self._draft_with_a_template(client, db_session, "Untouched by the refusal")
        job_before = job_snapshot(db_session, work_order)
        template_before = _every_column(template_row(db_session, template_id))

        response = client.delete(
            f"{WORK_ORDERS}/{work_order.id}",
            headers=headers_for(admin),
            params={"hard_delete": "true"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text

        db_session.rollback()
        db_session.expire_all()
        assert job_snapshot(db_session, work_order) == job_before
        assert _every_column(template_row(db_session, template_id)) == template_before

        # And the template is still usable afterwards, which is the reason the guard
        # exists rather than a restatement of the assertions above.
        assert use_template(client, headers_for(admin), template_id).status_code == status.HTTP_201_CREATED

    def test_a_soft_deleted_template_blocks_the_hard_delete_too(self, client: TestClient, db_session: Session):
        """The tombstoned template's foreign key is just as real, and only Postgres notices.

        ``is_deleted`` is an application concept. Postgres does not consult it before
        refusing to drop a referenced row, so filtering tombstones out of the guard's
        probe (which the first implementation did, via ``live_templates_query``) puts
        the 500 straight back for this population — and no test on this repo's SQLite
        could ever show it, because the delete would simply succeed here.

        So the probe is FK-scoped (``templates_pointing_at_work_order``) and this
        refuses. The message does NOT name the tombstoned template: a deleted template
        has no name the planner can act on, and "delete those templates first" is not a
        remedy for one that is already deleted. Soft-deleting the work order is, and it
        stays available — the next test.
        """
        admin, work_order, template_id = self._draft_with_a_template(client, db_session, "Deleted but still pointing")
        deleted = client.delete(f"{BASE}/{template_id}", headers=headers_for(admin))
        assert deleted.status_code == status.HTTP_200_OK, deleted.text
        db_session.expire_all()
        assert template_row(db_session, template_id).is_deleted is True

        response = client.delete(
            f"{WORK_ORDERS}/{work_order.id}",
            headers=headers_for(admin),
            params={"hard_delete": "true"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert "1 work order template(s)" in response.json()["detail"]
        assert "Deleted but still pointing" not in response.json()["detail"], "a tombstone has no actionable name"

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one_or_none() is not None

    def test_hard_delete_is_unaffected_when_no_template_points_at_the_job(
        self, client: TestClient, db_session: Session
    ):
        """The guard is narrow. A draft nobody catalogued still hard-deletes.

        Without this, the two tests above are equally satisfied by a guard that refuses
        every hard delete, which would break an unrelated verb in the name of this one.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        work_order = make_draft_job(db_session)

        response = client.delete(
            f"{WORK_ORDERS}/{work_order.id}",
            headers=headers_for(admin),
            params={"hard_delete": "true"},
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one_or_none() is None


# --------------------------------------------------------------------------- #
# 6. SOFT delete is the remedy, and it is untouched
# --------------------------------------------------------------------------- #
class TestSoftDeleteIsUnaffected:
    """Every refusal above names soft delete as the way out, so soft delete must work.

    This is the closing loop of the whole feature: the owner's rule ("templates need to
    stay") is only true if the ordinary delete a planner reaches for is the one that
    keeps them working. A guard that accidentally extended to soft delete would make the
    reported bug worse, not better — the job would become undeletable AND the template
    would still be the thing everyone blamed.
    """

    def test_soft_deleting_a_job_a_template_points_at_succeeds(self, client: TestClient, db_session: Session):
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Survives the tidy-up")

        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        db_session.expire_all()
        row = db_session.query(WorkOrder).filter(WorkOrder.id == source.work_order.id).one()
        assert row.is_deleted is True, "soft delete, so the row the template points at is still there"
        assert template_row(db_session, template_id).is_deleted is False, "the template is not collateral damage"

    def test_the_template_works_immediately_after_the_soft_delete(self, client: TestClient, db_session: Session):
        """The owner's sentence, end to end, through the two public verbs.

        Delete the job the way a planner actually would (the endpoint, not
        ``soft_delete()`` on the model), then read the catalog and press Use. If this
        test ever fails, the reported bug is back.
        """
        admin = make_user(db_session, role=UserRole.ADMIN)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Still runnable")

        soft_delete_via_api(client, headers_for(admin), source.work_order.id)

        plan = plan_from_list(client, headers_for(admin), template_id)
        assert plan["available"] is True and plan["source_work_order_deleted"] is True
        assert plan["nest_count"] == 2

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        draft = created_work_order(db_session, response)
        assert draft.status == WorkOrderStatus.DRAFT
        assert len(nests_of(db_session, draft)) == 2
