"""The update-only Approved Supplier List import (QL 7.4) onto existing vendors.

Fixtures are SYNTHETIC workbooks SHAPED like the controlled sheet -- a title block (form
number / revision / rev date) on rows 1-3, a blank row 4, the header on row 5, en-dash and
trailing-space dropdown labels, the dropdown's two misspellings, blank-STATUS "planning to
add" rows, punctuation-spaced supplier names, two near-duplicate names, and the workbook's
other tabs. No real supplier data is used.
"""

import io
from datetime import date, datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.user import UserRole
from app.services.import_service import ImportFileError, build_import_template_workbook, parse_import_file
from app.services.vendor_asl_import_service import parse_asl_file, vendor_name_key
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

PREVIEW = "/api/v1/purchasing/vendors/asl-import/preview"
COMMIT = "/api/v1/purchasing/vendors/asl-import/commit"

HEADER = [
    "SUPPLIER NAME",
    "SUPPLIER ADDRESS",
    "STATUS",
    "APPROVED SCOPE OF SUPPLY",
    "APPROVED DATE",
    "APPROVAL BASIS",
    "RE-EVALUATION DUE DATE",
    "PERFORMANCE EVALUATION DATE",
    "NOTES",
]


def asl_workbook(rows, *, header=None, title_block=True, first_sheet_other=False) -> bytes:
    """A QL 7.4-shaped workbook: title block, blank row, header row 5, then ``rows``."""
    workbook = Workbook()
    sheet = workbook.active
    if first_sheet_other:
        sheet.title = "Cover"
        sheet.append(["Not the list"])
        sheet = workbook.create_sheet("Approved Supplier List")
    else:
        sheet.title = "Approved Supplier List"
    if title_block:
        sheet.append(["APPROVED SUPPLIER LIST"])
        sheet.append(["FORM NUMBER:", "QL 7.4", None, None, None, None, "REVISION:", "A"])
        sheet.append(["OWNER:", "Purchasing / Quality", None, None, None, None, "REV. DATE:", datetime(2026, 1, 30)])
        sheet.append([])
    sheet.append(header or HEADER)
    for row in rows:
        sheet.append(row)
    log = workbook.create_sheet("Approved Supplier Action Log")
    log.append(["SUPPLIER", "CHANGE TYPE", "REASON FOR CHANGE", "EFFECTIVE DATE", "CHANGE MADE BY"])
    dropdowns = workbook.create_sheet("Dropdown Lists")
    dropdowns.append(["STATUS", "APPROVED SCOPE OF SUPPLY", "APPROVAL BASIS"])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def make_vendor(db: Session, name: str, *, company_id: int = 1, code: str = None, **fields) -> Vendor:
    _ensure_company(db, company_id)
    vendor = Vendor(
        company_id=company_id,
        code=code or f"VND-{_next():04d}",
        name=name,
        is_active=True,
        is_approved=fields.pop("is_approved", True),
        **fields,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def upload(client: TestClient, url: str, headers: dict, content: bytes, *, name="QL 7.4 - ASL.xlsx", **form):
    return client.post(url, headers=headers, files={"file": (name, io.BytesIO(content))}, data=form or None)


def preview_and_commit(client, headers, content, name="QL 7.4 - ASL.xlsx"):
    preview = upload(client, PREVIEW, headers, content, name=name)
    assert preview.status_code == status.HTTP_200_OK, preview.text
    return preview, upload(
        client,
        COMMIT,
        headers,
        content,
        name=name,
        expected_source_hash=preview.json()["source_hash"],
        expected_plan_hash=preview.json()["plan_hash"],
    )


def asl_audit_rows(db: Session):
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type.in_(["vendor", "vendor_asl_import"]))
        .order_by(AuditLog.id)
        .all()
    )


@pytest.fixture
def quality(db_session):
    return make_user(db_session, role=UserRole.QUALITY, company_id=1)


@pytest.fixture
def suppliers(db_session):
    """The company's vendor master, shaped like a go-live load: names typed by hand."""
    return {
        "northwind": make_vendor(
            db_session,
            "Northwind Coatings",
            address_line1="100 Main St",
            city="Tulsa",
            state="OK",
            postal_code="74101",
            notes="Loaded at go-live; ASL in notes",
        ),
        "northwind_okc": make_vendor(db_session, "Northwind Coatings - OKC"),
        "delta": make_vendor(db_session, "Delta-Forge Metals"),
        "jk": make_vendor(db_session, "J.K. Fasteners", reevaluation_due=date(2026, 6, 1)),
        "harbor": make_vendor(db_session, "Harbor Labs"),
        "bluesky": make_vendor(db_session, "Blue Sky Plastics", is_approved=False),
    }


