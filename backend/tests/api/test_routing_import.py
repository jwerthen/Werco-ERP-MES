"""API tests for the A0.2 routing CSV/XLSX import wizard.

Mirrors the open-WO / open-PO migration-import tests: rows are uploaded as
multipart files to the preview (dry-run) and commit endpoints, and behavior is
asserted against the response body plus the database.

Covered behaviors:
* happy-path commit — two parts grouped into two draft routings, totals computed
  by ``calculate_routing_totals``, operations carry company_id + resolved
  work_center_id + parsed inspection/outside flags, one audit CREATE per routing;
* dry-run preview — zero routings and zero audit rows persisted, results carry
  ``routing_id=None`` and ``dry_run=True``, with per-operation detail;
* optional work_center_code — a blank code previews with ``needs_work_center`` and
  no error; a non-blank UNKNOWN code is still a row error;
* UI assignments — commit with an ``assignments`` map persists the chosen
  work_center_id; an unassigned op makes its routing error (creates nothing); an
  assigned cross-tenant / inactive work_center_id is rejected;
* row errors — unknown part, non-engineering part, missing/inactive work center
  (whole-group skip with sibling "skipped" errors), duplicate sequence,
  existing-revision conflict (and the untouched existing routing), all with
  partial success of the other valid parts;
* tenant isolation — another company's part / work center is not resolvable;
* RBAC — operator (and viewer) are forbidden on both endpoints.
"""

import io
import json
from io import BytesIO

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.part import Part
from app.models.routing import Routing, RoutingOperation
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter

PREVIEW_URL = "/api/v1/routing/import/preview"
COMMIT_URL = "/api/v1/routing/import/commit"


def _csv_file(text: str):
    return {"file": ("routings.csv", BytesIO(text.encode("utf-8")), "text/csv")}


