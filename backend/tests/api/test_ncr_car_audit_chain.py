"""NCR / CAR writes are on the audit chain, tenant-validated, and final once closed or void.

Covers ``app/services/quality_record_service.py`` through the ``/api/v1/quality`` routes:

* every write (NCR create / update / disposition / closure / reopen / CAR link, CAR
  create / update / verification / closure / void) commits exactly one required audit
  row with old -> new values -- read back through a SECOND connection
  (``export_audit_helpers.fresh_session``), so a handler that logged but never
  committed fails here instead of passing on the shared test session;
* a lost audit row rolls the write back and answers 503;
* a foreign-tenant, deleted or inactive reference is refused before anything changes;
* create-CAR-from-NCR is idempotent (409 naming the existing CAR);
* a CLOSED record refuses edits except a status-only reopen, and a VOID CAR refuses all;
* ``POST /quality/car/{id}/void`` requires a reason and refuses a CLOSED CAR.
"""

import itertools
from datetime import date

import pytest
from sqlalchemy import inspect

from app.models.audit_log import AuditLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.quality import (
    CARStatus,
    CorrectiveActionRequest,
    NCRDisposition,
    NCRSource,
    NCRStatus,
    NonConformanceReport,
)
from app.models.user import UserRole
from app.models.work_order import WorkOrder
from app.services.audit_service import AuditService
from tests.api.export_audit_helpers import fresh_session
from tests.api.kiosk_test_helpers import ensure_company, make_user, user_headers

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

BASE = "/api/v1/quality"
ROOT_CAUSE = "Fixture wear let the edge burr through deburr."
_seq = itertools.count(1)


# --------------------------------------------------------------------------- builders


