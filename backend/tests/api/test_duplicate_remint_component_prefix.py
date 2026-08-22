"""Duplicating a work order must not inject a DEAD part number into a fresh job.

A BOM-exploded operation's ``name`` is minted ``f"{component.part_number} -
{routing_op.name}"``. That prefix is a snapshot of the number the component
carried when the SOURCE work order was raised. ``duplicate_work_order`` copies
``name`` verbatim alongside a ``component_part_id`` it copies unchanged -- so
after a renumber, duplicating an old job puts a number nothing recognizes onto a
brand-new DRAFT that will be released and run. Not a decaying legacy problem:
every future duplicate of that work order carries it forward.

So the prefix is re-minted -- but ONLY under a proof: the existing prefix must
itself resolve to THIS operation's component part, through its current number or
one of its RETIRED numbers.

THAT GUARD IS THE POINT OF THIS FILE. ``PUT /work-orders/operations/{id}`` exposes
``name`` as free text, so an operation can legitimately hold a hand-typed name
that merely LOOKS minted -- ``"Weld - Station 3"`` satisfies every naive pattern
test. Rewriting that destroys a supervisor's instruction. A copy that fails to
refresh is cosmetic staleness the floor reads past; a copy that rewrites a human's
words is lost information. So the tests below spend more effort on what must NOT
be rewritten than on what must.
"""

import pytest
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.work_order_duplicate_service import _remint_component_prefix

COMPANY_A = 1


def _part(db: Session, *, number: str, name: str = "Bracket") -> Part:
    part = Part(
        part_number=number,
        name=name,
        part_type="purchased",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _retire(db: Session, *, part: Part, number: str) -> None:
    db.add(
        PartNumberAlias(
            part_id=part.id,
            alias_number=number,
            alias_number_key=normalize_alias_key(number),
            reason="renumbered",
            company_id=COMPANY_A,
        )
    )
    db.commit()


def _operation(db: Session, *, name: str, component: Part | None, suffix: str) -> WorkOrderOperation:
    wc = WorkCenter(name=f"WC {suffix}", code=f"WC-{suffix}", work_center_type="press_brake", company_id=COMPANY_A)
    db.add(wc)
    produced = Part(
        part_number=f"ASSY-{suffix}",
        name="Assembly",
        part_type="assembly",
        unit_of_measure="each",
        is_active=True,
        company_id=COMPANY_A,
    )
    db.add(produced)
    db.flush()
    wo = WorkOrder(
        work_order_number=f"WO-DUP-{suffix}",
        part_id=produced.id,
        quantity_ordered=5,
        status="complete",
        priority=3,
        company_id=COMPANY_A,
    )
    db.add(wo)
    db.flush()
    op = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=wc.id,
        sequence=10,
        operation_number="10",
        name=name,
        component_part_id=component.id if component else None,
        company_id=COMPANY_A,
    )
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


@pytest.mark.api
@pytest.mark.requires_db
class TestRemintsAStaleComponentPrefix:
    def test_a_retired_number_in_the_prefix_is_refreshed(self, db_session: Session):
        """The case the feature exists for: the source job predates a renumber."""
        component = _part(db_session, number="NEW-456")
        _retire(db_session, part=component, number="OLD-123")
        op = _operation(db_session, name="OLD-123 - Deburr", component=component, suffix="R1")

        assert _remint_component_prefix(op, COMPANY_A, db_session) == "NEW-456 - Deburr"

    def test_a_current_prefix_is_returned_untouched(self, db_session: Session):
        """The common case. Returns the ORIGINAL string, not a rebuilt one."""
        component = _part(db_session, number="NEW-456")
        op = _operation(db_session, name="NEW-456 - Deburr", component=component, suffix="R2")

        assert _remint_component_prefix(op, COMPANY_A, db_session) == "NEW-456 - Deburr"

    def test_case_insensitive_match_is_not_treated_as_stale(self, db_session: Session):
        """Mixed-case rows exist; a case-only difference is the same number."""
        component = _part(db_session, number="NEW-456")
        op = _operation(db_session, name="new-456 - Deburr", component=component, suffix="R3")

        # Recognized as current, so left alone rather than rewritten to differ only in case.
        assert _remint_component_prefix(op, COMPANY_A, db_session) == "new-456 - Deburr"

    def test_the_remainder_is_preserved_exactly(self, db_session: Session):
        """Only the prefix moves. Everything after the first ' - ' is the instruction."""
        component = _part(db_session, number="NEW-456")
        _retire(db_session, part=component, number="OLD-123")
        op = _operation(db_session, name="OLD-123 - Deburr - then break edges", component=component, suffix="R4")

        assert _remint_component_prefix(op, COMPANY_A, db_session) == "NEW-456 - Deburr - then break edges"


