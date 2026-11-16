"""``export_control_service.build_controlled_index``: the owner's rules (2026-11-13), record by record.

- A customer is controlled by ANY contract row (inactive, expired) flagged ITAR / export
  controlled / CUI or classified ``itar_export_controlled`` / ``cui``, or by a classified
  handling instruction (via the customer or via a contract). FCI / restricted / an export
  review flag alone do not control. A soft-deleted customer still controls its history.
- POSITIVE CLEARANCE: a free-text ``customer_name`` clears a record only when it is a known
  customer's name or code (case and whitespace ignored) with no controlling contract; blank
  or unresolvable -> unlinkable. A blank material-supply part is generic stock and passes.
- The linkage map: one-step part closure from controlled work orders, parents control
  children, a standalone laser work order is unlinkable, NCRs via part / WO / receipt, CARs
  via their own part, customer and customer-name snapshot AND the NCRs and complaints that
  reference them (every link must clear), raw-material purchases are unlinkable,
  quotes via name, lines and the fabrication quote's customer.
- Tenant isolation: another company's contracts never classify anything here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count
from typing import Optional

import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.customer_complaint import CustomerComplaint
from app.models.fabrication_quote import FabricationQuote
from app.models.governance import CustomerContract, CustomerHandlingInstruction
from app.models.part import Part
from app.models.part_number_alias import PartNumberAlias, normalize_alias_key
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import CorrectiveActionRequest, FirstArticleInspection, NonConformanceReport
from app.models.quote import Quote, QuoteLine
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.services.export_control_redaction import UNLINKABLE
from app.services.export_control_service import build_controlled_index, controlled_customers
from tests.api.kiosk_test_helpers import ensure_company

pytestmark = pytest.mark.unit

_seq = count(1)


def _n() -> int:
    return next(_seq)


def customer(db: Session, name: str, code: Optional[str] = None, *, company_id: int = 1) -> Customer:
    row = Customer(name=name, code=code or f"C{_n():04d}", company_id=company_id)
    db.add(row)
    db.flush()
    return row


def contract(db: Session, customer_row: Customer, **flags) -> CustomerContract:
    row = CustomerContract(
        company_id=customer_row.company_id,
        customer_id=customer_row.id,
        contract_name=f"contract {_n()}",
        **flags,
    )
    db.add(row)
    db.flush()
    return row


def part(
    db: Session, customer_name: Optional[str] = None, *, part_type: str = "manufactured", company_id: int = 1
) -> Part:
    row = Part(
        part_number=f"P-{_n():05d}",
        name="a part",
        part_type=part_type,
        customer_name=customer_name,
        company_id=company_id,
    )
    db.add(row)
    db.flush()
    return row


def work_center(db: Session, company_id: int = 1) -> WorkCenter:
    row = WorkCenter(name=f"WC {_n()}", code=f"WC-{_n()}", work_center_type="general", company_id=company_id)
    db.add(row)
    db.flush()
    return row


def work_order(
    db: Session,
    *,
    customer_name: Optional[str] = None,
    part_row: Optional[Part] = None,
    parent: Optional[WorkOrder] = None,
    laser: bool = False,
    company_id: int = 1,
) -> WorkOrder:
    if part_row is None and not laser:
        # A part is required off laser; a blank raw-material part is neutral (generic stock passes).
        part_row = part(db, None, part_type="raw_material", company_id=company_id)
    row = WorkOrder(
        work_order_number=f"WO-{_n():05d}",
        part_id=part_row.id if part_row is not None else None,
        parent_work_order_id=parent.id if parent is not None else None,
        work_order_type="laser_cutting" if laser else "production",
        quantity_ordered=1,
        customer_name=customer_name,
        company_id=company_id,
    )
    db.add(row)
    db.flush()
    return row


def operation(db: Session, wo: WorkOrder, *, component: Optional[Part] = None) -> WorkOrderOperation:
    row = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=work_center(db, wo.company_id).id,
        sequence=10,
        name="Op",
        component_part_id=component.id if component is not None else None,
        company_id=wo.company_id,
    )
    db.add(row)
    db.flush()
    return row


def control(index, record_type: str, record_id: int):
    return index.by_id(record_type, record_id)


@pytest.fixture
def itar(db_session: Session) -> Customer:
    row = customer(db_session, "Acme Defense", "ACMED")
    contract(db_session, row, itar_controlled=True)
    return row


@pytest.fixture
def clear(db_session: Session) -> Customer:
    return customer(db_session, "Open Commerce", "OPEN")


class TestControlledCustomers:
    @pytest.mark.parametrize(
        "flags,classification",
        [
            ({"itar_controlled": True}, "itar_export_controlled"),
            ({"export_controlled": True}, "itar_export_controlled"),
            ({"default_data_classification": "itar_export_controlled"}, "itar_export_controlled"),
            ({"contains_cui": True}, "cui"),
            ({"default_data_classification": "cui"}, "cui"),
            ({"contains_cui": True, "itar_controlled": True}, "itar_export_controlled"),
        ],
    )
    def test_each_controlling_flag(self, db_session: Session, flags, classification):
        row = customer(db_session, "X")
        made = contract(db_session, row, **flags)
        found = controlled_customers(db_session, 1)[row.id]
        assert found.classification == classification
        assert (found.customer_id, found.contract_id) == (row.id, made.id)

    @pytest.mark.parametrize(
        "flags",
        [
            {},
            {"contains_fci": True},
            {"default_data_classification": "restricted_customer"},
            {"default_data_classification": "fci"},
            {"default_data_classification": "internal"},
        ],
    )
    def test_flags_that_do_not_control(self, db_session: Session, flags):
        row = customer(db_session, "Y")
        contract(db_session, row, **flags)
        assert row.id not in controlled_customers(db_session, 1)

    def test_inactive_and_expired_contracts_still_control(self, db_session: Session):
        row = customer(db_session, "Z")
        contract(
            db_session,
            row,
            itar_controlled=True,
            active=False,
            effective_date=datetime.utcnow() - timedelta(days=900),
            expiration_date=datetime.utcnow() - timedelta(days=500),
        )
        assert row.id in controlled_customers(db_session, 1)

    def test_handling_instructions_control_via_the_customer_or_the_contract(self, db_session: Session):
        direct = customer(db_session, "Direct")
        via = customer(db_session, "Via contract")
        plain_contract = contract(db_session, via)
        review_only = customer(db_session, "Review only")
        db_session.add_all(
            [
                CustomerHandlingInstruction(
                    company_id=1,
                    customer_id=direct.id,
                    instruction_type="marking",
                    instruction_text="CUI",
                    default_data_classification="cui",
                ),
                CustomerHandlingInstruction(
                    company_id=1,
                    contract_id=plain_contract.id,
                    instruction_type="export",
                    instruction_text="ITAR",
                    default_data_classification="itar_export_controlled",
                ),
                CustomerHandlingInstruction(
                    company_id=1,
                    customer_id=review_only.id,
                    instruction_type="review",
                    instruction_text="check",
                    requires_export_review=True,
                ),
            ]
        )
        db_session.flush()
        found = controlled_customers(db_session, 1)
        assert found[direct.id].classification == "cui"
        assert found[via.id].classification == "itar_export_controlled"
        assert found[via.id].contract_id == plain_contract.id
        assert review_only.id not in found

    def test_a_soft_deleted_customer_still_controls_its_history(self, db_session: Session, itar: Customer):
        wo = work_order(db_session, customer_name="Acme Defense")
        itar.is_deleted = True
        db_session.flush()
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", wo.id).classification == "itar_export_controlled"


class TestPositiveClearance:
    def test_name_and_code_resolve_any_case_or_spacing(self, db_session: Session, itar: Customer, clear: Customer):
        by_code = work_order(db_session, customer_name="  acmed ")
        by_name = work_order(db_session, customer_name="ACME   defense")
        cleared = work_order(db_session, customer_name=" open commerce")
        cleared_code = work_order(db_session, customer_name="open")
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", by_code.id).customer_id == itar.id
        assert control(index, "work_order", by_name.id).customer_id == itar.id
        assert control(index, "work_order", cleared.id) is None
        assert control(index, "work_order", cleared_code.id) is None

    def test_blank_or_unresolvable_names_are_unlinkable(self, db_session: Session, clear: Customer):
        typo = work_order(db_session, customer_name="Open Comerce")
        blank_engineering = part(db_session, None)
        blank_material = part(db_session, None, part_type="raw_material")
        named_material = part(db_session, "Nobody Inc", part_type="hardware")
        cleared_part = part(db_session, "Open Commerce")
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", typo.id) is UNLINKABLE
        assert control(index, "part", blank_engineering.id) is UNLINKABLE
        assert control(index, "part", blank_material.id) is None, "generic stock passes"
        assert control(index, "part", named_material.id) is UNLINKABLE
        assert control(index, "part", cleared_part.id) is None
        # Unresolvable names are withheld from text too (a misspelled controlled customer).
        assert {"Open Comerce", "Nobody Inc"} <= set(index.sensitive_tokens)
        assert "Open Commerce" not in index.sensitive_tokens

    def test_a_work_order_with_no_name_is_its_parts(self, db_session: Session, itar: Customer, clear: Customer):
        cleared = work_order(db_session, part_row=part(db_session, "Open Commerce"))
        unknown = work_order(db_session, part_row=part(db_session, None))
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", cleared.id) is None
        assert control(index, "work_order", unknown.id) is UNLINKABLE

    def test_an_id_the_index_never_saw_is_unlinkable(self, db_session: Session):
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", 987654) is UNLINKABLE


class TestLinkageMap:
    def test_part_closure_is_one_step(self, db_session: Session, itar: Customer, clear: Customer):
        shared = part(db_session, "Open Commerce")  # a cleared customer's part ...
        component = part(db_session, "Open Commerce")
        controlled_wo = work_order(db_session, customer_name="Acme Defense", part_row=shared)
        operation(db_session, controlled_wo, component=component)  # ... built and consumed on an ITAR job
        other = work_order(db_session, customer_name="Open Commerce", part_row=shared)
        uses_component = work_order(
            db_session, customer_name="Open Commerce", part_row=part(db_session, "Open Commerce")
        )
        operation(db_session, uses_component, component=component)
        index = build_controlled_index(db_session, 1)
        assert control(index, "part", shared.id).customer_id == itar.id
        assert control(index, "part", component.id).customer_id == itar.id
        assert control(index, "work_order", other.id).customer_id == itar.id, "a WO building a controlled part"
        assert control(index, "work_order", uses_component.id).customer_id == itar.id

    def test_operations_follow_their_work_order_and_component(
        self, db_session: Session, itar: Customer, clear: Customer
    ):
        controlled_wo = work_order(db_session, customer_name="Acme Defense", part_row=part(db_session, "Acme Defense"))
        cleared_wo = work_order(db_session, customer_name="Open Commerce", part_row=part(db_session, "Open Commerce"))
        op_controlled = operation(db_session, controlled_wo)
        op_cleared = operation(db_session, cleared_wo)
        op_component = operation(db_session, cleared_wo, component=part(db_session, None))
        index = build_controlled_index(db_session, 1)
        assert control(index, "operation", op_controlled.id).customer_id == itar.id
        assert control(index, "operation", op_cleared.id) is None
        assert control(index, "operation", op_component.id) is UNLINKABLE

    def test_parents_control_children_and_laser_work_orders(self, db_session: Session, itar: Customer, clear: Customer):
        parent = work_order(db_session, customer_name="Acme Defense", part_row=part(db_session, "Acme Defense"))
        child = work_order(
            db_session, customer_name="Open Commerce", part_row=part(db_session, "Open Commerce"), parent=parent
        )
        laser_child = work_order(db_session, laser=True, parent=parent)
        cleared_parent = work_order(
            db_session, customer_name="Open Commerce", part_row=part(db_session, "Open Commerce")
        )
        cleared_laser_child = work_order(db_session, laser=True, parent=cleared_parent)
        standalone = work_order(db_session, laser=True)
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", child.id).customer_id == itar.id
        assert control(index, "work_order", laser_child.id).customer_id == itar.id
        assert control(index, "work_order", cleared_laser_child.id) is None
        assert control(index, "work_order", standalone.id) is UNLINKABLE

    def test_purchasing_receipts_and_ncrs(self, db_session: Session, itar: Customer, clear: Customer):
        vendor = Vendor(code="V1", name="Vendor", company_id=1)
        db_session.add(vendor)
        db_session.flush()
        reporter = User(
            email="r@x.com",
            employee_id="R1",
            first_name="R",
            last_name="R",
            hashed_password="x",
            role=UserRole.QUALITY,
            company_id=1,
        )
        db_session.add(reporter)
        db_session.flush()

        def po_with(part_row: Part):
            po = PurchaseOrder(po_number=f"PO-{_n()}", vendor_id=vendor.id, company_id=1)
            db_session.add(po)
            db_session.flush()
            line = PurchaseOrderLine(
                purchase_order_id=po.id,
                line_number=1,
                part_id=part_row.id,
                quantity_ordered=1,
                unit_price=1,
                company_id=1,
            )
            db_session.add(line)
            db_session.flush()
            receipt = POReceipt(
                receipt_number=f"R-{_n()}",
                po_line_id=line.id,
                quantity_received=1,
                lot_number=f"L-{_n()}",
                received_by=reporter.id,
                company_id=1,
            )
            db_session.add(receipt)
            db_session.flush()
            return po, receipt

        itar_po, itar_receipt = po_with(part(db_session, "Acme Defense"))
        raw_po, raw_receipt = po_with(part(db_session, None, part_type="raw_material"))
        clear_po, clear_receipt = po_with(part(db_session, "Open Commerce"))
        empty_po = PurchaseOrder(po_number="PO-EMPTY", vendor_id=vendor.id, company_id=1)
        db_session.add(empty_po)
        db_session.flush()

        def ncr(**links):
            row = NonConformanceReport(
                ncr_number=f"N-{_n()}", source="in_process", title="t", description="d", company_id=1, **links
            )
            db_session.add(row)
            db_session.flush()
            return row

        ncr_receipt = ncr(receipt_id=itar_receipt.id)
        ncr_raw = ncr(receipt_id=raw_receipt.id)
        ncr_none = ncr()
        ncr_clear = ncr(part_id=part(db_session, "Open Commerce").id)
        index = build_controlled_index(db_session, 1)
        assert control(index, "purchase_order", itar_po.id).customer_id == itar.id
        assert control(index, "purchase_order", raw_po.id) is UNLINKABLE, "a raw-material buy"
        assert control(index, "purchase_order", clear_po.id) is None
        assert control(index, "purchase_order", empty_po.id) is UNLINKABLE
        assert control(index, "receipt", itar_receipt.id).customer_id == itar.id
        assert control(index, "receipt", raw_receipt.id) is UNLINKABLE
        assert control(index, "receipt", clear_receipt.id) is None
        assert control(index, "ncr", ncr_receipt.id).customer_id == itar.id
        assert control(index, "ncr", ncr_raw.id) is UNLINKABLE
        assert control(index, "ncr", ncr_none.id) is UNLINKABLE
        assert control(index, "ncr", ncr_clear.id) is None

    def test_cars_follow_their_referrers(self, db_session: Session, itar: Customer, clear: Customer):
        def car():
            row = CorrectiveActionRequest(car_number=f"CAR-{_n()}", title="t", problem_description="p", company_id=1)
            db_session.add(row)
            db_session.flush()
            return row

        by_complaint, by_clear_ncr, orphan = car(), car(), car()
        db_session.add(
            CustomerComplaint(
                complaint_number="CC-1",
                customer_id=itar.id,
                customer_name="whatever",
                title="t",
                description="d",
                car_id=by_complaint.id,
                company_id=1,
            )
        )
        db_session.add(
            NonConformanceReport(
                ncr_number="N-CAR",
                source="in_process",
                title="t",
                description="d",
                part_id=part(db_session, "Open Commerce").id,
                car_id=by_clear_ncr.id,
                company_id=1,
            )
        )
        db_session.flush()
        index = build_controlled_index(db_session, 1)
        assert control(index, "car", by_complaint.id).customer_id == itar.id
        assert control(index, "car", by_clear_ncr.id) is None
        assert control(index, "car", orphan.id) is UNLINKABLE

    def test_cars_also_follow_their_own_part_and_customer(self, db_session: Session, itar: Customer, clear: Customer):
        """Migration 122 gave a CAR its own part, customer and customer-name snapshot. A CAR is
        cleared only when EVERY link -- direct and via referrers -- clears; any controlled or
        unresolvable one withholds it, and the snapshot is judged on its own."""

        def car(**links):
            row = CorrectiveActionRequest(
                car_number=f"CAR-{_n()}", title="t", problem_description="p", company_id=1, **links
            )
            db_session.add(row)
            db_session.flush()
            return row

        clear_part, itar_part = part(db_session, "Open Commerce"), part(db_session, "Acme Defense")
        by_clear_customer = car(customer_id=clear.id, customer_name=clear.name)
        by_itar_customer = car(customer_id=itar.id, customer_name=itar.name)
        by_clear_part = car(part_id=clear_part.id)
        by_itar_part = car(part_id=itar_part.id)
        clear_part_itar_customer = car(part_id=clear_part.id, customer_id=itar.id, customer_name=itar.name)
        # The snapshot alone names the controlled customer (by code, any case): withheld even
        # though the id clears -- the CAR recorded that name.
        snapshot_controlled = car(customer_id=clear.id, customer_name="  acmed ")
        # A snapshot no customer carries (a renamed customer's old name, a typo): fail closed.
        snapshot_unresolvable = car(customer_id=clear.id, customer_name="Open Comerce")
        # Escalated from a CLEARED NCR, then its customer set to the controlled one.
        escalated = car(part_id=clear_part.id, customer_id=itar.id, customer_name=itar.name)
        # And the other way round: a controlled referrer is not cleared by a cleared customer.
        referred_by_itar = car(customer_id=clear.id, customer_name=clear.name)
        for linked_car, linked_part in ((escalated, clear_part), (referred_by_itar, itar_part)):
            db_session.add(
                NonConformanceReport(
                    ncr_number=f"N-{_n()}",
                    source="in_process",
                    title="t",
                    description="d",
                    part_id=linked_part.id,
                    car_id=linked_car.id,
                    company_id=1,
                )
            )
        db_session.flush()

        index = build_controlled_index(db_session, 1)
        assert control(index, "car", by_clear_customer.id) is None
        assert control(index, "car", by_clear_part.id) is None
        for withheld in (by_itar_customer, by_itar_part, clear_part_itar_customer, snapshot_controlled):
            assert control(index, "car", withheld.id).customer_id == itar.id, withheld.car_number
        assert control(index, "car", snapshot_unresolvable.id) is UNLINKABLE
        assert "Open Comerce" in index.sensitive_tokens, "an unresolvable snapshot may be a controlled name misspelled"
        assert control(index, "car", escalated.id).customer_id == itar.id
        assert control(index, "car", referred_by_itar.id).customer_id == itar.id
        # A CAR id the index never enumerated is never "fine".
        assert index.by_id("car", 10**9) is UNLINKABLE

    def test_fais_and_quotes(self, db_session: Session, itar: Customer, clear: Customer):
        itar_part = part(db_session, "Acme Defense")
        clear_part = part(db_session, "Open Commerce")
        fai_itar = FirstArticleInspection(fai_number="F-1", part_id=itar_part.id, company_id=1)
        fai_clear = FirstArticleInspection(fai_number="F-2", part_id=clear_part.id, company_id=1)
        q_name = Quote(quote_number="Q-IDX-1", customer_name="acmed", company_id=1)
        q_line = Quote(quote_number="Q-IDX-2", customer_name="Open Commerce", company_id=1)
        q_fab = Quote(quote_number="Q-IDX-3", customer_name="Open Commerce", company_id=1)
        q_clear = Quote(quote_number="Q-IDX-4", customer_name="Open Commerce", company_id=1)
        q_typo = Quote(quote_number="Q-IDX-5", customer_name="Opne Commerce", company_id=1)
        db_session.add_all([fai_itar, fai_clear, q_name, q_line, q_fab, q_clear, q_typo])
        db_session.flush()
        db_session.add(
            QuoteLine(
                quote_id=q_line.id,
                line_number=1,
                part_id=itar_part.id,
                description="d",
                quantity=1,
                unit_price=1,
                company_id=1,
            )
        )
        admin = User(
            email="a@x.com",
            employee_id="A1",
            first_name="A",
            last_name="A",
            hashed_password="x",
            role=UserRole.ADMIN,
            company_id=1,
        )
        db_session.add(admin)
        db_session.flush()
        db_session.add(
            FabricationQuote(
                title="fab",
                plan_json={},
                calculation_json={},
                created_by=admin.id,
                customer_id=itar.id,
                erp_quote_id=q_fab.id,
                company_id=1,
            )
        )
        db_session.flush()
        index = build_controlled_index(db_session, 1)
        assert control(index, "fai", fai_itar.id).customer_id == itar.id
        assert control(index, "fai", fai_clear.id) is None
        assert control(index, "quote", q_name.id).customer_id == itar.id
        assert control(index, "quote", q_line.id).customer_id == itar.id
        assert control(index, "quote", q_fab.id).customer_id == itar.id
        assert control(index, "quote", q_clear.id) is None
        assert control(index, "quote", q_typo.id) is UNLINKABLE


class TestSensitiveTokensAndNumbers:
    def test_tokens_cover_numbers_aliases_drawings_and_the_customer(self, db_session: Session, itar: Customer):
        row = part(db_session, "Acme Defense")
        row.customer_part_number = "CUST-77"
        row.drawing_number = "DWG-88"
        alias = "OLD-NUMBER-1"
        db_session.add(
            PartNumberAlias(
                part_id=row.id,
                alias_number=alias,
                alias_number_key=normalize_alias_key(alias),
                reason="r",
                company_id=1,
            )
        )
        wo = work_order(db_session, customer_name="Acme Defense", part_row=row)
        db_session.flush()
        index = build_controlled_index(db_session, 1)
        tokens = set(index.sensitive_tokens)
        assert {row.part_number, "CUST-77", "DWG-88", alias, "Acme Defense", "ACMED"} <= tokens
        assert index.by_number("part", alias.lower()) == (row.id, control(index, "part", row.id))
        assert index.by_number("work_order", wo.work_order_number)[0] == wo.id
        assert index.by_customer_name(" acme defense ")[0] == itar.id


class TestTenantIsolation:
    def test_another_companys_contract_never_classifies_here(self, db_session: Session, clear: Customer):
        ensure_company(db_session, 2)
        twin = customer(db_session, "Open Commerce", "OPEN", company_id=2)
        contract(db_session, twin, itar_controlled=True)
        wo = work_order(db_session, customer_name="Open Commerce", part_row=part(db_session, "Open Commerce"))
        index = build_controlled_index(db_session, 1)
        assert control(index, "work_order", wo.id) is None
        assert control(index, "customer", clear.id) is None
        assert twin.id not in index.known["customer"], "company 2's rows are not in company 1's index"
        other = build_controlled_index(db_session, 2)
        assert control(other, "customer", twin.id).classification == "itar_export_controlled"
        assert wo.id not in other.known["work_order"]
