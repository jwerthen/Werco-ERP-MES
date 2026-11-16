"""P2.5 -- the ERP's FAI module is the FAI log of record.

Covers, through the ``/api/v1/quality/fai`` and ``/api/v1/exports/fai/export`` routes:

* ``GET /quality/fai/log`` -- one typed line per FAI carrying the paper log's columns (FAI
  Report #, date, part #, description, customer, job #, complete) plus what that log never
  captured; tenant-isolated even through a legacy FK that points at another company's work
  order or part; a since-deleted part / work order is still named (historical record);
  filters (status, customer, part, work order, search, Central date range) and offset paging;
* ``GET /exports/fai/export`` -- the legacy column order first, the form's ``X`` for complete,
  formula-initiating text neutralized, and the disclosure audited (the role gate and the
  commit-before-stream property are covered for every exporter in
  ``test_export_gate_and_audit.py``);
* ``POST /quality/fai/{id}/customer-approval`` -- Admin/Manager/Quality only, a person only
  (an API token is 403), only on a PASSED or CONDITIONAL FAI, once, never in the future,
  writes nothing but the two customer-approval fields, and its APPROVE row commits with it
  or nothing is saved (503);
* the create/disposition fixes: ``reason`` and a characteristic's ``specification`` are
  bounded at their column widths (422, not a Postgres 500), the inspected revision is
  stamped from the part when omitted, and the completion date is the Central shop day;
* ``FAIResponse`` resolves the inspector / approver names, job, customer and PO tenant-scoped.
"""

import csv
import io
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook
from sqlalchemy import inspect

from app.core import time_utils
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import FAICharacteristic, FAIStatus, FirstArticleInspection
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services import fai_service
from app.services.audit_service import AuditService
from app.services.fai_service import API_TOKEN_CUSTOMER_APPROVAL, API_TOKEN_FAI_DISPOSITION
from tests.api.export_audit_helpers import committed_export_rows, fresh_session

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/quality/fai"
LOG = f"{BASE}/log"
EXPORT = "/api/v1/exports/fai/export"
API_TOKENS_URL = "/api/v1/api-tokens/"
PASSWORD_HASH = "$2b$12$abcdefghijklmnopqrstuv"  # tokens minted directly; never used for login
_seq = itertools.count(1)

LEGACY_COLUMNS = ["fai_number", "log_date", "part_number", "part_name", "customer", "job_number", "is_complete"]


# --------------------------------------------------------------------------- builders


def _company(db, company_id=1):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        company = Company(id=company_id, name=f"Company {company_id}", slug=f"fai-log-co-{company_id}", is_active=True)
        db.add(company)
        db.commit()
    return company


def _user(db, role=UserRole.QUALITY, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    user = User(
        email=f"fai-log-{n}@co{company_id}.test",
        employee_id=f"FAIL-{n:05d}",
        first_name=extra.pop("first_name", "Quinn"),
        last_name=extra.pop("last_name", f"Inspector{n}"),
        hashed_password=PASSWORD_HASH,
        role=role,
        is_active=True,
        company_id=company_id,
        **extra,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user, active_company_id=None):
    company_id = active_company_id if active_company_id is not None else user.company_id
    token = create_access_token(subject=user.id, company_id=company_id)
    return {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}


def _part(db, company_id=1, **extra):
    _company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=extra.pop("part_number", f"FAIL-P-{company_id}-{n:04d}"),
        name=extra.pop("name", f"Angle bracket {n}"),
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
        revision=extra.pop("revision", "C"),
        **extra,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _work_order(db, part, company_id=1, **extra):
    n = next(_seq)
    wo = WorkOrder(
        work_order_number=extra.pop("work_order_number", f"WO-FAIL-{company_id}-{n:04d}"),
        part_id=part.id,
        quantity_ordered=5,
        status=WorkOrderStatus.RELEASED,
        company_id=company_id,
        customer_name=extra.pop("customer_name", "Rockwell Collins"),
        customer_po=extra.pop("customer_po", f"PO-{n:05d}"),
        **extra,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _fai(db, part, company_id=1, **extra):
    n = next(_seq)
    fai = FirstArticleInspection(
        company_id=company_id,
        fai_number=extra.pop("fai_number", f"FAI-LOG-{company_id}-{n:05d}"),
        part_id=part.id,
        part_revision=extra.pop("part_revision", part.revision),
        fai_type=extra.pop("fai_type", "full"),
        status=extra.pop("status", FAIStatus.PENDING),
        total_characteristics=extra.pop("total_characteristics", 0),
        characteristics_passed=0,
        characteristics_failed=0,
        **extra,
    )
    db.add(fai)
    db.commit()
    db.refresh(fai)
    return fai


def _token_headers(client, db, user):
    admin = _user(db, role=UserRole.ADMIN, company_id=user.company_id)
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "fai probe"}, headers=_headers(admin))
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}", "X-Requested-With": "XMLHttpRequest"}


