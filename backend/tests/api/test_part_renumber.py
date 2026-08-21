"""Renumbering a part in place.

The owner's case (2026-08-21): a part the shop has been RUNNING needs a different
number. Supersession was considered and rejected -- stock, open work orders and BOM
lines all point at the existing part, so superseding forces BOM rework, a stock
transfer, and two permanent identities for one physical article.

The properties worth pinning, and why a naive test would not catch them:

* **THE DRAIN ORDERING.** Operation names on assembly work orders bake in the
  component's number, and that text doubles as a LOOKUP KEY for repairing a NULL
  ``component_part_id`` and reconciling ``component_quantity`` -- the operator's
  quantity target. The renumber must repair those links BEFORE the swap, while the
  old number still matches. Asserting "the renumber returned 200" passes with the
  ordering reversed; asserting the FK landed does not.
* **STRINGS ARE NEVER REWRITTEN.** An operation name on a released work order is
  part of the released quality plan. A test that only checked the FK would pass if
  someone "helpfully" also rewrote the text.
* **THE AUDIT ROW IS FILED UNDER THE OLD NUMBER.** ``log_update`` reads the
  attribute after the swap, so the natural implementation files it under the NEW
  one -- and searching the OLD number is exactly the search anyone investigating a
  renumber performs.
* **THE SHEET SPEC.** For sheet and plate the number IS the material spec. The
  impact read must say what the matcher reads before and after, because losing the
  spec silently stops the sheet being suggested for nests with no error anywhere.
* **COLLISIONS.** Three holders (live, soft-deleted, retired alias) and the
  compare-and-swap. ``Part`` maps no version column, so the old number string is
  the only concurrency control there is.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.bom import BOM, BOMItem
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation

COMPANY_A = 1


def _part(
    db: Session,
    *,
    number: str,
    name: str = "Widget",
    part_type: str = "manufactured",
    is_deleted: bool = False,
) -> Part:
    p = Part(
        part_number=number,
        name=name,
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        is_deleted=is_deleted,
        company_id=COMPANY_A,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _renumber(
    client: TestClient,
    headers: dict,
    part: Part,
    new: str,
    *,
    expected: str = None,
    reason="Customer renumbered the print",
):
    return client.post(
        f"/api/v1/parts/{part.id}/renumber",
        headers=headers,
        json={
            "new_part_number": new,
            "expected_part_number": expected if expected is not None else part.part_number,
            "reason": reason,
        },
    )


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberHappyPath:
    def test_number_moves_and_the_old_one_is_retired(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _part(db_session, number="OLD-123")

        response = _renumber(client, auth_headers, part, "NEW-456")
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["part_number"] == "NEW-456"
        assert body["previous_part_number"] == "OLD-123"
        assert body["alias_created"] is True

        db_session.refresh(part)
        assert part.part_number == "NEW-456"

        alias = db_session.query(PartNumberAlias).filter(PartNumberAlias.part_id == part.id).one()
        assert alias.alias_number == "OLD-123"
        assert alias.alias_number_key == "OLD-123"
        assert alias.reason == "Customer renumbered the print"

    def test_the_old_number_still_finds_the_part(self, client: TestClient, auth_headers: dict, db_session: Session):
        """The promise the whole feature makes."""
        part = _part(db_session, number="OLD-123")
        assert _renumber(client, auth_headers, part, "NEW-456").status_code == 200

        found = client.get("/api/v1/parts/by-number/OLD-123", headers=auth_headers)
        assert found.status_code == status.HTTP_200_OK
        assert found.json()["part_number"] == "NEW-456"
        assert found.headers.get("X-Resolved-From-Alias") == "OLD-123"

    def test_the_new_number_is_uppercased(self, client: TestClient, auth_headers: dict, db_session: Session):
        """Mirrors PartBase.uppercase_part_number.

        Without it the verb could mint a lowercase number no create path could
        produce -- and the unique constraint is case-SENSITIVE, so it would sit
        happily beside its own twin.
        """
        part = _part(db_session, number="OLD-123")
        assert _renumber(client, auth_headers, part, "new-456").status_code == 200
        db_session.refresh(part)
        assert part.part_number == "NEW-456"

    def test_restating_the_current_number_is_a_no_op(self, client: TestClient, auth_headers: dict, db_session: Session):
        """A request that changes nothing must not fail -- and must not write."""
        part = _part(db_session, number="SAME-1")
        before = db_session.query(AuditLog).count()

        response = _renumber(client, auth_headers, part, "SAME-1")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["no_op"] is True
        assert db_session.query(PartNumberAlias).count() == 0
        assert db_session.query(AuditLog).count() == before, "a no-op must not write an audit row"

    def test_renaming_back_reclaims_the_alias(self, client: TestClient, auth_headers: dict, db_session: Session):
        """A -> B -> A must not collide with its own retired number.

        The number is live again, and a live part always beats a retired one, so the
        row is dead weight. Reclaiming it keeps the two key spaces disjoint without
        inventing a third 'retired but superseded' state.
        """
        part = _part(db_session, number="A-1")
        assert _renumber(client, auth_headers, part, "B-2").status_code == 200
        db_session.refresh(part)

        response = _renumber(client, auth_headers, part, "A-1", expected="B-2")
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["alias_reclaimed"] is True

        db_session.refresh(part)
        assert part.part_number == "A-1"
        keys = {a.alias_number_key for a in db_session.query(PartNumberAlias).all()}
        assert keys == {"B-2"}, f"A-1 should no longer be a retired number: {keys}"


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberRefusals:
    def test_live_holder_refuses_409(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _part(db_session, number="OLD-123")
        _part(db_session, number="TAKEN-1", name="Something else")

        response = _renumber(client, auth_headers, part, "TAKEN-1")
        assert response.status_code == status.HTTP_409_CONFLICT
        db_session.refresh(part)
        assert part.part_number == "OLD-123", "a refusal must leave the row untouched"
        assert db_session.query(PartNumberAlias).count() == 0

    def test_soft_deleted_holder_refuses_409_not_500(self, client: TestClient, auth_headers: dict, db_session: Session):
        """The constraint has no partial predicate, so a tombstone still owns its number."""
        part = _part(db_session, number="OLD-123")
        _part(db_session, number="GONE-1", is_deleted=True)

        response = _renumber(client, auth_headers, part, "GONE-1")
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "deleted" in response.json()["detail"].lower()

    def test_retired_number_of_another_part_refuses_409(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """The dangerous case: re-issuing a retired number is undetectable afterwards."""
        other = _part(db_session, number="OTHER-NEW")
        db_session.add(
            PartNumberAlias(
                part_id=other.id,
                alias_number="RETIRED-1",
                alias_number_key="RETIRED-1",
                reason="earlier renumber",
                company_id=COMPANY_A,
            )
        )
        db_session.commit()
        part = _part(db_session, number="OLD-123")

        response = _renumber(client, auth_headers, part, "RETIRED-1")
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "retired" in response.json()["detail"].lower()

    def test_stale_expected_number_refuses_409(self, client: TestClient, auth_headers: dict, db_session: Session):
        """Compare-and-swap. Part maps NO version column, so this string is the lock."""
        part = _part(db_session, number="ACTUAL-1")

        response = _renumber(client, auth_headers, part, "NEW-456", expected="WHAT-THE-CLIENT-SAW")
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "changed while you were editing" in response.json()["detail"]
        db_session.refresh(part)
        assert part.part_number == "ACTUAL-1"

    def test_blank_reason_refuses_422(self, client: TestClient, auth_headers: dict, db_session: Session):
        """min_length=1 alone passes "   " -- every identity verb here requires a real reason."""
        part = _part(db_session, number="OLD-123")
        response = _renumber(client, auth_headers, part, "NEW-456", reason="   ")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_unknown_part_404s(self, client: TestClient, auth_headers: dict):
        response = client.post(
            "/api/v1/parts/999999/renumber",
            headers=auth_headers,
            json={"new_part_number": "NEW-1", "expected_part_number": "OLD-1", "reason": "x"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberRbac:
    def _headers_for(self, client: TestClient, db_session: Session, role: UserRole) -> dict:
        from tests.api.kiosk_test_helpers import make_user, user_headers

        user = make_user(db_session, role=role, company_id=COMPANY_A)
        return user_headers(user)

    def test_supervisor_is_refused(self, client: TestClient, db_session: Session):
        """Deliberately NARROWER than PUT /parts/{id}.

        Renumbering is a controlled change to article identity (AS9100D 8.5.2), so it
        sits with POST /parts/{id}/revision, not with the edit tier. A supervisor who
        could see the button and get a 403 is the disagreement the gate prevents.
        """
        part = _part(db_session, number="OLD-123")
        headers = self._headers_for(client, db_session, UserRole.SUPERVISOR)
        assert _renumber(client, headers, part, "NEW-456").status_code == status.HTTP_403_FORBIDDEN

    def test_operator_is_refused(self, client: TestClient, db_session: Session):
        part = _part(db_session, number="OLD-123")
        headers = self._headers_for(client, db_session, UserRole.OPERATOR)
        assert _renumber(client, headers, part, "NEW-456").status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberAudit:
    def test_audit_row_is_filed_under_the_OLD_number(self, client: TestClient, auth_headers: dict, db_session: Session):
        """The search anyone investigating a renumber actually performs.

        ``log_update`` reads ``part.part_number`` AFTER the swap, so the natural
        implementation files the row under the NEW number -- and an auditor searching
        the old one finds nothing at all.
        """
        part = _part(db_session, number="OLD-123")
        assert _renumber(client, auth_headers, part, "NEW-456").status_code == 200

        row = (
            db_session.query(AuditLog)
            .filter(AuditLog.resource_type == "part", AuditLog.resource_id == part.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.resource_identifier == "OLD-123", "must be findable by the retired number"

        extra = row.extra_data or {}
        assert extra["old_part_number"] == "OLD-123"
        assert extra["new_part_number"] == "NEW-456"
        assert extra["reason"] == "Customer renumbered the print"

    def test_resource_type_is_part_from_the_parts_door(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """An auditor must not need to know which URL was used."""
        part = _part(db_session, number="RM-OLD", part_type="raw_material")
        assert _renumber(client, auth_headers, part, "RM-NEW").status_code == 200
        row = db_session.query(AuditLog).filter(AuditLog.resource_id == part.id).order_by(AuditLog.id.desc()).first()
        assert row.resource_type == "part"


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberDrainOrdering:
    """The load-bearing ordering constraint of the whole design."""

    def _assembly_job(self, db: Session, component: Part) -> WorkOrderOperation:
        """A work order whose operation carries the component's number as its name prefix.

        This is what work_orders.py mints for a BOM-exploded assembly:
        ``f"{component.part_number} - {routing_op.name}"``, with
        ``component_part_id`` NULL -- precisely the state in which the STRING is
        load-bearing rather than decorative.

        **The ACTIVE BOM is not scenery.** ``_reconcile_operation_component_quantities``
        is BOM-membership-gated: it builds its number->id map from the work order's
        own produced part's active BOM and bails outright when there is none. Without
        a real BOM here the drain would be a no-op and every assertion below would
        pass against a completely broken implementation -- which is exactly the class
        of test this file exists to avoid.
        """
        wc = WorkCenter(name="Brake", code=f"BRK-{component.id}", work_center_type="press_brake", company_id=COMPANY_A)
        db.add(wc)
        assembly = _part(db, number=f"ASSY-{component.id}", name="Assembly", part_type="assembly")

        bom = BOM(
            part_id=assembly.id,
            revision="A",
            status="released",
            is_active=True,
            bom_type="standard",
            company_id=COMPANY_A,
        )
        db.add(bom)
        db.flush()
        db.add(
            BOMItem(
                bom_id=bom.id,
                component_part_id=component.id,
                item_number=10,
                quantity=2.0,
                item_type="buy",
                line_type="component",
                unit_of_measure="each",
                company_id=COMPANY_A,
            )
        )

        wo = WorkOrder(
            work_order_number=f"WO-DRAIN-{component.id}",
            part_id=assembly.id,
            quantity_ordered=10,
            status="released",
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
            name=f"{component.part_number} - Deburr",
            component_part_id=None,
            company_id=COMPANY_A,
        )
        db.add(op)
        db.commit()
        db.refresh(op)
        return op

    def test_the_fixture_actually_exercises_the_drain(self, db_session: Session):
        """Guard on the guard.

        Every assertion in this class is meaningless if the reconcile bails for lack
        of an active BOM. This proves the fixture reaches the code path -- so that if
        someone later simplifies the fixture, THIS fails loudly instead of the drain
        tests silently becoming no-ops.
        """
        from app.api.endpoints.work_orders import _bom_required_quantities_by_component

        component = _part(db_session, number="GATE-CHECK-1", part_type="purchased")
        op = self._assembly_job(db_session, component)
        wo = db_session.query(WorkOrder).filter(WorkOrder.id == op.work_order_id).one()

        quantities, by_number, _ = _bom_required_quantities_by_component(db_session, wo, COMPANY_A)
        assert quantities, "fixture has no active BOM -- the drain would be a no-op"
        assert "GATE-CHECK-1" in by_number

    def test_links_are_repaired_BEFORE_the_swap(self, client: TestClient, auth_headers: dict, db_session: Session):
        """THE load-bearing property of the entire design.

        The operation's ``component_part_id`` is NULL and the only thing connecting it
        to the component is the name prefix. The renumber must repair that link while
        the old number still matches; afterwards the lookup would miss forever and
        the miss is a silent ``continue`` -- taking ``component_quantity``, the
        OPERATOR'S QUANTITY TARGET, out of reconciliation with it.

        Asserting "the renumber returned 200" passes with the ordering reversed.
        Asserting the FK landed does not.
        """
        component = _part(db_session, number="OLD-123", part_type="purchased")
        op = self._assembly_job(db_session, component)
        assert op.component_part_id is None

        response = _renumber(client, auth_headers, component, "NEW-456")
        assert response.status_code == 200, response.text

        db_session.refresh(op)
        assert op.component_part_id == component.id, (
            "the component link was NOT repaired before the swap -- the name prefix is now "
            "stale and the quantity target will never reconcile again"
        )
        assert response.json()["work_orders_repaired"] >= 1

    def test_the_quantity_target_still_reconciles_after_the_renumber(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """What the repaired link is FOR.

        BOM says 2 per assembly, work order is for 10, so the operation's target is
        20. If the drain did not run, the reconcile can no longer find this operation
        and the target never updates.
        """
        component = _part(db_session, number="OLD-123", part_type="purchased")
        op = self._assembly_job(db_session, component)

        assert _renumber(client, auth_headers, component, "NEW-456").status_code == 200

        db_session.refresh(op)
        assert float(op.component_quantity or 0) == 20.0, f"expected 2/assembly x 10 = 20, got {op.component_quantity}"

    def test_operation_names_are_never_rewritten(self, client: TestClient, auth_headers: dict, db_session: Session):
        """An operation name on a released work order is part of the released quality plan.

        CLAUDE.md's operation_number convention made this exact call for this exact
        reason. A test that only checked the FK would pass if someone also rewrote
        the text "to keep it consistent".
        """
        component = _part(db_session, number="OLD-123", part_type="purchased")
        op = self._assembly_job(db_session, component)
        original_name = op.name

        assert _renumber(client, auth_headers, component, "NEW-456").status_code == 200

        db_session.refresh(op)
        assert op.name == original_name, "released operation text must not be mutated"
        assert op.name.startswith("OLD-123 - ")

    def test_response_reports_what_still_carries_the_old_prefix(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        component = _part(db_session, number="OLD-123", part_type="purchased")
        self._assembly_job(db_session, component)

        response = _renumber(client, auth_headers, component, "NEW-456")
        assert response.status_code == 200
        assert response.json()["operations_with_stale_prefix"] >= 1


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberImpactRead:
    def test_impact_is_a_pure_read(self, client: TestClient, auth_headers: dict, db_session: Session):
        """No audit row, no alias row -- structurally, the service takes no actor."""
        part = _part(db_session, number="OLD-123")
        audit_before = db_session.query(AuditLog).count()

        response = client.get(
            f"/api/v1/parts/{part.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": "NEW-456"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert db_session.query(AuditLog).count() == audit_before
        assert db_session.query(PartNumberAlias).count() == 0
        db_session.refresh(part)
        assert part.part_number == "OLD-123"

    def test_impact_reports_blockers_verbatim(self, client: TestClient, auth_headers: dict, db_session: Session):
        """The screen must never disagree with the refusal the operator is about to get."""
        part = _part(db_session, number="OLD-123")
        _part(db_session, number="TAKEN-1", name="Something else")

        impact = client.get(
            f"/api/v1/parts/{part.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": "TAKEN-1"},
        ).json()
        assert impact["eligible"] is False
        assert len(impact["blockers"]) >= 1

        write = _renumber(client, auth_headers, part, "TAKEN-1")
        assert write.status_code == 409
        assert write.json()["detail"] == impact["blockers"][0]["detail"], "read and write must agree"

    def test_impact_accepts_an_invalid_candidate_without_422ing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """A PartNumber-typed query param would 422 before the handler ran.

        The operator could then not ask "what would happen if I used this?", and the
        READ would fail earlier and differently than the WRITE -- leaving the screen
        unable to explain the very refusal it exists to preview.
        """
        part = _part(db_session, number="OLD-123")
        response = client.get(
            f"/api/v1/parts/{part.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": '1/4" PLATE 48 X 96'},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_impact_lists_existing_retired_numbers(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _part(db_session, number="B-2")
        db_session.add(
            PartNumberAlias(
                part_id=part.id,
                alias_number="A-1",
                alias_number_key="A-1",
                reason="first renumber",
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        impact = client.get(f"/api/v1/parts/{part.id}/renumber-impact", headers=auth_headers).json()
        assert impact["existing_aliases"] == ["A-1"]


@pytest.mark.api
@pytest.mark.requires_db
class TestSheetSpecDisclosure:
    """The owner's actual case is sheet stock, so this is the disclosure that matters.

    For sheet and plate the part number IS the material spec -- thickness, size and
    alloy are parsed out of the string. A renumber that stops the number stating a
    spec makes the sheet stop being SUGGESTED for nests, silently, with no error
    anywhere. That is the shape of the July 2026 failure where 65 of 74 nests went
    untied.
    """

    def test_losing_the_spec_is_disclosed(self, client: TestClient, auth_headers: dict, db_session: Session):
        sheet = _part(db_session, number="0.250-60X120-A36", name="Plate", part_type="raw_material")

        impact = client.get(
            f"/api/v1/parts/{sheet.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": "RM-1042"},
        ).json()

        codes = {a["code"] for a in impact["advisories"]}
        assert "SHEET_SPEC_LOST" in codes, impact["advisories"]
        # And it is an ADVISORY, not a blocker -- if the current string is wrong the
        # matcher is ALREADY mis-matching, so refusing would block the repair.
        assert impact["eligible"] is True
        assert impact["sheet"]["thickness_before"] is not None
        assert impact["sheet"]["thickness_after"] is None

    def test_gaining_a_spec_is_disclosed(self, client: TestClient, auth_headers: dict, db_session: Session):
        sheet = _part(db_session, number="WERCO-SHT-12", name="Plate stock", part_type="raw_material")

        impact = client.get(
            f"/api/v1/parts/{sheet.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": "0.250-60X120-A36"},
        ).json()
        codes = {a["code"] for a in impact["advisories"]}
        assert "SHEET_SPEC_GAINED" in codes, impact["advisories"]

    def test_a_non_sheet_part_gets_no_sheet_advisory(self, client: TestClient, auth_headers: dict, db_session: Session):
        part = _part(db_session, number="OLD-123", name="Bracket", part_type="manufactured")
        impact = client.get(
            f"/api/v1/parts/{part.id}/renumber-impact",
            headers=auth_headers,
            params={"new_part_number": "NEW-456"},
        ).json()
        codes = {a["code"] for a in impact["advisories"]}
        assert not (codes & {"SHEET_SPEC_LOST", "SHEET_SPEC_GAINED", "SHEET_SPEC_CHANGED"})

    def test_the_write_reports_whether_the_spec_moved(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        sheet = _part(db_session, number="0.250-60X120-A36", name="Plate", part_type="raw_material")
        response = _renumber(client, auth_headers, sheet, "RM-1042")
        assert response.status_code == 200
        assert response.json()["sheet_spec_changed"] is True


@pytest.mark.api
@pytest.mark.requires_db
class TestRenumberTenantIsolation:
    def test_cannot_renumber_another_companys_part(self, client: TestClient, auth_headers: dict, db_session: Session):
        other = Part(
            part_number="THEIRS-1",
            name="Not ours",
            part_type="manufactured",
            unit_of_measure="each",
            is_active=True,
            company_id=999,
        )
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        response = client.post(
            f"/api/v1/parts/{other.id}/renumber",
            headers=auth_headers,
            json={"new_part_number": "MINE-1", "expected_part_number": "THEIRS-1", "reason": "nope"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND
        db_session.refresh(other)
        assert other.part_number == "THEIRS-1"

    def test_another_companys_number_is_not_a_collision(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        """Uniqueness is (company_id, part_number) -- another tenant's number is free here."""
        db_session.add(
            Part(
                part_number="SHARED-1",
                name="Theirs",
                part_type="manufactured",
                unit_of_measure="each",
                is_active=True,
                company_id=999,
            )
        )
        db_session.commit()
        part = _part(db_session, number="OLD-123")

        assert _renumber(client, auth_headers, part, "SHARED-1").status_code == 200