def good_rows():
    return [
        [
            "Northwind Coatings",
            "100 Main St, Tulsa, OK 74101",
            "Approved",
            "Paint / Powders",
            datetime(2019, 2, 19),
            "AS9100 Cert",
            datetime(2027, 8, 31),
            datetime(2025, 12, 16),
            None,
        ],
        [
            "Northwind Coatings - OKC",
            "9 Elm, Oklahoma City, OK",
            "Conditional",
            "Paint / Powders",
            datetime(2021, 3, 1),
            "Questionnaire",
            datetime(2024, 12, 10),
            None,
            "Awaiting corrective action",
        ],
        [
            "Delta- Forge Metals",  # punctuation spacing differs from the vendor record
            None,
            "Approved ",
            "Raw Material – Metalics",  # en-dash + the dropdown's misspelling
            datetime(2020, 5, 5),
            "ISO 9001 Cert",
            datetime(2027, 5, 5),
            datetime(2025, 11, 1),
            None,
        ],
        [
            "J.K.Fasteners",
            None,
            "Approved - Customer Directed",
            "Hardware ",
            None,
            "Customer Requirement",
            None,  # blank date never clears the vendor's existing one
            None,
            None,
        ],
        [
            "Harbor Labs",
            None,
            "Approved",
            "NDT / Inspection / Labroatory Services",
            datetime(2022, 7, 7),
            "ISO 17025 Cert",
            datetime(2026, 7, 7),
            None,
            None,
        ],
        ["Blue Sky Plastics", None, None, None, None, None, None, None, "Planning to add"],
    ]


# =========================================================================== parser


@pytest.mark.unit
class TestHeaderDetection:
    def test_the_title_block_is_skipped_and_kept_as_preamble(self):
        table = parse_asl_file("asl.xlsx", asl_workbook(good_rows()))
        assert table.headers[:3] == ["supplier_name", "supplier_address", "status"]
        assert "re_evaluation_due_date" in table.headers
        assert len(table.rows) == 6 and table.rows[0][0] == 6  # data starts on sheet row 6
        assert table.preamble[1][:2] == ["FORM NUMBER:", "QL 7.4"]

    def test_the_named_sheet_is_preferred_over_the_first(self):
        table = parse_asl_file("asl.xlsx", asl_workbook(good_rows()[:1], first_sheet_other=True))
        assert [row["supplier_name"] for _, row in table.rows] == ["Northwind Coatings"]

    def test_a_file_with_no_recognizable_header_is_refused(self):
        content = b"just,some,columns\n1,2,3\n"
        with pytest.raises(ImportFileError, match="Could not find the header row"):
            parse_import_file("x.csv", content, header_detect={"supplier_name", "status"})

    def test_too_many_title_rows_is_refused(self):
        content = ("\n".join(f"title {i}" for i in range(12)) + "\nSUPPLIER NAME,STATUS\nA,Approved\n").encode()
        with pytest.raises(ImportFileError, match="none of the first 10"):
            parse_import_file("x.csv", content, header_detect={"supplier_name", "status"})

    def test_without_header_detect_the_first_row_is_still_the_header(self):
        table = parse_import_file("x.csv", b"part_number\nP-1\n")
        assert table.headers == ["part_number"] and table.preamble == []

    def test_the_template_parses_with_the_asl_importer(self):
        table = parse_asl_file("t.xlsx", build_import_template_workbook("approved-supplier-list"))
        assert table.headers[0] == "supplier_name" and "vendor_code" in table.headers
        assert table.rows == []

    def test_name_key_tolerates_spacing_and_dashes_only(self):
        assert vendor_name_key("Cinder- Forge") == vendor_name_key("cinder-forge")
        assert vendor_name_key("R.T.Hollis") == vendor_name_key("R.T. Hollis")
        assert vendor_name_key("Acme  –  West ") == vendor_name_key("acme-west")
        assert vendor_name_key("Northwind Coatings") != vendor_name_key("Northwind Coatings - OKC")


# =========================================================================== preview


