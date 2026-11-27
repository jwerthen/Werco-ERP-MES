"""Gap coverage for routing changes on a released work order (owner decision 2026-11-25).

``test_released_routing_changes.py`` pins the main contract. This file covers what it left
unexercised:

* required-audit failure (503, nothing saved) on unskip / add / resequence / the reasoned PUT,
  and NOT on a DRAFT add (a draft's plan is still being built, best-effort audit);
* tenant isolation on every new verb (another company's work order / operation is 404);
* role refusal (Operator / Viewer 403) on unskip / add / resequence;
* every remaining refusal branch, with the row state read back unchanged;
* a SKIPPED step refused on every start / complete / hold / resume / production door, the
  kiosk badge clock-in and the scanner's legal actions;
* a SKIPPED step absent from the kiosk queue and the dispatch board, and counted finished
  (never "blocking") on the dispatch plan lanes;
* the read-path reconcile never re-derives a SKIPPED step's status from evidence;
* resequence never rewrites ``operation_number``, and a no-op move writes nothing;
* narrowed (read-only / AI-external) API tokens are refused on every routing verb;
* the audit rows carry the reason and the old / new values.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.time_entry import TimeEntry
from app.models.user import UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus, WorkOrderType
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operation_action_gates import MSG_OPERATION_SKIPPED
from app.services.routing_change_service import ROUTING_AUDIT_UNAVAILABLE
from tests.api.export_control_helpers import issue_token
from tests.api.kiosk_test_helpers import (
    bearer,
    kiosk_token_for,
    make_kiosk_station,
    make_user,
    make_work_center,
    queue_url,
    user_headers,
)
from tests.api.short_count_helpers import add_entry, fresh, make_routing, op_audit_rows, set_op

pytestmark = [pytest.mark.api, pytest.mark.requires_db]

COMPANY_B = 2
API_TOKENS_URL = "/api/v1/api-tokens/"
PENDING, READY, IN_PROGRESS, COMPLETE, SKIPPED = (
    OperationStatus.PENDING,
    OperationStatus.READY,
    OperationStatus.IN_PROGRESS,
    OperationStatus.COMPLETE,
    OperationStatus.SKIPPED,
)


# --------------------------------------------------------------------------- helpers


def skip_url(op) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/skip"


def unskip_url(op) -> str:
    return f"/api/v1/work-orders/operations/{op.id}/unskip"


def add_url(wo) -> str:
    return f"/api/v1/work-orders/{wo.id}/operations"


def reseq_url(wo) -> str:
    return f"/api/v1/work-orders/{wo.id}/operations/resequence"


def put_url(op) -> str:
    return f"/api/v1/work-orders/operations/{op.id}"


def _version(db: Session, op) -> int:
    return fresh(db, WorkOrderOperation, op.id).version


def _skip(client, db, op, headers, reason="customer dropped the paint step"):
    return client.post(skip_url(op), headers=headers, json={"version": _version(db, op), "reason": reason})


def _unskip(client, db, op, headers, reason="customer wants paint after all"):
    return client.post(unskip_url(op), headers=headers, json={"version": _version(db, op), "reason": reason})


def _reseq_body(db, moves, reason="weld before paint"):
    return {
        "reason": reason,
        "operations": [{"id": op.id, "version": _version(db, op), "sequence": seq} for op, seq in moves],
    }


def _add_body(wc, sequence, **extra):
    return {"work_center_id": wc.id, "sequence": sequence, "name": "Deburr", **extra}


def _rows(db: Session, ops) -> list:
    """(status, sequence, operation_number, skip_reason, version) per op, freshly read."""
    db.expire_all()
    out = []
    for op in ops:
        row = db.get(WorkOrderOperation, op.id)
        out.append((row.status, row.sequence, row.operation_number, row.skip_reason, row.version))
    return out


def _op_count(db: Session, wo) -> int:
    db.expire_all()
    return db.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).count()


def _routing_audit_count(db: Session, ops) -> int:
    db.expire_all()
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id.in_([op.id for op in ops]))
        .count()
    )


def _fail_required_audit(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AuditWriteError("no audit")

    monkeypatch.setattr(AuditService, "log_required", fail)


def _full_token(client: TestClient, admin_headers: dict, user) -> dict:
    issued = client.post(API_TOKENS_URL, json={"user_id": user.id, "label": "routing gap probe"}, headers=admin_headers)
    assert issued.status_code == 201, issued.text
    return bearer(issued.json()["token"])


def _badge_headers(user) -> dict:
    """A 5-minute badge-minted operator token, as POST /auth/kiosk-badge-token mints it."""
    return bearer(
        create_access_token(
            subject=user.id, company_id=user.company_id, expires_delta=timedelta(minutes=5), scope="kiosk"
        )
    )


@pytest.fixture
def supervisor(db_session: Session):
    return make_user(db_session, role=UserRole.SUPERVISOR)


@pytest.fixture
def three_step(db_session: Session):
    """A released, SEQUENCED routing across three machines: 10 READY, 20 / 30 PENDING."""
    wcs = [make_work_center(db_session) for _ in range(3)]
    return make_routing(db_session, work_centers=wcs, sequential=True)


@pytest.fixture
def skipped_middle(client, db_session, supervisor, three_step):
    """op10 COMPLETE (10 good), op20 SKIPPED, op30 READY (promoted past the skip)."""
    wo, (op10, op20, op30) = three_step
    set_op(db_session, op10, status=COMPLETE, quantity_complete=10)
    set_op(db_session, op20, status=READY)
    assert _skip(client, db_session, op20, user_headers(supervisor)).status_code == 200
    assert _rows(db_session, [op20, op30])[0][0] == SKIPPED
    set_op(db_session, fresh(db_session, WorkOrderOperation, op30.id), status=READY)
    return wo, (op10, op20, op30)


# --------------------------------------------------------------------------- required audit: 503, nothing saved


class TestRequiredAuditFailureSavesNothing:
    def test_unskip(self, client, db_session, supervisor, three_step, monkeypatch):
        _, (op10, op20, op30) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers, reason="drop it").status_code == 200
        before = _rows(db_session, [op10, op20, op30])
        assert [r[0] for r in before] == [SKIPPED, READY, PENDING]
        audit_rows = _routing_audit_count(db_session, [op10, op20, op30])
        _fail_required_audit(monkeypatch)
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 503
        assert resp.json()["detail"] == ROUTING_AUDIT_UNAVAILABLE
        # The restore AND the demotion of op20 are both rolled back.
        assert _rows(db_session, [op10, op20, op30]) == before
        restored = fresh(db_session, WorkOrderOperation, op10.id)
        assert restored.skipped_by == supervisor.id and restored.skipped_at is not None
        assert _routing_audit_count(db_session, [op10, op20, op30]) == audit_rows

    def test_add_on_a_released_work_order(self, client, db_session, supervisor, three_step, monkeypatch):
        wo, ops = three_step
        _fail_required_audit(monkeypatch)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 40, change_reason="customer added QC"),
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == ROUTING_AUDIT_UNAVAILABLE
        assert _op_count(db_session, wo) == 3

    def test_add_in_front_rolls_back_the_demotion_too(self, client, db_session, supervisor, monkeypatch):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op30) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, READY])
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op30, sequence=30)
        _fail_required_audit(monkeypatch)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 20, change_reason="x"),
        )
        assert resp.status_code == 503
        assert _op_count(db_session, wo) == 2
        assert fresh(db_session, WorkOrderOperation, op30.id).status == READY

    def test_a_draft_add_is_best_effort_and_still_saves(self, client, db_session, supervisor, monkeypatch):
        wo, _ = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        _fail_required_audit(monkeypatch)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 20)
        )
        assert resp.status_code == 200, resp.text
        assert _op_count(db_session, wo) == 2

    def test_resequence(self, client, db_session, supervisor, three_step, monkeypatch):
        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        _fail_required_audit(monkeypatch)
        resp = client.post(
            reseq_url(wo),
            headers=user_headers(supervisor),
            json=_reseq_body(db_session, [(op10, 40), (op30, 10)]),
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == ROUTING_AUDIT_UNAVAILABLE
        # Neither the moves nor the demotion of op10 / promotion of op30 survived.
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_put_content_with_a_reason(self, client, db_session, supervisor, three_step, monkeypatch):
        _, (_, op20, _) = three_step
        set_op(db_session, op20, run_instructions="torque to spec")
        version = _version(db_session, op20)
        _fail_required_audit(monkeypatch)
        resp = client.put(
            put_url(op20),
            headers=user_headers(supervisor),
            json={"version": version, "run_instructions": "new torque spec", "change_reason": "ECO 41"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == ROUTING_AUDIT_UNAVAILABLE
        row = fresh(db_session, WorkOrderOperation, op20.id)
        assert (row.run_instructions, row.version) == ("torque to spec", version)


# --------------------------------------------------------------------------- tenant isolation


class TestAnotherCompanysRoutingIs404:
    @pytest.fixture
    def outsider(self, db_session):
        return make_user(db_session, company_id=COMPANY_B, role=UserRole.ADMIN)

    def test_unskip(self, client, db_session, supervisor, three_step, outsider):
        _, (op10, _, _) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        assert _unskip(client, db_session, op10, user_headers(outsider)).status_code == 404
        assert _rows(db_session, [op10])[0][0] == SKIPPED

    def test_add(self, client, db_session, three_step, outsider):
        wo, _ = three_step
        own_wc = make_work_center(db_session, company_id=COMPANY_B)
        resp = client.post(add_url(wo), headers=user_headers(outsider), json=_add_body(own_wc, 40, change_reason="x"))
        assert resp.status_code == 404
        assert _op_count(db_session, wo) == 3

    def test_add_with_another_companys_work_center(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        foreign_wc = make_work_center(db_session, company_id=COMPANY_B)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(foreign_wc, 40, change_reason="x")
        )
        assert resp.status_code == 404
        assert _op_count(db_session, wo) == 3

    def test_resequence(self, client, db_session, three_step, outsider):
        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            reseq_url(wo), headers=user_headers(outsider), json=_reseq_body(db_session, [(op20, 30), (op30, 20)])
        )
        assert resp.status_code == 404
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_resequence_naming_another_companys_operation(self, client, db_session, supervisor, three_step, outsider):
        wo, (op10, op20, op30) = three_step
        wcs_b = [make_work_center(db_session, company_id=COMPANY_B) for _ in range(2)]
        _, (foreign, _) = make_routing(db_session, work_centers=wcs_b, company_id=COMPANY_B)
        before = _rows(db_session, [op10, op20, op30, foreign])
        resp = client.post(
            reseq_url(wo),
            headers=user_headers(supervisor),
            json=_reseq_body(db_session, [(op20, 40), (foreign, 20)]),
        )
        assert resp.status_code == 404
        assert _rows(db_session, [op10, op20, op30, foreign]) == before

    def test_put_content_edit(self, client, db_session, three_step, outsider):
        _, (_, op20, _) = three_step
        version = _version(db_session, op20)
        resp = client.put(
            put_url(op20),
            headers=user_headers(outsider),
            json={"version": version, "name": "Renamed", "change_reason": "x"},
        )
        assert resp.status_code == 404
        assert fresh(db_session, WorkOrderOperation, op20.id).name == "Step 20"


# --------------------------------------------------------------------------- roles


class TestOnlyAdminManagerSupervisor:
    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER, UserRole.QUALITY])
    def test_unskip_add_resequence_refuse_other_roles(self, client, db_session, supervisor, three_step, role):
        wo, (op10, op20, op30) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        before = _rows(db_session, [op10, op20, op30])
        headers = user_headers(make_user(db_session, role=role))
        assert _unskip(client, db_session, op10, headers).status_code == 403
        body = _add_body(make_work_center(db_session), 40, change_reason="x")
        assert client.post(add_url(wo), headers=headers, json=body).status_code == 403
        resp = client.post(reseq_url(wo), headers=headers, json=_reseq_body(db_session, [(op20, 30), (op30, 20)]))
        assert resp.status_code == 403
        assert _rows(db_session, [op10, op20, op30]) == before
        assert _op_count(db_session, wo) == 3

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
    def test_admin_and_manager_may_skip_and_restore(self, client, db_session, three_step, role):
        _, (op10, _, _) = three_step
        headers = user_headers(make_user(db_session, role=role))
        assert _skip(client, db_session, op10, headers).status_code == 200
        assert _unskip(client, db_session, op10, headers).status_code == 200


# --------------------------------------------------------------------------- remaining refusal branches


class TestUnskipRefusals:
    @pytest.fixture
    def skipped10(self, client, db_session, supervisor, three_step):
        wo, ops = three_step
        assert _skip(client, db_session, ops[0], user_headers(supervisor)).status_code == 200
        return wo, ops

    def test_a_stale_version_is_409_and_nothing_changes(self, client, db_session, supervisor, skipped10):
        _, (op10, op20, op30) = skipped10
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            unskip_url(op10),
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op10) + 7, "reason": "x"},
        )
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
    def test_a_terminal_work_order_is_409(self, client, db_session, supervisor, skipped10, wo_status):
        wo, (op10, _, _) = skipped10
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=wo_status)
        assert _unskip(client, db_session, op10, user_headers(supervisor)).status_code == 409
        assert _rows(db_session, [op10])[0][0] == SKIPPED

    def test_a_deleted_work_order_is_404(self, client, db_session, supervisor, skipped10):
        wo, (op10, _, _) = skipped10
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), is_deleted=True)
        assert _unskip(client, db_session, op10, user_headers(supervisor)).status_code == 404
        assert _rows(db_session, [op10])[0][0] == SKIPPED

    def test_a_laser_work_order_is_409(self, client, db_session, supervisor, skipped10):
        wo, (op10, _, _) = skipped10
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), work_order_type=WorkOrderType.LASER_CUTTING.value)
        assert _unskip(client, db_session, op10, user_headers(supervisor)).status_code == 409
        assert _rows(db_session, [op10])[0][0] == SKIPPED

    def test_an_unknown_operation_is_404(self, client, supervisor):
        resp = client.post(
            "/api/v1/work-orders/operations/987654321/unskip",
            headers=user_headers(supervisor),
            json={"version": 1, "reason": "x"},
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
    def test_a_reason_is_required(self, client, db_session, supervisor, skipped10, reason):
        _, (op10, _, _) = skipped10
        resp = client.post(
            unskip_url(op10),
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op10), "reason": reason},
        )
        assert resp.status_code == 422
        assert _rows(db_session, [op10])[0][0] == SKIPPED


class TestSkipRefusalsLeaveTheRowAlone:
    def test_an_unknown_operation_is_404(self, client, supervisor):
        resp = client.post(
            "/api/v1/work-orders/operations/987654321/skip",
            headers=user_headers(supervisor),
            json={"version": 1, "reason": "x"},
        )
        assert resp.status_code == 404

    def test_a_stale_version_writes_nothing(self, client, db_session, supervisor, three_step):
        _, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            skip_url(op10), headers=user_headers(supervisor), json={"version": op10.version + 5, "reason": "x"}
        )
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before
        assert _routing_audit_count(db_session, [op10, op20, op30]) == 0

    def test_an_already_skipped_step_cannot_be_skipped_again(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers, reason="first").status_code == 200
        resp = _skip(client, db_session, op10, headers, reason="second")
        assert resp.status_code == 409
        row = fresh(db_session, WorkOrderOperation, op10.id)
        assert (row.status, row.skip_reason) == (SKIPPED, "first")

    def test_pieces_recorded_without_labor_still_count_as_recorded_work(
        self, client, db_session, supervisor, three_step
    ):
        _, (op10, _, _) = three_step
        set_op(db_session, op10, quantity_complete=3)
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]
        assert _rows(db_session, [op10])[0][0] == READY

    def test_a_stamped_start_counts_as_recorded_work(self, client, db_session, supervisor, three_step):
        from datetime import datetime

        _, (op10, _, _) = three_step
        set_op(db_session, op10, actual_start=datetime.utcnow())
        resp = _skip(client, db_session, op10, user_headers(supervisor))
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]
        assert _rows(db_session, [op10])[0][0] == READY


class TestResequenceRefusals:
    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED])
    def test_a_terminal_work_order_is_409(self, client, db_session, supervisor, three_step, wo_status):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=wo_status)
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op20, 30), (op30, 20)])
        )
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_deleted_work_order_is_404(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), is_deleted=True)
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op20, 30), (op30, 20)])
        )
        assert resp.status_code == 404
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_laser_work_order_is_409(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), work_order_type=WorkOrderType.LASER_CUTTING.value)
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op20, 30), (op30, 20)])
        )
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_skipped_step_cannot_be_reordered(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        assert _skip(client, db_session, op20, user_headers(supervisor)).status_code == 200
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op20, 40)]))
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_completed_step_cannot_be_reordered(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        set_op(db_session, op10, status=COMPLETE, quantity_complete=10)
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op10, 40)]))
        assert resp.status_code == 409
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_an_unlisted_steps_sequence_counts_as_taken(self, client, db_session, supervisor, three_step):
        """A collision with a step that is NOT listed is still a collision (409, nothing moved)."""
        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op30, 10)]))
        assert resp.status_code == 409
        assert "would be used by both" in resp.json()["detail"]
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_listed_step_with_labor_but_ready_status_is_refused(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        add_entry(db_session, make_user(db_session), op10, produced=0)
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op10, 40)]))
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]
        assert _rows(db_session, [op10, op20, op30]) == before

    @pytest.mark.parametrize(
        "body",
        [
            {"reason": "x", "operations": []},
            {"reason": "", "operations": [{"id": 1, "version": 1, "sequence": 20}]},
            {"reason": "x", "operations": [{"id": 1, "version": 1, "sequence": 25}]},
            {"reason": "x", "operations": [{"id": 1, "version": 1, "sequence": 1000}]},
            {
                "reason": "x",
                "operations": [{"id": 1, "version": 1, "sequence": 20}, {"id": 1, "version": 1, "sequence": 30}],
            },
        ],
        ids=["empty", "no-reason", "not-multiple-of-10", "above-990", "listed-twice"],
    )
    def test_a_malformed_body_is_422(self, client, db_session, supervisor, three_step, body):
        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        assert client.post(reseq_url(wo), headers=user_headers(supervisor), json=body).status_code == 422
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_move_to_the_same_sequence_writes_nothing(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(reseq_url(wo), headers=user_headers(supervisor), json=_reseq_body(db_session, [(op20, 20)]))
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed_operation_ids"] == []
        assert _rows(db_session, [op10, op20, op30]) == before
        assert _routing_audit_count(db_session, [op10, op20, op30]) == 0


class TestAddRefusals:
    def test_a_laser_work_order_is_refused(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), work_order_type=WorkOrderType.LASER_CUTTING.value)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 40, change_reason="x"),
        )
        assert resp.status_code in (400, 409)
        assert _op_count(db_session, wo) == 3

    def test_a_whitespace_reason_counts_as_none(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 40, change_reason="    "),
        )
        assert resp.status_code == 422
        assert _op_count(db_session, wo) == 3

    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.ON_HOLD, WorkOrderStatus.IN_PROGRESS])
    def test_a_reason_is_required_on_every_non_draft_status(
        self, client, db_session, supervisor, three_step, wo_status
    ):
        wo, _ = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=wo_status)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(make_work_center(db_session), 40)
        )
        assert resp.status_code == 422
        assert _op_count(db_session, wo) == 3


class TestPutContentRefusals:
    @pytest.mark.parametrize("wo_status", [WorkOrderStatus.ON_HOLD, WorkOrderStatus.IN_PROGRESS])
    def test_a_reason_is_required_on_every_non_draft_status(
        self, client, db_session, supervisor, three_step, wo_status
    ):
        wo, (_, op20, _) = three_step
        set_op(db_session, fresh(db_session, WorkOrder, wo.id), status=wo_status)
        version = _version(db_session, op20)
        resp = client.put(
            put_url(op20), headers=user_headers(supervisor), json={"version": version, "description": "new text"}
        )
        assert resp.status_code == 422
        row = fresh(db_session, WorkOrderOperation, op20.id)
        assert (row.description, row.version) == (None, version)

    def test_resubmitting_the_stored_content_needs_no_reason(self, client, db_session, supervisor, three_step):
        """Only a CHANGED content field is a routing change: an unchanged echo is not."""
        _, (_, op20, _) = three_step
        resp = client.put(
            put_url(op20),
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op20), "name": "Step 20"},
        )
        assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- a skipped step on every door


class TestASkippedStepIsRefusedOnEveryDoor:
    """The FIRST step is skipped, so no predecessor gate stands in front of it: the only thing
    refusing each door is the SKIPPED status itself."""

    @pytest.fixture
    def skipped(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        # Present the skipped step in the slot the doors address (``op20`` below).
        return wo, (None, fresh(db_session, WorkOrderOperation, op10.id), op30)

    def _assert_untouched(self, db, op):
        row = fresh(db, WorkOrderOperation, op.id)
        assert row.status == SKIPPED
        assert float(row.quantity_complete or 0) == 0 and float(row.quantity_scrapped or 0) == 0
        assert row.actual_start is None
        assert db.query(TimeEntry).filter(TimeEntry.operation_id == op.id).count() == 0

    def test_office_complete(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.post(
            f"/api/v1/work-orders/operations/{op20.id}/complete",
            headers=user_headers(supervisor),
            params={"quantity_complete": 10},
        )
        assert resp.status_code in (400, 409), resp.text
        self._assert_untouched(db_session, op20)

    def test_shop_floor_complete(self, client, db_session, supervisor, skipped):
        """Labor evidence is planted on the skipped row (the floor close refuses a labor-less
        operation on its own), so only the SKIPPED status can be what refuses the close."""
        _, (_, op20, _) = skipped
        add_entry(db_session, make_user(db_session), op20, produced=10)
        resp = client.post(
            f"/api/v1/shop-floor/operations/{op20.id}/complete", headers=user_headers(supervisor), json={}
        )
        assert resp.status_code in (400, 409), resp.text
        row = fresh(db_session, WorkOrderOperation, op20.id)
        assert row.status == SKIPPED and float(row.quantity_complete or 0) == 0

    @pytest.mark.parametrize("status", ["in_progress", "ready", "complete", "pending", "on_hold"])
    def test_put_status(self, client, db_session, supervisor, skipped, status):
        _, (_, op20, _) = skipped
        resp = client.put(
            put_url(op20),
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op20), "status": status},
        )
        assert resp.status_code == 409 and resp.json()["detail"] == MSG_OPERATION_SKIPPED
        self._assert_untouched(db_session, op20)

    def test_put_counts(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.put(
            put_url(op20),
            headers=user_headers(supervisor),
            json={"version": _version(db_session, op20), "quantity_complete": 5},
        )
        assert resp.status_code == 409 and resp.json()["detail"] == MSG_OPERATION_SKIPPED
        self._assert_untouched(db_session, op20)

    def test_shop_floor_hold(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.put(f"/api/v1/shop-floor/operations/{op20.id}/hold", headers=user_headers(supervisor), json={})
        assert resp.status_code == 409, resp.text
        self._assert_untouched(db_session, op20)

    def test_shop_floor_resume(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.put(f"/api/v1/shop-floor/operations/{op20.id}/resume", headers=user_headers(supervisor))
        assert resp.status_code in (400, 409), resp.text
        self._assert_untouched(db_session, op20)

    def test_production_report(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.post(
            f"/api/v1/shop-floor/operations/{op20.id}/production",
            headers=user_headers(supervisor),
            json={"quantity_complete_delta": 3},
        )
        assert resp.status_code in (400, 409), resp.text
        self._assert_untouched(db_session, op20)

    def test_kiosk_badge_clock_in(self, client, db_session, skipped):
        _, (_, op20, _) = skipped
        operator = make_user(db_session, role=UserRole.OPERATOR)
        resp = client.post(
            "/api/v1/shop-floor/clock-in",
            headers=_badge_headers(operator),
            json={
                "work_order_id": op20.work_order_id,
                "operation_id": op20.id,
                "work_center_id": op20.work_center_id,
                "entry_type": "run",
                "source": "kiosk",
            },
        )
        assert resp.status_code in (400, 409), resp.text
        self._assert_untouched(db_session, op20)

    def test_the_scanner_offers_no_action(self, client, db_session, supervisor, skipped):
        _, (_, op20, _) = skipped
        resp = client.post(
            "/api/v1/scanner/resolve-action",
            json={"code": f"OP:{op20.id}", "work_center_id": op20.work_center_id},
            headers=user_headers(make_user(db_session, role=UserRole.OPERATOR)),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation"]["status"] == "skipped"
        assert body["legal_actions"] == []
        assert set(body["blockers"]) >= {"clock_in", "complete", "hold", "resume"}
        self._assert_untouched(db_session, op20)


class TestASkippedStepIsOffTheBoards:
    def test_the_kiosk_queue_shows_neither_queued_nor_held(self, client, db_session, supervisor):
        """Two steps at ONE machine (pooled): the skipped one is gone, the live one is queued."""
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc, wc], sequential=False, statuses=[READY, READY])
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        station = make_kiosk_station(db_session, work_center=wc)
        for headers in (user_headers(make_user(db_session)), bearer(kiosk_token_for(station))):
            resp = client.get(queue_url(wc.id), headers=headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            listed = {row["operation_id"] for row in body["queue"]} | {row["operation_id"] for row in body["held"]}
            assert op10.id not in listed
            assert op20.id in listed

    def test_the_dispatch_board_drops_it(self, client, db_session, supervisor):
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc, wc], sequential=False, statuses=[READY, READY])
        assert _skip(client, db_session, op10, user_headers(supervisor)).status_code == 200
        board = client.get("/api/v1/shop-floor/dispatch-board", headers=user_headers(supervisor))
        assert board.status_code == 200, board.text
        column = next(col for col in board.json()["work_centers"] if col["id"] == wc.id)
        ids = {row["operation_id"] for row in column["queue"]}
        assert op10.id not in ids and op20.id in ids

    def test_the_dispatch_lanes_count_it_finished_and_never_blocking(
        self, client, db_session, supervisor, skipped_middle
    ):
        wo, (op10, op20, op30) = skipped_middle
        resp = client.get("/api/v1/shop-floor/dispatch-board/work-orders", headers=user_headers(supervisor))
        assert resp.status_code == 200, resp.text
        lane = next(lane for lane in resp.json()["work_orders"] if lane["work_order_id"] == wo.id)
        assert lane["ops_complete"] == 2
        by_id = {op["operation_id"]: op for op in lane["operations"]}
        assert by_id[op20.id]["status"] == "skipped"
        assert by_id[op30.id]["blocked_by_predecessor"] is False

    def test_shop_floor_operations_ready_filter(self, client, db_session, supervisor, skipped_middle):
        _, (_, op20, op30) = skipped_middle
        body = client.get(
            "/api/v1/shop-floor/operations", headers=user_headers(supervisor), params={"status": "ready"}
        ).json()
        ids = {row["id"] for row in body["operations"]}
        assert op20.id not in ids and op30.id in ids


# --------------------------------------------------------------------------- read-path reconcile


class TestTheReadPathNeverRevivesASkippedStep:
    def test_evidence_on_a_skipped_row_does_not_flip_it(self, client, db_session, supervisor, skipped_middle):
        """Even a SKIPPED row that somehow carries a full count + closed labor stays SKIPPED on
        every reconciling read (GET /work-orders, /work-orders/{id}, /shop-floor/operations)."""
        wo, (_, op20, _) = skipped_middle
        set_op(db_session, fresh(db_session, WorkOrderOperation, op20.id), quantity_complete=10)
        add_entry(db_session, make_user(db_session), fresh(db_session, WorkOrderOperation, op20.id), produced=10)
        headers = user_headers(supervisor)
        assert client.get(f"/api/v1/work-orders/{wo.id}", headers=headers).status_code == 200
        assert client.get("/api/v1/work-orders", headers=headers).status_code == 200
        assert client.get("/api/v1/shop-floor/operations", headers=headers).status_code == 200
        row = fresh(db_session, WorkOrderOperation, op20.id)
        assert row.status == SKIPPED
        assert row.skip_reason == "customer dropped the paint step"
        assert fresh(db_session, WorkOrder, wo.id).status not in (WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED)

    def test_a_skipped_step_is_never_promoted_by_a_read(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        assert _skip(client, db_session, op30, user_headers(supervisor)).status_code == 200
        set_op(db_session, fresh(db_session, WorkOrderOperation, op10.id), status=COMPLETE, quantity_complete=10)
        set_op(db_session, fresh(db_session, WorkOrderOperation, op20.id), status=COMPLETE, quantity_complete=10)
        # Both live steps complete: a read completes the work order and leaves the skipped step SKIPPED.
        assert client.get(f"/api/v1/work-orders/{wo.id}", headers=user_headers(supervisor)).status_code == 200
        assert fresh(db_session, WorkOrderOperation, op30.id).status == SKIPPED


# --------------------------------------------------------------------------- resequence keeps identifiers


class TestResequenceKeepsIdentifiers:
    def test_moving_into_a_skipped_steps_sequence(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op20, headers).status_code == 200
        resp = client.post(reseq_url(wo), headers=headers, json=_reseq_body(db_session, [(op30, 20)]))
        assert resp.status_code == 200, resp.text
        rows = _rows(db_session, [op10, op20, op30])
        assert [(r[0], r[1], r[2]) for r in rows] == [(READY, 10, "10"), (SKIPPED, 20, "20"), (PENDING, 20, "30")]
        # Response operations carry the untouched identifiers too.
        by_id = {op["id"]: op for op in resp.json()["operations"]}
        assert by_id[op30.id]["operation_number"] == "30" and by_id[op30.id]["sequence"] == 20

    def test_a_three_way_rotation(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(3)]
        wo, (op10, op20, op30) = make_routing(db_session, work_centers=wcs, statuses=[PENDING, PENDING, PENDING])
        resp = client.post(
            reseq_url(wo),
            headers=user_headers(supervisor),
            json=_reseq_body(db_session, [(op10, 30), (op20, 10), (op30, 20)]),
        )
        assert resp.status_code == 200, resp.text
        rows = _rows(db_session, [op10, op20, op30])
        assert [(r[1], r[2]) for r in rows] == [(30, "10"), (10, "20"), (20, "30")]
        # The new first step is promoted (released job, nothing in front of it).
        assert rows[1][0] == READY


# --------------------------------------------------------------------------- narrowed tokens


class TestNarrowedTokensAreRefused:
    @pytest.fixture(params=[False, True], ids=["read_only", "ai_external"])
    def narrowed(self, request, client, admin_headers, supervisor):
        _id, token = issue_token(
            client, admin_headers, supervisor, scope="read_only", ai_external=request.param, label="narrowed probe"
        )
        return bearer(token)

    def test_every_routing_verb(self, client, db_session, supervisor, three_step, narrowed):
        wo, (op10, op20, op30) = three_step
        assert _skip(client, db_session, op30, user_headers(supervisor)).status_code == 200
        before = _rows(db_session, [op10, op20, op30])
        audit_rows = _routing_audit_count(db_session, [op10, op20, op30])
        assert _skip(client, db_session, op10, narrowed).status_code == 403
        assert _unskip(client, db_session, op30, narrowed).status_code == 403
        body = _add_body(make_work_center(db_session), 40, change_reason="x")
        assert client.post(add_url(wo), headers=narrowed, json=body).status_code == 403
        resp = client.post(reseq_url(wo), headers=narrowed, json=_reseq_body(db_session, [(op20, 40)]))
        assert resp.status_code == 403
        resp = client.put(
            put_url(op20),
            headers=narrowed,
            json={"version": _version(db_session, op20), "name": "Renamed", "change_reason": "x"},
        )
        assert resp.status_code == 403
        assert _rows(db_session, [op10, op20, op30]) == before
        assert _op_count(db_session, wo) == 3
        assert _routing_audit_count(db_session, [op10, op20, op30]) == audit_rows

    def test_even_a_draft_add_and_a_work_center_move(self, client, db_session, narrowed):
        """The doors a FULL token keeps (a DRAFT add, a work-center move) stay shut to a narrowed one."""
        draft, (draft_op,) = make_routing(
            db_session, work_centers=[make_work_center(db_session)], statuses=[PENDING], wo_status=WorkOrderStatus.DRAFT
        )
        resp = client.post(add_url(draft), headers=narrowed, json=_add_body(make_work_center(db_session), 20))
        assert resp.status_code == 403
        assert _op_count(db_session, draft) == 1
        _, (released_op, _) = make_routing(db_session, work_centers=[make_work_center(db_session) for _ in range(2)])
        old_wc = released_op.work_center_id
        resp = client.put(
            put_url(released_op),
            headers=narrowed,
            json={"version": _version(db_session, released_op), "work_center_id": make_work_center(db_session).id},
        )
        assert resp.status_code == 403
        assert fresh(db_session, WorkOrderOperation, released_op.id).work_center_id == old_wc

    def test_a_full_token_is_refused_with_the_routing_text_on_resequence(
        self, client, db_session, admin_headers, supervisor, three_step
    ):
        from app.core.credentials import API_TOKEN_ROUTING_CHANGE

        wo, (op10, op20, op30) = three_step
        before = _rows(db_session, [op10, op20, op30])
        resp = client.post(
            reseq_url(wo),
            headers=_full_token(client, admin_headers, supervisor),
            json=_reseq_body(db_session, [(op20, 30), (op30, 20)]),
        )
        assert resp.status_code == 403 and resp.json()["detail"] == API_TOKEN_ROUTING_CHANGE
        assert _rows(db_session, [op10, op20, op30]) == before


# --------------------------------------------------------------------------- audit evidence


class TestAuditRowsCarryTheEvidence:
    def test_skip_row(self, client, db_session, supervisor, three_step):
        wo, (op10, _, _) = three_step
        assert _skip(client, db_session, op10, user_headers(supervisor), reason="dropped by ECO 9").status_code == 200
        (row,) = [
            r for r in op_audit_rows(db_session, op10.id) if r.extra_data.get("transition") == "operation_skipped"
        ]
        assert row.action == "STATUS_CHANGE"
        assert row.old_values == {"status": "ready"}
        assert row.new_values == {"status": "skipped", "skip_reason": "dropped by ECO 9"}
        assert row.user_id == supervisor.id and row.company_id == wo.company_id
        assert row.extra_data["operation_number"] == "10" and row.extra_data["sequence"] == 10
        assert row.extra_data["work_order_id"] == wo.id
        assert row.extra_data["work_order_status"] == "released"
        assert "dropped by ECO 9" in row.description

    def test_unskip_row_records_what_the_skip_was(self, client, db_session, supervisor, three_step):
        _, (op10, _, _) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers, reason="dropped by ECO 9").status_code == 200
        skipped_at = fresh(db_session, WorkOrderOperation, op10.id).skipped_at
        assert _unskip(client, db_session, op10, headers, reason="ECO 9 reverted").status_code == 200
        (row,) = [
            r for r in op_audit_rows(db_session, op10.id) if r.extra_data.get("transition") == "operation_unskipped"
        ]
        assert row.old_values["status"] == "skipped"
        assert row.old_values["skip_reason"] == "dropped by ECO 9"
        assert row.old_values["skipped_by"] == supervisor.id
        assert row.old_values["skipped_at"] == skipped_at.isoformat()
        assert row.new_values == {"status": "pending"}
        assert row.extra_data["reason"] == "ECO 9 reverted"
        assert row.user_id == supervisor.id

    def test_add_row(self, client, db_session, supervisor, three_step):
        wo, _ = three_step
        wc = make_work_center(db_session)
        resp = client.post(
            add_url(wo), headers=user_headers(supervisor), json=_add_body(wc, 40, change_reason="customer added QC")
        )
        assert resp.status_code == 200, resp.text
        (row,) = [r for r in op_audit_rows(db_session, resp.json()["id"]) if r.action == "CREATE"]
        assert row.new_values["sequence"] == 40
        assert row.new_values["work_center_id"] == wc.id
        assert row.new_values["name"] == "Deburr"
        assert row.extra_data["transition"] == "operation_added"
        assert row.extra_data["work_order_status"] == "released"
        assert row.user_id == supervisor.id

    def test_add_demotion_row_carries_the_reason(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op30) = make_routing(db_session, work_centers=wcs, statuses=[COMPLETE, READY])
        set_op(db_session, op10, quantity_complete=10)
        set_op(db_session, op30, sequence=30)
        resp = client.post(
            add_url(wo),
            headers=user_headers(supervisor),
            json=_add_body(make_work_center(db_session), 20, change_reason="insert deburr"),
        )
        assert resp.status_code == 200, resp.text
        demotions = [r for r in op_audit_rows(db_session, op30.id) if r.new_values == {"status": "pending"}]
        assert len(demotions) == 1
        assert demotions[0].extra_data["reason"] == "insert deburr"
        assert demotions[0].extra_data["added_operation_id"] == resp.json()["id"]

    def test_resequence_rows_one_per_moved_step(self, client, db_session, supervisor, three_step):
        wo, (op10, op20, op30) = three_step
        resp = client.post(
            reseq_url(wo),
            headers=user_headers(supervisor),
            json=_reseq_body(db_session, [(op20, 30), (op30, 20)], reason="paint before weld"),
        )
        assert resp.status_code == 200, resp.text
        for op, old, new in ((op20, 20, 30), (op30, 30, 20)):
            (row,) = [
                r
                for r in op_audit_rows(db_session, op.id)
                if r.extra_data.get("transition") == "operations_resequenced"
            ]
            assert row.old_values == {"sequence": old} and row.new_values == {"sequence": new}
            assert row.extra_data["reason"] == "paint before weld"
            assert row.extra_data["sequence_changes"] == {
                str(op20.id): {"from": 20, "to": 30},
                str(op30.id): {"from": 30, "to": 20},
            }
        assert not [
            r for r in op_audit_rows(db_session, op10.id) if r.extra_data.get("transition") == "operations_resequenced"
        ]

    def test_put_content_row_old_and_new(self, client, db_session, supervisor, three_step):
        _, (_, op20, _) = three_step
        set_op(db_session, op20, run_instructions="torque to spec")
        resp = client.put(
            put_url(op20),
            headers=user_headers(supervisor),
            json={
                "version": _version(db_session, op20),
                "run_instructions": "torque to 40 Nm",
                "change_reason": "ECO 41",
            },
        )
        assert resp.status_code == 200, resp.text
        (row,) = [
            r
            for r in op_audit_rows(db_session, op20.id)
            if r.extra_data.get("transition") == "operation_content_changed"
        ]
        assert row.old_values["run_instructions"] == "torque to spec"
        assert row.new_values["run_instructions"] == "torque to 40 Nm"
        assert row.extra_data["fields"] == ["run_instructions"]
        assert row.extra_data["reason"] == "ECO 41"
        assert row.user_id == supervisor.id


# --------------------------------------------------------------------------- the recorded-work gate is the WO's own rule


class TestRestoreAsksTheWorkOrdersOwnGate:
    def test_restore_in_front_of_a_completed_step_is_refused(self, client, db_session, supervisor, three_step):
        """COMPLETE is recorded work: a restored step may not land in front of it (it would
        become an unfinished predecessor of a completed stage on a sequenced routing)."""
        _, (op10, op20, op30) = three_step
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers).status_code == 200
        set_op(db_session, fresh(db_session, WorkOrderOperation, op20.id), status=COMPLETE, quantity_complete=10)
        before = _rows(db_session, [op10, op20, op30])
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]
        assert _rows(db_session, [op10, op20, op30]) == before

    def test_a_pooled_same_machine_step_does_not_block_so_the_restore_is_allowed(self, client, db_session, supervisor):
        """On a POOLED work order the gate waives a same-work-center predecessor, so restoring
        a step in front of running work at the SAME machine is allowed -- the routing verbs ask
        the work order's own gate rather than a stricter copy of it."""
        wc = make_work_center(db_session)
        wo, (op10, op20) = make_routing(db_session, work_centers=[wc, wc], sequential=False, statuses=[READY, READY])
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers).status_code == 200
        add_entry(db_session, make_user(db_session), fresh(db_session, WorkOrderOperation, op20.id), open_entry=True)
        set_op(db_session, fresh(db_session, WorkOrderOperation, op20.id), status=IN_PROGRESS)
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 200, resp.text
        assert [r[0] for r in _rows(db_session, [op10, op20])] == [READY, IN_PROGRESS]

    def test_a_pooled_step_at_another_machine_does_block(self, client, db_session, supervisor):
        wcs = [make_work_center(db_session) for _ in range(2)]
        wo, (op10, op20) = make_routing(db_session, work_centers=wcs, sequential=False, statuses=[READY, PENDING])
        headers = user_headers(supervisor)
        assert _skip(client, db_session, op10, headers).status_code == 200
        add_entry(db_session, make_user(db_session), fresh(db_session, WorkOrderOperation, op20.id), open_entry=True)
        set_op(db_session, fresh(db_session, WorkOrderOperation, op20.id), status=IN_PROGRESS)
        resp = _unskip(client, db_session, op10, headers)
        assert resp.status_code == 409 and "recorded work" in resp.json()["detail"]
        assert [r[0] for r in _rows(db_session, [op10, op20])] == [SKIPPED, IN_PROGRESS]


class TestASkipFreesTheCapacityReservation:
    def test_scheduled_dates_are_cleared(self, client, db_session, supervisor, three_step):
        from datetime import datetime

        _, (_, _, op30) = three_step
        start = datetime(2026, 12, 1, 14, 0)
        set_op(db_session, op30, scheduled_start=start, scheduled_end=start + timedelta(hours=2))
        assert _skip(client, db_session, op30, user_headers(supervisor)).status_code == 200
        row = fresh(db_session, WorkOrderOperation, op30.id)
        assert row.scheduled_start is None and row.scheduled_end is None
