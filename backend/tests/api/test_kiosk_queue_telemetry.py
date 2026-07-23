"""Kiosk queue / my-active-job telemetry payloads (Kiosk Foundry redesign, B1-B8).

Locks the additive payload blocks the redesigned kiosk renders:

- ``work_center`` on the work-center-queue envelope (B3): the machine-identity
  top-bar block; **null** (not 404) for an unknown/cross-tenant id -- the read
  has always answered an unknown work center with an empty queue and must not
  leak that the id exists elsewhere.
- ``part_revision`` on queue rows and the my-active-job job dict (B1).
- ``last_report`` (B4): the operation's most recent production-evidence report
  -- the DELTAS of that single report, not running totals -- stamped by
  ``POST /operations/{id}/production`` and by a quantity-carrying clock-out
  (and NOT by a 0/0 clock-out). Null until the first report (correct-forward).
- ``server_time`` on my-active-job (B2), including the EMPTY payload, so the
  kiosk clock keeps its skew correction between jobs.
- session ``quantity_produced`` / ``quantity_scrapped`` on the job dict (B7):
  THIS entry's own counts, distinct from the operation totals.
- ``downtime_minutes`` (B8): Σ blocker spans for the operation -- resolved
  blockers contribute their closed span, open ones accrue to now (monotonic).
- ``next_operation`` (B5) on the job dict AND the ``/complete`` response:
  next routing step by sequence, with its work center; null on the last op.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.work_order import OperationStatus, WorkOrderOperation
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from tests.api.kiosk_test_helpers import (
    COMPANY_A,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

MY_ACTIVE_JOB = "/api/v1/shop-floor/my-active-job"


def clock_in(client: TestClient, headers: dict, wo, op) -> int:
    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": op.work_center_id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["id"]


def report_production(client: TestClient, headers: dict, operation_id: int, good: float, scrap: float = 0.0) -> dict:
    payload: dict = {"quantity_complete_delta": good, "quantity_scrapped_delta": scrap}
    if scrap > 0:
        payload["scrap_reason"] = "Test scrap"
    resp = client.post(f"/api/v1/shop-floor/operations/{operation_id}/production", headers=headers, json=payload)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def active_job(client: TestClient, headers: dict) -> dict:
    resp = client.get(MY_ACTIVE_JOB, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


def queue_row(client: TestClient, headers: dict, work_center_id: int, operation_id: int) -> dict:
    resp = client.get(queue_url(work_center_id), headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    rows = [r for r in resp.json()["queue"] if r["operation_id"] == operation_id]
    assert rows, f"operation {operation_id} missing from queue: {resp.text}"
    return rows[0]


def add_second_operation(db: Session, wo, work_center, *, sequence: int = 20, name: str = "Deburr"):
    op2 = WorkOrderOperation(
        work_order_id=wo.id,
        work_center_id=work_center.id,
        sequence=sequence,
        operation_number=f"OP{sequence}",
        name=name,
        status=OperationStatus.PENDING,
        company_id=COMPANY_A,
    )
    db.add(op2)
    db.commit()
    db.refresh(op2)
    return op2


class TestQueueEnvelope:
    def test_work_center_block_and_server_time(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wc.description = "5x10 fiber laser"
        wc.current_status = "running"
        db_session.commit()
        make_wo_with_operation(db_session, work_center=wc)

        resp = client.get(queue_url(wc.id), headers=user_headers(operator))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["work_center"] == {
            "id": wc.id,
            "code": wc.code,
            "name": wc.name,
            "description": "5x10 fiber laser",
            "current_status": "running",
        }
        # server_time parses as an aware-UTC instant (trailing Z).
        assert body["server_time"].endswith("Z")
        datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))

    def test_unknown_work_center_id_yields_null_block_not_404(self, client: TestClient, db_session: Session):
        """Unknown AND cross-tenant ids answer identically: empty queue, null
        work_center -- never a 404 that would confirm the id exists elsewhere."""
        operator = make_user(db_session)
        from tests.api.kiosk_test_helpers import COMPANY_B

        wc_b = make_work_center(db_session, company_id=COMPANY_B)

        for wc_id in (999_999, wc_b.id):
            resp = client.get(queue_url(wc_id), headers=user_headers(operator))
            assert resp.status_code == status.HTTP_200_OK, resp.text
            assert resp.json()["work_center"] is None
            assert resp.json()["queue"] == []

    def test_queue_rows_carry_part_revision(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        wo.part.revision = "C"
        db_session.commit()

        row = queue_row(client, user_headers(operator), wc.id, op.id)
        assert row["part_revision"] == "C"


class TestLastReportTelemetry:
    def test_null_until_first_report_then_deltas_of_the_last_report(self, client: TestClient, db_session: Session):
        """last_report carries the LAST single report's deltas, not totals:
        report 5 good, then 3 good + 2 scrap -> the tile says +3 / 2."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, quantity_ordered=50)

        assert queue_row(client, headers, wc.id, op.id)["last_report"] is None

        clock_in(client, headers, wo, op)
        report_production(client, headers, op.id, good=5)
        first = queue_row(client, headers, wc.id, op.id)["last_report"]
        assert first["good"] == 5.0
        assert first["scrap"] == 0.0
        assert first["at"].endswith("Z")

        report_production(client, headers, op.id, good=3, scrap=2)
        second = queue_row(client, headers, wc.id, op.id)["last_report"]
        assert second["good"] == 3.0, "must be the LAST report's delta, not the 8.0 running total"
        assert second["scrap"] == 2.0
        assert second["at"] >= first["at"]

        # The same block rides the my-active-job job dict.
        job = active_job(client, headers)["active_job"]
        assert job["last_report"] == second

    def test_clock_out_with_quantities_stamps_last_report(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, quantity_ordered=50)
        entry_id = clock_in(client, headers, wo, op)

        resp = client.post(
            f"/api/v1/shop-floor/clock-out/{entry_id}",
            headers=headers,
            json={"quantity_produced": 4, "quantity_scrapped": 0},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.refresh(op)
        assert op.last_reported_at is not None
        assert op.last_reported_good == 4.0
        assert op.last_reported_scrapped == 0.0

    def test_zero_zero_clock_out_does_not_stamp_last_report(self, client: TestClient, db_session: Session):
        """A quantity-less clock-out (walk-away / shift end) is labor evidence,
        not production evidence -- the LAST REPORT tile must not move."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        entry_id = clock_in(client, headers, wo, op)

        resp = client.post(
            f"/api/v1/shop-floor/clock-out/{entry_id}",
            headers=headers,
            json={"quantity_produced": 0, "quantity_scrapped": 0},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text

        db_session.refresh(op)
        assert op.last_reported_at is None
        assert op.last_reported_good is None
        assert op.last_reported_scrapped is None


class TestMyActiveJobPayload:
    def test_server_time_present_on_empty_and_populated_payloads(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)

        empty = active_job(client, headers)
        assert empty["active_jobs"] == []
        assert empty["active_job"] is None
        assert empty["server_time"].endswith("Z")
        datetime.fromisoformat(empty["server_time"].replace("Z", "+00:00"))

        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        clock_in(client, headers, wo, op)

        populated = active_job(client, headers)
        assert populated["active_job"] is not None
        assert populated["server_time"].endswith("Z")

    def test_session_counts_and_part_revision(self, client: TestClient, db_session: Session):
        """quantity_produced / quantity_scrapped are THIS entry's session
        counts (B7) -- alongside the operation totals, not instead of them."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, quantity_ordered=50)
        wo.part.revision = "D"
        # Pre-existing progress from an earlier shift: op total != session count.
        op.quantity_complete = 10
        db_session.commit()

        clock_in(client, headers, wo, op)
        report_production(client, headers, op.id, good=4, scrap=1)

        job = active_job(client, headers)["active_job"]
        assert job["part_revision"] == "D"
        assert job["quantity_produced"] == 4.0
        assert job["quantity_scrapped"] == 1.0
        assert job["quantity_complete"] == 14.0  # 10 prior + 4 this session

    def test_downtime_minutes_sums_resolved_and_open_blockers(self, client: TestClient, db_session: Session):
        """One resolved 10-minute blocker + one open ~5-minute blocker ≈ 15
        minutes, and the open one keeps accruing (monotonic across reads)."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        clock_in(client, headers, wo, op)

        now = datetime.utcnow()
        db_session.add(
            WorkOrderBlocker(
                work_order_id=wo.id,
                operation_id=op.id,
                category="machine_down",
                title="Alarm 4012",
                status=WorkOrderBlockerStatus.RESOLVED.value,
                reported_at=now - timedelta(minutes=30),
                resolved_at=now - timedelta(minutes=20),
                company_id=COMPANY_A,
            )
        )
        db_session.add(
            WorkOrderBlocker(
                work_order_id=wo.id,
                operation_id=op.id,
                category="material_missing",
                title="Waiting on sheet",
                status=WorkOrderBlockerStatus.OPEN.value,
                reported_at=now - timedelta(minutes=5),
                resolved_at=None,
                company_id=COMPANY_A,
            )
        )
        db_session.commit()

        first = active_job(client, headers)["active_job"]["downtime_minutes"]
        assert 14.9 <= first <= 16.0, first

        second = active_job(client, headers)["active_job"]["downtime_minutes"]
        assert second >= first, "the open blocker accrues -- downtime can never run backwards"

    def test_downtime_excludes_other_operations_blockers(self, client: TestClient, db_session: Session):
        """Per-operation on purpose: a blocker on a sibling op contributes 0."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        other = add_second_operation(db_session, wo, wc)
        db_session.add(
            WorkOrderBlocker(
                work_order_id=wo.id,
                operation_id=other.id,
                category="machine_down",
                title="Sibling op blocker",
                reported_at=datetime.utcnow() - timedelta(minutes=45),
                company_id=COMPANY_A,
            )
        )
        db_session.commit()
        clock_in(client, headers, wo, op)

        assert active_job(client, headers)["active_job"]["downtime_minutes"] == 0.0


class TestNextOperation:
    def test_mid_routing_job_carries_the_next_step_with_work_center(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc_laser = make_work_center(db_session)
        wc_deburr = make_work_center(db_session, name="Deburr Bench")
        wo, op10 = make_wo_with_operation(db_session, work_center=wc_laser)
        op20 = add_second_operation(db_session, wo, wc_deburr, sequence=20, name="Deburr")
        clock_in(client, headers, wo, op10)

        job = active_job(client, headers)["active_job"]
        assert job["next_operation"] == {
            "operation_number": "OP20",
            "name": "Deburr",
            "status": "pending",
            "work_center": {"id": wc_deburr.id, "code": wc_deburr.code, "name": wc_deburr.name},
        }
        assert op20.id  # fixture really exists

    def test_last_operation_carries_null(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc)
        clock_in(client, headers, wo, op)

        assert active_job(client, headers)["active_job"]["next_operation"] is None

    def test_complete_response_carries_next_operation(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc_a = make_work_center(db_session)
        wc_b = make_work_center(db_session)
        wo, op10 = make_wo_with_operation(db_session, work_center=wc_a, quantity_ordered=5)
        add_second_operation(db_session, wo, wc_b, sequence=20, name="Weld out")
        clock_in(client, headers, wo, op10)

        resp = client.post(
            f"/api/v1/shop-floor/operations/{op10.id}/complete",
            headers=headers,
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        nxt = resp.json()["next_operation"]
        assert nxt["operation_number"] == "OP20"
        assert nxt["work_center"] == {"id": wc_b.id, "code": wc_b.code, "name": wc_b.name}

    def test_complete_response_next_operation_null_on_last_op(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = make_wo_with_operation(db_session, work_center=wc, quantity_ordered=5)
        clock_in(client, headers, wo, op)

        resp = client.post(
            f"/api/v1/shop-floor/operations/{op.id}/complete",
            headers=headers,
            json={"quantity_complete": 5},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.json()["next_operation"] is None
