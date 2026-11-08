"""``python -m scripts.seed_data`` stamps a release record on every non-DRAFT work order.

Resume Work Order (``POST /work-orders/{id}/resume``) decides where a held job goes back to
from its RELEASE RECORD (``released_at`` / ``released_by``): with none it lands DRAFT. The
seed created its released / in-progress jobs as raw rows with no stamp, so in every seeded
environment -- the demo, and the E2E suite (CLAUDE.md: Playwright "needs a seeded backend")
-- Hold -> Resume took the restrictive no_release_record branch, and on WO-...-001 produced
a DRAFT job with live work on it. Run the real seeder against this worker's test database
and pin the stamp and the restore target it now yields.
"""

import pytest
from sqlalchemy.orm import sessionmaker

import scripts.seed_data as seed_data
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.services.work_order_state_service import _worked_operation_ids, work_order_hold_restore_target

pytestmark = [pytest.mark.requires_db]


def _seed(db_session, monkeypatch) -> None:
    bind = db_session.get_bind()
    monkeypatch.setattr(seed_data, "engine", bind)
    monkeypatch.setattr(seed_data, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=bind))
    seed_data.seed_database()
    db_session.expire_all()


def test_seeded_non_draft_work_orders_carry_the_seed_admins_release_and_drafts_carry_none(db_session, monkeypatch):
    _seed(db_session, monkeypatch)

    admin = db_session.query(User).filter(User.email == "admin@werco.com").one()
    seeded = {
        wo.work_order_number: wo
        for wo in db_session.query(WorkOrder).filter(WorkOrder.work_order_number.like("WO-20260102-%")).all()
    }
    assert set(seeded) == {"WO-20260102-001", "WO-20260102-002", "WO-20260102-003", "WO-20260102-004"}
    for wo in seeded.values():
        if wo.status == WorkOrderStatus.DRAFT:
            assert wo.released_by is None and wo.released_at is None, wo.work_order_number
        else:
            assert wo.released_by == admin.id, wo.work_order_number
            assert wo.released_at is not None, wo.work_order_number
    # At least one of each, or the loop above proves nothing.
    assert {wo.status for wo in seeded.values()} >= {WorkOrderStatus.DRAFT, WorkOrderStatus.RELEASED}


@pytest.mark.parametrize(
    "number, expected",
    [
        ("WO-20260102-001", (WorkOrderStatus.IN_PROGRESS, "release_record_with_work")),
        ("WO-20260102-002", (WorkOrderStatus.RELEASED, "release_record")),
    ],
)
def test_a_held_seeded_job_resumes_to_the_floor_not_to_draft(db_session, monkeypatch, number, expected):
    _seed(db_session, monkeypatch)
    wo = db_session.query(WorkOrder).filter(WorkOrder.work_order_number == number).one()
    operations = db_session.query(WorkOrderOperation).filter(WorkOrderOperation.work_order_id == wo.id).all()

    target = work_order_hold_restore_target(
        wo, operations, _worked_operation_ids(db_session, wo.company_id, operations)
    )

    assert target == expected
