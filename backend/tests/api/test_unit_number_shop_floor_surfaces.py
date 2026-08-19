"""``unit_number`` (083) on the four read surfaces the shop actually looks at.

The office half of the column — create/update/audit/search — is in
``test_work_order_unit_number.py``. This file covers the reads, because they are where
the value earns its existence: the whole reason the number moved out of ``notes`` is
that ``notes`` is unbounded free text and therefore cannot go on an unattended screen.

Four payloads, and the reason each needs an assertion of its own:

1. **The kiosk queue row** (``_kiosk_job_row``), which also feeds the HELD list through
   ``_held_job_row``.
2. **The running-job panel** (``get_my_active_job``) — a SEPARATE hand-built dict. These
   two duplicate their identity keys; only ``_job_guidance_fields`` is shared between
   them. So a regression on one is invisible to a test of the other, and the failure
   mode is specific and bad: the running-job hero is the screen the welder stares at for
   hours, and it would be the one screen that cannot name the unit on the bench.
3. **The wallboard tile** — carried UNGATED, deliberately unlike ``customer_name``. That
   contrast is asserted on ONE tile in ONE response, not in two separate tests, because
   the realistic regression is somebody "harmonizing" the two fields in either
   direction: withholding the unit from a public board (which deletes the feature's
   point) or leaking the customer onto one (which breaks the standing posture).
4. **The dispatch-board row** (``DispatchQueueRow`` via ``dispatch_service``), including
   after the run-order rewrite — that ``PUT`` rebuilds the whole column and returns it,
   so it is a path that can drop a field without any read ever noticing.

Query cost is NOT re-asserted here. ``unit_number`` is read off the same joinedloaded
``work_order`` the guidance block already reads, so the flat-cost guards in
``test_kiosk_operation_guidance.py::TestNoNPlusOne`` fence this field too; adding a
fourth copy of that guard would pin the same eager load a fourth time.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrderStatus
from app.services.wallboard_service import build_wallboard_payload
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_wo_with_operation,
    make_work_center,
    queue_url,
    user_headers,
)
from tests.lean_phase1_helpers import COMPANY_A
from tests.lean_phase1_helpers import make_op as make_lean_op
from tests.lean_phase1_helpers import make_part as make_lean_part
from tests.lean_phase1_helpers import make_wo as make_lean_wo
from tests.lean_phase1_helpers import make_work_center as make_lean_work_center

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

MY_ACTIVE_JOB = "/api/v1/shop-floor/my-active-job"
BOARD_URL = "/api/v1/shop-floor/dispatch-board"
WALLBOARD_URL = "/api/v1/shop-floor/wallboard"
DISPLAY_TOKEN_URL = "/api/v1/auth/display-token"

UNIT = "2410048"
CUSTOMER = "Globex Aerospace Inc"


def _run_order_url(work_center_id: int) -> str:
    return f"/api/v1/shop-floor/work-centers/{work_center_id}/run-order"


def _kiosk_job(db: Session, *, work_center, unit_number=None, op_status=OperationStatus.READY):
    """A kiosk-shaped WO+operation whose work order carries (or does not carry) a unit."""
    wo, op = make_wo_with_operation(db, work_center=work_center, op_status=op_status)
    wo.unit_number = unit_number
    db.commit()
    db.refresh(wo)
    return wo, op


def _clock_in(client: TestClient, headers: dict, wo, op) -> int:
    resp = client.post(
        "/api/v1/shop-floor/clock-in",
        headers=headers,
        json={"work_order_id": wo.id, "operation_id": op.id, "work_center_id": op.work_center_id},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 1 + 2. The two kiosk payloads (queue / held / running job)
# ---------------------------------------------------------------------------


class TestKioskPayloads:
    def test_queue_row_carries_it(self, client: TestClient, db_session: Session):
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        _wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)

        body = client.get(queue_url(wc.id), headers=user_headers(operator)).json()
        row = next(r for r in body["queue"] if r["operation_id"] == op.id)
        assert row["unit_number"] == UNIT

    def test_held_row_inherits_it(self, client: TestClient, db_session: Session):
        """``_held_job_row`` builds on ``_kiosk_job_row``, so the held card gets the key
        with no code of its own. Pinned because that inheritance is exactly what a
        future hand-rolled held row would silently drop — and a held job is precisely
        when somebody needs to know WHICH unit is stopped."""
        operator = make_user(db_session)
        wc = make_work_center(db_session)
        _wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT, op_status=OperationStatus.ON_HOLD)

        body = client.get(queue_url(wc.id), headers=user_headers(operator)).json()
        held = next(r for r in body["held"] if r["operation_id"] == op.id)
        assert held["unit_number"] == UNIT
        # Really the held list, not the queue.
        assert held["startable"] is False
        assert not any(r["operation_id"] == op.id for r in body["queue"])

    def test_running_job_panel_carries_it(self, client: TestClient, db_session: Session):
        """``get_my_active_job`` is a SEPARATE hand-built dict from ``_kiosk_job_row``
        — the identity keys are duplicated between them, so this cannot ride on the
        queue test above."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)
        _clock_in(client, headers, wo, op)

        body = client.get(MY_ACTIVE_JOB, headers=headers).json()
        assert body["active_job"]["unit_number"] == UNIT

    def test_the_queue_row_and_the_running_job_agree(self, client: TestClient, db_session: Session):
        """THE test for the duplicated-key hazard. One work order, two hand-built
        payloads, asserted equal in one place so a change to either one alone fails."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)
        _clock_in(client, headers, wo, op)

        row = next(
            r for r in client.get(queue_url(wc.id), headers=headers).json()["queue"] if r["operation_id"] == op.id
        )
        job = client.get(MY_ACTIVE_JOB, headers=headers).json()["active_job"]
        assert row["unit_number"] == job["unit_number"] == UNIT

    def test_a_job_without_a_unit_reports_the_key_as_null_on_both(self, client: TestClient, db_session: Session):
        """Present-and-``None``, never a missing key: the kiosk renders off one stable
        shape and ``UnitBadge`` decides whether to draw anything."""
        operator = make_user(db_session)
        headers = user_headers(operator)
        wc = make_work_center(db_session)
        wo, op = _kiosk_job(db_session, work_center=wc, unit_number=None)
        _clock_in(client, headers, wo, op)

        row = next(
            r for r in client.get(queue_url(wc.id), headers=headers).json()["queue"] if r["operation_id"] == op.id
        )
        job = client.get(MY_ACTIVE_JOB, headers=headers).json()["active_job"]
        assert "unit_number" in row and row["unit_number"] is None
        assert "unit_number" in job and job["unit_number"] is None

    def test_a_pin_unlocked_station_sees_it_too(self, client: TestClient, db_session: Session):
        """The crew station holds a station-type JWT and no user — the screen the whole
        083 story starts from. It is inside the disclosure boundary for planning
        identity, exactly as it is for the five guidance fields."""
        wc = make_work_center(db_session)
        station = make_kiosk_station(db_session, work_center=wc)
        _wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)

        body = client.get(queue_url(wc.id), headers=bearer(kiosk_token_for(station))).json()
        row = next(r for r in body["queue"] if r["operation_id"] == op.id)
        assert row["unit_number"] == UNIT


# ---------------------------------------------------------------------------
# 3. The wallboard tile — ungated, unlike customer_name
# ---------------------------------------------------------------------------


def _wall_wo(db: Session, *, unit_number=None, customer_name: str = CUSTOMER, company_id: int = COMPANY_A):
    """A company-scoped IN_PROGRESS WO with an open op — the job-wall shape."""
    part = make_lean_part(db, company_id=company_id)
    wc = make_lean_work_center(db, company_id=company_id)
    wo = make_lean_wo(
        db,
        part,
        company_id=company_id,
        status_=WorkOrderStatus.IN_PROGRESS,
        customer_name=customer_name,
        quantity_ordered=10,
    )
    wo.unit_number = unit_number
    db.commit()
    make_lean_op(db, wo, wc, company_id=company_id, status_=OperationStatus.READY)
    db.commit()
    db.refresh(wo)
    return wo


class TestWallboardTile:
    def test_service_payload_carries_it(self, db_session: Session):
        wo = _wall_wo(db_session, unit_number=UNIT)

        payload = build_wallboard_payload(db_session, COMPANY_A)
        tile = next(job for job in payload.jobs if job.wo_number == wo.work_order_number)
        assert tile.unit_number == UNIT

    def test_a_blank_unit_collapses_to_none(self, db_session: Session):
        """``wo.unit_number or None`` — a blank free-text value must render the tile's
        PRE-083 layout, not an empty badge with a label and no number."""
        wo = _wall_wo(db_session, unit_number="")

        payload = build_wallboard_payload(db_session, COMPANY_A)
        tile = next(job for job in payload.jobs if job.wo_number == wo.work_order_number)
        assert tile.unit_number is None
        # Non-vacuity: the job really is on the wall, so the None is a blank, not an absence.
        assert tile.part_number is not None

    def test_a_public_display_token_gets_the_unit_and_still_no_customer(
        self, client: TestClient, admin_headers: dict, db_session: Session
    ):
        """BOTH HALVES, DELIBERATELY IN ONE TEST, on ONE tile.

        An un-flagged display token is the un-attended shop-floor TV: no user, no
        session, ``show_customer_names`` false. It gets the unit number, because a
        bounded <=50-char build number is not customer data and putting it on the wall
        is the entire reason 083 exists. It still does NOT get ``customer_name``.

        Asserted together because the realistic failure is a later reader harmonizing
        the two: gating the unit deletes the feature, and ungating the customer breaks
        the standing "no customer names on a public screen" posture.
        """
        wo = _wall_wo(db_session, unit_number=UNIT)
        issued = client.post(DISPLAY_TOKEN_URL, json={"label": "Weld bay TV"}, headers=admin_headers)
        assert issued.status_code == status.HTTP_200_OK, issued.text
        token = issued.json()["token"]
        assert issued.json()["show_customer_names"] is False

        resp = client.get(WALLBOARD_URL, headers=bearer(token))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        tile = next(job for job in resp.json()["jobs"] if job["wo_number"] == wo.work_order_number)

        assert tile["unit_number"] == UNIT, "the unit number is NOT gated like customer_name"
        assert tile["customer_name"] is None, "customer_name is still withheld from a public board"
        assert CUSTOMER not in resp.text

    def test_a_non_privileged_signed_in_role_also_sees_the_unit(self, client: TestClient, db_session: Session):
        """The other side of the same gate. An operator is redacted for
        ``customer_name`` and must NOT be for the unit number."""
        wo = _wall_wo(db_session, unit_number=UNIT)
        operator = make_user(db_session, role=UserRole.OPERATOR)

        resp = client.get(WALLBOARD_URL, headers=user_headers(operator))
        tile = next(job for job in resp.json()["jobs"] if job["wo_number"] == wo.work_order_number)
        assert tile["unit_number"] == UNIT
        assert tile["customer_name"] is None


# ---------------------------------------------------------------------------
# 4. The dispatch board, including across the run-order rewrite
# ---------------------------------------------------------------------------


def _column(payload: dict, work_center_id: int) -> dict:
    for column in payload["work_centers"]:
        if column["id"] == work_center_id:
            return column
    raise AssertionError(f"work center {work_center_id} missing from board")


class TestDispatchBoard:
    def test_board_row_carries_it(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _wo, op = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        assert resp.status_code == status.HTTP_200_OK, resp.text
        row = next(r for r in _column(resp.json(), wc.id)["queue"] if r["operation_id"] == op.id)
        assert row["unit_number"] == UNIT

    def test_a_row_without_one_is_null_not_missing(self, client: TestClient, db_session: Session):
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _wo, op = _kiosk_job(db_session, work_center=wc, unit_number=None)

        resp = client.get(BOARD_URL, headers=user_headers(manager))
        row = next(r for r in _column(resp.json(), wc.id)["queue"] if r["operation_id"] == op.id)
        assert "unit_number" in row and row["unit_number"] is None

    def test_the_run_order_rewrite_returns_the_column_with_the_unit_intact(
        self, client: TestClient, db_session: Session
    ):
        """``PUT .../run-order`` REBUILDS the column and returns it, so it is a write
        path that can drop a read-only field without any GET ever noticing. Ranking a
        job must not blank the badge the floor is reading off the same card."""
        manager = make_user(db_session, role=UserRole.MANAGER)
        wc = make_work_center(db_session)
        _wo_a, op_a = _kiosk_job(db_session, work_center=wc, unit_number=UNIT)
        _wo_b, op_b = _kiosk_job(db_session, work_center=wc, unit_number=None)

        resp = client.put(
            _run_order_url(wc.id),
            json={"operation_ids": [op_b.id, op_a.id]},
            headers=user_headers(manager),
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        rows = {r["operation_id"]: r for r in resp.json()["queue"]}

        # The rank really was applied (so the payload below is the rebuilt one).
        assert [rows[op_b.id]["run_order"], rows[op_a.id]["run_order"]] == [1, 2]
        assert rows[op_a.id]["unit_number"] == UNIT
        assert "unit_number" in rows[op_b.id] and rows[op_b.id]["unit_number"] is None

        # And the next board read still agrees.
        board = client.get(BOARD_URL, headers=user_headers(manager)).json()
        board_rows = {r["operation_id"]: r for r in _column(board, wc.id)["queue"]}
        assert board_rows[op_a.id]["unit_number"] == UNIT