def _xlsx_file(rows, filename="routings.xlsx"):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    out.seek(0)
    return {"file": (filename, out, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


def _assignments(mapping: dict) -> dict:
    """Build the multipart ``data`` payload carrying the assignments JSON field."""
    return {"assignments": json.dumps({str(k): v for k, v in mapping.items()})}


CSV_HEADER = (
    "part_number,routing_revision,routing_description,sequence,operation_name,"
    "work_center_code,setup_hours,run_hours_per_unit,description,"
    "is_inspection_point,is_outside_operation\n"
)


def _make_work_center(db: Session, code: str, company_id: int = 1, is_active: bool = True) -> WorkCenter:
    wc = WorkCenter(
        code=code,
        name=f"WC {code}",
        work_center_type="machining",
        is_active=is_active,
        company_id=company_id,
    )
    db.add(wc)
    db.flush()
    return wc


def _make_part(
    db: Session,
    part_number: str,
    *,
    part_type: str = "manufactured",
    company_id: int = 1,
) -> Part:
    part = Part(
        part_number=part_number,
        name=f"Part {part_number}",
        part_type=part_type,
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.flush()
    return part


def _viewer_headers(db_session: Session) -> dict:
    """Mint a VIEWER user + token (no shared fixture exists for this role)."""
    user = User(
        email="viewer-routing-import@werco.com",
        employee_id="EMP-VIEW-RIMP",
        first_name="View",
        last_name="Only",
        hashed_password="$2b$12$abcdefghijklmnopqrstuv",
        role=UserRole.VIEWER,
        is_active=True,
        company_id=1,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    token = create_access_token(subject=user.id, company_id=user.company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingImportCommit:
    def test_happy_path_two_parts_create_two_draft_routings(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_work_center(db_session, "MILL-1")
        _make_work_center(db_session, "DEBURR-1")
        _make_work_center(db_session, "INSP-1")
        _make_part(db_session, "RT-100", part_type="manufactured")
        _make_part(db_session, "RT-200", part_type="assembly")
        db_session.commit()

        before_audit = db_session.query(AuditLog).count()
        csv_text = CSV_HEADER + (
            # RT-100: two ops (a machining + an inspection point)
            "RT-100,A,First routing,10,Mill faces,MILL-1,1.5,0.25,Rough mill,N,N\n"
            "RT-100,A,First routing,20,Final inspect,INSP-1,0.5,0.1,CMM check,Y,N\n"
            # RT-200: one outside operation
            "RT-200,A,Assembly route,10,Outside plating,DEBURR-1,0,0.4,Send out,false,true\n"
        )
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()

        assert body["dry_run"] is False
        assert body["total_rows"] == 3
        assert body["parts_detected"] == 2
        assert body["routings_created"] == 2
        assert body["total_operations"] == 3
        assert body["operations_needing_work_center"] == 0
        assert body["skipped_count"] == 0
        assert body["errors"] == []
        assert len(body["created_ids"]) == 2

        results = {r["part_number"]: r for r in body["results"]}
        rt100 = results["RT-100"]
        assert rt100["status"] == "draft"
        assert rt100["routing_revision"] == "A"
        assert rt100["operation_count"] == 2
        assert rt100["routing_id"] is not None
        # Per-operation detail with resolved work centers (no op needs assignment).
        ops_detail = sorted(rt100["operations"], key=lambda o: o["sequence"])
        assert [o["sequence"] for o in ops_detail] == [10, 20]
        assert [o["work_center_code"] for o in ops_detail] == ["MILL-1", "INSP-1"]
        assert all(o["work_center_id"] is not None for o in ops_detail)
        assert all(o["needs_work_center"] is False for o in ops_detail)
        assert ops_detail[0]["work_center_name"] == "WC MILL-1"
        # totals == calculate_routing_totals(): setup 1.5+0.5, run 0.25+0.1
        assert rt100["total_setup_hours"] == pytest.approx(2.0)
        assert rt100["total_run_hours_per_unit"] == pytest.approx(0.35)
        assert sorted(rt100["rows"]) == [2, 3]

        rt200 = results["RT-200"]
        assert rt200["operation_count"] == 1
        assert rt200["total_setup_hours"] == pytest.approx(0.0)
        assert rt200["total_run_hours_per_unit"] == pytest.approx(0.4)

        # DB: two draft routings, persisted operations with company_id + resolved WC.
        routings = db_session.query(Routing).filter_by(company_id=1).all()
        assert len(routings) == 2
        assert all(r.status == "draft" for r in routings)

        rt100_routing = db_session.query(Routing).filter_by(id=rt100["routing_id"]).one()
        ops = sorted(rt100_routing.operations, key=lambda op: op.sequence)
        assert [op.sequence for op in ops] == [10, 20]
        assert all(op.company_id == 1 for op in ops)
        mill_wc = db_session.query(WorkCenter).filter_by(code="MILL-1").one()
        insp_wc = db_session.query(WorkCenter).filter_by(code="INSP-1").one()
        assert ops[0].work_center_id == mill_wc.id
        assert ops[1].work_center_id == insp_wc.id
        # Y/N flag parsing
        assert ops[0].is_inspection_point is False
        assert ops[1].is_inspection_point is True

        rt200_routing = db_session.query(Routing).filter_by(id=rt200["routing_id"]).one()
        op200 = rt200_routing.operations[0]
        # true/false flag parsing
        assert op200.is_outside_operation is True
        assert op200.is_inspection_point is False

        # One audit CREATE row per routing, resource_type="routing".
        created_ids = body["created_ids"]
        audit_rows = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.resource_type == "routing",
                AuditLog.action == "CREATE",
                AuditLog.resource_id.in_(created_ids),
            )
            .all()
        )
        assert len(audit_rows) == 2
        assert db_session.query(AuditLog).count() == before_audit + 2

    def test_part_not_found_partial_success(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_work_center(db_session, "MILL-2")
        _make_part(db_session, "RT-OK", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + (
            "RT-MISSING,A,,10,Mill,MILL-2,1,0.1,,N,N\n"  # part does not exist
            "RT-OK,A,,10,Mill,MILL-2,1,0.1,,N,N\n"  # valid -> still imports
        )
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        assert body["routings_created"] == 1
        assert body["results"][0]["part_number"] == "RT-OK"

        reasons = {e["row"]: e["reason"] for e in body["errors"]}
        assert "part 'RT-MISSING' not found" in reasons[2]
        # The missing part's routing was not created; the valid one was.
        assert db_session.query(Routing).filter_by(company_id=1).count() == 1

    def test_non_engineering_part_rejected(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_work_center(db_session, "MILL-3")
        _make_part(db_session, "RT-RAW", part_type="raw_material")
        _make_part(db_session, "RT-BUY", part_type="purchased")
        db_session.commit()

        csv_text = CSV_HEADER + ("RT-RAW,A,,10,Mill,MILL-3,1,0.1,,N,N\n" "RT-BUY,A,,10,Mill,MILL-3,1,0.1,,N,N\n")
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        assert body["routings_created"] == 0
        reasons = {e["row"]: e["reason"] for e in body["errors"]}
        assert "not a manufactured or assembly part" in reasons[2]
        assert "not a manufactured or assembly part" in reasons[3]
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

    def test_missing_or_inactive_work_center_skips_whole_routing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_work_center(db_session, "GOOD-WC")
        _make_work_center(db_session, "DEAD-WC", is_active=False)
        _make_part(db_session, "RT-WCBAD", part_type="manufactured")
        _make_part(db_session, "RT-WCGONE", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + (
            # RT-WCBAD: op 20 references an INACTIVE work center -> whole group fails.
            "RT-WCBAD,A,,10,Mill,GOOD-WC,1,0.1,,N,N\n"
            "RT-WCBAD,A,,20,Plate,DEAD-WC,1,0.1,,N,N\n"
            # RT-WCGONE: references a code that does not exist at all.
            "RT-WCGONE,A,,10,Mill,NO-SUCH-WC,1,0.1,,N,N\n"
        )
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        assert body["routings_created"] == 0
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

        reasons = {e["row"]: e["reason"] for e in body["errors"]}
        # The row that names the bad WC reports the bad code; siblings report "skipped".
        bad_wc_msgs = [r for r in reasons.values() if "DEAD-WC" in r]
        assert bad_wc_msgs, f"expected an error naming DEAD-WC, got {reasons}"
        assert any("NO-SUCH-WC" in r for r in reasons.values())

    def test_duplicate_sequence_within_part_rejected(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_work_center(db_session, "MILL-DUP")
        _make_part(db_session, "RT-DUP", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + (
            "RT-DUP,A,,10,Mill,MILL-DUP,1,0.1,,N,N\n"
            "RT-DUP,A,,10,Mill again,MILL-DUP,1,0.1,,N,N\n"  # duplicate sequence 10
        )
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        assert body["routings_created"] == 0
        assert any("duplicate sequence 10" in e["reason"] for e in body["errors"])
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

    def test_existing_revision_conflict_leaves_existing_untouched(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        wc = _make_work_center(db_session, "MILL-REV")
        part = _make_part(db_session, "RT-REV", part_type="manufactured")
        # Pre-existing released routing at revision 'A'.
        existing = Routing(
            part_id=part.id,
            revision="A",
            status="released",
            is_active=True,
            company_id=1,
        )
        db_session.add(existing)
        db_session.flush()
        db_session.add(
            RoutingOperation(
                routing_id=existing.id,
                sequence=10,
                operation_number="Op 10",
                name="Existing op",
                work_center_id=wc.id,
                run_hours_per_unit=0.2,
                company_id=1,
            )
        )
        db_session.commit()
        existing_id = existing.id

        # Importing revision 'A' again -> conflict, existing untouched.
        conflict = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(CSV_HEADER + "RT-REV,A,,10,New mill,MILL-REV,1,0.1,,N,N\n"),
        )
        cbody = conflict.json()
        assert cbody["routings_created"] == 0
        assert any("already has a routing at revision 'A'" in e["reason"] for e in cbody["errors"])
        assert db_session.query(Routing).filter_by(part_id=part.id, company_id=1).count() == 1

        # Importing revision 'B' -> new draft routing alongside the existing one.
        ok = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(CSV_HEADER + "RT-REV,B,,10,New mill,MILL-REV,1.0,0.1,,N,N\n"),
        )
        obody = ok.json()
        assert obody["routings_created"] == 1
        assert obody["results"][0]["routing_revision"] == "B"
        new_id = obody["results"][0]["routing_id"]

        # The original revision-A routing is unchanged: still released, still active, still present.
        db_session.expire_all()
        original = db_session.query(Routing).filter_by(id=existing_id).one()
        assert original.revision == "A"
        assert original.status == "released"
        assert original.is_active is True
        new_routing = db_session.query(Routing).filter_by(id=new_id).one()
        assert new_routing.revision == "B"
        assert new_routing.status == "draft"
        assert db_session.query(Routing).filter_by(part_id=part.id, company_id=1).count() == 2


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingImportPreview:
    def test_dry_run_writes_nothing(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_work_center(db_session, "MILL-DRY")
        _make_part(db_session, "RT-DRY1", part_type="manufactured")
        _make_part(db_session, "RT-DRY2", part_type="manufactured")
        db_session.commit()

        before_routings = db_session.query(Routing).filter_by(company_id=1).count()
        before_ops = db_session.query(RoutingOperation).filter_by(company_id=1).count()
        before_audit = db_session.query(AuditLog).count()

        csv_text = CSV_HEADER + (
            "RT-DRY1,A,,10,Mill,MILL-DRY,1.5,0.25,,N,N\n"
            "RT-DRY1,A,,20,Inspect,MILL-DRY,0.5,0.1,,Y,N\n"
            "RT-DRY2,A,,10,Mill,MILL-DRY,1,0.1,,N,N\n"
        )
        response = client.post(PREVIEW_URL, headers=auth_headers, files=_csv_file(csv_text))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()

        assert body["dry_run"] is True
        assert body["routings_created"] == 2
        assert body["total_operations"] == 3
        assert body["created_ids"] == []
        # Full validation/expansion happened inside the rolled-back savepoint.
        rt1 = next(r for r in body["results"] if r["part_number"] == "RT-DRY1")
        assert rt1["routing_id"] is None
        assert rt1["operation_count"] == 2
        assert rt1["total_setup_hours"] == pytest.approx(2.0)

        # Nothing persisted: no routings, no operations, no audit rows.
        assert db_session.query(Routing).filter_by(company_id=1).count() == before_routings
        assert db_session.query(RoutingOperation).filter_by(company_id=1).count() == before_ops
        assert db_session.query(AuditLog).count() == before_audit

    def test_preview_reports_row_errors_without_writing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_work_center(db_session, "MILL-PV")
        _make_part(db_session, "RT-PV-OK", part_type="manufactured")
        db_session.commit()
        before_audit = db_session.query(AuditLog).count()

        csv_text = CSV_HEADER + (
            "RT-PV-OK,A,,10,Mill,MILL-PV,1,0.1,,N,N\n"
            "RT-PV-NOPE,A,,10,Mill,MILL-PV,1,0.1,,N,N\n"  # missing part
        )
        response = client.post(PREVIEW_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        assert body["dry_run"] is True
        assert body["routings_created"] == 1
        assert any("not found" in e["reason"] for e in body["errors"])
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0
        assert db_session.query(AuditLog).count() == before_audit


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingImportOptionalWorkCenter:
    """work_center_code is OPTIONAL — blank = assign-in-UI; the UI sends back assignments."""

    def test_blank_work_center_previews_with_needs_flag_and_no_error(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_work_center(db_session, "MILL-OPT")
        _make_part(db_session, "RT-OPT", part_type="manufactured")
        db_session.commit()

        # Row 2 names a real WC; rows 3 & 4 leave work_center_code BLANK.
        csv_text = CSV_HEADER + (
            "RT-OPT,A,,10,Mill,MILL-OPT,1.5,0.25,,N,N\n"
            "RT-OPT,A,,20,Deburr,,0.5,0.1,,N,N\n"  # blank WC
            "RT-OPT,A,,30,Inspect,,0.5,0.1,,Y,N\n"  # blank WC
        )
        response = client.post(PREVIEW_URL, headers=auth_headers, files=_csv_file(csv_text))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()

        # Blank WC is NOT an error; the routing previews fine.
        assert body["dry_run"] is True
        assert body["routings_created"] == 1
        assert body["errors"] == []
        assert body["operations_needing_work_center"] == 2

        result = body["results"][0]
        ops = sorted(result["operations"], key=lambda o: o["sequence"])
        assert [o["needs_work_center"] for o in ops] == [False, True, True]
        # The resolved op carries its work_center_id; the blank ones don't.
        assert ops[0]["work_center_id"] is not None
        assert ops[1]["work_center_id"] is None and ops[1]["work_center_code"] is None
        # Totals are still summed (don't depend on the work center).
        assert result["total_setup_hours"] == pytest.approx(2.5)
        assert result["total_run_hours_per_unit"] == pytest.approx(0.45)
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

    def test_unknown_non_blank_work_center_code_is_still_a_row_error(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        _make_part(db_session, "RT-BADCODE", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + "RT-BADCODE,A,,10,Mill,NO-SUCH-WC,1,0.1,,N,N\n"
        response = client.post(PREVIEW_URL, headers=auth_headers, files=_csv_file(csv_text))
        body = response.json()
        # A typo'd code (non-blank, unresolvable) remains an error, not a needs-assignment.
        assert body["routings_created"] == 0
        assert any("NO-SUCH-WC" in e["reason"] for e in body["errors"])

    def test_preview_accepts_assignments_and_clears_needs_flag(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        wc = _make_work_center(db_session, "ASSIGN-WC")
        _make_part(db_session, "RT-PVA", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + (
            "RT-PVA,A,,10,Mill,,1,0.1,,N,N\n"  # blank WC -> assign row 2
            "RT-PVA,A,,20,Deburr,,1,0.1,,N,N\n"  # blank WC -> assign row 3
        )
        response = client.post(
            PREVIEW_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({2: wc.id, 3: wc.id}),
        )
        body = response.json()
        assert body["operations_needing_work_center"] == 0
        ops = body["results"][0]["operations"]
        assert all(o["work_center_id"] == wc.id for o in ops)
        assert all(o["needs_work_center"] is False for o in ops)
        # Still a dry run — nothing persisted.
        assert body["dry_run"] is True
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

    def test_commit_with_assignments_persists_chosen_work_center(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        wc_a = _make_work_center(db_session, "WCA")
        wc_b = _make_work_center(db_session, "WCB")
        _make_part(db_session, "RT-CMA", part_type="manufactured")
        db_session.commit()
        before_audit = db_session.query(AuditLog).count()

        # Both ops have a blank WC; the UI assigns row 2 -> WCA, row 3 -> WCB.
        csv_text = CSV_HEADER + ("RT-CMA,A,,10,Mill,,1,0.1,,N,N\n" "RT-CMA,A,,20,Deburr,,1,0.1,,N,N\n")
        response = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({2: wc_a.id, 3: wc_b.id}),
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["routings_created"] == 1
        assert body["operations_needing_work_center"] == 0
        assert body["errors"] == []

        routing_id = body["results"][0]["routing_id"]
        routing = db_session.query(Routing).filter_by(id=routing_id).one()
        ops = sorted(routing.operations, key=lambda op: op.sequence)
        assert [op.work_center_id for op in ops] == [wc_a.id, wc_b.id]
        # Audited like any other created routing.
        assert db_session.query(AuditLog).count() == before_audit + 1

    def test_commit_mixed_code_and_assignment_resolve_per_operation(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        wc_file = _make_work_center(db_session, "FILE-WC")
        wc_assigned = _make_work_center(db_session, "UI-WC")
        _make_part(db_session, "RT-MIX", part_type="manufactured")
        db_session.commit()

        # Row 2 resolves from the file code; row 3 is blank -> assigned in the UI.
        csv_text = CSV_HEADER + ("RT-MIX,A,,10,Mill,FILE-WC,1,0.1,,N,N\n" "RT-MIX,A,,20,Deburr,,1,0.1,,N,N\n")
        response = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({3: wc_assigned.id}),
        )
        body = response.json()
        assert body["routings_created"] == 1
        routing = db_session.query(Routing).filter_by(id=body["results"][0]["routing_id"]).one()
        ops = sorted(routing.operations, key=lambda op: op.sequence)
        assert ops[0].work_center_id == wc_file.id
        assert ops[1].work_center_id == wc_assigned.id

    def test_commit_with_unassigned_operation_errors_and_creates_nothing(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        wc = _make_work_center(db_session, "WC-PARTIAL")
        _make_part(db_session, "RT-UNASSIGNED", part_type="manufactured")
        db_session.commit()

        # Row 2 assigned, row 3 left blank with NO assignment -> whole routing errors.
        csv_text = CSV_HEADER + ("RT-UNASSIGNED,A,,10,Mill,,1,0.1,,N,N\n" "RT-UNASSIGNED,A,,20,Deburr,,1,0.1,,N,N\n")
        response = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({2: wc.id}),  # row 3 unassigned
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["routings_created"] == 0
        # The error names the unassigned row(s).
        reasons = " ".join(e["reason"] for e in body["errors"])
        assert "without a work center" in reasons and "3" in reasons
        # Nothing persisted — not even the assigned op.
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0
        assert db_session.query(RoutingOperation).filter_by(company_id=1).count() == 0

    def test_commit_assigned_inactive_work_center_is_rejected(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        dead_wc = _make_work_center(db_session, "DEAD-ASSIGN", is_active=False)
        _make_part(db_session, "RT-DEADASSIGN", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + "RT-DEADASSIGN,A,,10,Mill,,1,0.1,,N,N\n"
        response = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({2: dead_wc.id}),
        )
        body = response.json()
        assert body["routings_created"] == 0
        assert any("not found or inactive" in e["reason"] for e in body["errors"])
        assert db_session.query(Routing).filter_by(company_id=1).count() == 0

    def test_commit_assigned_cross_tenant_work_center_is_rejected(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        other = Company(id=2, name="Other Assign Co", slug="other-assign", is_active=True)
        db_session.add(other)
        db_session.flush()
        foreign_wc = _make_work_center(db_session, "FOREIGN-ASSIGN", company_id=2)
        _make_part(db_session, "RT-XTENANT", part_type="manufactured", company_id=1)
        db_session.commit()

        csv_text = CSV_HEADER + "RT-XTENANT,A,,10,Mill,,1,0.1,,N,N\n"
        response = client.post(
            COMMIT_URL,  # company 1 token
            headers=auth_headers,
            files=_csv_file(csv_text),
            data=_assignments({2: foreign_wc.id}),  # id belongs to company 2
        )
        body = response.json()
        assert body["routings_created"] == 0
        assert any("not found or inactive" in e["reason"] for e in body["errors"])
        # No routing landed in either company.
        assert db_session.query(Routing).count() == 0

    def test_malformed_assignments_json_is_a_400(self, client: TestClient, auth_headers: dict, db_session: Session):
        _make_part(db_session, "RT-BADJSON", part_type="manufactured")
        db_session.commit()

        csv_text = CSV_HEADER + "RT-BADJSON,A,,10,Mill,,1,0.1,,N,N\n"
        response = client.post(
            COMMIT_URL,
            headers=auth_headers,
            files=_csv_file(csv_text),
            data={"assignments": "{not valid json"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingImportTenantIsolation:
    def test_other_company_part_and_work_center_not_resolvable(
        self, client: TestClient, auth_headers: dict, db_session: Session
    ):
        other = Company(id=2, name="Other Co", slug="other-rimp", is_active=True)
        db_session.add(other)
        db_session.flush()
        # Foreign part + foreign work center (company 2).
        _make_part(db_session, "FOREIGN-PART", part_type="manufactured", company_id=2)
        _make_work_center(db_session, "FOREIGN-WC", company_id=2)
        # A local active work center so the only failure is the foreign part.
        _make_work_center(db_session, "LOCAL-WC", company_id=1)
        # A local part so the only failure (second row) is the foreign WC.
        _make_part(db_session, "LOCAL-PART", part_type="manufactured", company_id=1)
        db_session.commit()

        csv_text = CSV_HEADER + (
            "FOREIGN-PART,A,,10,Mill,LOCAL-WC,1,0.1,,N,N\n"  # foreign part not visible to company 1
            "LOCAL-PART,A,,10,Mill,FOREIGN-WC,1,0.1,,N,N\n"  # foreign WC not visible to company 1
        )
        response = client.post(COMMIT_URL, headers=auth_headers, files=_csv_file(csv_text))  # company 1 token
        body = response.json()
        assert body["routings_created"] == 0
        reasons = {e["row"]: e["reason"] for e in body["errors"]}
        assert "FOREIGN-PART" in reasons[2] and "not found" in reasons[2]
        assert "FOREIGN-WC" in reasons[3]
        # No routing landed in either company.
        assert db_session.query(Routing).count() == 0


@pytest.mark.api
@pytest.mark.requires_db
class TestRoutingImportRBAC:
    @pytest.mark.parametrize("url", [PREVIEW_URL, COMMIT_URL])
    def test_operator_forbidden(self, client: TestClient, operator_headers: dict, url: str):
        response = client.post(
            url,
            headers=operator_headers,
            files=_csv_file(CSV_HEADER + "X,A,,10,Mill,WC,1,0.1,,N,N\n"),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.parametrize("url", [PREVIEW_URL, COMMIT_URL])
    def test_viewer_forbidden(self, client: TestClient, db_session: Session, url: str):
        headers = _viewer_headers(db_session)
        response = client.post(
            url,
            headers=headers,
            files=_csv_file(CSV_HEADER + "X,A,,10,Mill,WC,1,0.1,,N,N\n"),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
