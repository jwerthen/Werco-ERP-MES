"""``POST /work-order-templates/{id}/use`` with a ``count`` — several drafts, one per unit.

WHAT THIS FILE EXISTS TO HOLD DOWN
----------------------------------
A weld assembly is built ONE UNIT PER WORK ORDER: each unit carries its own Unit #, its
own traveler, its own labor and its own quality record. So "run five of these" is five
work orders, not one work order with a quantity of five — those are different plans, and
only the first can be reported against per unit. ``count`` creates that many SEPARATE
drafts, each a full copy of the plan under its own number, and ``unit_numbers`` gives
each of them its own build identity from a list the planner supplies.

Everything the single-use path already pins lives in
``tests/api/test_work_order_templates.py`` and is deliberately not re-litigated here.
What is new, and what would plausibly regress:

* **The five numbers must be five numbers.** ``generate_work_order_number`` picks the
  next number by reading the HIGHEST EXISTING one, and the session runs
  ``autoflush=False``, so a batch minting inside one transaction is only correct because
  ``work_order_duplicate_service._copy_header`` does ``db.add()`` + ``db.flush()`` before
  it returns. That property belongs to a module this feature was told not to touch, and
  the advisory lock does NOT stand in for it — it is re-entrant, so it gives no
  protection between two calls inside one transaction. :class:`TestSeveralDrafts` pins
  the observable (001..005) and :class:`TestTheNumberMintIsBlindToUnflushedRows` pins
  the mechanism directly, so a regression names its own cause instead of surfacing as a
  unique-constraint 409 nobody can explain.

* **Unit numbers are POSITIONAL, and positional data misaligns silently.** Entry three
  becomes the third work order's unit. A list one entry short would shift every unit
  after the gap onto the wrong job — the drafts still look right, and the wrong build
  identity travels to the kiosk, the dispatch board and the TV wall with no error
  anywhere. Hence the length check, the in-batch duplicate check, and
  :class:`TestUnitNumbersArePositional`'s insistence on values that cannot be confused
  for one another.

* **A unit number that already exists elsewhere is ACCEPTED, on purpose.**
  ``work_orders.unit_number`` carries a plain index and NO unique constraint, because a
  rework or replacement work order legitimately rebuilds the unit it is named for.
  :class:`TestUnitNumbersArePositional` pins that so nobody "fixes" the missing
  constraint into a refusal.

* **A nest-bearing template is one at a time (409).** A laser work order's quantity is
  DEFINED as the sum of its nests' planned runs, so five copies is the wrong shape for
  the ask. Refused before the first mutation.

* **The batch is ALL-OR-NOTHING.** ``duplicate_work_order`` flushes and never commits and
  ``atomic_transaction`` is not re-entrant, so the router holds ONE transaction around
  the whole loop. :class:`TestAllOrNothing` injects a failure on the third copy and
  asserts the first two — which really were created; the test proves it — are gone.

* **One ``USE_TEMPLATE`` audit row per created work order** (invariant 2), keyed together
  by ``batch_id`` and stamped for a single use too, so the chain has one shape to read.

* **A stamped Unit # writes the WORK ORDER's own audit row as well**, because the
  ``USE_TEMPLATE`` row is filed under the TEMPLATE and the natural audit query —
  ``resource_type='work_order' AND resource_id=N`` — never returns it. The copy engine's
  CREATE row is snapshotted before the stamp, so without that second row an auditor
  reads "created with no unit, never changed" against a traveler showing 2410048.
  :class:`TestTheStampedUnitNumberIsOnTheWorkOrdersOwnChain` asserts the auditor's query
  itself, and asserts the CREATE row's silence beside it so the second row cannot be
  deleted as redundant.

* **The two skip lists are UNIONED across the batch, not concatenated.** Every copy
  reads the SAME source, so an omission every copy made is ONE omission — reported once
  on the envelope while each copy's own audit row keeps its own full list.
  :class:`TestTheSkipListsAreUnionedAcrossTheBatch` reaches that path the only way a
  batch can (a source whose nests are ALL soft-deleted summarises as ``nest_count == 0``,
  so the nest-bearing 409 does not fire) and pins the difference: three copies, six
  omissions on the chain, two on the planner's screen.

Builders are IMPORTED from the two suites this feature sits between rather than
re-declared, so "a batch produces what a single use produces" stays a property two
files can compare rather than two independently-built fictions.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.endpoints.work_orders import generate_work_order_number
from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderStatus
from app.schemas.work_order import WorkOrderDuplicateSkippedAllocation, WorkOrderDuplicateSkippedOperation
from app.schemas.work_order_template import MAX_TEMPLATE_USE_COUNT
from app.services import work_order_template_service as templates
from app.services.audit_service import AuditService
from tests.api.test_work_order_duplicate import (
    COMPANY_A,
    COMPANY_B,
    audit_rows,
    headers_for,
    make_part,
    make_user,
    make_work_order,
    operations_of,
    ties_of,
)
from tests.api.test_work_order_templates import (
    build_brake_source,
    build_nest_source,
    saved,
    template_row,
    use_template,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

# A real Miratech-shaped unit number: digits only, so it can never accidentally match a
# work-order number, a part number or a customer name and make an assertion vacuous.
UNIT = "2410048"

# The nest refusal's wording, RESTATED here rather than imported and formatted.
# ``NEST_BEARING_BATCH_REFUSAL`` is the thing under test: comparing it to itself would
# pass against any rewrite, including one that dropped the remedy sentence — which is
# the half that tells the planner what to do instead, and the only reason the refusal is
# better than a silent success.
EXPECTED_NEST_REFUSAL = (
    "Template 'Miratech nest group' cuts sheets: its quantity is the sum of its nests' planned runs, so "
    "running more of it means more runs on the nests, not more work orders. Create one draft and raise "
    "the nests' planned runs on it."
)

# Appended to the constraint 409 for a batch only. Stated independently for the same
# reason: the planner's safe guess after a failure ("the earlier ones survived") is the
# WRONG one, and this sentence is the only thing that corrects it.
EXPECTED_ROLLBACK_SENTENCE = " Nothing was created — the whole batch was rolled back."


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def work_order_numbers(body: dict) -> list:
    """Every created work order's number, in the order the envelope lists them."""
    return [entry["work_order_number"] for entry in body["work_orders"]]


