"""Unit coverage for app/services/supplier_approval_service.py -- the ASL vocabulary, the one
writer (``apply_vendor_approval``) and the advisory PO checks (``approval_warnings``).

Pure: no database. The labels exercised are SHAPED like the QL 7.4 dropdown list (en-dash
labels, trailing spaces, its two misspellings, the " Cert" suffix) but are typed here, not read
from the controlled workbook.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.purchasing import VENDOR_APPROVAL_STATUS_VALUES
from app.services import supplier_approval_service as asl

pytestmark = [pytest.mark.unit]

TODAY = date(2026, 11, 13)


def vendor(**overrides):
    values = dict(
        name="Northwind Metals",
        approval_status=None,
        approved_scopes=None,
        approval_bases=None,
        approval_date=None,
        reevaluation_due=None,
        last_performance_evaluation=None,
        approval_notes=None,
        is_approved=False,
        is_active=True,
        is_deleted=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def part(part_id, part_type="purchased", supply_scope=None, number=None):
    return SimpleNamespace(
        id=part_id, part_number=number or f"P-{part_id}", part_type=part_type, supply_scope=supply_scope
    )


# --------------------------------------------------------------------------- vocabulary


def test_status_vocabulary_matches_the_model_check():
    assert tuple(status.value for status in asl.ApprovalStatus) == VENDOR_APPROVAL_STATUS_VALUES
    assert set(asl.STATUS_LABELS) == set(VENDOR_APPROVAL_STATUS_VALUES)


def test_nineteen_scopes_and_seven_bases_each_with_a_label():
    assert len(asl.AslScope) == 19 and set(asl.SCOPE_LABELS) == {s.value for s in asl.AslScope}
    assert len(asl.ApprovalBasis) == 7 and set(asl.BASIS_LABELS) == {b.value for b in asl.ApprovalBasis}


@pytest.mark.parametrize(
    "label, slug",
    [
        ("Approved", "approved"),
        ("approved ", "approved"),
        ("Approved - Customer Directed", "approved_customer_directed"),
        ("Approved – Customer Directed", "approved_customer_directed"),
        ("APPROVED-CUSTOMER DIRECTED", "approved_customer_directed"),
        ("Conditional", "conditional"),
        ("disqualified", "disqualified"),
    ],
)
def test_status_labels_normalize(label, slug):
    assert asl.parse_status(label) == slug


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_status_is_not_on_the_asl(blank):
    assert asl.parse_status(blank) is None


@pytest.mark.parametrize(
    "label, slug",
    [
        # Trailing spaces exactly as the dropdown carries them.
        ("Hardware ", "hardware"),
        ("Machining ", "machining"),
        ("Special Process- Heat Treatment ", "special_process_heat_treatment"),
        ("Special Process- Plating / Coating", "special_process_plating_coating"),
        ("Special Process - Passivation", "special_process_passivation"),
        # En-dash labels and the sheet's misspellings.
        ("Raw Material – Metalics", "raw_material_metallics"),
        ("Raw Material – Composite", "raw_material_composite"),
        ("Raw Material — Other", "raw_material_other"),
        ("NDT / Inspection / Labroatory Services", "ndt_inspection_laboratory"),
        # The corrected spellings and the slugs themselves are accepted too.
        ("Raw Material - Metallics", "raw_material_metallics"),
        ("NDT / Inspection / Laboratory Services", "ndt_inspection_laboratory"),
        ("paint_powders", "paint_powders"),
        ("Customer-Directed Supplier", "customer_directed_supplier"),
    ],
)
def test_scope_labels_normalize(label, slug):
    assert asl.parse_scope(label) == slug


@pytest.mark.parametrize(
    "label, slug",
    [
        ("AS9100 Cert", "as9100"),
        ("AS9100", "as9100"),
        ("ISO 9001 Cert", "iso9001"),
        ("iso9001", "iso9001"),
        ("ISO 17025 Cert", "iso17025"),
        ("NADCAP Cert", "nadcap"),
        ("Questionnaire", "questionnaire"),
        ("Customer Requirement", "customer_requirement"),
    ],
)
def test_basis_labels_normalize(label, slug):
    assert asl.parse_basis(label) == slug


def test_unknown_labels_are_refused_naming_the_allowed_values():
    with pytest.raises(asl.AslValueError, match="Allowed: Approved, Conditional"):
        asl.parse_status("Probationary")
    with pytest.raises(asl.AslValueError, match="Hardware"):
        asl.parse_scope("Raw Material - Unobtainium")
    with pytest.raises(asl.AslValueError, match="NADCAP Cert"):
        asl.parse_basis("Handshake")


def test_multi_value_cells_dedupe_into_canonical_order():
    assert asl.parse_scopes("Machining ; Hardware ;hardware") == ["hardware", "machining"]
    assert asl.parse_scopes(["Paint / Powders", "Adhesives / Bonding Materials"]) == [
        "adhesives_bonding",
        "paint_powders",
    ]
    assert asl.parse_bases("NADCAP Cert, AS9100 Cert") == ["as9100", "nadcap"]
    assert asl.parse_scopes("") == [] and asl.parse_scopes(None) == []


# --------------------------------------------------------------------------- the writer


@pytest.mark.parametrize(
    "status, expected",
    [
        ("approved", True),
        ("approved_customer_directed", True),
        ("conditional", False),
        ("disqualified", False),
    ],
)
@pytest.mark.parametrize("was", [True, False])
def test_is_approved_is_derived_from_the_status(status, expected, was):
    row = vendor(is_approved=was)
    asl.apply_vendor_approval(row, status=status)
    assert row.approval_status == status and row.is_approved is expected


@pytest.mark.parametrize("was", [True, False])
def test_no_status_leaves_is_approved_exactly_as_it_was(was):
    row = vendor(is_approved=was, approval_status="approved")
    asl.apply_vendor_approval(row, status=None)
    assert row.approval_status is None and row.is_approved is was


def test_writer_touches_only_what_it_is_given_and_never_active_or_deleted():
    row = vendor(approval_notes="keep", reevaluation_due=date(2027, 1, 1), is_active=False, is_deleted=False)
    asl.apply_vendor_approval(row, status="approved", scopes=["hardware"], bases=[])
    assert row.approved_scopes == ["hardware"] and row.approval_bases is None
    assert row.approval_notes == "keep" and row.reevaluation_due == date(2027, 1, 1)
    assert row.is_active is False and row.is_deleted is False


def test_writer_refuses_a_status_outside_the_vocabulary():
    with pytest.raises(asl.AslValueError):
        asl.apply_vendor_approval(vendor(), status="Approved")


def test_changed_fields_lists_only_differences_as_json_ready_values():
    before = asl.snapshot(vendor(approval_status="conditional", reevaluation_due=date(2026, 1, 1)))
    row = vendor(approval_status="conditional", reevaluation_due=date(2026, 1, 1))
    asl.apply_vendor_approval(row, status="approved", reevaluation_due=date(2027, 1, 1))
    old, new = asl.changed_fields(before, asl.snapshot(row))
    assert set(old) == {"approval_status", "reevaluation_due", "is_approved"}
    assert new["reevaluation_due"] == "2027-01-01" and old["reevaluation_due"] == "2026-01-01"
    assert old["is_approved"] is False and new["is_approved"] is True


# --------------------------------------------------------------------------- PO checks


def codes(warnings):
    return [w["code"] for w in warnings]


def test_a_vendor_not_on_the_asl_warns_unless_it_is_legacy_approved():
    assert codes(asl.approval_warnings(vendor(), [], today=TODAY)) == ["vendor_not_on_asl"]
    assert asl.approval_warnings(vendor(is_approved=True), [part(1, "hardware")], today=TODAY) == []


def test_disqualified_is_a_warning_on_reads_and_a_409_on_create():
    row = vendor(approval_status="disqualified")
    warnings = asl.approval_warnings(row, [part(1, "hardware")], today=TODAY)
    assert codes(warnings) == ["vendor_disqualified"]
    with pytest.raises(HTTPException) as refused:
        asl.assert_vendor_not_disqualified(row)
    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "Northwind Metals is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it."
    )
    asl.assert_vendor_not_disqualified(vendor(approval_status="conditional"))  # only Disqualified refuses


def test_conditional_and_overdue_reevaluation_warn():
    row = vendor(approval_status="conditional", approved_scopes=["hardware"], reevaluation_due=date(2026, 11, 12))
    assert codes(asl.approval_warnings(row, [part(1, "hardware")], today=TODAY)) == [
        "vendor_conditional",
        "reevaluation_overdue",
    ]
    due_today = vendor(approval_status="approved", approved_scopes=["hardware"], reevaluation_due=TODAY)
    assert asl.approval_warnings(due_today, [part(1, "hardware")], today=TODAY) == []


def test_scope_matrix():
    row = vendor(approval_status="approved", approved_scopes=["raw_material_metallics", "hardware"])
    parts = [
        part(1, "raw_material", supply_scope="raw_material_metallics"),  # approved scope
        part(2, "raw_material", supply_scope="raw_material_composite"),  # scope not approved
        part(3, "hardware"),  # hint satisfied
        part(4, "consumable"),  # hint not satisfied -> unverified
        part(5, "purchased"),  # no hint -> nothing
        part(2, "raw_material", supply_scope="raw_material_composite"),  # same part twice -> once
    ]
    warnings = asl.approval_warnings(row, parts, today=TODAY)
    assert [(w["code"], w["part_id"]) for w in warnings] == [("scope_not_approved", 2), ("scope_unverified", 4)]
    assert "Raw Material - Composite" in warnings[0]["message"] and warnings[0]["part_number"] == "P-2"


def test_a_vendor_on_the_asl_with_no_scopes_warns_on_every_scoped_part():
    row = vendor(approval_status="approved", approved_scopes=None)
    warnings = asl.approval_warnings(row, [part(1, "hardware"), part(2, "purchased", "machining")], today=TODAY)
    assert codes(warnings) == ["scope_unverified", "scope_not_approved"]


def test_customer_directed_skips_the_scope_checks():
    row = vendor(approval_status="approved_customer_directed", approved_scopes=["customer_directed_supplier"])
    assert asl.approval_warnings(row, [part(1, "hardware", "hardware")], today=TODAY) == []


def test_status_label_for_display():
    assert asl.status_label("approved_customer_directed") == "Approved - Customer Directed"
    assert asl.status_label(None) == "Not on the ASL"
