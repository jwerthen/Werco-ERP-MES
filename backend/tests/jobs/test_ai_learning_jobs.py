"""The nightly AI job mints and sweeps; it never applies (owner ruling 3, 2026-11-05).

Pins the stale-blocker escalation rules: at most one escalation card per blocker
(pending, snoozed, accepted or dismissed all suppress a re-mint and its critical
notification), no card when escalating would change nothing, none for a finished job,
and a warning when a retired auto-execute env var is still set.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import app.jobs.ai_learning_jobs as ai_learning_jobs
from app.models.ai_learning import AIRecommendation
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker
from app.services.ai_learning_service import AILearningService

pytestmark = [pytest.mark.unit, pytest.mark.requires_db]


@pytest.fixture
def run_job(db_session: Session, monkeypatch):
    """Point the job's own ``SessionLocal`` at the test session (and keep it open)."""
    monkeypatch.setattr(ai_learning_jobs, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    for name in ai_learning_jobs.RETIRED_AUTO_EXECUTE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def _run() -> dict:
        return asyncio.run(ai_learning_jobs.aggregate_ai_learning_task())

    return _run


def _stale_blocker(
    db: Session,
    number: str,
    *,
    wo_status=WorkOrderStatus.IN_PROGRESS,
    priority: int = 5,
    blocker_status: str = "open",
    severity: str = "medium",
) -> WorkOrderBlocker:
    part = Part(
        part_number=f"P-{number}",
        name=f"Part {number}",
        part_type="manufactured",
        unit_of_measure="each",
        is_active=True,
        company_id=1,
    )
    db.add(part)
    db.flush()
    work_order = WorkOrder(
        work_order_number=number,
        part_id=part.id,
        quantity_ordered=1,
        status=wo_status,
        priority=priority,
        company_id=1,
    )
    db.add(work_order)
    db.flush()
    blocker = WorkOrderBlocker(
        company_id=1,
        work_order_id=work_order.id,
        title="Waiting on steel",
        status=blocker_status,
        severity=severity,
        reported_at=datetime.utcnow() - timedelta(hours=48),
    )
    db.add(blocker)
    db.commit()
    db.refresh(blocker)
    return blocker


def _escalation_cards(db: Session, blocker: WorkOrderBlocker):
    return (
        db.query(AIRecommendation)
        .filter(
            AIRecommendation.recommendation_type == "stale_blocker_escalation",
            AIRecommendation.target_entity_id == blocker.id,
        )
        .order_by(AIRecommendation.id)
        .all()
    )


def _escalated_events(db: Session, blocker: WorkOrderBlocker) -> int:
    return (
        db.query(OperationalEvent)
        .filter(OperationalEvent.event_type == "work_order_blocker_escalated", OperationalEvent.entity_id == blocker.id)
        .count()
    )


def _existing_card(db: Session, blocker: WorkOrderBlocker, status_: str = "pending") -> AIRecommendation:
    rec = AIRecommendation(
        company_id=1,
        source_module="shop_floor",
        recommendation_type="stale_blocker_escalation",
        status=status_,
        title="Escalate stale blocker",
        summary="Open for more than 24 hours.",
        target_entity_type="work_order_blocker",
        target_entity_id=blocker.id,
        suggested_action={"type": "escalate_blocker", "blocker_id": blocker.id, "autonomy": "apply_on_accept"},
        confidence_score=0.82,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def test_stale_blocker_mints_one_apply_on_accept_card_and_never_applies(db_session: Session, run_job):
    blocker = _stale_blocker(db_session, "WO-JOB-MINT", priority=5)

    summary = run_job()

    assert summary["stale_blockers_escalated"] == 1
    assert "auto_executed" not in summary
    (card,) = _escalation_cards(db_session, blocker)
    assert card.status == "pending" and card.accepted_by is None
    assert card.suggested_action["autonomy"] == "apply_on_accept"
    assert _escalated_events(db_session, blocker) == 1
    db_session.refresh(blocker)
    assert blocker.status == "open"  # minted, never applied
    assert db_session.query(WorkOrder).filter(WorkOrder.id == blocker.work_order_id).one().priority == 5

    # The pending card suppresses a re-mint and a second critical notification.
    assert run_job()["stale_blockers_escalated"] == 0
    assert len(_escalation_cards(db_session, blocker)) == 1
    assert _escalated_events(db_session, blocker) == 1


def test_snoozed_stale_rec_suppresses_remint_and_event(db_session: Session, admin_user, run_job):
    blocker = _stale_blocker(db_session, "WO-JOB-SNOOZE")
    card = _existing_card(db_session, blocker)
    AILearningService(db_session).snooze_recommendation(
        recommendation_id=card.id, company_id=1, user=admin_user, days=7
    )
    db_session.commit()

    assert run_job()["stale_blockers_escalated"] == 0
    assert len(_escalation_cards(db_session, blocker)) == 1
    assert _escalated_events(db_session, blocker) == 0
    db_session.refresh(card)
    assert card.status == "snoozed"


@pytest.mark.parametrize("decided", ["accepted", "dismissed"])
def test_accepted_or_dismissed_stale_rec_suppresses_remint(db_session: Session, run_job, decided: str):
    blocker = _stale_blocker(db_session, f"WO-JOB-{decided.upper()}")
    _existing_card(db_session, blocker, status_=decided)

    assert run_job()["stale_blockers_escalated"] == 0
    assert len(_escalation_cards(db_session, blocker)) == 1
    assert _escalated_events(db_session, blocker) == 0


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_acknowledged_high_blocker_with_p2_wo_is_not_escalated(db_session: Session, run_job, severity: str):
    blocker = _stale_blocker(
        db_session, f"WO-JOB-ACK-{severity}", priority=2, blocker_status="acknowledged", severity=severity
    )

    assert run_job()["stale_blockers_escalated"] == 0
    assert _escalation_cards(db_session, blocker) == []
    assert _escalated_events(db_session, blocker) == 0


def test_terminal_wo_blocker_not_escalated(db_session: Session, run_job):
    blocker = _stale_blocker(db_session, "WO-JOB-DONE", wo_status=WorkOrderStatus.COMPLETE)

    assert run_job()["stale_blockers_escalated"] == 0
    assert _escalation_cards(db_session, blocker) == []
    assert _escalated_events(db_session, blocker) == 0


def test_retired_auto_execute_env_warns(monkeypatch, caplog):
    for name in ai_learning_jobs.RETIRED_AUTO_EXECUTE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.WARNING, logger=ai_learning_jobs.logger.name):
        ai_learning_jobs._warn_retired_auto_execute_env()
    assert not [r for r in caplog.records if "retired setting" in r.getMessage()]

    monkeypatch.setenv("AI_AUTO_EXECUTE_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_AUTO_EXECUTE_MODEL", "claude-haiku")
    with caplog.at_level(logging.WARNING, logger=ai_learning_jobs.logger.name):
        ai_learning_jobs._warn_retired_auto_execute_env()
    messages = [r.getMessage() for r in caplog.records if "retired setting" in r.getMessage()]
    assert len(messages) == 1
    assert "AI_AUTO_EXECUTE_ENABLED" in messages[0] and "ANTHROPIC_AUTO_EXECUTE_MODEL" in messages[0]
    assert "Delete these variables" in messages[0]