@pytest.mark.api
@pytest.mark.requires_db
class TestNeverRewritesSomethingItCannotProve:
    """The guard. Each of these would be DESTROYED by a naive prefix rewrite."""

    def test_a_hand_typed_name_that_looks_minted_is_left_alone(self, db_session: Session):
        """The motivating hazard.

        `PUT /work-orders/operations/{id}` exposes `name` as free text, so
        "Weld - Station 3" is a perfectly ordinary supervisor-typed instruction that
        satisfies every naive "split on ' - '" test. Rewriting it to
        "NEW-456 - Station 3" destroys what a human wrote.
        """
        component = _part(db_session, number="NEW-456")
        op = _operation(db_session, name="Weld - Station 3", component=component, suffix="G1")

        assert _remint_component_prefix(op, COMPANY_A, db_session) == "Weld - Station 3"

    def test_a_prefix_naming_a_DIFFERENT_part_is_left_alone(self, db_session: Session):
        """Resolvable, but not to THIS component -- so it is not this operation's prefix."""
        component = _part(db_session, number="NEW-456")
        _part(db_session, number="SOMEONE-ELSE-1", name="Different article")
        op = _operation(db_session, name="SOMEONE-ELSE-1 - Deburr", component=component, suffix="G2")

        assert _remint_component_prefix(op, COMPANY_A, db_session) == "SOMEONE-ELSE-1 - Deburr"

    def test_an_operation_with_no_component_is_left_alone(self, db_session: Session):
        """Most operations carry no component at all."""
        op = _operation(db_session, name="OLD-123 - Deburr", component=None, suffix="G3")
        assert _remint_component_prefix(op, COMPANY_A, db_session) == "OLD-123 - Deburr"

    def test_a_name_with_no_separator_is_left_alone(self, db_session: Session):
        component = _part(db_session, number="NEW-456")
        op = _operation(db_session, name="Deburr", component=component, suffix="G4")
        assert _remint_component_prefix(op, COMPANY_A, db_session) == "Deburr"

    def test_an_empty_prefix_is_left_alone(self, db_session: Session):
        component = _part(db_session, number="NEW-456")
        op = _operation(db_session, name=" - Deburr", component=component, suffix="G5")
        assert _remint_component_prefix(op, COMPANY_A, db_session) == " - Deburr"

    def test_another_companys_retired_number_does_not_resolve(self, db_session: Session):
        """Invariant 1 -- the resolve is tenant-scoped, so a cross-tenant alias is inert."""
        component = _part(db_session, number="NEW-456")
        other = Part(
            part_number="THEIRS-1",
            name="Theirs",
            part_type="purchased",
            unit_of_measure="each",
            is_active=True,
            company_id=999,
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            PartNumberAlias(
                part_id=other.id,
                alias_number="OLD-123",
                alias_number_key="OLD-123",
                reason="theirs",
                company_id=999,
            )
        )
        db_session.commit()

        op = _operation(db_session, name="OLD-123 - Deburr", component=component, suffix="G6")
        assert _remint_component_prefix(op, COMPANY_A, db_session) == "OLD-123 - Deburr"


@pytest.mark.api
@pytest.mark.requires_db
class TestDuplicateEndToEnd:
    def test_the_duplicate_carries_the_refreshed_prefix(self, db_session: Session):
        """Through the real service, not just the helper.

        The source work order is COMPLETE and its operation still names the retired
        number -- exactly what a job raised before a renumber looks like. The
        duplicate is a brand-new DRAFT that will be released and run, so it must not
        inherit a dead identifier.
        """
        from app.services.work_order_duplicate_service import duplicate_work_order

        component = _part(db_session, number="NEW-456")
        _retire(db_session, part=component, number="OLD-123")
        op = _operation(db_session, name="OLD-123 - Deburr", component=component, suffix="E1")
        source = db_session.query(WorkOrder).filter(WorkOrder.id == op.work_order_id).one()

        from app.services.audit_service import AuditService

        result = duplicate_work_order(
            db_session,
            source=source,
            quantity_ordered=5,
            due_date=None,
            company_id=COMPANY_A,
            user_id=1,
            audit=AuditService(db_session, None, None),
        )
        db_session.flush()

        new_ops = (
            db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == result.work_order.id).all()
        )
        assert len(new_ops) == 1
        assert new_ops[0].name == "NEW-456 - Deburr", "the duplicate inherited a dead part number"
        # The component FK is copied unchanged -- the re-mint only refreshes how it is
        # DISPLAYED, never what the operation is for.
        assert new_ops[0].component_part_id == component.id

        # And the SOURCE is untouched: this is a copy-time transform, not a backfill.
        db_session.refresh(op)
        assert op.name == "OLD-123 - Deburr"
