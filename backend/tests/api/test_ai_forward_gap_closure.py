from datetime import date, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.models.ai_learning import AIRecommendation
from app.models.company import Company
from app.models.notification import NotificationLog
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker


def _make_released_work_order(db_session, *, number: str, work_center: WorkCenter, priority: int = 3, due=None):
    part = Part(
        part_number=f"PART-{number}",
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=work_center.company_id,
    )
    db_session.add(part)
    db_session.flush()
    work_order = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=WorkOrderStatus.RELEASED,
        priority=priority,
        due_date=due or date.today() + timedelta(days=5),
        company_id=work_center.company_id,
    )
    db_session.add(work_order)
    db_session.flush()
    operation = WorkOrderOperation(
        work_order_id=work_order.id,
        work_center_id=work_center.id,
        sequence=10,
        operation_number="Op 10",
        name="Laser Cut",
        operation_group="LASER",
        status=OperationStatus.PENDING,
        setup_time_hours=1,
        run_time_hours=1,
        company_id=work_center.company_id,
    )
    db_session.add(operation)
    db_session.commit()
    return work_order, operation


@pytest.mark.api
@pytest.mark.requires_db
class TestAIForwardGapClosure:
    def test_material_blocker_notifies_learns_and_supports_nl_search(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        work_center = WorkCenter(
            code="LASER-01",
            name="Laser Cell",
            work_center_type="laser",
            is_active=True,
            company_id=1,
        )
        db_session.add(work_center)
        db_session.commit()
        work_order, operation = _make_released_work_order(
            db_session,
            number="WO-LATE-MAT",
            work_center=work_center,
            priority=1,
            due=date.today() - timedelta(days=1),
        )

        response = client.post(
            f"/api/v1/work-order-blockers/work-orders/{work_order.id}",
            headers=auth_headers,
            json={
                "operation_id": operation.id,
                "category": "material_missing",
                "severity": "high",
                "note": "No sheet stock at the laser.",
            },
        )

        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["category"] == "material_missing"
        assert payload["status"] == "open"

        db_session.refresh(operation)
        assert operation.status == OperationStatus.ON_HOLD
        assert db_session.query(WorkOrderBlocker).filter_by(work_order_id=work_order.id, company_id=1).count() == 1
        assert db_session.query(NotificationLog).filter_by(event_type="WO_BLOCKED", company_id=1).count() >= 1
        assert (
            db_session.query(AIRecommendation)
            .filter_by(target_entity_type="work_order_blocker", source_module="shop_floor", company_id=1)
            .count()
            == 1
        )
        assert (
            db_session.query(OperationalEvent)
            .filter_by(event_type="work_order_blocker_created", company_id=1)
            .count()
            == 1
        )

        search = client.post(
            "/api/v1/search/nl",
            headers=auth_headers,
            json={"query": "show late laser jobs waiting on material", "limit": 10},
        )
        assert search.status_code == status.HTTP_200_OK
        result_titles = [item["title"] for item in search.json()["results"]]
        assert "WO-LATE-MAT" in result_titles

    def test_operational_events_are_tenant_scoped(self, client: TestClient, auth_headers: dict, db_session):
        db_session.add(Company(id=2, name="Other Co", slug="other-co", is_active=True))
        db_session.add_all(
            [
                OperationalEvent(
                    company_id=1,
                    event_type="inventory_received",
                    source_module="inventory",
                    severity="info",
                    event_payload={"part": "A"},
                ),
                OperationalEvent(
                    company_id=2,
                    event_type="inventory_received",
                    source_module="inventory",
                    severity="info",
                    event_payload={"part": "B"},
                ),
            ]
        )
        db_session.commit()

        response = client.get("/api/v1/operational-events/", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        events = response.json()
        assert len(events) == 1
        assert events[0]["company_id"] == 1

    def test_scheduler_uses_priority_one_first_and_skips_blocked_operations(
        self, client: TestClient, auth_headers: dict, db_session
    ):
        work_center = WorkCenter(
            code="WC-SCHED-AI",
            name="AI Schedule Cell",
            work_center_type="laser",
            capacity_hours_per_day=8,
            is_active=True,
            company_id=1,
        )
        db_session.add(work_center)
        db_session.commit()

        high_wo, high_op = _make_released_work_order(
            db_session, number="WO-PRI-1", work_center=work_center, priority=1
        )
        low_wo, low_op = _make_released_work_order(
            db_session, number="WO-PRI-9", work_center=work_center, priority=9
        )
        blocked_wo, blocked_op = _make_released_work_order(
            db_session, number="WO-BLOCKED-HIGH", work_center=work_center, priority=1
        )
        client.post(
            f"/api/v1/work-order-blockers/work-orders/{blocked_wo.id}",
            headers=auth_headers,
            json={
                "operation_id": blocked_op.id,
                "category": "material_missing",
                "severity": "high",
                "note": "Missing material.",
            },
        )

        response = client.post(
            "/api/v1/scheduling/run",
            headers=auth_headers,
            json={"work_center_ids": [work_center.id], "horizon_days": 10, "optimize_setup": False},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        scheduled_numbers = [item["work_order"] for item in data["scheduled_operations"]]
        assert scheduled_numbers[0] == high_wo.work_order_number
        assert low_wo.work_order_number in scheduled_numbers
        assert blocked_wo.work_order_number not in scheduled_numbers