def test_preview_matches_diffs_warns_and_writes_nothing(client, db_session, quality, suppliers):
    response = upload(client, PREVIEW, headers_for(quality), asl_workbook(good_rows()))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["committed"] is False and len(body["source_hash"]) == 64
    assert body["document"] == {"form_number": "QL 7.4", "revision": "A", "rev_date": "2026-01-30"}
    assert body["summary"] == {
        "total_rows": 6,
        "matched_rows": 6,
        "update_count": 6,
        "unchanged_count": 0,
        "error_count": 0,
        "warning_count": 2,
        "not_on_asl_count": 1,
    }
    rows = {row["supplier_name"]: row for row in body["rows"]}
    assert rows["Delta- Forge Metals"]["vendor_id"] == suppliers["delta"].id
    assert rows["Delta- Forge Metals"]["changes"]["approved_scopes"]["new"] == ["raw_material_metallics"]
    assert rows["Harbor Labs"]["changes"]["approved_scopes"]["new"] == ["ndt_inspection_laboratory"]
    assert rows["Northwind Coatings - OKC"]["vendor_id"] == suppliers["northwind_okc"].id
    assert rows["Northwind Coatings - OKC"]["changes"]["is_approved"] == {"old": True, "new": False}
    assert rows["Northwind Coatings"]["changes"]["approval_date"]["new"] == "2019-02-19"
    assert rows["Northwind Coatings"]["warnings"] == []  # same address, different punctuation
    assert "reevaluation_due" not in rows["J.K.Fasteners"]["changes"]
    assert rows["J.K.Fasteners"]["changes"]["approval_bases"]["new"] == ["customer_requirement"]
    assert any("STATUS is blank" in w for w in rows["Blue Sky Plastics"]["warnings"])
    assert "approval_status" not in rows["Blue Sky Plastics"]["changes"]
    assert rows["Blue Sky Plastics"]["changes"]["approval_notes"]["new"] == "Planning to add"
    assert any("SUPPLIER ADDRESS differs" in w for w in rows["Northwind Coatings - OKC"]["warnings"])

    assert asl_audit_rows(db_session) == []
    assert db_session.get(Vendor, suppliers["northwind"].id).approval_status is None


def test_preview_reports_every_matching_error(client, db_session, quality, suppliers):
    make_vendor(db_session, "Twin Supply")
    make_vendor(db_session, "Twin  supply")  # normalizes to the same name -> ambiguous
    gone = make_vendor(db_session, "Gone Metals")
    gone.soft_delete(quality.id)
    db_session.commit()
    rows = [
        ["Nobody Industries", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None],
        ["Twin Supply", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None],
        ["Gone Metals", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None],
        ["Harbor Labs", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None],
        ["harbor labs", None, "Approved", "Machining", None, "AS9100 Cert", None, None, None],
        ["Northwind Coatings", None, "Probationary", "Glitter", None, "Handshake", "not a date", None, None],
    ]
    body = upload(client, PREVIEW, headers_for(quality), asl_workbook(rows)).json()
    errors = {row["row"]: row["error"] for row in body["rows"]}
    assert "no vendor named 'Nobody Industries'" in errors[6] and "never creates" in errors[6]
    assert "matches 2 vendors" in errors[7]
    assert "is deleted" in errors[8] and f"/vendors/{gone.id}/restore" in errors[8]
    assert "rows 9, 10 all match vendor" in errors[9] and errors[9] == errors[10]
    for fragment in ("Unknown supplier status", "Unknown scope of supply", "Unknown approval basis", "must be a date"):
        assert fragment in errors[11]
    assert body["summary"]["error_count"] == 6 and body["summary"]["update_count"] == 0


def test_a_vendor_code_column_matches_by_code(client, db_session, quality, suppliers):
    """The code picks the vendor -- e.g. between two that share a normalized name -- and the
    row's SUPPLIER NAME must still name it (spacing / dash differences are fine)."""
    make_vendor(db_session, "Harbor  labs")  # a second "Harbor Labs" once normalized
    header = HEADER + ["VENDOR CODE"]
    harbor = suppliers["harbor"]
    rows = [
        ["Harbor Labs", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None, harbor.code.lower()],
        [
            "Northwind Coatings-OKC",
            None,
            "Approved",
            "Paint / Powders",
            None,
            None,
            None,
            None,
            None,
            suppliers["northwind_okc"].code,
        ],
    ]
    body = upload(client, PREVIEW, headers_for(quality), asl_workbook(rows, header=header)).json()
    first, second = body["rows"]
    assert first["match_by"] == "code" and first["vendor_id"] == harbor.id and first["error"] is None
    assert second["vendor_id"] == suppliers["northwind_okc"].id and second["error"] is None