def _snapshot(db):
    """Every FAI / characteristic / audit / event row, for "nothing changed" assertions."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (FirstArticleInspection, FAICharacteristic, AuditLog, OperationalEvent)
    }


def _log(client, user, **params):
    response = client.get(LOG, headers=_headers(user), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _numbers(rows):
    return [row["fai_number"] for row in rows]


# ============================================================================ the log


class TestLogLines:
    def test_a_line_carries_the_paper_log_columns_and_the_extras(self, client, db_session):
        part = _part(db_session, name="PANEL DIVIDER RH", revision="D", customer_name="Part-level customer")
        wo = _work_order(db_session, part, customer_name="ROCKWELL-TX", customer_po="PO-620ARG002")
        fai = _fai(
            db_session,
            part,
            work_order_id=wo.id,
            part_revision="C",
            serial_number="SN-0042",
            reason="new_part",
            status=FAIStatus.PASSED,
            inspection_date=date(2026, 3, 2),
            completed_date=date(2026, 3, 4),
            customer_approval_required=True,
        )

        rows = _log(client, _user(db_session, role=UserRole.VIEWER))

        assert len(rows) == 1
        row = rows[0]
        assert row["fai_id"] == fai.id
        assert row["fai_number"] == fai.fai_number
        assert row["log_date"] == "2026-03-04"  # completion date wins
        assert row["part_number"] == part.part_number
        assert row["part_name"] == "PANEL DIVIDER RH"
        assert row["customer"] == "ROCKWELL-TX"  # the job's customer, ahead of the part's
        assert row["job_number"] == wo.work_order_number
        assert row["is_complete"] is True
        assert row["part_revision"] == "C"  # the revision inspected ...
        assert row["part_current_revision"] == "D"  # ... is not the part's revision today
        assert row["customer_po"] == "PO-620ARG002"
        assert row["status"] == "passed"
        assert row["serial_number"] == "SN-0042"
        assert row["reason"] == "new_part"
        assert row["customer_approval_required"] is True and row["customer_approved"] is False
        assert row["created_at"].endswith("Z")

    def test_log_date_falls_back_to_inspection_then_to_the_central_creation_day(self, client, db_session):
        part = _part(db_session)
        inspected = _fai(db_session, part, inspection_date=date(2026, 5, 1))
        # 03:30 UTC on Nov 14 is still Nov 13 in Central: the log dates it the shop's day.
        created_only = _fai(db_session, part, created_at=datetime(2026, 11, 14, 3, 30))

        rows = {row["fai_id"]: row for row in _log(client, _user(db_session))}

        assert rows[inspected.id]["log_date"] == "2026-05-01"
        assert rows[inspected.id]["is_complete"] is False
        assert rows[created_only.id]["log_date"] == "2026-11-13"

    def test_customer_falls_back_to_the_part_and_a_job_less_fai_has_no_job_or_po(self, client, db_session):
        part = _part(db_session, customer_name="L3-LINK")
        blank_customer_wo = _work_order(db_session, part, customer_name="   ")
        via_wo = _fai(db_session, part, work_order_id=blank_customer_wo.id)
        no_wo = _fai(db_session, part)

        rows = {row["fai_id"]: row for row in _log(client, _user(db_session))}

        assert rows[via_wo.id]["customer"] == "L3-LINK"
        assert rows[via_wo.id]["job_number"] == blank_customer_wo.work_order_number
        assert rows[no_wo.id]["customer"] == "L3-LINK"
        assert rows[no_wo.id]["job_number"] is None and rows[no_wo.id]["customer_po"] is None
        # The customer FILTER (SQL) reads the same fallback the line displays (Python): a
        # whitespace-only job customer falls through to the part's, so filtering on the
        # customer the log shows finds both lines.
        filtered = _log(client, _user(db_session), customer="l3-li")
        assert {row["fai_id"] for row in filtered} == {via_wo.id, no_wo.id}

    def test_a_since_deleted_part_and_work_order_are_still_named(self, client, db_session):
        part = _part(db_session, name="Retired bracket")
        wo = _work_order(db_session, part, customer_name="HELICOMB")
        fai = _fai(db_session, part, work_order_id=wo.id, status=FAIStatus.PASSED, completed_date=date(2026, 1, 5))
        part.is_deleted = True
        wo.is_deleted = True
        db_session.commit()

        row = _log(client, _user(db_session))[0]

        assert row["fai_id"] == fai.id
        assert row["part_name"] == "Retired bracket"
        assert row["job_number"] == wo.work_order_number
        assert row["customer"] == "HELICOMB"


class TestLogTenantIsolation:
    def test_another_companys_fais_never_appear(self, client, db_session):
        own = _fai(db_session, _part(db_session))
        foreign_part = _part(db_session, company_id=2)
        _fai(db_session, foreign_part, company_id=2)

        rows = _log(client, _user(db_session))

        assert [row["fai_id"] for row in rows] == [own.id]

    def test_a_legacy_fk_into_another_company_leaks_no_label(self, client, db_session):
        """A malformed FK to a foreign work order / part joins nothing: blanks, never their data."""
        own_part = _part(db_session)
        foreign_part = _part(db_session, company_id=2, name="Foreign secret part", customer_name="Foreign Customer")
        foreign_wo = _work_order(
            db_session, foreign_part, company_id=2, customer_name="Foreign Customer", customer_po="FOREIGN-PO"
        )
        fai = _fai(db_session, own_part, work_order_id=foreign_wo.id)
        fai.part_id = foreign_part.id
        db_session.commit()

        response = client.get(LOG, headers=_headers(_user(db_session)))
        assert response.status_code == 200, response.text
        row = response.json()[0]

        assert row["fai_id"] == fai.id
        assert row["part_number"] is None and row["part_name"] is None
        assert row["job_number"] is None and row["customer_po"] is None and row["customer"] is None
        for secret in ("Foreign secret part", "Foreign Customer", "FOREIGN-PO", foreign_wo.work_order_number):
            assert secret not in response.text

    def test_a_platform_admin_reads_the_active_companys_log(self, client, db_session):
        foreign = _fai(db_session, _part(db_session, company_id=2), company_id=2)
        _fai(db_session, _part(db_session))
        platform = _user(db_session, role=UserRole.PLATFORM_ADMIN)

        response = client.get(LOG, headers=_headers(platform, active_company_id=2))

        assert response.status_code == 200, response.text
        assert [row["fai_id"] for row in response.json()] == [foreign.id]


class TestLogFiltersAndPaging:
    def test_status_part_work_order_and_customer_filters(self, client, db_session):
        part_a = _part(db_session, customer_name="XOMETRY")
        part_b = _part(db_session)
        wo = _work_order(db_session, part_b, customer_name="Newton Design")
        passed = _fai(db_session, part_a, status=FAIStatus.PASSED, completed_date=date(2026, 2, 1))
        pending = _fai(db_session, part_b, work_order_id=wo.id)
        user = _user(db_session)

        assert _numbers(_log(client, user, status="passed")) == [passed.fai_number]
        assert _numbers(_log(client, user, part_id=part_b.id)) == [pending.fai_number]
        assert _numbers(_log(client, user, work_order_id=wo.id)) == [pending.fai_number]
        assert _numbers(_log(client, user, customer="newton")) == [pending.fai_number]
        assert _numbers(_log(client, user, customer="xomet")) == [passed.fai_number]  # the part's customer

    def test_a_whitespace_only_customer_or_search_filter_is_no_filter(self, client, db_session):
        # Regression: "   " passed the truthiness check and became the pattern "%%", which
        # dropped every FAI whose Customer is empty (no job, no customer on the part).
        with_customer = _fai(db_session, _part(db_session, customer_name="Acme"))
        without_customer = _fai(db_session, _part(db_session))
        viewer = _user(db_session, role=UserRole.VIEWER)

        for params in ({"customer": "   "}, {"search": "  "}, {"customer": " ", "search": "\t"}):
            numbers = _numbers(_log(client, viewer, **params))
            assert with_customer.fai_number in numbers and without_customer.fai_number in numbers, params

    def test_search_matches_fai_part_job_po_and_serial_and_treats_wildcards_literally(self, client, db_session):
        part = _part(db_session, part_number="818-3931-618", name="ROD END MANUAL PULL")
        wo = _work_order(db_session, part, work_order_number="WA-7201", customer_po="PO 100_A")
        fai = _fai(db_session, part, work_order_id=wo.id, serial_number="SN-77")
        other = _fai(db_session, _part(db_session))
        user = _user(db_session)

        for term in ("3931", "manual pull", "WA-72", "100_A", "sn-77", fai.fai_number):
            assert _numbers(_log(client, user, search=term)) == [fai.fai_number], term
        assert _numbers(_log(client, user, search="%")) == []
        assert _numbers(_log(client, user, search="100%A")) == []
        assert other.fai_number in _numbers(_log(client, user))

    def test_date_range_is_inclusive_central_days_on_the_log_date(self, client, db_session):
        part = _part(db_session)
        completed = _fai(db_session, part, status=FAIStatus.PASSED, completed_date=date(2026, 6, 10))
        inspected = _fai(db_session, part, inspection_date=date(2026, 6, 12))
        # Created 2026-06-15 02:00 UTC = 2026-06-14 21:00 Central: it belongs to the 14th.
        created = _fai(db_session, part, created_at=datetime(2026, 6, 15, 2, 0))
        user = _user(db_session)

        assert set(_numbers(_log(client, user, start_date="2026-06-10", end_date="2026-06-14"))) == {
            completed.fai_number,
            inspected.fai_number,
            created.fai_number,
        }
        assert _numbers(_log(client, user, start_date="2026-06-11", end_date="2026-06-13")) == [inspected.fai_number]
        assert _numbers(_log(client, user, start_date="2026-06-15")) == []
        assert _numbers(_log(client, user, end_date="2026-06-13")) == [inspected.fai_number, completed.fai_number]

    def test_newest_first_with_offset_paging(self, client, db_session):
        part = _part(db_session)
        base = datetime(2026, 4, 1, 15, 0)
        fais = [_fai(db_session, part, created_at=base + timedelta(days=i)) for i in range(5)]
        user = _user(db_session)
        newest_first = [f.fai_number for f in reversed(fais)]

        assert _numbers(_log(client, user)) == newest_first
        assert _numbers(_log(client, user, limit=2)) == newest_first[:2]
        assert _numbers(_log(client, user, limit=2, offset=2)) == newest_first[2:4]
        assert _numbers(_log(client, user, limit=2, offset=4)) == newest_first[4:]

    @pytest.mark.parametrize("param", ["customer", "search"])
    def test_free_text_filters_are_bounded(self, client, db_session, param):
        response = client.get(LOG, headers=_headers(_user(db_session)), params={param: "x" * 256})
        assert response.status_code == 422

    def test_paging_is_bounded(self, client, db_session):
        response = client.get(LOG, headers=_headers(_user(db_session)), params={"limit": 501})
        assert response.status_code == 422

    def test_anonymous_caller_is_refused(self, client):
        assert client.get(LOG).status_code in (401, 403)


# ============================================================================ the export


def _csv_rows(text):
    return list(csv.reader(io.StringIO(text)))


class TestLogExport:
    def test_default_columns_lead_with_the_paper_logs_order(self, client, db_session):
        part = _part(db_session, name="ANGLE")
        wo = _work_order(db_session, part, customer_name="ROCKWELL-TX")
        _fai(db_session, part, work_order_id=wo.id, status=FAIStatus.PASSED, completed_date=date(2026, 3, 14))
        _fai(db_session, part)

        response = client.get(EXPORT, headers=_headers(_user(db_session, role=UserRole.MANAGER)))

        assert response.status_code == 200, response.text
        assert "attachment" in response.headers["content-disposition"]
        rows = _csv_rows(response.text)
        assert rows[0][:7] == LEGACY_COLUMNS
        assert rows[0][7:] == [
            "part_revision",
            "customer_po",
            "status",
            "fai_type",
            "reason",
            "serial_number",
            "inspection_date",
            "completed_date",
            "customer_approval_required",
            "customer_approved",
            "customer_approval_date",
        ]
        by_status = {row[rows[0].index("status")]: dict(zip(rows[0], row)) for row in rows[1:]}
        assert by_status["passed"]["is_complete"] == "X"
        assert by_status["passed"]["log_date"] == "2026-03-14"
        assert by_status["passed"]["customer"] == "ROCKWELL-TX"
        assert by_status["passed"]["part_name"] == "ANGLE"
        assert by_status["pending"]["is_complete"] == ""

    def test_formula_initiating_text_is_neutralized_in_csv_and_never_a_formula_in_xlsx(self, client, db_session):
        part = _part(db_session, name='=HYPERLINK("http://evil.test","x")')
        wo = _work_order(db_session, part, customer_name="+cmd|' /C calc'!A0")
        _fai(db_session, part, work_order_id=wo.id)
        manager = _user(db_session, role=UserRole.MANAGER)

        csv_response = client.get(EXPORT, headers=_headers(manager), params={"format": "csv"})
        assert csv_response.status_code == 200, csv_response.text
        header, line = _csv_rows(csv_response.text)
        values = dict(zip(header, line))
        assert values["part_name"].startswith("'=HYPERLINK")
        assert values["customer"].startswith("'+cmd")

        xlsx_response = client.get(EXPORT, headers=_headers(manager), params={"format": "xlsx"})
        assert xlsx_response.status_code == 200
        sheet = load_workbook(io.BytesIO(xlsx_response.content)).active
        assert sheet.title == "FAI Log"
        cells = [cell for row in sheet.iter_rows() for cell in row]
        assert any(cell.value == '=HYPERLINK("http://evil.test","x")' for cell in cells)
        assert all(cell.data_type != "f" for cell in cells)

    def test_export_applies_the_log_filters_and_audits_the_request(self, client, db_session):
        part = _part(db_session)
        wo = _work_order(db_session, part, customer_name="L3-LINK")
        wanted = _fai(db_session, part, work_order_id=wo.id, status=FAIStatus.PASSED, completed_date=date(2026, 7, 1))
        _fai(db_session, part)
        manager = _user(db_session, role=UserRole.MANAGER)

        response = client.get(
            EXPORT,
            headers=_headers(manager),
            params={"status": "passed", "customer": "l3", "start_date": "2026-07-01", "columns": ["fai_number"]},
        )

        assert response.status_code == 200, response.text
        assert _csv_rows(response.text) == [["fai_number"], [wanted.fai_number]]
        rows = committed_export_rows("fai")
        assert len(rows) == 1
        assert rows[0].description == "Exported 1 FAI log record(s) to CSV"
        assert rows[0].new_values["columns"] == ["fai_number"]
        assert rows[0].new_values["filters"] == {"status": "passed", "customer": "l3", "start_date": "2026-07-01"}
        assert wanted.fai_number not in str(rows[0].new_values)

    def test_export_is_tenant_isolated_even_through_a_foreign_fk(self, client, db_session):
        foreign_part = _part(db_session, company_id=2, name="Foreign secret part")
        foreign_wo = _work_order(db_session, foreign_part, company_id=2, customer_name="Foreign Customer")
        _fai(db_session, foreign_part, company_id=2, fai_number="FAI-FOREIGN-ONLY")
        own = _fai(db_session, _part(db_session), work_order_id=foreign_wo.id)

        response = client.get(EXPORT, headers=_headers(_user(db_session, role=UserRole.ADMIN)))

        assert response.status_code == 200, response.text
        assert own.fai_number in response.text
        for secret in ("FAI-FOREIGN-ONLY", "Foreign secret part", "Foreign Customer", foreign_wo.work_order_number):
            assert secret not in response.text

    def test_quality_cannot_export_but_can_read_the_log(self, client, db_session):
        quality = _user(db_session, role=UserRole.QUALITY)
        assert client.get(EXPORT, headers=_headers(quality)).status_code == 403
        assert client.get(LOG, headers=_headers(quality)).status_code == 200
        assert committed_export_rows("fai") == []


# ============================================================================ customer approval


def _approve(client, user, fai, body=None, headers=None):
    return client.post(
        f"{BASE}/{fai.id}/customer-approval", headers=headers or _headers(user), json=body if body is not None else {}
    )


def _approval_rows(fai_id):
    with fresh_session() as verify:
        return [
            {
                "action": row.action,
                "user_id": row.user_id,
                "company_id": row.company_id,
                "old_values": row.old_values,
                "new_values": row.new_values,
                "extra_data": row.extra_data,
            }
            for row in verify.query(AuditLog)
            .filter(AuditLog.resource_type == "fai", AuditLog.resource_id == fai_id, AuditLog.action == "APPROVE")
            .order_by(AuditLog.id)
            .all()
        ]


class TestCustomerApproval:
    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
    @pytest.mark.parametrize("final_status", [FAIStatus.PASSED, FAIStatus.CONDITIONAL])
    def test_records_only_the_two_fields_with_a_committed_approve_row(self, client, db_session, role, final_status):
        fai = _fai(
            db_session,
            _part(db_session),
            status=final_status,
            completed_date=date(2026, 9, 1),
            notes="Frozen evidence",
            customer_approval_required=True,
        )
        user = _user(db_session, role=role)
        before = {k: v for k, v in _snapshot(db_session)["fais"][0].items()}

        response = _approve(client, user, fai, {"customer_approval_date": "2026-09-03"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["customer_approved"] is True and body["customer_approval_date"] == "2026-09-03"
        after = _snapshot(db_session)["fais"][0]
        changed = {key for key in after if after[key] != before[key]}
        assert changed <= {"customer_approved", "customer_approval_date", "updated_at"}
        assert {"customer_approved", "customer_approval_date"} <= changed
        assert after["status"] == final_status and after["notes"] == "Frozen evidence"
        rows = _approval_rows(fai.id)
        assert len(rows) == 1
        assert rows[0]["user_id"] == user.id and rows[0]["company_id"] == 1
        assert rows[0]["old_values"] == {"customer_approved": False, "customer_approval_date": None}
        assert rows[0]["new_values"] == {"customer_approved": True, "customer_approval_date": "2026-09-03"}
        assert rows[0]["extra_data"]["customer_approval"] is True
        assert rows[0]["extra_data"]["status"] == final_status.value

    def test_date_defaults_to_the_central_shop_day(self, client, db_session, monkeypatch):
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 13))
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 11, 1))

        response = _approve(client, _user(db_session), fai)

        assert response.status_code == 200, response.text
        assert response.json()["customer_approval_date"] == "2026-11-13"

    def test_future_date_is_refused_and_nothing_changes(self, client, db_session, monkeypatch):
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 13))
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 11, 1))
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session), fai, {"customer_approval_date": "2026-11-14"})

        assert response.status_code == 422, response.text
        assert _snapshot(db_session) == before

    def test_a_date_before_the_inspection_date_is_refused_and_nothing_changes(self, client, db_session):
        fai = _fai(
            db_session,
            _part(db_session),
            status=FAIStatus.PASSED,
            inspection_date=date(2026, 9, 10),
            completed_date=date(2026, 9, 11),
        )
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session), fai, {"customer_approval_date": "2026-09-09"})

        assert response.status_code == 422, response.text
        assert "09/10/2026" in response.json()["detail"]
        assert _snapshot(db_session) == before
        same_day = _approve(client, _user(db_session), fai, {"customer_approval_date": "2026-09-10"})
        assert same_day.status_code == 200, same_day.text

    def test_a_frozen_future_inspection_date_does_not_block_the_approval(self, client, db_session, monkeypatch):
        # A row finalized with a typo'd future inspection date (stored before PUT refused one)
        # can no longer be corrected. Neither today nor the inspection date itself would pass
        # both date rules, so the lower bound does not apply to it.
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 15))
        fai = _fai(
            db_session,
            _part(db_session),
            status=FAIStatus.PASSED,
            inspection_date=date(2027, 12, 20),
            completed_date=date(2026, 11, 15),
        )

        response = _approve(client, _user(db_session), fai)

        assert response.status_code == 200, response.text
        assert response.json()["customer_approval_date"] == "2026-11-15"

    def test_the_body_is_optional_and_defaults_to_today(self, client, db_session, monkeypatch):
        # Every field of the body is optional, so a bodyless call (an MCP tool call with no
        # arguments) records today's approval instead of answering 422.
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 13))
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 11, 1))

        response = client.post(f"{BASE}/{fai.id}/customer-approval", headers=_headers(_user(db_session)))

        assert response.status_code == 200, response.text
        assert response.json()["customer_approved"] is True
        assert response.json()["customer_approval_date"] == "2026-11-13"

    @pytest.mark.parametrize("status", [FAIStatus.PENDING, FAIStatus.IN_PROGRESS, FAIStatus.FAILED])
    def test_refused_before_a_passing_final_disposition(self, client, db_session, status):
        completed = date(2026, 9, 1) if status == FAIStatus.FAILED else None
        fai = _fai(db_session, _part(db_session), status=status, completed_date=completed)
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session), fai)

        assert response.status_code == 409, response.text
        assert fai.fai_number in response.json()["detail"]
        assert _snapshot(db_session) == before

    def test_recorded_once(self, client, db_session):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))
        first = _approve(client, _user(db_session), fai, {"customer_approval_date": "2026-09-02"})
        assert first.status_code == 200, first.text
        before = _snapshot(db_session)

        again = _approve(client, _user(db_session, role=UserRole.ADMIN), fai, {"customer_approval_date": "2026-09-05"})

        assert again.status_code == 409, again.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.OPERATOR, UserRole.VIEWER, UserRole.SHIPPING])
    def test_roles_outside_admin_manager_quality_are_refused(self, client, db_session, role):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session, role=role), fai)

        assert response.status_code == 403, response.text
        assert _snapshot(db_session) == before

    def test_api_token_is_refused_and_nothing_changes(self, client, db_session):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)

        response = _approve(client, None, fai, headers=headers)

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == API_TOKEN_CUSTOMER_APPROVAL
        assert _snapshot(db_session) == before

    def test_foreign_tenant_fai_is_404(self, client, db_session):
        fai = _fai(
            db_session,
            _part(db_session, company_id=2),
            company_id=2,
            status=FAIStatus.PASSED,
            completed_date=date.today(),
        )
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session), fai)

        assert response.status_code == 404, response.text
        assert _snapshot(db_session) == before

    def test_the_request_cannot_carry_other_fields(self, client, db_session):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))
        before = _snapshot(db_session)

        response = _approve(client, _user(db_session), fai, {"customer_approved": False, "status": "pending"})

        assert response.status_code == 422, response.text
        assert _snapshot(db_session) == before

    def test_audit_failure_saves_nothing(self, client, db_session, monkeypatch):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))
        user = _user(db_session)
        before = _snapshot(db_session)
        monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)

        response = _approve(client, user, fai)

        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "Unable to save audit record"}
        assert _snapshot(db_session) == before

    def test_emits_an_update_event_that_is_not_a_second_completion(self, client, db_session):
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, completed_date=date(2026, 9, 1))

        assert _approve(client, _user(db_session), fai).status_code == 200

        event = (
            db_session.query(OperationalEvent)
            .filter(OperationalEvent.entity_type == "fai", OperationalEvent.entity_id == fai.id)
            .one()
        )
        assert event.event_type == "fai_updated"
        assert event.event_payload["previous_status"] == event.event_payload["status"] == "passed"
        assert event.event_payload["changed_fields"] == ["customer_approved", "customer_approval_date"]

    def test_put_still_cannot_touch_customer_approval(self, client, db_session):
        fai = _fai(db_session, _part(db_session))
        response = client.put(
            f"{BASE}/{fai.id}",
            headers=_headers(_user(db_session)),
            json={"version": 0, "customer_approved": True, "customer_approval_date": "2026-01-01"},
        )
        assert response.status_code == 200, response.text
        db_session.expire_all()
        row = db_session.get(FirstArticleInspection, fai.id)
        assert not row.customer_approved and row.customer_approval_date is None


# ============================================================================ create / disposition fixes


class TestCreateAndDisposition:
    def test_reason_is_bounded_at_the_column_width(self, client, db_session):
        part = _part(db_session)
        user = _user(db_session)
        before = _snapshot(db_session)

        too_long = client.post(BASE, headers=_headers(user), json={"part_id": part.id, "reason": "r" * 51})

        assert too_long.status_code == 422, too_long.text
        assert _snapshot(db_session) == before
        at_bound = client.post(BASE, headers=_headers(user), json={"part_id": part.id, "reason": "r" * 50})
        assert at_bound.status_code == 200, at_bound.text

    def test_characteristic_specification_is_bounded_at_the_column_width(self, client, db_session):
        fai = _fai(db_session, _part(db_session))
        user = _user(db_session)
        url = f"{BASE}/{fai.id}/characteristics"
        body = {"char_number": 1, "characteristic": "Overall length"}

        assert client.post(url, headers=_headers(user), json={**body, "specification": "s" * 256}).status_code == 422
        ok = client.post(url, headers=_headers(user), json={**body, "specification": "s" * 255})
        assert ok.status_code == 200, ok.text

    def test_revision_defaults_to_the_parts_current_revision_and_is_normalized(self, client, db_session):
        part = _part(db_session, revision="F")
        user = _user(db_session)

        omitted = client.post(BASE, headers=_headers(user), json={"part_id": part.id})
        blank = client.post(BASE, headers=_headers(user), json={"part_id": part.id, "part_revision": "  "})
        lowered = client.post(BASE, headers=_headers(user), json={"part_id": part.id, "part_revision": " e "})

        assert omitted.json()["part_revision"] == "F"
        assert blank.json()["part_revision"] == "F"
        assert lowered.json()["part_revision"] == "E"
        assert lowered.json()["part_current_revision"] == "F"
        audit = (
            db_session.query(AuditLog)
            .filter_by(resource_type="fai", resource_id=omitted.json()["id"], action="CREATE")
            .one()
        )
        assert audit.new_values["part_revision"] == "F"

    def test_create_end_to_end_with_a_work_order_reads_back_job_customer_and_po(self, client, db_session):
        part = _part(db_session, revision="B")
        wo = _work_order(db_session, part, customer_name="CAE - Arlington", customer_po="PO-555")
        user = _user(db_session)

        response = client.post(
            BASE,
            headers=_headers(user),
            json={
                "part_id": part.id,
                "work_order_id": wo.id,
                "serial_number": "SN-1",
                "due_date": "2026-12-01",
                "reason": "new_part",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["work_order_number"] == wo.work_order_number
        assert body["customer_name"] == "CAE - Arlington"
        assert body["customer_po"] == "PO-555"
        assert body["part_revision"] == "B" and body["serial_number"] == "SN-1"
        assert body["due_date"] == "2026-12-01"

    def test_final_disposition_stamps_the_central_shop_day(self, client, db_session, monkeypatch):
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2001, 2, 3))
        fai = _fai(db_session, _part(db_session))
        user = _user(db_session, role=UserRole.QUALITY)

        response = client.put(f"{BASE}/{fai.id}", headers=_headers(user), json={"version": 0, "status": "passed"})

        assert response.status_code == 200, response.text
        assert response.json()["completed_date"] == "2001-02-03"
        assert response.json()["approved_by"] == user.id
        assert response.json()["approved_by_name"] == user.full_name

    def test_a_future_inspection_date_is_refused_and_nothing_changes(self, client, db_session, monkeypatch):
        # Regression: a typo'd future inspection date, frozen by the disposition, left the
        # customer approval no date it could carry (not before the inspection, not future).
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 15))
        fai = _fai(db_session, _part(db_session), status=FAIStatus.IN_PROGRESS)
        user = _user(db_session, role=UserRole.QUALITY)
        before = _snapshot(db_session)

        response = client.put(
            f"{BASE}/{fai.id}", headers=_headers(user), json={"version": 0, "inspection_date": "2027-12-20"}
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "The inspection date cannot be in the future."
        assert _snapshot(db_session) == before
        today = client.put(
            f"{BASE}/{fai.id}", headers=_headers(user), json={"version": 0, "inspection_date": "2026-11-15"}
        )
        assert today.status_code == 200, today.text
        assert today.json()["inspection_date"] == "2026-11-15"

    def test_the_disposition_refuses_a_stored_future_inspection_date(self, client, db_session, monkeypatch):
        # A future date stored before PUT refused one is corrected before the disposition
        # freezes it; a disposition that corrects it in the same request goes through.
        monkeypatch.setattr(fai_service, "central_today", lambda: date(2026, 11, 15))
        fai = _fai(db_session, _part(db_session), status=FAIStatus.IN_PROGRESS, inspection_date=date(2027, 12, 20))
        user = _user(db_session, role=UserRole.QUALITY)
        before = _snapshot(db_session)

        response = client.put(f"{BASE}/{fai.id}", headers=_headers(user), json={"version": 0, "status": "passed"})

        assert response.status_code == 409, response.text
        assert "12/20/2027" in response.json()["detail"] and fai.fai_number in response.json()["detail"]
        assert _snapshot(db_session) == before
        corrected = client.put(
            f"{BASE}/{fai.id}",
            headers=_headers(user),
            json={"version": 0, "status": "passed", "inspection_date": "2026-11-14"},
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["inspection_date"] == "2026-11-14"
        assert corrected.json()["completed_date"] == "2026-11-15"
        approval = _approve(client, user, fai)
        assert approval.status_code == 200, approval.text

    @pytest.mark.parametrize("final_status", ["passed", "failed", "conditional"])
    def test_api_token_cannot_record_the_final_disposition(self, client, db_session, final_status):
        # The disposition is the inspection's quality judgment: it stamps the approver and
        # freezes the evidence. A standing token is refused before the lock and any write,
        # even when it is bound to a Quality user who could disposition in the app.
        fai = _fai(db_session, _part(db_session))
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))
        before = _snapshot(db_session)

        response = client.put(f"{BASE}/{fai.id}", headers=headers, json={"version": 0, "status": final_status})

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == API_TOKEN_FAI_DISPOSITION
        assert _snapshot(db_session) == before

    def test_api_token_may_still_edit_an_unfinished_fai(self, client, db_session):
        fai = _fai(db_session, _part(db_session))
        headers = _token_headers(client, db_session, _user(db_session, role=UserRole.QUALITY))

        response = client.put(
            f"{BASE}/{fai.id}", headers=headers, json={"version": 0, "status": "in_progress", "notes": "Started"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "in_progress" and response.json()["completed_date"] is None

    def test_a_work_order_for_another_part_is_refused_and_nothing_is_written(self, client, db_session):
        # Regression: the log names the linked job's number, customer and PO, so an FAI of
        # part X linked to part Y's job put Y's customer on X's first article.
        inspected = _part(db_session)
        other_job = _work_order(db_session, _part(db_session), customer_name="Someone Else")
        before = _snapshot(db_session)

        response = client.post(
            BASE, headers=_headers(_user(db_session)), json={"part_id": inspected.id, "work_order_id": other_job.id}
        )

        assert response.status_code == 422, response.text
        assert other_job.work_order_number in response.json()["detail"]
        assert inspected.part_number in response.json()["detail"]
        assert _snapshot(db_session) == before

    def test_a_component_of_an_assembly_job_and_a_part_less_job_are_accepted(self, client, db_session):
        component = _part(db_session)
        assembly_job = _work_order(db_session, _part(db_session))
        center = WorkCenter(
            name=f"FAIL-WC-{next(_seq)}",
            code=f"FAIL-WC-{next(_seq)}",
            work_center_type="assembly",
            hourly_rate=100,
            is_active=True,
            company_id=1,
        )
        db_session.add(center)
        db_session.commit()
        db_session.add(
            WorkOrderOperation(
                work_order_id=assembly_job.id,
                work_center_id=center.id,
                sequence=10,
                operation_number="10",
                name="Weld component",
                status=OperationStatus.PENDING,
                component_part_id=component.id,
                company_id=1,
            )
        )
        part_less_job = WorkOrder(
            work_order_number=f"WO-FAIL-NEST-{next(_seq):04d}",
            part_id=None,
            work_order_type="laser_cutting",  # a standalone laser-nest job names no part
            quantity_ordered=1,
            status=WorkOrderStatus.RELEASED,
            company_id=1,
        )
        db_session.add(part_less_job)
        db_session.commit()
        user = _user(db_session)

        for job in (assembly_job, part_less_job):
            response = client.post(
                BASE, headers=_headers(user), json={"part_id": component.id, "work_order_id": job.id}
            )
            assert response.status_code == 200, response.text
            assert response.json()["work_order_id"] == job.id

    def test_inspector_and_approver_names_resolve_tenant_scoped(self, client, db_session):
        inspector = _user(db_session, first_name="Ivy", last_name="Inspector")
        foreign = _user(db_session, company_id=2, first_name="Foreign", last_name="Person")
        own = _fai(db_session, _part(db_session), inspector_id=inspector.id)
        legacy = _fai(db_session, _part(db_session), inspector_id=foreign.id, approved_by=foreign.id)
        viewer = _user(db_session, role=UserRole.VIEWER)

        own_body = client.get(f"{BASE}/{own.id}", headers=_headers(viewer)).json()
        legacy_response = client.get(f"{BASE}/{legacy.id}", headers=_headers(viewer))

        assert own_body["inspector_id"] == inspector.id and own_body["inspector_name"] == "Ivy Inspector"
        assert legacy_response.json()["inspector_name"] is None
        assert legacy_response.json()["approved_by_name"] is None
        assert "Foreign" not in legacy_response.text

    def test_a_platform_principals_disposition_is_still_named(self, client, db_session):
        platform = _user(db_session, role=UserRole.PLATFORM_ADMIN, company_id=2, first_name="Pat", last_name="Platform")
        fai = _fai(db_session, _part(db_session), status=FAIStatus.PASSED, approved_by=platform.id)

        body = client.get(f"{BASE}/{fai.id}", headers=_headers(_user(db_session, role=UserRole.VIEWER))).json()

        assert body["approved_by_name"] == "Pat Platform"


def test_central_today_reads_the_shop_calendar_not_the_host(monkeypatch):
    """03:00 UTC on Feb 4 is 21:00 on Feb 3 in Central (a date far from any real "today")."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2001, 2, 4, 3, 0, tzinfo=timezone.utc)
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    # ``central_today`` lives in ``app.core.time_utils`` (shared with the CAR service); patch
    # the clock it actually reads, or this would pass on any host whose Central date matches.
    monkeypatch.setattr(time_utils, "datetime", _Frozen)
    assert fai_service.central_today() == date(2001, 2, 3)
    assert fai_service.central_today is time_utils.central_today