def _make_part(db, company_id=1, **extra):
    ensure_company(db, company_id)
    n = next(_seq)
    part = Part(
        part_number=f"NCRP-{company_id}-{n:04d}",
        name=f"Bracket {n}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=company_id,
        **extra,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def _make_work_order(db, company_id=1, **extra):
    part = _make_part(db, company_id)
    n = next(_seq)
    wo = WorkOrder(
        work_order_number=f"NCRWO-{company_id}-{n:04d}",
        part_id=part.id,
        quantity_ordered=5,
        company_id=company_id,
        **extra,
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


def _make_ncr(db, company_id=1, **extra):
    ensure_company(db, company_id)
    n = next(_seq)
    fields = {
        "ncr_number": f"NCR-T{company_id}-{n:04d}",
        "source": NCRSource.IN_PROCESS,
        "title": "Edge burr on bracket",
        "description": "Burr found on the outside edge at final inspection.",
        "quantity_affected": 5,
        "quantity_rejected": 0,
        "status": NCRStatus.OPEN,
        "disposition": NCRDisposition.PENDING,
        "company_id": company_id,
    }
    fields.update(extra)
    ncr = NonConformanceReport(**fields)
    db.add(ncr)
    db.commit()
    db.refresh(ncr)
    return ncr


def _make_car(db, company_id=1, **extra):
    ensure_company(db, company_id)
    n = next(_seq)
    fields = {
        "car_number": f"CAR-T{company_id}-{n:04d}",
        "title": "Corrective action for edge burr",
        "problem_description": "Repeated edge burrs at final inspection.",
        "status": CARStatus.OPEN,
        "priority": 3,
        "company_id": company_id,
    }
    fields.update(extra)
    car = CorrectiveActionRequest(**fields)
    db.add(car)
    db.commit()
    db.refresh(car)
    return car


def _closeable_car(db, **extra):
    return _make_car(
        db,
        root_cause="Worn deburr fixture.",
        corrective_action="Replace and add fixture to PM.",
        verification_method="Inspect 3 consecutive lots.",
        verification_results="No burrs in 3 lots.",
        **extra,
    )


def _headers(db, role=UserRole.QUALITY, company_id=1):
    user = make_user(db, role=role, company_id=company_id)
    return user, user_headers(user)


# --------------------------------------------------------------------------- readers


def _committed(resource_type, resource_id=None):
    """Committed audit rows for one resource, read on a separate connection."""
    with fresh_session() as verify:
        query = verify.query(AuditLog).filter(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            query = query.filter(AuditLog.resource_id == resource_id)
        return [
            {
                "action": row.action,
                "resource_id": row.resource_id,
                "resource_identifier": row.resource_identifier,
                "user_id": row.user_id,
                "company_id": row.company_id,
                "old_values": row.old_values,
                "new_values": row.new_values,
                "extra_data": row.extra_data,
                "integrity_hash": row.integrity_hash,
            }
            for row in query.order_by(AuditLog.id).all()
        ]


def _snapshot(db):
    """Every NCR / CAR / audit / event row, for "nothing changed" assertions."""
    db.expire_all()
    return {
        model.__tablename__: [
            {column.key: getattr(row, column.key) for column in inspect(model).columns}
            for row in db.query(model).order_by(model.id).all()
        ]
        for model in (NonConformanceReport, CorrectiveActionRequest, AuditLog, OperationalEvent)
    }


def _ui_ncr_payload(record, **overrides):
    """What QualityRecordDetail sends: every field, blanks as '' (not null)."""
    payload = {
        "version": 0,
        "status": record["status"],
        "disposition": record["disposition"] or "pending",
        "quantity_rejected": float(record["quantity_rejected"] or 0),
        "containment_action": record["containment_action"] or "",
        "root_cause": record["root_cause"] or "",
    }
    payload.update(overrides)
    return payload


# ============================================================================ NCR create


class TestCreateNCR:
    def test_create_commits_one_create_row(self, client, db_session):
        # Filing an NCR stays open to any authenticated user of the company.
        user, headers = _headers(db_session, role=UserRole.OPERATOR)
        part = _make_part(db_session)
        resp = client.post(
            f"{BASE}/ncr",
            headers=headers,
            json={
                "part_id": part.id,
                "source": "in_process",
                "title": "Edge burr on bracket",
                "description": "Burr found on the outside edge at final inspection.",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["part"]["part_number"] == part.part_number

        rows = _committed("ncr", body["id"])
        assert [row["action"] for row in rows] == ["CREATE"]
        row = rows[0]
        assert row["user_id"] == user.id and row["company_id"] == 1 and row["integrity_hash"]
        assert row["resource_identifier"] == body["ncr_number"]
        assert row["new_values"]["status"] == "open" and row["new_values"]["part_id"] == part.id

    @pytest.mark.parametrize("case", ["foreign", "deleted", "missing"])
    def test_bad_part_reference_is_404_without_changes(self, client, db_session, case):
        _, headers = _headers(db_session)
        if case == "foreign":
            part_id = _make_part(db_session, company_id=2).id
        elif case == "deleted":
            part_id = _make_part(db_session, is_deleted=True).id
        else:
            part_id = 999999
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr",
            headers=headers,
            json={
                "part_id": part_id,
                "source": "in_process",
                "title": "Foreign part NCR",
                "description": "Should never be stored against another tenant's part.",
            },
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Part not found"
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("case", ["foreign", "deleted"])
    def test_bad_work_order_reference_is_404_without_changes(self, client, db_session, case):
        _, headers = _headers(db_session)
        wo = _make_work_order(db_session, company_id=2) if case == "foreign" else _make_work_order(db_session)
        if case == "deleted":
            wo.is_deleted = True
            db_session.commit()
        before = _snapshot(db_session)
        resp = client.post(
            f"{BASE}/ncr",
            headers=headers,
            json={
                "work_order_id": wo.id,
                "source": "in_process",
                "title": "Foreign work order NCR",
                "description": "Should never be stored against another tenant's work order.",
            },
        )
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before

    def test_legacy_foreign_part_is_not_echoed_on_reads(self, client, db_session):
        # A row stored before references were validated must not leak the other
        # tenant's part number and name through NCRResponse.part.
        foreign_part = _make_part(db_session, company_id=2)
        ncr = _make_ncr(db_session, part_id=foreign_part.id)
        _, headers = _headers(db_session, role=UserRole.VIEWER)

        detail = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["part"] is None

        listing = client.get(f"{BASE}/ncr", headers=headers)
        assert listing.status_code == 200
        row = next(item for item in listing.json() if item["id"] == ncr.id)
        assert row["part"] is None
        assert foreign_part.part_number not in listing.text


# ============================================================================ NCR update


class TestUpdateNCR:
    def test_content_edit_commits_update_row_with_old_and_new(self, client, db_session):
        user, headers = _headers(db_session)
        ncr = _make_ncr(db_session, containment_action="Tagged lot")
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "containment_action": "Quarantined lot"}
        )
        assert resp.status_code == 200, resp.text
        rows = _committed("ncr", ncr.id)
        assert [row["action"] for row in rows] == ["UPDATE"]
        assert rows[0]["user_id"] == user.id and rows[0]["company_id"] == 1
        assert rows[0]["old_values"] == {"containment_action": "Tagged lot"}
        assert rows[0]["new_values"] == {"containment_action": "Quarantined lot"}
        assert rows[0]["extra_data"]["changes"] == {
            "containment_action": {"old": "Tagged lot", "new": "Quarantined lot"}
        }

    def test_disposition_change_is_audited(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "disposition": "rework"})
        assert resp.status_code == 200, resp.text
        rows = _committed("ncr", ncr.id)
        assert len(rows) == 1 and rows[0]["action"] == "UPDATE"
        assert rows[0]["old_values"] == {"disposition": "pending"}
        assert rows[0]["new_values"] == {"disposition": "rework"}
        assert rows[0]["extra_data"]["disposition_change"] is True

    def test_closure_commits_status_change_with_disposition_and_closer(self, client, db_session):
        user, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=headers,
            json={"version": 0, "status": "closed", "disposition": "scrap", "root_cause": ROOT_CAUSE},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["closed_date"] == date.today().isoformat()
        rows = _committed("ncr", ncr.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        old, new = rows[0]["old_values"], rows[0]["new_values"]
        assert (old["status"], new["status"]) == ("open", "closed")
        assert (old["disposition"], new["disposition"]) == ("pending", "scrap")
        assert old["closed_by"] is None and new["closed_by"] == user.id
        assert new["closed_date"] == date.today().isoformat() and new["root_cause"] == ROOT_CAUSE
        assert rows[0]["extra_data"]["closure"] is True and rows[0]["extra_data"]["disposition_change"] is True

    def test_unchanged_resave_writes_nothing(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        record = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers).json()
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json=_ui_ncr_payload(record))
        assert resp.status_code == 200, resp.text
        assert _snapshot(db_session) == before

    def test_closed_ncr_refuses_content_edit(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(
            db_session,
            status=NCRStatus.CLOSED,
            disposition=NCRDisposition.SCRAP,
            root_cause=ROOT_CAUSE,
            closed_date=date(2026, 10, 1),
        )
        record = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers).json()
        before = _snapshot(db_session)
        resp = client.put(
            f"{BASE}/ncr/{ncr.id}",
            headers=headers,
            json=_ui_ncr_payload(record, containment_action="Rewritten after closure"),
        )
        assert resp.status_code == 409, resp.text
        assert "closed" in resp.json()["detail"] and "Reopen" in resp.json()["detail"]
        assert _snapshot(db_session) == before

    def test_closed_ncr_unchanged_resave_is_not_refused(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session, status=NCRStatus.CLOSED, disposition=NCRDisposition.SCRAP, root_cause=ROOT_CAUSE)
        record = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers).json()
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json=_ui_ncr_payload(record))
        assert resp.status_code == 200, resp.text
        assert _snapshot(db_session) == before

    def test_closed_ncr_status_only_reopen_clears_closure_and_is_audited(self, client, db_session):
        closer = make_user(db_session, role=UserRole.QUALITY)
        _, headers = _headers(db_session)
        ncr = _make_ncr(
            db_session,
            status=NCRStatus.CLOSED,
            disposition=NCRDisposition.SCRAP,
            root_cause=ROOT_CAUSE,
            closed_date=date(2026, 10, 1),
            closed_by=closer.id,
        )
        record = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers).json()
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json=_ui_ncr_payload(record, status="open"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "open" and resp.json()["closed_date"] is None
        rows = _committed("ncr", ncr.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        assert rows[0]["old_values"] == {"status": "closed", "closed_date": "2026-10-01", "closed_by": closer.id}
        assert rows[0]["new_values"] == {"status": "open", "closed_date": None, "closed_by": None}
        assert rows[0]["extra_data"]["reopen"] is True

        # Once reopened, the content edit goes through as its own audited save.
        record = client.get(f"{BASE}/ncr/{ncr.id}", headers=headers).json()
        edit = client.put(
            f"{BASE}/ncr/{ncr.id}", headers=headers, json=_ui_ncr_payload(record, containment_action="Re-inspected")
        )
        assert edit.status_code == 200, edit.text
        assert [row["action"] for row in _committed("ncr", ncr.id)] == ["STATUS_CHANGE", "UPDATE"]

    def test_legacy_void_status_ncr_refuses_every_change(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session, status=NCRStatus.VOID)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "status": "open"})
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("case", ["foreign_user", "inactive_user", "missing_user"])
    def test_bad_assignee_is_404_without_changes(self, client, db_session, case):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        if case == "foreign_user":
            assignee = make_user(db_session, company_id=2, role=UserRole.QUALITY).id
        elif case == "inactive_user":
            assignee = make_user(db_session, role=UserRole.QUALITY, is_active=False).id
        else:
            assignee = 999999
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "assigned_to": assignee})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Assigned user not found"
        assert _snapshot(db_session) == before

    def test_own_active_assignee_is_stored_and_audited(self, client, db_session):
        _, headers = _headers(db_session)
        assignee = make_user(db_session, role=UserRole.QUALITY)
        ncr = _make_ncr(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "assigned_to": assignee.id})
        assert resp.status_code == 200, resp.text
        db_session.refresh(ncr)
        assert ncr.assigned_to == assignee.id
        assert _committed("ncr", ncr.id)[0]["new_values"] == {"assigned_to": assignee.id}

    @pytest.mark.parametrize("case,code", [("foreign", 404), ("missing", 404), ("void", 409)])
    def test_bad_car_link_is_refused_without_changes(self, client, db_session, case, code):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        if case == "foreign":
            car_id = _make_car(db_session, company_id=2).id
        elif case == "void":
            car_id = _make_car(db_session, status=CARStatus.VOID).id
        else:
            car_id = 999999
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "car_id": car_id})
        assert resp.status_code == code, resp.text
        assert _snapshot(db_session) == before

    def test_null_status_is_refused(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{ncr.id}", headers=headers, json={"version": 0, "status": None})
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    def test_foreign_tenant_ncr_is_404(self, client, db_session):
        _, headers = _headers(db_session)
        foreign = _make_ncr(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/ncr/{foreign.id}", headers=headers, json={"version": 0, "root_cause": ROOT_CAUSE})
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before


# ===================================================================== CAR from an NCR


class TestCreateCARFromNCR:
    def test_creates_car_and_links_with_two_committed_rows(self, client, db_session):
        user, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        resp = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers)
        assert resp.status_code == 200, resp.text
        car = resp.json()
        db_session.refresh(ncr)
        assert ncr.car_id == car["id"] and ncr.car_required is True

        car_rows = _committed("car", car["id"])
        assert [row["action"] for row in car_rows] == ["CREATE"]
        assert car_rows[0]["extra_data"]["ncr_id"] == ncr.id and car_rows[0]["user_id"] == user.id
        ncr_rows = _committed("ncr", ncr.id)
        assert [row["action"] for row in ncr_rows] == ["UPDATE"]
        assert ncr_rows[0]["new_values"] == {"car_required": True, "car_id": car["id"]}

    def test_second_call_is_409_naming_the_existing_car(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        first = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers)
        assert first.status_code == 200, first.text
        before = _snapshot(db_session)
        second = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers)
        assert second.status_code == 409, second.text
        assert first.json()["car_number"] in second.json()["detail"]
        assert _snapshot(db_session) == before
        assert db_session.query(CorrectiveActionRequest).count() == 1

    def test_closed_ncr_can_still_raise_its_car(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session, status=NCRStatus.CLOSED, disposition=NCRDisposition.SCRAP, root_cause=ROOT_CAUSE)
        resp = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers)
        assert resp.status_code == 200, resp.text
        db_session.refresh(ncr)
        assert ncr.status == NCRStatus.CLOSED and ncr.disposition == NCRDisposition.SCRAP

    def test_voided_linked_car_allows_a_fresh_one(self, client, db_session):
        _, headers = _headers(db_session)
        ncr = _make_ncr(db_session)
        first = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers).json()
        voided = client.post(f"{BASE}/car/{first['id']}/void", headers=headers, json={"reason": "Raised in error"})
        assert voided.status_code == 200, voided.text
        second = client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers)
        assert second.status_code == 200, second.text
        db_session.refresh(ncr)
        assert ncr.car_id == second.json()["id"] != first["id"]
        assert _committed("ncr", ncr.id)[-1]["old_values"]["car_id"] == first["id"]

    def test_foreign_tenant_ncr_is_404(self, client, db_session):
        _, headers = _headers(db_session)
        foreign = _make_ncr(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/ncr/{foreign.id}/create-car", headers=headers)
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before

    def test_operator_cannot_create_car_from_ncr(self, client, db_session):
        _, headers = _headers(db_session, role=UserRole.OPERATOR)
        ncr = _make_ncr(db_session)
        assert client.post(f"{BASE}/ncr/{ncr.id}/create-car", headers=headers).status_code == 403


# ================================================================================ CAR


class TestCAR:
    def test_create_commits_one_create_row(self, client, db_session):
        user, headers = _headers(db_session)
        resp = client.post(
            f"{BASE}/car",
            headers=headers,
            json={"title": "Recurring edge burrs", "problem_description": "Three lots with edge burrs."},
        )
        assert resp.status_code == 200, resp.text
        rows = _committed("car", resp.json()["id"])
        assert [row["action"] for row in rows] == ["CREATE"]
        assert rows[0]["user_id"] == user.id and rows[0]["new_values"]["status"] == "open"

    def test_verification_records_verifier_on_status_change_row(self, client, db_session):
        user, headers = _headers(db_session)
        car = _make_car(db_session, status=CARStatus.CORRECTIVE_ACTION)
        resp = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "status": "verification"})
        assert resp.status_code == 200, resp.text
        rows = _committed("car", car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        assert rows[0]["old_values"] == {"status": "corrective_action", "verified_by": None}
        assert rows[0]["new_values"] == {"status": "verification", "verified_by": user.id}
        assert rows[0]["extra_data"]["verification"] is True

    def test_closure_requires_evidence(self, client, db_session):
        _, headers = _headers(db_session)
        car = _make_car(db_session)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "status": "closed"})
        assert resp.status_code == 422, resp.text
        assert "Closure requires" in resp.json()["detail"]
        assert _snapshot(db_session) == before

    def test_closure_commits_status_change_row(self, client, db_session):
        user, headers = _headers(db_session)
        car = _closeable_car(db_session, status=CARStatus.VERIFICATION)
        resp = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "status": "closed"})
        assert resp.status_code == 200, resp.text
        rows = _committed("car", car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        assert rows[0]["new_values"] == {
            "status": "closed",
            "closed_date": date.today().isoformat(),
            "closed_by": user.id,
        }
        assert rows[0]["extra_data"]["closure"] is True

    def test_closed_car_refuses_content_edit_but_allows_reopen(self, client, db_session):
        _, headers = _headers(db_session)
        car = _closeable_car(db_session, status=CARStatus.CLOSED, closed_date=date(2026, 10, 2))
        before = _snapshot(db_session)
        edit = client.put(
            f"{BASE}/car/{car.id}",
            headers=headers,
            json={"version": 0, "status": "closed", "corrective_action": "Rewritten after closure"},
        )
        assert edit.status_code == 409, edit.text
        assert _snapshot(db_session) == before

        reopen = client.put(
            f"{BASE}/car/{car.id}",
            headers=headers,
            json={"version": 0, "status": "corrective_action", "corrective_action": car.corrective_action},
        )
        assert reopen.status_code == 200, reopen.text
        assert reopen.json()["closed_date"] is None
        rows = _committed("car", car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        assert rows[0]["extra_data"]["reopen"] is True

    @pytest.mark.parametrize("case", ["foreign_user", "inactive_user"])
    def test_bad_assignee_is_404_without_changes(self, client, db_session, case):
        _, headers = _headers(db_session)
        car = _make_car(db_session)
        if case == "foreign_user":
            assignee = make_user(db_session, company_id=2, role=UserRole.QUALITY).id
        else:
            assignee = make_user(db_session, role=UserRole.QUALITY, is_active=False).id
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "assigned_to": assignee})
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before

    def test_foreign_tenant_car_is_404(self, client, db_session):
        _, headers = _headers(db_session)
        foreign = _make_car(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.put(f"{BASE}/car/{foreign.id}", headers=headers, json={"version": 0, "priority": 1})
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before


# =========================================================================== CAR void


class TestVoidCAR:
    def test_void_records_reason_and_leaves_open_count(self, client, db_session):
        user, headers = _headers(db_session)
        car = _make_car(db_session, status=CARStatus.ROOT_CAUSE_ANALYSIS)
        assert client.get(f"{BASE}/summary", headers=headers).json()["open_cars"] == 1

        resp = client.post(f"{BASE}/car/{car.id}/void", headers=headers, json={"reason": "  Duplicate of CAR-1  "})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "void"
        rows = _committed("car", car.id)
        assert [row["action"] for row in rows] == ["STATUS_CHANGE"]
        assert rows[0]["old_values"] == {"status": "root_cause_analysis"}
        assert rows[0]["new_values"] == {"status": "void"}
        assert rows[0]["extra_data"]["reason"] == "Duplicate of CAR-1" and rows[0]["user_id"] == user.id
        assert client.get(f"{BASE}/summary", headers=headers).json()["open_cars"] == 0
        # Still readable: VOID is the CAR's tombstone, not a deletion.
        assert client.get(f"{BASE}/car/{car.id}", headers=headers).json()["status"] == "void"

    @pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
    def test_reason_is_required(self, client, db_session, body):
        _, headers = _headers(db_session)
        car = _make_car(db_session)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/void", headers=headers, json=body)
        assert resp.status_code == 422, resp.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.VIEWER])
    def test_roles_outside_admin_manager_quality_are_forbidden(self, client, db_session, role):
        _, headers = _headers(db_session, role=role)
        car = _make_car(db_session)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/void", headers=headers, json={"reason": "No"})
        assert resp.status_code == 403, resp.text
        assert _snapshot(db_session) == before

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY])
    def test_admin_manager_quality_may_void(self, client, db_session, role):
        _, headers = _headers(db_session, role=role)
        car = _make_car(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/void", headers=headers, json={"reason": "Raised in error"})
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("status_", [CARStatus.CLOSED, CARStatus.VOID])
    def test_closed_or_already_void_car_is_refused(self, client, db_session, status_):
        _, headers = _headers(db_session)
        car = _make_car(db_session, status=status_)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{car.id}/void", headers=headers, json={"reason": "Try"})
        assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    def test_voided_car_refuses_every_edit(self, client, db_session):
        _, headers = _headers(db_session)
        car = _make_car(db_session, status=CARStatus.VOID)
        before = _snapshot(db_session)
        for body in ({"version": 0, "priority": 1}, {"version": 0, "status": "open"}):
            resp = client.put(f"{BASE}/car/{car.id}", headers=headers, json=body)
            assert resp.status_code == 409, resp.text
        assert _snapshot(db_session) == before

    def test_foreign_tenant_car_is_404(self, client, db_session):
        _, headers = _headers(db_session)
        foreign = _make_car(db_session, company_id=2)
        before = _snapshot(db_session)
        resp = client.post(f"{BASE}/car/{foreign.id}/void", headers=headers, json={"reason": "Not mine"})
        assert resp.status_code == 404, resp.text
        assert _snapshot(db_session) == before


# ======================================================================= fail-closed


VERBS = [
    "create_ncr",
    "update_ncr",
    "close_ncr",
    "void_ncr",
    "restore_ncr",
    "create_car_from_ncr",
    "create_car",
    "update_car",
    "void_car",
]


def _perform(client, db_session, headers, verb):
    if verb == "create_ncr":
        return lambda: client.post(
            f"{BASE}/ncr",
            headers=headers,
            json={"source": "in_process", "title": "Audit loss NCR", "description": "Audit loss rolls this back."},
        )
    if verb in ("update_ncr", "close_ncr", "void_ncr", "create_car_from_ncr"):
        ncr = _make_ncr(db_session)
        url = f"{BASE}/ncr/{ncr.id}"
        if verb == "update_ncr":
            return lambda: client.put(url, headers=headers, json={"version": 0, "disposition": "rework"})
        if verb == "close_ncr":
            body = {"version": 0, "status": "closed", "disposition": "scrap", "root_cause": ROOT_CAUSE}
            return lambda: client.put(url, headers=headers, json=body)
        if verb == "void_ncr":
            return lambda: client.request("DELETE", url, headers=headers, json={"reason": "Raised in error"})
        return lambda: client.post(f"{url}/create-car", headers=headers)
    if verb == "restore_ncr":
        ncr = _make_ncr(db_session, status=NCRStatus.VOID, is_deleted=True)
        return lambda: client.post(f"{BASE}/ncr/{ncr.id}/restore", headers=headers)
    if verb == "create_car":
        return lambda: client.post(
            f"{BASE}/car",
            headers=headers,
            json={"title": "Audit loss corrective action", "problem_description": "Audit loss rolls this back."},
        )
    car = _closeable_car(db_session)
    if verb == "update_car":
        return lambda: client.put(f"{BASE}/car/{car.id}", headers=headers, json={"version": 0, "status": "closed"})
    return lambda: client.post(f"{BASE}/car/{car.id}/void", headers=headers, json={"reason": "Raised in error"})


@pytest.mark.parametrize("verb", VERBS)
def test_lost_audit_row_rolls_the_write_back(client, db_session, monkeypatch, verb):
    _, headers = _headers(db_session)
    call = _perform(client, db_session, headers, verb)
    before = _snapshot(db_session)
    monkeypatch.setattr(AuditService, "log", lambda *args, **kwargs: None)
    resp = call()
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "Unable to save audit record"}
    assert _snapshot(db_session) == before