def test_a_code_that_names_another_supplier_is_a_row_error_not_a_warning(client, db_session, quality, suppliers):
    """A mistyped code (VND-012 for VND-021) used to bind the row to the coded vendor with only
    a warning, landing this supplier's approval -- Disqualified, say -- on ANOTHER supplier's
    controlled record while the intended one went untouched. Now the code and the name must
    agree, and a code with no name to check it against is refused too."""
    header = HEADER + ["VENDOR CODE"]
    wrong = suppliers["delta"]
    rows = [
        ["Harbor Labs", None, "Disqualified", None, None, None, None, None, "Failed audit", wrong.code],
        [None, None, "Approved", "Hardware", None, None, None, None, None, suppliers["harbor"].code],
    ]
    content = asl_workbook(rows, header=header)
    body = upload(client, PREVIEW, headers_for(quality), content).json()
    first, second = body["rows"]
    assert first["outcome"] == "error" and first["vendor_id"] is None and first["changes"] == {}
    assert f"is {wrong.code} 'Delta-Forge Metals', but SUPPLIER NAME is 'Harbor Labs'" in first["error"]
    assert second["outcome"] == "error" and second["error"] == "SUPPLIER NAME is required"
    _, commit = preview_and_commit(client, headers_for(quality), content)
    assert commit.status_code == status.HTTP_409_CONFLICT
    db_session.expire_all()
    assert db_session.get(Vendor, wrong.id).approval_status is None
    assert db_session.get(Vendor, suppliers["harbor"].id).approval_status is None
    assert asl_audit_rows(db_session) == []


def test_a_code_shared_by_two_live_vendors_differing_in_case_is_ambiguous(client, db_session, quality, suppliers):
    """Legacy codes can differ only in case. That is ambiguity -- not a deleted vendor -- and
    the error says so."""
    make_vendor(db_session, "Casey Supply", code="cas-1")
    make_vendor(db_session, "Casey Supply", code="CAS-1")
    header = HEADER + ["VENDOR CODE"]
    rows = [["Casey Supply", None, "Approved", "Hardware", None, None, None, None, None, "Cas-1"]]
    (row,) = upload(client, PREVIEW, headers_for(quality), asl_workbook(rows, header=header)).json()["rows"]
    assert row["outcome"] == "error"
    assert "matches 2 vendors whose codes differ only in case (CAS-1, cas-1)" in row["error"]
    assert "deleted" not in row["error"]


# =========================================================================== commit


def test_commit_writes_changed_vendors_with_required_audit_and_is_idempotent(client, db_session, quality, suppliers):
    content = asl_workbook(good_rows())
    preview, commit = preview_and_commit(client, headers_for(quality), content)
    assert commit.status_code == status.HTTP_200_OK, commit.text
    assert commit.json()["committed"] is True and commit.json()["summary"]["update_count"] == 6

    db_session.expire_all()
    northwind = db_session.get(Vendor, suppliers["northwind"].id)
    assert northwind.approval_status == "approved" and northwind.is_approved is True
    assert northwind.approved_scopes == ["paint_powders"] and northwind.approval_bases == ["as9100"]
    assert northwind.approval_date == date(2019, 2, 19) and northwind.reevaluation_due == date(2027, 8, 31)
    assert northwind.last_performance_evaluation == date(2025, 12, 16)
    assert northwind.notes == "Loaded at go-live; ASL in notes" and northwind.address_line1 == "100 Main St"
    okc = db_session.get(Vendor, suppliers["northwind_okc"].id)
    assert okc.approval_status == "conditional" and okc.is_approved is False
    assert okc.approval_notes == "Awaiting corrective action"
    jk = db_session.get(Vendor, suppliers["jk"].id)
    assert jk.approval_status == "approved_customer_directed" and jk.reevaluation_due == date(2026, 6, 1)
    bluesky = db_session.get(Vendor, suppliers["bluesky"].id)
    assert bluesky.approval_status is None and bluesky.is_approved is False  # left as it was

    rows = asl_audit_rows(db_session)
    vendor_rows = [row for row in rows if row.resource_type == "vendor"]
    (summary,) = [row for row in rows if row.resource_type == "vendor_asl_import"]
    assert len(vendor_rows) == 6 and summary.action == "IMPORT"
    first = next(row for row in vendor_rows if row.resource_id == northwind.id)
    assert first.action == "UPDATE" and first.user_id == quality.id and first.company_id == 1
    assert first.extra_data["source"] == "asl_import" and first.extra_data["form"] == "QL 7.4"
    assert first.extra_data["revision"] == "A" and first.extra_data["rev_date"] == "2026-01-30"
    assert first.extra_data["source_hash"] == preview.json()["source_hash"] and first.extra_data["row"] == 6
    assert set(first.new_values) == {
        "approval_status",
        "approved_scopes",
        "approval_bases",
        "approval_date",
        "reevaluation_due",
        "last_performance_evaluation",
    }
    assert summary.new_values["update_count"] == 6 and len(summary.extra_data["updated_vendor_ids"]) == 6

    # Re-running the same sheet changes nothing and writes no audit row at all.
    again_preview, again = preview_and_commit(client, headers_for(quality), content)
    assert again_preview.json()["summary"]["unchanged_count"] == 6
    assert again.status_code == status.HTTP_200_OK and again.json()["summary"]["update_count"] == 0
    assert len(asl_audit_rows(db_session)) == len(rows)


