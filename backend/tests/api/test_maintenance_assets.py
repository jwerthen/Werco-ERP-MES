"""Equipment assets as maintenance targets (P2.3, migration 124).

Non-production machines (forklifts, compressors, a manual mill) are ``MaintenanceAsset``
rows, never fake work centers. A schedule, maintenance work order or log targets EXACTLY ONE
of a work center or an asset -- the request schemas refuse both / neither (422) and the
database CHECK refuses it underneath. Also pinned here: the enum fields 422 an unknown value
(the old form's ``usage_based`` frequency used to 500), ``custom`` needs ``frequency_days``,
the asset CRUD's roles, case-insensitive tag uniqueness, version CAS and audit rows, tenant
isolation of asset ids, and that the MTBF/MTTR dashboard stays work-center-only.
"""

from datetime import datetime

import pytest
from fastapi import status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.maintenance import (
    MaintenanceAsset,
    MaintenanceLog,
    MaintenanceSchedule,
    MaintenanceStatus,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.user import UserRole
from app.models.work_center import WorkCenter
from tests.api.test_receiving_compliance import _ensure_company, _next, headers_for, make_user

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

ASSETS = "/api/v1/maintenance/assets"
SCHEDULES = "/api/v1/maintenance/schedules"
WORK_ORDERS = "/api/v1/maintenance/work-orders"
LOG = "/api/v1/maintenance/log"


def make_wc(db: Session, *, company_id: int = 1, code: str = None) -> WorkCenter:
    _ensure_company(db, company_id)
    wc = WorkCenter(
        company_id=company_id,
        code=code or f"WC-{_next():04d}",
        name="Synthetic Mill",
        work_center_type="cnc_machining",
        is_active=True,
        capacity_hours_per_day=8,
    )
    db.add(wc)
    db.commit()
    db.refresh(wc)
    return wc


def make_asset(
    db: Session, *, company_id: int = 1, tag: str = None, name: str = "Synthetic Forklift"
) -> MaintenanceAsset:
    _ensure_company(db, company_id)
    asset = MaintenanceAsset(
        company_id=company_id, asset_tag=tag or f"AS-{_next():04d}", name=name, asset_type="forklift"
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@pytest.fixture
def planner(db_session):
    return make_user(db_session, role=UserRole.SUPERVISOR, company_id=1)


@pytest.fixture
def planner_headers(planner):
    return headers_for(planner)


def audit_rows(db: Session, resource_type: str):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.resource_type == resource_type).order_by(AuditLog.id).all()


# ---------------------------------------------------------------------------
# Asset CRUD
# ---------------------------------------------------------------------------


class TestAssetCrud:
    def test_create_list_get_and_audit(self, client, db_session, planner_headers):
        resp = client.post(
            ASSETS,
            headers=planner_headers,
            json={"asset_tag": " FL-01 ", "name": "Forklift 1", "asset_type": "forklift", "location": "Dock"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["asset_tag"] == "FL-01" and body["version"] == 1 and body["is_active"] is True
        assert body["active_schedule_count"] == 0 and body["open_work_order_count"] == 0
        assert body["created_at"].endswith("Z")

        listed = client.get(ASSETS, headers=planner_headers).json()
        assert [row["asset_tag"] for row in listed] == ["FL-01"]
        assert client.get(f"{ASSETS}/{body['id']}", headers=planner_headers).json()["name"] == "Forklift 1"
        rows = audit_rows(db_session, "maintenance_asset")
        assert [row.action for row in rows] == ["CREATE"]
        assert rows[0].new_values["asset_tag"] == "FL-01"

    def test_tag_is_unique_per_company_ignoring_case(self, client, db_session, planner_headers):
        make_asset(db_session, tag="AC-01")
        resp = client.post(ASSETS, headers=planner_headers, json={"asset_tag": "ac-01", "name": "Compressor"})
        assert resp.status_code == status.HTTP_409_CONFLICT
        # Another company may use the same tag.
        make_asset(db_session, company_id=2, tag="AC-02")
        assert client.post(ASSETS, headers=planner_headers, json={"asset_tag": "AC-02", "name": "x"}).status_code == 200

    def test_database_key_backstops_an_exact_duplicate(self, db_session):
        make_asset(db_session, tag="DUP-1")
        db_session.add(MaintenanceAsset(company_id=1, asset_tag="DUP-1", name="again"))
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_update_with_version_cas_and_retire(self, client, db_session, planner_headers):
        asset = make_asset(db_session, tag="EZ-01", name="EZ-TRAK 1")
        resp = client.put(f"{ASSETS}/{asset.id}", headers=planner_headers, json={"version": 1, "location": "Tool room"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == 2 and resp.json()["location"] == "Tool room"

        stale = client.put(f"{ASSETS}/{asset.id}", headers=planner_headers, json={"version": 1, "name": "x"})
        assert stale.status_code == status.HTTP_409_CONFLICT
        db_session.expire_all()
        assert db_session.get(MaintenanceAsset, asset.id).name == "EZ-TRAK 1"

        unchanged = client.put(
            f"{ASSETS}/{asset.id}", headers=planner_headers, json={"version": 2, "location": "Tool room"}
        )
        assert unchanged.json()["version"] == 2  # a no-op save writes nothing

        retired = client.delete(f"{ASSETS}/{asset.id}", headers=planner_headers)
        assert retired.status_code == 200 and retired.json()["is_active"] is False
        db_session.expire_all()
        assert db_session.get(MaintenanceAsset, asset.id) is not None  # never a physical delete
        assert client.get(ASSETS, headers=planner_headers).json() == []
        assert len(client.get(f"{ASSETS}?include_inactive=true", headers=planner_headers).json()) == 1

        restored = client.put(f"{ASSETS}/{asset.id}", headers=planner_headers, json={"version": 3, "is_active": True})
        assert restored.json()["is_active"] is True
        actions = [(row.action, row.new_values) for row in audit_rows(db_session, "maintenance_asset")]
        assert [action for action, _ in actions] == ["UPDATE", "UPDATE", "UPDATE"]

    def test_explicit_null_and_unknown_fields_are_422(self, client, db_session, planner_headers):
        asset = make_asset(db_session)
        assert (
            client.put(f"{ASSETS}/{asset.id}", headers=planner_headers, json={"version": 1, "name": None}).status_code
            == 422
        )
        assert (
            client.post(ASSETS, headers=planner_headers, json={"asset_tag": "X", "name": "y", "bogus": 1}).status_code
            == 422
        )
        assert client.post(ASSETS, headers=planner_headers, json={"asset_tag": "  ", "name": "y"}).status_code == 422

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY])
    def test_writes_need_a_planning_role_reads_do_not(self, client, db_session, role):
        user = make_user(db_session, role=role, company_id=1)
        asset = make_asset(db_session)
        headers = headers_for(user)
        assert client.post(ASSETS, headers=headers, json={"asset_tag": "NO-1", "name": "n"}).status_code == 403
        assert client.put(f"{ASSETS}/{asset.id}", headers=headers, json={"version": 1, "name": "n"}).status_code == 403
        assert client.delete(f"{ASSETS}/{asset.id}", headers=headers).status_code == 403
        assert client.get(ASSETS, headers=headers).status_code == 200

    def test_another_companys_asset_is_a_flat_404_everywhere(self, client, db_session, planner_headers):
        foreign = make_asset(db_session, company_id=2, tag="ACME-SECRET-FL", name="ACME-SECRET forklift")
        for method, url, body in (
            ("get", f"{ASSETS}/{foreign.id}", None),
            ("put", f"{ASSETS}/{foreign.id}", {"version": 1, "name": "pwned"}),
            ("delete", f"{ASSETS}/{foreign.id}", None),
            ("get", f"{ASSETS}/{foreign.id}/history", None),
            ("post", SCHEDULES, {"asset_id": foreign.id, "name": "PM"}),
            ("post", WORK_ORDERS, {"asset_id": foreign.id, "title": "Fix"}),
            ("post", LOG, {"asset_id": foreign.id, "event_type": "repair", "description": "x"}),
        ):
            resp = getattr(client, method)(url, headers=planner_headers, **({"json": body} if body else {}))
            assert resp.status_code == 404, (method, url, resp.text)
            assert "ACME-SECRET" not in resp.text
        assert "ACME-SECRET" not in client.get(ASSETS, headers=planner_headers).text
        db_session.expire_all()
        assert db_session.get(MaintenanceAsset, foreign.id).name == "ACME-SECRET forklift"

    def test_search_and_counts(self, client, db_session, planner_headers):
        asset = make_asset(db_session, tag="CMP-1", name="Air compressor")
        make_asset(db_session, tag="FL-9", name="Forklift")
        db_session.add(MaintenanceSchedule(company_id=1, asset_id=asset.id, name="CMP-1 Monthly PM"))
        db_session.add(
            MaintenanceWorkOrder(company_id=1, asset_id=asset.id, wo_number=f"MWO-T-{_next()}", title="Drain tank")
        )
        db_session.commit()
        rows = client.get(f"{ASSETS}?search=compress", headers=planner_headers).json()
        assert [row["asset_tag"] for row in rows] == ["CMP-1"]
        assert rows[0]["active_schedule_count"] == 1 and rows[0]["open_work_order_count"] == 1


# ---------------------------------------------------------------------------
# Exactly one target, and the asset-targeted records
# ---------------------------------------------------------------------------


class TestTargets:
    def test_schedule_work_order_and_log_on_an_asset(self, client, db_session, planner_headers):
        asset = make_asset(db_session, tag="SHR-1", name="Shear")
        sched = client.post(
            SCHEDULES,
            headers=planner_headers,
            json={"asset_id": asset.id, "name": "Shear monthly", "frequency": "monthly", "checklist": '["Oil"]'},
        )
        assert sched.status_code == 200, sched.text
        body = sched.json()
        assert body["target_type"] == "asset" and body["asset_tag"] == "SHR-1" and body["work_center_id"] is None
        assert body["checklist_items"] == ["Oil"]

        # A work order created from the schedule inherits its target.
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"schedule_id": body["id"], "title": "Oil shear"})
        assert wo.status_code == 200, wo.text
        assert wo.json()["asset_id"] == asset.id and wo.json()["target_name"] == "Shear"

        log = client.post(
            LOG, headers=planner_headers, json={"asset_id": asset.id, "event_type": "inspection", "description": "ok"}
        )
        assert log.status_code == 200 and log.json()["target_type"] == "asset"

        listed = client.get(f"{SCHEDULES}?asset_id={asset.id}", headers=planner_headers).json()
        assert [row["id"] for row in listed] == [body["id"]]
        assert [
            row["asset_id"] for row in client.get(f"{WORK_ORDERS}?asset_id={asset.id}", headers=planner_headers).json()
        ] == [asset.id]
        history = client.get(f"{ASSETS}/{asset.id}/history", headers=planner_headers).json()
        assert history["asset_tag"] == "SHR-1" and [row["event_type"] for row in history["logs"]] == ["inspection"]

    def test_start_and_complete_an_asset_work_order_logs_against_the_asset(self, client, db_session, planner_headers):
        asset = make_asset(db_session)
        wo = client.post(
            WORK_ORDERS,
            headers=planner_headers,
            json={"asset_id": asset.id, "title": "Service", "requires_shutdown": True},
        ).json()
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/start", headers=planner_headers).status_code == 200
        assert client.post(f"{WORK_ORDERS}/{wo['id']}/complete", headers=planner_headers, json={}).status_code == 200
        db_session.expire_all()
        logs = db_session.query(MaintenanceLog).filter(MaintenanceLog.maintenance_wo_id == wo["id"]).all()
        assert {(log.asset_id, log.work_center_id) for log in logs} == {(asset.id, None)}

    @pytest.mark.parametrize(
        "url,body",
        [
            (SCHEDULES, {"name": "PM"}),
            (SCHEDULES, {"name": "PM", "work_center_id": 1, "asset_id": 1}),
            (WORK_ORDERS, {"title": "Fix"}),
            (WORK_ORDERS, {"title": "Fix", "work_center_id": 1, "asset_id": 1}),
            (LOG, {"event_type": "repair", "description": "x"}),
            (LOG, {"event_type": "repair", "description": "x", "work_center_id": 1, "asset_id": 1}),
        ],
    )
    def test_both_or_neither_target_is_422(self, client, planner_headers, url, body):
        resp = client.post(url, headers=planner_headers, json=body)
        assert resp.status_code == 422, resp.text

    def test_the_database_refuses_both_and_neither(self, db_session):
        wc = make_wc(db_session)
        asset = make_asset(db_session)
        for kwargs in ({}, {"work_center_id": wc.id, "asset_id": asset.id}):
            db_session.add(MaintenanceSchedule(company_id=1, name="bad", **kwargs))
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()

    def test_dashboard_mtbf_stays_work_center_only(self, client, db_session, planner_headers):
        wc = make_wc(db_session, code="MTBF-1")
        asset = make_asset(db_session)
        now = datetime.utcnow()
        for kwargs in ({"work_center_id": wc.id}, {"asset_id": asset.id}):
            db_session.add(
                MaintenanceWorkOrder(
                    company_id=1,
                    wo_number=f"MWO-T-{_next()}",
                    title="done",
                    status=MaintenanceStatus.COMPLETED,
                    maintenance_type=MaintenanceType.CORRECTIVE,
                    completed_at=now,
                    actual_duration_hours=2,
                    **kwargs,
                )
            )
        db_session.commit()
        resp = client.get("/api/v1/maintenance/dashboard", headers=planner_headers)
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["work_center_metrics"]
        assert [(row["work_center_code"], row["wo_count"]) for row in metrics] == [("MTBF-1", 1)]


# ---------------------------------------------------------------------------
# Server-side enum / frequency validation (the form could not create a schedule)
# ---------------------------------------------------------------------------


class TestScheduleValidation:
    @pytest.mark.parametrize(
        "patch",
        [
            {"frequency": "usage_based"},
            {"frequency": "custom"},
            {"frequency": "custom", "frequency_days": 0},
            {"maintenance_type": "routine"},
            {"priority": "urgent"},
            {"name": "   "},
        ],
    )
    def test_create_refuses_422(self, client, db_session, planner_headers, patch):
        wc = make_wc(db_session)
        resp = client.post(SCHEDULES, headers=planner_headers, json={"work_center_id": wc.id, "name": "PM", **patch})
        assert resp.status_code == 422, resp.text
        assert db_session.query(MaintenanceSchedule).count() == 0

    def test_custom_with_days_and_every_real_frequency_create(self, client, db_session, planner_headers):
        wc = make_wc(db_session)
        for frequency in ("daily", "weekly", "biweekly", "monthly", "quarterly", "semi_annual", "annual"):
            resp = client.post(
                SCHEDULES,
                headers=planner_headers,
                json={"work_center_id": wc.id, "name": frequency, "frequency": frequency},
            )
            assert resp.status_code == 200, resp.text
        resp = client.post(
            SCHEDULES,
            headers=planner_headers,
            json={"work_center_id": wc.id, "name": "c", "frequency": "custom", "frequency_days": 45},
        )
        assert resp.status_code == 200 and resp.json()["frequency_days"] == 45

    def test_update_refuses_unknown_enum_and_custom_without_days(self, client, db_session, planner_headers):
        wc = make_wc(db_session)
        schedule = client.post(SCHEDULES, headers=planner_headers, json={"work_center_id": wc.id, "name": "PM"}).json()
        url = f"{SCHEDULES}/{schedule['id']}"
        assert client.put(url, headers=planner_headers, json={"frequency": "usage_based"}).status_code == 422
        assert client.put(url, headers=planner_headers, json={"frequency": "custom"}).status_code == 422
        assert client.put(url, headers=planner_headers, json={"name": None}).status_code == 422
        ok = client.put(url, headers=planner_headers, json={"frequency": "custom", "frequency_days": 10})
        assert ok.status_code == 200 and ok.json()["frequency"] == "custom"

    def test_work_order_enums_are_422(self, client, db_session, planner_headers):
        wc = make_wc(db_session)
        assert (
            client.post(
                WORK_ORDERS, headers=planner_headers, json={"work_center_id": wc.id, "title": "x", "priority": "asap"}
            ).status_code
            == 422
        )
        wo = client.post(WORK_ORDERS, headers=planner_headers, json={"work_center_id": wc.id, "title": "x"}).json()
        assert (
            client.put(f"{WORK_ORDERS}/{wo['id']}", headers=planner_headers, json={"status": "done"}).status_code == 422
        )