def created_rows(db: Session, body: dict) -> list:
    """The created WO ROWS, in envelope order. Read fresh — the wire is a separate claim."""
    db.expire_all()
    by_id = {
        row.id: row
        for row in db.query(WorkOrder).filter(WorkOrder.id.in_([entry["id"] for entry in body["work_orders"]])).all()
    }
    return [by_id[entry["id"]] for entry in body["work_orders"]]


def use_rows(db: Session, template_id: int) -> list:
    """``USE_TEMPLATE`` chain rows for one template, oldest first."""
    return (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "work_order_template",
            AuditLog.resource_id == template_id,
            AuditLog.action == "USE_TEMPLATE",
        )
        .order_by(AuditLog.id)
        .all()
    )


def minted_work_orders(db: Session) -> list:
    """Work orders minted by the app (``WO-YYYYMMDD-NNN``), not by the fixtures.

    The builders number their rows ``DUP-WO-NNNNN``, so this counts exactly what a use
    created — which is what "nothing was written" has to mean on a refusal.
    """
    return db.query(WorkOrder).filter(WorkOrder.work_order_number.like("WO-%")).all()


# --------------------------------------------------------------------------- #
# A. Several drafts, five numbers — the property the whole batch rests on
# --------------------------------------------------------------------------- #
class TestSeveralDrafts:
    """``count=5`` is five work orders at the same quantity, never one at five."""

    def test_five_copies_land_five_drafts_numbered_in_sequence(self, client: TestClient, db_session: Session):
        """THE load-bearing case: five DISTINCT, consecutively-numbered drafts.

        The distinctness is not free. ``generate_work_order_number`` reads the highest
        existing ``WO-YYYYMMDD-%`` row to pick the next suffix, the session runs
        ``autoflush=False``, and the whole batch mints inside ONE transaction — so every
        copy after the first can only see its predecessor because ``_copy_header``
        flushes before returning. Remove that flush and all five copies mint
        ``WO-…-001``; the batch then dies on the unique index, and this assertion is what
        says so in one line rather than as an unexplained 409.

        The suffixes are asserted as an exact ascending run rather than merely "five
        different strings", because a gap or a repeat is the shape a partially-blind mint
        produces.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=5, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert body["created_count"] == 5
        assert len(body["work_orders"]) == 5

        numbers = work_order_numbers(body)
        assert len(set(numbers)) == 5, numbers
        prefix = f"WO-{datetime.now().strftime('%Y%m%d')}-"
        assert numbers == [f"{prefix}{n:03d}" for n in range(1, 6)], numbers

        # The wire and the rows are separate claims: the planner writes these numbers
        # onto travelers off the response, so five rows behind four numbers (or the
        # reverse) is exactly the failure that must not go unnoticed.
        rows = created_rows(db_session, body)
        assert [row.work_order_number for row in rows] == numbers
        assert {row.status for row in rows} == {WorkOrderStatus.DRAFT}
        assert len({row.id for row in rows}) == 5

    def test_each_draft_carries_the_whole_plan_at_the_requested_quantity(self, client: TestClient, db_session: Session):
        """Quantity is per work order — never a total divided between them.

        A batch that split 3 across three drafts would be one job in three pieces, which
        is the shape this feature exists to NOT produce: a serialized unit is a whole
        job. The operation copy is asserted per draft for the same reason — five headers
        sharing one set of operations would be five plans nobody could run.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=3, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        rows = created_rows(db_session, response.json())

        assert [float(row.quantity_ordered) for row in rows] == [1.0, 1.0, 1.0]
        for row in rows:
            copied = operations_of(db_session, row)
            assert [op.sequence for op in copied] == [10, 20]
            assert {op.status for op in copied} == {OperationStatus.PENDING}
            assert {op.work_order_id for op in copied} == {row.id}

        # Non-vacuity for "per work order": the SOURCE ran 20, so a quantity of 1 on each
        # draft is the request being honoured rather than anything being inherited.
        db_session.refresh(source.work_order)
        assert float(source.work_order.quantity_ordered) == 20.0

    def test_the_envelope_is_a_strict_superset_and_its_first_entry_is_the_singular_field(
        self, client: TestClient, db_session: Session
    ):
        """``work_orders[0]`` IS ``work_order`` — not a second serialization of it.

        The result view this endpoint shares with the Duplicate dialog dereferences the
        SINGULAR field, so an envelope that carried only the list would not merely change
        shape, it would blank a screen. Asserted as EQUALITY of the whole object, because
        two serializations of the same row that disagree on one field is the failure a
        caller reading one and ignoring the other would never see.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=4, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert set(body) == {
            "work_order",
            "work_orders",
            "created_count",
            "skipped_operations",
            "skipped_material_allocations",
        }
        assert body["work_order"] == body["work_orders"][0]
        assert body["created_count"] == len(body["work_orders"]) == 4

    def test_a_use_with_no_count_is_a_batch_of_one(self, client: TestClient, db_session: Session):
        """The click-once case: today's body, plus the two keys stamped on every use.

        One code path rather than a batch special case beside a singular one — the shape
        that let the office and floor sequencing gates drift apart in this system once
        already. A client that has not been updated still reads ``work_order`` and its
        skip lists and is still right; a client that has reads a one-entry list.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert set(body) == {
            "work_order",
            "work_orders",
            "created_count",
            "skipped_operations",
            "skipped_material_allocations",
        }
        assert body["created_count"] == 1
        assert body["work_orders"] == [body["work_order"]]
        assert body["skipped_operations"] == [] and body["skipped_material_allocations"] == []
        assert created_rows(db_session, body)[0].status == WorkOrderStatus.DRAFT

    @pytest.mark.parametrize("count", [1, 2, MAX_TEMPLATE_USE_COUNT])
    def test_the_cap_and_its_neighbours_are_all_creatable(self, client: TestClient, db_session: Session, count: int):
        """The cap is a limit, not an off-by-one: 20 must actually work."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=count, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["created_count"] == count
        assert len(set(work_order_numbers(response.json()))) == count


# --------------------------------------------------------------------------- #
# B. The mechanism the batch rests on, pinned directly
# --------------------------------------------------------------------------- #
class TestTheNumberMintIsBlindToUnflushedRows:
    """Why ``_copy_header``'s ``db.flush()`` is load-bearing, stated as an observable.

    This is a property of ``generate_work_order_number`` plus ``autoflush=False``, and it
    is the reason a batch inside ONE transaction produces N numbers rather than N copies
    of one number. The advisory lock does not provide it: ``pg_advisory_xact_lock`` is
    re-entrant, so a second call inside the same transaction takes it again for free and
    reads the same tail.

    Pinned here rather than in the duplicate suite because it is THIS feature that made
    it load-bearing — before the batch, no caller minted twice in one transaction.
    """

    def test_the_mint_repeats_a_number_until_the_previous_row_is_flushed(self, db_session: Session):
        part = make_part(db_session)

        first = generate_work_order_number(db_session, COMPANY_A)
        db_session.add(
            WorkOrder(
                work_order_number=first,
                part_id=part.id,
                work_order_type="production",
                quantity_ordered=1.0,
                status=WorkOrderStatus.DRAFT,
                priority=3,
                company_id=COMPANY_A,
            )
        )

        # Not flushed: the mint's "highest number so far" query cannot see it, so it
        # hands out the SAME number again. This is the failure a batch would ship.
        assert generate_work_order_number(db_session, COMPANY_A) == first

        db_session.flush()
        assert generate_work_order_number(db_session, COMPANY_A) != first

        db_session.rollback()


# --------------------------------------------------------------------------- #
# C. Unit numbers — supplied, positional, and never invented
# --------------------------------------------------------------------------- #
class TestUnitNumbersArePositional:
    """One entry per work order, in creation order. No generator, no fill-down."""

    def test_each_unit_lands_on_its_own_draft_in_order(self, client: TestClient, db_session: Session):
        """Values chosen so a shift by one is visible: none is derivable from another.

        A trailing-digit sequence would make an off-by-one misalignment look correct,
        which is precisely the bug class the length check exists to stop.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        units = [UNIT, "K-9812", "SN00042", "2410099", "X7"]

        response = use_template(
            client, headers_for(admin), template_id, count=5, quantity_ordered=1, unit_numbers=units
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        assert [entry["unit_number"] for entry in body["work_orders"]] == units
        assert [row.unit_number for row in created_rows(db_session, body)] == units

    def test_a_blank_entry_stores_null_and_whitespace_is_trimmed(self, client: TestClient, db_session: Session):
        """A gap is expressible; ``""`` is never stored.

        The third of five units may not be known yet, and NULL is the shape that says so
        — an empty string reads as "has a unit number" to anything doing a presence test
        and as blank to anything rendering one.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(
            client,
            headers_for(admin),
            template_id,
            count=5,
            quantity_ordered=1,
            unit_numbers=[f"  {UNIT}  ", "", None, "   ", "\tK-9812\n"],
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        expected = [UNIT, None, None, None, "K-9812"]
        assert [entry["unit_number"] for entry in body["work_orders"]] == expected
        rows = created_rows(db_session, body)
        assert [row.unit_number for row in rows] == expected
        # Stored as NULL, not as an empty string — asserted on the ROW, because the
        # response schema would normalise ``""`` to null on the way out and hide it.
        assert [row.unit_number for row in rows[1:4]] == [None, None, None]

    def test_omitting_the_list_leaves_every_draft_without_a_unit(self, client: TestClient, db_session: Session):
        """Nothing generates one. The planner adds them later, or never."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=3, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert [row.unit_number for row in created_rows(db_session, response.json())] == [None, None, None]

    def test_a_single_use_may_carry_one_unit_number(self, client: TestClient, db_session: Session):
        """``count`` omitted is ``count=1``, and a one-entry list is valid against it.

        One code path, not a batch special case: the same request shape works at 1.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=1, unit_numbers=[UNIT])
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["work_order"]["unit_number"] == UNIT
        assert created_rows(db_session, response.json())[0].unit_number == UNIT

    def test_a_unit_number_already_on_another_work_order_is_accepted(self, client: TestClient, db_session: Session):
        """NOT a duplicate check against the shop — pinned so nobody adds one.

        ``work_orders.unit_number`` carries a plain index and no unique constraint
        BECAUSE a rework or replacement work order legitimately rebuilds the unit it is
        named for. Refusing here would refuse the case the column was designed for, and
        would do it at the moment the shop is most sure it is right.
        """
        admin = make_user(db_session)
        existing = make_work_order(
            db_session,
            part=make_part(db_session),
            quantity_ordered=1.0,
            status_value=WorkOrderStatus.RELEASED,
            unit_number=UNIT,
        )
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(
            client, headers_for(admin), template_id, count=2, quantity_ordered=1, unit_numbers=[UNIT, "K-9812"]
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert [row.unit_number for row in created_rows(db_session, response.json())] == [UNIT, "K-9812"]

        # And the work order that already held it is untouched: two live rows naming the
        # same unit is the legitimate rework shape, not a collision that was resolved.
        db_session.refresh(existing)
        assert existing.unit_number == UNIT

    def test_a_draft_that_gets_no_unit_is_not_written_a_second_time(self, client: TestClient, db_session: Session):
        """The no-op assignment is SKIPPED, so the click-once path writes what it always did.

        SQLAlchemy records an attribute set as history without comparing values, so
        assigning ``None`` over the ``None`` the copy left would emit an UPDATE and move
        ``version`` (invariant 4) on every draft in the batch. Asserted as a RELATIVE
        difference so it stays true whatever the copy engine's own write count is.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(
            client, headers_for(admin), template_id, count=2, quantity_ordered=1, unit_numbers=[UNIT, None]
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        with_unit, without_unit = created_rows(db_session, response.json())

        assert with_unit.unit_number == UNIT and without_unit.unit_number is None
        assert with_unit.version == without_unit.version + 1


# --------------------------------------------------------------------------- #
# D. The request refusals — every one before the first write
# --------------------------------------------------------------------------- #
class TestTheRequestIsRefusedBeforeAnythingIsWritten:
    """422 at the data boundary, and NOTHING created. Each case is its own failure mode."""

    def _template(self, client: TestClient, db: Session):
        admin = make_user(db)
        source = build_brake_source(db, quantity=20.0)
        return admin, saved(client, headers_for(admin), source.work_order.id, "Weld set")

    @pytest.mark.parametrize(
        "body, why",
        [
            ({"count": 5, "unit_numbers": [UNIT] * 8}, "more units than work orders shifts every unit after a gap"),
            ({"count": 5, "unit_numbers": [UNIT, "K-9812"]}, "fewer units than work orders drops the rest silently"),
            ({"count": 2, "unit_numbers": ["ab-1", "AB-1"]}, "case-insensitive repeat: one physical build, twice"),
            ({"count": 2, "unit_numbers": [UNIT, f"  {UNIT} "]}, "the same value once trimmed is the same value"),
            ({"count": 2, "unit_numbers": ["X" * 51, None]}, "51 chars will not fit the String(50) column"),
            ({"count": 0}, "zero work orders is not a request, it is a mistake"),
            ({"count": MAX_TEMPLATE_USE_COUNT + 1}, "above the cap: one transaction, one global audit lock"),
        ],
    )
    def test_a_malformed_request_is_422_and_creates_nothing(
        self, client: TestClient, db_session: Session, body: dict, why: str
    ):
        admin, template_id = self._template(client, db_session)

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=1, **body)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, f"{why}: {response.text}"

        db_session.expire_all()
        assert minted_work_orders(db_session) == []
        assert use_rows(db_session, template_id) == []

    def test_the_length_refusal_names_both_numbers(self, client: TestClient, db_session: Session):
        """A refusal a planner can act on says what they sent AND what was expected —
        a mis-pasted spreadsheet column is off by a header row or a trailing blank, and
        both are obvious once the two counts are on screen together."""
        admin, template_id = self._template(client, db_session)

        response = use_template(
            client, headers_for(admin), template_id, count=5, quantity_ordered=1, unit_numbers=[UNIT, "K-9812"]
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        message = response.text
        assert "2 entries" in message and "count is 5" in message, message

    def test_a_duplicate_refusal_names_the_repeated_value_and_both_positions(
        self, client: TestClient, db_session: Session
    ):
        admin, template_id = self._template(client, db_session)

        response = use_template(
            client,
            headers_for(admin),
            template_id,
            count=3,
            quantity_ordered=1,
            unit_numbers=[UNIT, "K-9812", UNIT],
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        message = response.text
        assert UNIT in message and "entries 1 and 3" in message, message

    def test_two_blank_entries_are_not_a_duplicate(self, client: TestClient, db_session: Session):
        """Two work orders with no unit yet is an ordinary state, not a collision."""
        admin, template_id = self._template(client, db_session)

        response = use_template(
            client, headers_for(admin), template_id, count=3, quantity_ordered=1, unit_numbers=["", "  ", None]
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert [row.unit_number for row in created_rows(db_session, response.json())] == [None, None, None]

    def test_a_fifty_character_unit_number_fits(self, client: TestClient, db_session: Session):
        """The cap is the COLUMN WIDTH, so the boundary value must be storable."""
        admin, template_id = self._template(client, db_session)
        longest = "X" * 50

        response = use_template(
            client, headers_for(admin), template_id, count=1, quantity_ordered=1, unit_numbers=[longest]
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert created_rows(db_session, response.json())[0].unit_number == longest


# --------------------------------------------------------------------------- #
# E. A nest-bearing template is one at a time
# --------------------------------------------------------------------------- #
class TestANestBearingTemplateRefusesABatch:
    """409 before the first mutation, with the remedy that actually produces more parts.

    A laser work order's ``quantity_ordered`` is the SUM of its nests' planned runs and
    the copy re-derives it, so "five of this template" is five work orders each carrying
    the same nests at the same run counts — not five times the parts. The remedy is one
    draft with higher run counts.
    """

    def test_count_above_one_is_refused_and_writes_nothing(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id, count=2)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == EXPECTED_NEST_REFUSAL

        db_session.expire_all()
        assert minted_work_orders(db_session) == []
        assert use_rows(db_session, template_id) == []

    def test_the_same_template_still_produces_one_draft(self, client: TestClient, db_session: Session):
        """Non-vacuity, half one: the refusal is about the COUNT, not about the template.

        Without this, a bug that broke nest templates outright would satisfy the 409 test
        above and look like the feature working.
        """
        admin = make_user(db_session)
        source = build_nest_source(db_session, runs=(3, 2))
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id, count=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["created_count"] == 1
        # And the derived quantity still rules: 3 + 2 runs, not the request.
        assert float(created_rows(db_session, response.json())[0].quantity_ordered) == 5.0

    def test_a_template_without_nests_takes_the_same_count(self, client: TestClient, db_session: Session):
        """Non-vacuity, half two: the refusal is about the NESTS, not about count > 1."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Miratech nest group")

        response = use_template(client, headers_for(admin), template_id, count=2, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert response.json()["created_count"] == 2


# --------------------------------------------------------------------------- #
# F. All-or-nothing
# --------------------------------------------------------------------------- #
class TestAllOrNothing:
    """A batch that failed on the fourth draft and left three behind is worse than none.

    The planner would be reconciling a half-filled form against a work order list, with
    no way to tell which units already have jobs. One transaction wraps the LOOP —
    ``atomic_transaction`` is not re-entrant, so it can never be moved inside it.
    """

    def _fail_on_the_third_copy(self, monkeypatch) -> list:
        """Let two copies run for real, then raise. Returns the ids they created.

        Returning the real ids is what makes the rollback assertion non-vacuous: it
        proves two work orders genuinely existed inside the transaction, so "they are
        gone" is a rollback rather than a batch that never started.
        """
        real = templates.duplicate_work_order
        created: list = []

        def failing(db, **kwargs):
            if len(created) == 2:
                raise IntegrityError("INSERT INTO work_orders ...", {}, Exception("simulated constraint fault"))
            result = real(db, **kwargs)
            created.append(result.work_order.id)
            return result

        monkeypatch.setattr(templates, "duplicate_work_order", failing)
        return created

    def test_a_failure_on_the_third_copy_leaves_none_of_the_first_two(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        audit_rows_before = db_session.query(AuditLog).count()
        created_ids = self._fail_on_the_third_copy(monkeypatch)

        response = use_template(
            client,
            headers_for(admin),
            template_id,
            count=5,
            quantity_ordered=1,
            unit_numbers=[UNIT, "K-9812", "SN00042", "2410099", "X7"],
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text

        # Non-vacuity: two copies really were created before the failure.
        assert len(created_ids) == 2, created_ids

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id.in_(created_ids)).all() == []
        assert minted_work_orders(db_session) == []
        # Invariant 2: no chain row may describe a work order that does not exist.
        assert use_rows(db_session, template_id) == []
        assert db_session.query(AuditLog).count() == audit_rows_before

    def test_the_refusal_says_the_whole_batch_was_rolled_back(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        """The planner's safe guess ("the earlier ones survived") is the wrong one."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        self._fail_on_the_third_copy(monkeypatch)

        response = use_template(client, headers_for(admin), template_id, count=5, quantity_ordered=1)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"].endswith(EXPECTED_ROLLBACK_SENTENCE)

    def test_a_single_use_does_not_claim_a_batch_was_rolled_back(
        self, client: TestClient, db_session: Session, monkeypatch
    ):
        """Non-vacuity for the sentence above, and a wording rule of its own: there is no
        batch to have rolled back when one copy was asked for, and saying otherwise
        invents a set of work orders for the planner to go looking for."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        def failing(db, **kwargs):
            raise IntegrityError("INSERT INTO work_orders ...", {}, Exception("simulated constraint fault"))

        monkeypatch.setattr(templates, "duplicate_work_order", failing)

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=1)
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert EXPECTED_ROLLBACK_SENTENCE.strip() not in response.json()["detail"]


# --------------------------------------------------------------------------- #
# G. Invariant 2 — one chain row per created work order, keyed as one action
# --------------------------------------------------------------------------- #
class TestTheAuditChain:
    """Five drafts are five rows, and the chain must be able to say they were one click.

    Five independent rows a few milliseconds apart are not distinguishable, after the
    fact, from five separate uses of the same template — which is a different fact about
    how the shop plans work.
    """

    def test_five_drafts_write_five_rows_keyed_by_one_batch_id(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        units = [UNIT, None, "SN00042", "2410099", "X7"]

        response = use_template(
            client, headers_for(admin), template_id, count=5, quantity_ordered=1, unit_numbers=units
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()

        rows = use_rows(db_session, template_id)
        assert len(rows) == 5
        assert len({row.extra_data["batch_id"] for row in rows}) == 1
        assert [row.extra_data["batch_index"] for row in rows] == [1, 2, 3, 4, 5]
        assert {row.extra_data["batch_size"] for row in rows} == {5}

        # Each row names the work order it made and the unit it stamped on it — the one
        # field of the new work order the TEMPLATE path supplies that the copy engine
        # does not, so nothing else records who set it.
        assert [row.extra_data["created_work_order_number"] for row in rows] == work_order_numbers(body)
        assert [row.extra_data["unit_number"] for row in rows] == units
        assert {row.extra_data["created_work_order_status"] for row in rows} == {"draft"}

    def test_a_single_use_is_stamped_the_same_way(self, client: TestClient, db_session: Session):
        """One code path, not a batch special case: a reader never has to branch on
        whether the keys are present."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        [row] = use_rows(db_session, template_id)
        assert row.extra_data["batch_size"] == 1
        assert row.extra_data["batch_index"] == 1
        assert row.extra_data["unit_number"] is None
        assert isinstance(row.extra_data["batch_id"], str) and row.extra_data["batch_id"]

    def test_two_batches_are_two_different_batch_ids(self, client: TestClient, db_session: Session):
        """Non-vacuity for the shared id above: it is minted per REQUEST, not per
        template — otherwise "these five were one action" would be true of every use of
        the template ever made."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        first = use_template(client, headers_for(admin), template_id, count=2, quantity_ordered=1)
        second = use_template(client, headers_for(admin), template_id, count=2, quantity_ordered=1)
        assert first.status_code == status.HTTP_201_CREATED, first.text
        assert second.status_code == status.HTTP_201_CREATED, second.text

        rows = use_rows(db_session, template_id)
        assert len(rows) == 4
        assert len({row.extra_data["batch_id"] for row in rows}) == 2


# --------------------------------------------------------------------------- #
# G2. Invariant 2 — the stamped Unit # on the WORK ORDER's own chain
# --------------------------------------------------------------------------- #
class TestTheStampedUnitNumberIsOnTheWorkOrdersOwnChain:
    """Setting the Unit # has to be readable where an auditor actually looks.

    The natural audit query — the one the Audit Log page builds — is
    ``resource_type='work_order' AND resource_id=N``. ``AuditService.log_create``
    snapshots the model EAGERLY and ``duplicate_work_order`` takes that snapshot before
    the unit is applied, so the work order's CREATE row is written with ``unit_number``
    unset. Without a second, WORK-ORDER-scoped row the only record of the value being
    set would live in ``extra_data`` on a ``work_order_template``-scoped USE_TEMPLATE
    row, which that query never returns: an auditor would read "created with no unit,
    never changed" against a traveler, a kiosk card and a TV tile all displaying
    2410048. Unit # is the build identity, so that is an AS9100D traceability defect
    rather than a cosmetic one.

    Every assertion below therefore runs the AUDITOR's query — ``audit_rows`` on the
    resource type + id pair — and never searches for the row by its reason or its
    description, because a row only findable if you already knew it existed is the
    thing this class exists to rule out.
    """

    def _rows_for(self, db: Session, work_order_id: int, action: str) -> list:
        """The auditor's query — resource type + id — narrowed to one action verb."""
        return [row for row in audit_rows(db, "work_order", work_order_id) if row.action == action]

    def test_each_stamped_draft_carries_its_own_update_row_naming_its_unit(
        self, client: TestClient, db_session: Session
    ):
        """Three drafts, three units, three rows — each found from its own work order id."""
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        units = [UNIT, "K-9812", "SN00042"]

        response = use_template(
            client, headers_for(admin), template_id, count=3, quantity_ordered=1, unit_numbers=units
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        drafts = created_rows(db_session, response.json())
        assert [row.unit_number for row in drafts] == units

        for draft, unit in zip(drafts, units):
            [row] = self._rows_for(db_session, draft.id, "UPDATE")
            # The stored value is discoverable from the row in both places a reader
            # looks: the value snapshot and the field-level diff.
            assert row.new_values["unit_number"] == unit
            assert row.old_values["unit_number"] is None
            assert row.extra_data["changes"]["unit_number"] == {"old": None, "new": unit}
            # It names the work order it moved, so the row reads without a join.
            assert row.resource_identifier == draft.work_order_number
            assert unit in row.description
            assert row.extra_data["reason"] == "work_order_template_use"
            # Invariant 1: tenant-tagged like every other chain row.
            assert row.company_id == COMPANY_A

    def test_the_create_row_alone_does_not_carry_the_unit(self, client: TestClient, db_session: Session):
        """Non-vacuity for the row above: this is exactly the gap it closes.

        The copy engine's CREATE row is written from a snapshot taken BEFORE the stamp,
        so it says ``unit_number: null``; the USE_TEMPLATE row that does carry the value
        is filed under a resource an auditor reading this job has no reason to query.
        Both halves are asserted, because if either ever stopped being true the second
        row would look redundant and somebody would delete it.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, quantity_ordered=1, unit_numbers=[UNIT])
        assert response.status_code == status.HTTP_201_CREATED, response.text
        [draft] = created_rows(db_session, response.json())
        assert draft.unit_number == UNIT

        [create_row] = self._rows_for(db_session, draft.id, "CREATE")
        assert create_row.new_values["unit_number"] is None

        # And the row that always knew is filed under the TEMPLATE, not this job.
        [use_row] = use_rows(db_session, template_id)
        assert use_row.extra_data["unit_number"] == UNIT
        assert use_row.resource_type == "work_order_template"
        assert use_row.resource_id != draft.id

    def test_a_batch_with_no_units_writes_no_work_order_update_row(self, client: TestClient, db_session: Session):
        """No unit supplied, no row written — the click-once path stays byte identical.

        The stamp is skipped as a no-op, so this row can never become a per-draft UPDATE
        that every already-deployed caller suddenly starts writing.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(client, headers_for(admin), template_id, count=3, quantity_ordered=1)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        for draft in created_rows(db_session, response.json()):
            assert draft.unit_number is None
            assert self._rows_for(db_session, draft.id, "UPDATE") == []
            # Non-vacuity for that empty list: the SAME query does return this work
            # order's rows, so "no UPDATE row" is an absence rather than a filter typo.
            assert len(self._rows_for(db_session, draft.id, "CREATE")) == 1

    def test_a_blank_entry_stores_null_and_writes_no_row_for_that_draft(self, client: TestClient, db_session: Session):
        """A gap in the list is not a change, so it is not recorded as one.

        Whitespace and an omitted entry both store NULL — which is what the copy engine
        already left — so there is nothing for a row to say. The stamped draft beside
        them in the SAME batch is what proves the writer ran at all.
        """
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")

        response = use_template(
            client,
            headers_for(admin),
            template_id,
            count=3,
            quantity_ordered=1,
            unit_numbers=[UNIT, "   ", None],
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        stamped, whitespace_only, omitted = created_rows(db_session, response.json())

        assert stamped.unit_number == UNIT
        assert whitespace_only.unit_number is None
        assert omitted.unit_number is None

        assert len(self._rows_for(db_session, stamped.id, "UPDATE")) == 1
        assert self._rows_for(db_session, whitespace_only.id, "UPDATE") == []
        assert self._rows_for(db_session, omitted.id, "UPDATE") == []


# --------------------------------------------------------------------------- #
# H. Invariants 1 + RBAC — a batch widens neither
# --------------------------------------------------------------------------- #
class TestTenancyAndRole:
    """``count`` adds volume, never reach: the same tenant scope and the same role tier."""

    def test_an_operator_is_refused_and_creates_nothing(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = build_brake_source(db_session, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        operator = make_user(db_session, role=UserRole.OPERATOR)

        response = use_template(
            client, headers_for(operator), template_id, count=5, quantity_ordered=1, unit_numbers=[UNIT] + [None] * 4
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text

        db_session.expire_all()
        assert minted_work_orders(db_session) == []
        assert use_rows(db_session, template_id) == []

    def test_another_companys_template_is_404_not_a_batch(self, client: TestClient, db_session: Session):
        """404, never 403 — a 403 confirms the id exists somewhere."""
        owner = make_user(db_session, company_id=COMPANY_A)
        source = build_brake_source(db_session, company_id=COMPANY_A, quantity=20.0)
        template_id = saved(client, headers_for(owner), source.work_order.id, "A's weld set")
        intruder = make_user(db_session, company_id=COMPANY_B)

        response = use_template(
            client, headers_for(intruder), template_id, count=3, quantity_ordered=1, unit_numbers=[UNIT, None, None]
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

        db_session.expire_all()
        assert minted_work_orders(db_session) == []
        assert use_rows(db_session, template_id) == []

    def test_every_row_a_batch_creates_belongs_to_the_active_company(self, client: TestClient, db_session: Session):
        """Invariant 1, including the units: a unit number is the CUSTOMER's scheme, so
        two tenants building for the same customer legitimately hold the same string —
        which makes "it landed" and "it landed only on my rows" one requirement."""
        caller = make_user(db_session, company_id=COMPANY_B)
        source = build_brake_source(db_session, company_id=COMPANY_B, quantity=20.0)
        template_id = saved(client, headers_for(caller), source.work_order.id, "B's weld set")
        # Company A holds the same unit number on a job of its own, and must come
        # through the batch untouched.
        a_side = make_work_order(
            db_session,
            company_id=COMPANY_A,
            part=make_part(db_session, company_id=COMPANY_A),
            quantity_ordered=1.0,
            status_value=WorkOrderStatus.RELEASED,
            unit_number=UNIT,
        )
        a_side_version = a_side.version

        response = use_template(
            client, headers_for(caller), template_id, count=3, quantity_ordered=1, unit_numbers=[UNIT, "K-9812", None]
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        rows = created_rows(db_session, response.json())

        assert {row.company_id for row in rows} == {COMPANY_B}
        assert [row.unit_number for row in rows] == [UNIT, "K-9812", None]
        assert {op.company_id for row in rows for op in operations_of(db_session, row)} == {COMPANY_B}
        assert {row.company_id for row in use_rows(db_session, template_id)} == {COMPANY_B}

        db_session.refresh(a_side)
        assert a_side.unit_number == UNIT and a_side.version == a_side_version


# --------------------------------------------------------------------------- #
# I. The other half of the trim rule: PUT /work-orders/{id}
# --------------------------------------------------------------------------- #
class TestABlankUnitNumberOnTheUpdateVerbStoresNull:
    """The batch and the edit form must agree on what a blank Unit # means.

    Lives with the batch tests because this feature is what made the two paths have to
    agree: consolidating the ``String(50)`` cap into ``core.validation.UnitNumber`` put
    the trim-to-NULL rule on the shared annotated type, and ``PUT /work-orders/{id}``
    inherits it.

    Before that, the update path was a blind ``setattr`` loop over
    ``model_dump(exclude_unset=True)`` with no trimming, so a planner clearing the field
    in the UI — which sends ``""``, not ``null`` — persisted an EMPTY STRING. That row
    then reads as "has a unit number" to a presence test and as blank to a renderer,
    which is why the wallboard already carries a defensive ``wo.unit_number or None``.
    """

    def _work_order(self, db: Session) -> WorkOrder:
        return make_work_order(
            db,
            part=make_part(db),
            quantity_ordered=1.0,
            status_value=WorkOrderStatus.RELEASED,
            unit_number=UNIT,
            customer_name="Miratech",
        )

    def test_an_empty_string_clears_it_to_null(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        work_order = self._work_order(db_session)

        response = client.put(
            f"/api/v1/work-orders/{work_order.id}",
            headers=headers_for(manager),
            json={"version": work_order.version, "unit_number": ""},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["unit_number"] is None

        db_session.expire_all()
        stored = db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one()
        assert stored.unit_number is None
        # The assertion the response cannot make: the schema would normalise a stored
        # ``""`` to null on the way out, so only the ROW distinguishes the fix from the
        # bug it replaces.
        assert stored.unit_number != ""

    def test_a_whitespace_only_value_clears_it_too(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        work_order = self._work_order(db_session)

        response = client.put(
            f"/api/v1/work-orders/{work_order.id}",
            headers=headers_for(manager),
            json={"version": work_order.version, "unit_number": "   "},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one().unit_number is None

    def test_a_padded_value_is_stored_trimmed(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        work_order = self._work_order(db_session)

        response = client.put(
            f"/api/v1/work-orders/{work_order.id}",
            headers=headers_for(manager),
            json={"version": work_order.version, "unit_number": "  2410099  "},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        db_session.expire_all()
        assert db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one().unit_number == "2410099"

    def test_the_rule_is_specific_to_the_unit_number(self, client: TestClient, db_session: Session):
        """Non-vacuity: a NEIGHBOURING free-text field on the same blind ``setattr`` loop
        still stores its blank, so the NULL above is this annotated type doing its job
        rather than a global blank-collapsing rule nobody asked for."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        work_order = self._work_order(db_session)

        response = client.put(
            f"/api/v1/work-orders/{work_order.id}",
            headers=headers_for(manager),
            json={"version": work_order.version, "unit_number": "", "customer_name": ""},
        )
        assert response.status_code == status.HTTP_200_OK, response.text

        db_session.expire_all()
        stored = db_session.query(WorkOrder).filter(WorkOrder.id == work_order.id).one()
        assert stored.unit_number is None
        assert stored.customer_name == ""


# --------------------------------------------------------------------------- #
# J. The skip lists — one omission, reported once
# --------------------------------------------------------------------------- #
class TestTheSkipListsAreUnionedAcrossTheBatch:
    """The batch's two skip lists are a UNION, and this is the class that proves it.

    A skipped material tie is the worst thing this envelope can carry: the draft is
    created and valid but holds NO demand for that material, so no shortage is raised,
    the work runs, and stock is never deducted (invariant 6). Every copy in a batch
    reads the SAME source, so an omission the source causes is made by all of them —
    reported per copy it would read as "six operations were dropped" when two were,
    which is the same class of error as reporting none.

    HOW A BATCH REACHES A SKIP AT ALL
    ---------------------------------
    Not obvious, and worth stating because it is what makes this testable rather than
    theoretical. The only producible operation skip is ``laser_nest_deleted``, and a
    template whose source carries LIVE nests refuses ``count > 1`` outright. The gap is
    that ``plan_summaries_for`` counts LIVE nests only: a source whose nests are ALL
    soft-deleted summarises as ``nest_count == 0``, so it is not nest-BEARING, the 409
    does not fire, and every copy then skips both nest-backed operations plus the ties
    scoped to them. That is a real shape — it is what a laser job looks like after its
    nests have been removed — and a planner can save a template from it.
    """

    def _source_whose_nests_are_all_dead(self, db: Session):
        """A two-nest laser job with both nests soft-deleted, and a tie on each operation.

        Committed so the copy reads it exactly as the summary does.
        """
        source = build_nest_source(db, runs=(3, 2), tie_material=True)
        for nest in source.nests:
            nest.is_deleted = True
        db.commit()
        return source

    def test_one_omission_is_reported_once_however_many_copies_made_it(self, client: TestClient, db_session: Session):
        admin = make_user(db_session)
        source = self._source_whose_nests_are_all_dead(db_session)
        source_operation_ids = [operation.id for operation in source.operations]
        source_tie_ids = sorted(tie.id for tie in ties_of(db_session, source.work_order))
        assert len(source_tie_ids) == 2, source_tie_ids
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id, count=3, quantity_ordered=4)
        assert response.status_code == status.HTTP_201_CREATED, response.text
        body = response.json()
        assert body["created_count"] == 3

        # THE ENVELOPE: two omissions, named once each, in the source's own sequence
        # order — not six, and not three copies of the first one.
        assert [entry["source_operation_id"] for entry in body["skipped_operations"]] == source_operation_ids
        assert {entry["reason"] for entry in body["skipped_operations"]} == {"laser_nest_deleted"}
        assert sorted(entry["source_allocation_id"] for entry in body["skipped_material_allocations"]) == (
            source_tie_ids
        )
        assert {entry["reason"] for entry in body["skipped_material_allocations"]} == {"operation_not_copied"}

        # THE CHAIN: each copy's row keeps its OWN full list, because a per-work-order
        # row must describe the work order it names. Six entries on the chain, two on
        # the planner's screen — that difference IS the dedupe.
        rows = use_rows(db_session, template_id)
        assert len(rows) == 3
        for row in rows:
            assert [
                entry["source_operation_id"] for entry in row.extra_data["skipped_operations"]
            ] == source_operation_ids
            assert (
                sorted(entry["source_allocation_id"] for entry in row.extra_data["skipped_material_allocations"])
                == source_tie_ids
            )
        assert sum(len(row.extra_data["skipped_operations"]) for row in rows) == 6
        assert sum(len(row.extra_data["skipped_material_allocations"]) for row in rows) == 6
        assert len(body["skipped_operations"]) == 2
        assert len(body["skipped_material_allocations"]) == 2

    def test_the_drafts_really_are_created_carrying_no_operations_and_no_demand(
        self, client: TestClient, db_session: Session
    ):
        """What the list is warning about, asserted rather than assumed.

        This is why the envelope is safety information and not a diagnostic: the call
        SUCCEEDS. Three drafts exist, each with no operation and no material tie — no
        shortage will be raised for the sheets, and if anybody releases one, stock is
        never deducted.
        """
        admin = make_user(db_session)
        source = self._source_whose_nests_are_all_dead(db_session)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Nest group")

        response = use_template(client, headers_for(admin), template_id, count=3, quantity_ordered=4)
        assert response.status_code == status.HTTP_201_CREATED, response.text

        drafts = created_rows(db_session, response.json())
        assert len(drafts) == 3
        for draft in drafts:
            assert draft.status == WorkOrderStatus.DRAFT
            assert operations_of(db_session, draft) == []
            assert ties_of(db_session, draft) == []
            # The quantity is the REQUESTED one, not a derived sum: no nest came
            # across, so nothing overrules it.
            assert float(draft.quantity_ordered) == 4.0

    def test_an_omission_only_one_copy_made_is_still_reported(self):
        """The union is a union, not "the first copy's list", and order is preserved.

        Asserted directly on the two helpers because the API cannot produce it: every
        copy reads the same source, so through the endpoint the lists are always
        identical and "take the first" would pass. The property that matters if a future
        skip ever becomes copy-specific is that a LATER copy's unique entry survives,
        and that first occurrence wins for a repeated id.
        """
        first = SimpleNamespace(
            skipped_operations=[
                WorkOrderDuplicateSkippedOperation(source_operation_id=10, sequence=10, reason="laser_nest_deleted")
            ],
            skipped_allocations=[
                WorkOrderDuplicateSkippedAllocation(source_allocation_id=90, part_id=5, reason="part_not_available")
            ],
        )
        second = SimpleNamespace(
            skipped_operations=[
                # A repeat of the first copy's entry, then one only this copy made.
                WorkOrderDuplicateSkippedOperation(source_operation_id=10, sequence=99, reason="laser_nest_deleted"),
                WorkOrderDuplicateSkippedOperation(source_operation_id=20, sequence=20, reason="laser_nest_deleted"),
            ],
            skipped_allocations=[
                WorkOrderDuplicateSkippedAllocation(source_allocation_id=90, part_id=5, reason="part_not_tieable"),
                WorkOrderDuplicateSkippedAllocation(source_allocation_id=91, part_id=6, reason="operation_not_copied"),
            ],
        )

        operations = templates.union_skipped_operations([first, second])
        assert [entry.source_operation_id for entry in operations] == [10, 20]
        # First occurrence wins, so the surviving entry is the first copy's own — the
        # list reads in the source's sequence order rather than in discovery order.
        assert operations[0].sequence == 10

        allocations = templates.union_skipped_allocations([first, second])
        assert [entry.source_allocation_id for entry in allocations] == [90, 91]
        assert allocations[0].reason == "part_not_available"


# --------------------------------------------------------------------------- #
# K. The direct-caller guard — ``use_template`` is exported, and the list is positional
# --------------------------------------------------------------------------- #
class TestAMisalignedUnitListIsRefusedRatherThanIndexed:
    """Over HTTP the schema refuses a mismatched list 422, and this is the other door.

    ``use_template`` is in the module's ``__all__``, so a service or a script can call it
    with a list the request schema never saw. Positional data misaligns SILENTLY: a
    short list used to raise ``IndexError`` part-way through the loop — a 500 on a
    half-built transaction — and a long one dropped its tail without a word. Both are
    the exact misalignment the schema validator exists to prevent, so the direct caller
    gets the same refusal instead of a worse version of the same bug, raised before
    anything is read or written.
    """

    def _template_and_actor(self, client: TestClient, db: Session):
        admin = make_user(db)
        source = build_brake_source(db, quantity=20.0)
        template_id = saved(client, headers_for(admin), source.work_order.id, "Weld set")
        return template_row(db, template_id), admin

    def _use(self, db: Session, template, admin, *, count: int, unit_numbers: list):
        return templates.use_template(
            db,
            template=template,
            quantity_ordered=1.0,
            due_date=None,
            company_id=COMPANY_A,
            user_id=admin.id,
            audit=AuditService(db, admin),
            count=count,
            unit_numbers=unit_numbers,
        )

    def test_a_short_list_is_a_value_error_not_an_index_error(self, client: TestClient, db_session: Session):
        """``pytest.raises(ValueError)`` is the whole assertion: ``IndexError`` is not one.

        So this fails loudly against the pre-guard behavior rather than passing on a
        technicality — and it fails with the ``IndexError`` itself, which names the bug.
        """
        template, admin = self._template_and_actor(client, db_session)

        with pytest.raises(ValueError) as raised:
            self._use(db_session, template, admin, count=3, unit_numbers=[UNIT, "K-9812"])

        # It names BOTH numbers, because "wrong length" without them is a message the
        # caller cannot act on.
        message = str(raised.value)
        assert "2 unit numbers" in message
        assert "count=3" in message

        db_session.rollback()
        # Refused before the first read or write: nothing was minted.
        assert minted_work_orders(db_session) == []

    def test_a_long_list_is_refused_rather_than_silently_dropping_its_tail(
        self, client: TestClient, db_session: Session
    ):
        """The half with no exception to make it visible — which is why it needs a test.

        A longer list used to create ``count`` work orders and discard the rest, so the
        caller got fewer jobs than units with nothing anywhere saying so.
        """
        template, admin = self._template_and_actor(client, db_session)

        with pytest.raises(ValueError) as raised:
            self._use(db_session, template, admin, count=2, unit_numbers=[UNIT, "K-9812", "SN00042"])

        assert "3 unit numbers" in str(raised.value)
        assert "count=2" in str(raised.value)

        db_session.rollback()
        assert minted_work_orders(db_session) == []

    def test_a_matching_list_still_runs(self, client: TestClient, db_session: Session):
        """Non-vacuity: the guard refuses a MISMATCH, not the direct call itself."""
        template, admin = self._template_and_actor(client, db_session)

        result = self._use(db_session, template, admin, count=2, unit_numbers=[UNIT, "K-9812"])

        assert result.created_count == 2
        assert [duplicate.work_order.unit_number for duplicate in result.duplicates] == [UNIT, "K-9812"]
        db_session.rollback()