def test_any_row_error_refuses_the_whole_commit(client, db_session, quality, suppliers):
    rows = good_rows() + [["Nobody Industries", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None]]
    _, commit = preview_and_commit(client, headers_for(quality), asl_workbook(rows))
    assert commit.status_code == status.HTTP_409_CONFLICT, commit.text
    detail = commit.json()["detail"]
    assert "nothing was imported" in detail["message"] and detail["errors"][0]["row"] == 12
    assert asl_audit_rows(db_session) == []
    assert db_session.get(Vendor, suppliers["northwind"].id).approval_status is None


def test_a_file_other_than_the_previewed_one_is_refused(client, db_session, quality, suppliers):
    previewed = upload(client, PREVIEW, headers_for(quality), asl_workbook(good_rows())).json()
    other = asl_workbook(good_rows()[:2])
    response = upload(
        client,
        COMMIT,
        headers_for(quality),
        other,
        expected_source_hash=previewed["source_hash"],
        expected_plan_hash=previewed["plan_hash"],
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert "Preview it again" in response.json()["detail"]
    assert asl_audit_rows(db_session) == []


def test_a_bad_file_is_400(client, db_session, quality):
    response = upload(client, PREVIEW, headers_for(quality), b"\x00not a workbook", name="asl.xlsx")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_a_lost_required_audit_rolls_the_import_back(client, db_session, quality, suppliers, monkeypatch):
    from app.services.audit_service import AuditService, AuditWriteError

    content = asl_workbook(good_rows())
    previewed = upload(client, PREVIEW, headers_for(quality), content).json()

    def refusing(self, *args, **kwargs):
        raise AuditWriteError("simulated")

    monkeypatch.setattr(AuditService, "log_required", refusing)
    response = upload(
        client,
        COMMIT,
        headers_for(quality),
        content,
        expected_source_hash=previewed["source_hash"],
        expected_plan_hash=previewed["plan_hash"],
    )
    assert response.status_code == 503, response.text
    db_session.rollback()
    assert db_session.get(Vendor, suppliers["northwind"].id).approval_status is None


# =========================================================================== authority


@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER])
def test_roles_outside_purchasing_and_quality_are_refused(client, db_session, suppliers, role):
    user = make_user(db_session, role=role, company_id=1)
    assert upload(client, PREVIEW, headers_for(user), asl_workbook(good_rows())).status_code == 403


def test_an_api_token_is_refused_on_both_routes(client, db_session, admin_headers, suppliers):
    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    issued = client.post("/api/v1/api-tokens/", json={"user_id": manager.id, "label": "asl"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    headers = {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}
    content = asl_workbook(good_rows())
    assert upload(client, PREVIEW, headers, content).status_code == 403
    committed = upload(client, COMMIT, headers, content, expected_source_hash="0" * 64, expected_plan_hash="0" * 64)
    assert committed.status_code == 403
    assert asl_audit_rows(db_session) == []


def test_tenant_isolation(client, db_session, suppliers):
    """Company 2 holds a vendor with the same name as company 1's. Each company's import
    binds only its own vendor."""
    foreign = make_vendor(db_session, "Northwind Coatings", company_id=2)
    other_quality = make_user(db_session, role=UserRole.QUALITY, company_id=2)
    rows = [good_rows()[0], ["Harbor Labs", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None]]
    _, commit = preview_and_commit(client, headers_for(other_quality), asl_workbook(rows))
    assert commit.status_code == status.HTTP_409_CONFLICT  # Harbor Labs is company 1's
    preview, commit = preview_and_commit(client, headers_for(other_quality), asl_workbook(rows[:1]))
    assert commit.status_code == status.HTTP_200_OK, commit.text
    assert preview.json()["rows"][0]["vendor_id"] == foreign.id
    db_session.expire_all()
    assert db_session.get(Vendor, foreign.id).approval_status == "approved"
    assert db_session.get(Vendor, suppliers["northwind"].id).approval_status is None
    assert "Harbor" not in str(preview.json())


# =========================================================================== edges the sheet will hit


def test_a_blank_status_never_takes_a_listed_vendor_off_the_asl(client, db_session, quality, suppliers):
    """A blank STATUS used to record a LISTED vendor as "not on the ASL" -- leaving
    ``is_approved`` true with no warning on its POs, and, from Disqualified, lifting the one
    hard purchase-order refusal. Taking a supplier off the list is a decision, so the import
    refuses it as a row error and points at Update approval, which records the reason."""
    for key, listed_as in (("harbor", "approved"), ("delta", "disqualified")):
        suppliers[key].approval_status = listed_as
        suppliers[key].is_approved = listed_as == "approved"
    db_session.commit()
    rows = [
        ["Harbor Labs", None, None, None, None, None, None, None, None],
        ["Delta-Forge Metals", None, " ", None, None, None, None, None, None],
        ["Blue Sky Plastics", None, None, None, None, None, None, None, None],  # never listed: fine
    ]
    preview, commit = preview_and_commit(client, headers_for(quality), asl_workbook(rows))
    harbor_row, delta_row, bluesky_row = preview.json()["rows"]
    for row, key, label in ((harbor_row, "harbor", "Approved"), (delta_row, "delta", "Disqualified")):
        assert row["outcome"] == "error" and row["changes"] == {}
        assert f"{suppliers[key].code} is {label} on the Approved Supplier List" in row["error"]
        assert f"/vendors/{suppliers[key].id}/approval" in row["error"]
    assert bluesky_row["outcome"] == "unchanged" and bluesky_row["error"] is None
    assert any("not on the Approved Supplier List yet" in w for w in bluesky_row["warnings"])
    assert commit.status_code == status.HTTP_409_CONFLICT, commit.text

    db_session.expire_all()
    assert db_session.get(Vendor, suppliers["harbor"].id).approval_status == "approved"
    assert db_session.get(Vendor, suppliers["delta"].id).approval_status == "disqualified"
    assert asl_audit_rows(db_session) == []


def test_a_decision_recorded_after_the_preview_is_not_overwritten(client, db_session, quality, suppliers):
    """The preview showed Harbor Labs unchanged (Approved). Quality then Disqualified it. The
    commit used to re-plan against the database and write Approved straight back -- a change
    the committer never saw. ``plan_hash`` binds the commit to what the preview showed."""
    harbor = suppliers["harbor"]
    harbor.approval_status, harbor.approved_scopes, harbor.approval_bases = "approved", ["hardware"], ["as9100"]
    db_session.commit()
    content = asl_workbook([["Harbor Labs", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None]])
    previewed = upload(client, PREVIEW, headers_for(quality), content).json()
    assert previewed["rows"][0]["outcome"] == "unchanged" and len(previewed["plan_hash"]) == 64

    updated_at = client.get(f"/api/v1/purchasing/vendors/{harbor.id}", headers=headers_for(quality)).json()[
        "updated_at"
    ]
    decided = client.put(
        f"/api/v1/purchasing/vendors/{harbor.id}/approval",
        headers=headers_for(quality),
        json={"approval_status": "disqualified", "reason": "Failed audit", "expected_updated_at": updated_at},
    )
    assert decided.status_code == status.HTTP_200_OK, decided.text

    commit = upload(
        client,
        COMMIT,
        headers_for(quality),
        content,
        expected_source_hash=previewed["source_hash"],
        expected_plan_hash=previewed["plan_hash"],
    )
    assert commit.status_code == status.HTTP_409_CONFLICT, commit.text
    assert "changed after this file was previewed" in commit.json()["detail"]
    db_session.expire_all()
    assert db_session.get(Vendor, harbor.id).approval_status == "disqualified"
    assert [row.resource_type for row in asl_audit_rows(db_session)] == ["vendor"]  # the decision only

    again = upload(client, PREVIEW, headers_for(quality), content).json()
    assert again["plan_hash"] != previewed["plan_hash"]
    assert again["rows"][0]["changes"]["approval_status"] == {"old": "disqualified", "new": "approved"}


def test_a_disqualified_row_warns_and_the_loaded_status_refuses_a_po(client, db_session, quality, suppliers):
    """End to end: a Disqualified status loaded from the sheet is the one the PO gate reads."""
    rows = [["Harbor Labs", None, "Disqualified", None, None, None, None, None, "Failed audit"]]
    preview, commit = preview_and_commit(client, headers_for(quality), asl_workbook(rows))
    (row,) = preview.json()["rows"]
    assert "Disqualified: purchase orders to this vendor will be refused." in row["warnings"]
    assert row["changes"]["is_approved"] == {"old": True, "new": False}
    assert commit.status_code == status.HTTP_200_OK, commit.text

    manager = make_user(db_session, role=UserRole.MANAGER, company_id=1)
    part = Part(
        company_id=1, part_number=f"ASLI-{_next():05d}", name="Probe", part_type="purchased", unit_of_measure="each"
    )
    db_session.add(part)
    db_session.commit()
    po = client.post(
        "/api/v1/purchasing/purchase-orders",
        headers=headers_for(manager),
        json={
            "vendor_id": suppliers["harbor"].id,
            "lines": [{"part_id": part.id, "quantity_ordered": 1, "unit_price": 1}],
        },
    )
    assert po.status_code == status.HTTP_409_CONFLICT, po.text
    assert "Harbor Labs is Disqualified on the Approved Supplier List" in po.json()["detail"]


def test_an_inactive_vendor_is_updated_but_stays_inactive(client, db_session, quality, suppliers):
    harbor = suppliers["harbor"]
    harbor.is_active = False
    db_session.commit()
    rows = [["Harbor Labs", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None]]
    preview, commit = preview_and_commit(client, headers_for(quality), asl_workbook(rows))
    (row,) = preview.json()["rows"]
    assert any("is inactive" in warning and "stays inactive" in warning for warning in row["warnings"])
    assert commit.status_code == status.HTTP_200_OK, commit.text
    db_session.expire_all()
    stored = db_session.get(Vendor, harbor.id)
    assert stored.approval_status == "approved" and stored.is_active is False


def test_code_matching_errors_name_the_problem(client, db_session, quality, suppliers):
    header = HEADER + ["VENDOR CODE"]
    gone = make_vendor(db_session, "Gone By Code")
    gone.soft_delete(quality.id)
    db_session.commit()
    rows = [
        ["Whoever", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None, "NO-SUCH-CODE"],
        ["Gone By Code", None, "Approved", "Hardware", None, "AS9100 Cert", None, None, None, gone.code.lower()],
    ]
    body = upload(client, PREVIEW, headers_for(quality), asl_workbook(rows, header=header)).json()
    errors = {row["row"]: row["error"] for row in body["rows"]}
    assert errors[6] == "no vendor with code 'NO-SUCH-CODE'"
    assert "is deleted" in errors[7] and f"/vendors/{gone.id}/restore" in errors[7]


def test_the_commit_re_matches_against_the_database_not_the_preview(client, db_session, quality, suppliers):
    """The preview is a snapshot, never authorization: a vendor deleted after the preview
    turns the commit into a 409 naming the restore, and nothing is written."""
    content = asl_workbook(good_rows())
    previewed = upload(client, PREVIEW, headers_for(quality), content).json()
    suppliers["harbor"].soft_delete(quality.id)
    db_session.commit()
    commit = upload(
        client,
        COMMIT,
        headers_for(quality),
        content,
        expected_source_hash=previewed["source_hash"],
        expected_plan_hash=previewed["plan_hash"],
    )
    assert commit.status_code == status.HTTP_409_CONFLICT, commit.text
    (error,) = commit.json()["detail"]["errors"]
    assert error["supplier_name"] == "Harbor Labs" and "restore" in error["error"]
    assert asl_audit_rows(db_session) == []
    assert db_session.get(Vendor, suppliers["northwind"].id).approval_status is None
