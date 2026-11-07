import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_audit_service, get_current_company_id, get_current_user
from app.api.endpoints.work_order_blockers import _broadcast_blocker
from app.core.realtime import safe_broadcast
from app.core.websocket import broadcast_dashboard_update, broadcast_work_order_update
from app.db.database import get_db
from app.db.tenant_filter import tenant_query
from app.models.user import User, UserRole
from app.models.work_order_blocker import WorkOrderBlocker
from app.schemas.ai_learning import (
    AIAggregationSummary,
    AIInteractionEventCreate,
    AIInteractionEventResponse,
    AIOutcomeCreate,
    AIOutcomeResponse,
    AIRecommendationAcceptRequest,
    AIRecommendationActionRequest,
    AIRecommendationApplyRequest,
    AIRecommendationApplyResponse,
    AIRecommendationCreate,
    AIRecommendationFeedbackRequest,
    AIRecommendationResponse,
    AIRecommendationSnoozeRequest,
)
from app.services import llm_client
from app.services.ai_action_applier import (
    ALREADY_DECIDED_ACCEPT_DETAIL,
    ALREADY_DECIDED_DISMISS_DETAIL,
    AIActionApplier,
    AIActionApplyError,
    annotate_apply_actions,
    apply_refusal,
    mark_handled_refusal,
    plan_for_recommendation,
)
from app.services.ai_context_service import AIContextService
from app.services.ai_governance_service import AIGovernanceService
from app.services.ai_learning_service import AILearningService, RecommendationStateError
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/events", response_model=AIInteractionEventResponse)
def record_ai_event(
    data: AIInteractionEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Capture a tenant-scoped AI/user interaction signal for product learning."""
    service = AILearningService(db)
    try:
        event = service.record_interaction(company_id=company_id, user=current_user, data=data)
        db.commit()
        db.refresh(event)
        return event
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/recommendations", response_model=List[AIRecommendationResponse])
def list_ai_recommendations(
    status: Optional[str] = Query("pending", pattern="^(pending|accepted|dismissed|stale|snoozed)$"),
    source_module: Optional[str] = Query(None, max_length=80),
    target_entity_type: Optional[str] = Query(None, max_length=80),
    target_entity_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """List AI recommendations for the Action Inbox and the embedded copilots.

    Each row carries ``target_href`` (a server-computed link to the record it is about)
    and, for a pending action card, ``apply_action``: what a person's Accept & apply
    would do right now, its ``fingerprint``, and whether this caller may apply it or
    mark it handled. Listing never changes anything.
    """
    service = AILearningService(db)
    recommendations = service.list_recommendations(
        company_id=company_id,
        status=status,
        source_module=source_module,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        limit=limit,
    )
    hrefs = service.target_hrefs(company_id, recommendations)
    for recommendation in recommendations:
        setattr(recommendation, "target_href", hrefs.get(recommendation.id))
    annotate_apply_actions(
        db,
        company_id,
        recommendations,
        current_user,
        lambda: llm_client.ai_egress_allowed(company_id),
    )
    return recommendations


@router.post("/recommendations", response_model=AIRecommendationResponse)
def create_ai_recommendation(
    data: AIRecommendationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create an Action Inbox recommendation. Intended for admin and service workflows.

    Creating one changes nothing else. ``suggested_action.autonomy`` must be
    ``suggest_only`` or ``apply_on_accept`` (422 otherwise; there is no auto tier), and
    even an ``apply_on_accept`` card runs only when a person clicks Accept & apply.
    """
    if (
        current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR}
        and not current_user.is_superuser
    ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    recommendation = AILearningService(db).create_recommendation(company_id=company_id, user=current_user, data=data)
    db.commit()
    db.refresh(recommendation)
    return recommendation


@router.post("/recommendations/{recommendation_id}/accept", response_model=AIRecommendationApplyResponse)
def accept_ai_recommendation(
    recommendation_id: int,
    data: Optional[AIRecommendationAcceptRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Record that you accepted an AI recommendation. Changes nothing else; never runs its action.

    Only a pending recommendation can be accepted (409 otherwise). On an action card --
    a pending recommendation with an allowlisted action -- accepting ("Mark handled")
    needs a person signed in whose role could apply that action (403 for API tokens,
    agents and lower roles). ``apply=true`` is refused with 422: running the action is
    a person's Accept & apply in the app, which is not available to agents.
    """
    service = AILearningService(db)
    try:
        recommendation = service.get_recommendation(recommendation_id, company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    refusal = mark_handled_refusal(recommendation, current_user)
    if refusal is not None:
        raise HTTPException(status_code=refusal.status_code, detail=refusal.detail)

    try:
        recommendation = service.set_recommendation_status(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            status="accepted",
            expected_statuses={"pending"},
            reason=data.reason if data else None,
        )
    except RecommendationStateError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=ALREADY_DECIDED_ACCEPT_DETAIL.format(status=exc.current_status)
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    db.refresh(recommendation)
    return AIRecommendationApplyResponse(recommendation=recommendation, applied=False)


def _broadcast_applied(db: Session, *, company_id: int, action_type: str, result: Dict[str, Any]) -> None:
    """After-commit, company-scoped nudges so open Work Order / Dashboard / Shop Floor screens refetch."""
    if action_type in {"escalate_blocker", "acknowledge_blocker"} and result.get("blocker_id"):
        blocker = (
            tenant_query(db, WorkOrderBlocker, company_id).filter(WorkOrderBlocker.id == result["blocker_id"]).first()
        )
        if blocker is not None:
            _broadcast_blocker(blocker, "work_order_blocker_updated", company_id=company_id)
    work_order_id = result.get("work_order_id")
    if result.get("new_priority") is not None and work_order_id:
        payload = {
            "event": "work_order_priority_updated",
            "work_order_id": work_order_id,
            "priority": result["new_priority"],
            "reason": "ai_recommendation",
        }
        safe_broadcast(broadcast_work_order_update, work_order_id, payload, company_id=company_id)
        safe_broadcast(broadcast_dashboard_update, payload, company_id=company_id)
    if action_type in {"create_draft_po", "create_draft_ncr"}:
        safe_broadcast(
            broadcast_dashboard_update,
            {
                "event": "ai_recommendation_applied",
                "action_type": action_type,
                "purchase_order_id": result.get("purchase_order_id"),
                "ncr_id": result.get("ncr_id"),
            },
            company_id=company_id,
        )


@router.post("/recommendations/{recommendation_id}/apply", response_model=AIRecommendationApplyResponse)
def apply_ai_recommendation(
    recommendation_id: int,
    data: AIRecommendationApplyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Accept & apply: a signed-in person runs the recommendation's allowlisted action.

    The ONLY executor of an AI recommendation (owner ruling 3, 2026-11-05: nothing
    AI-driven executes without a named person clicking Accept). Not an MCP tool and
    refused for API tokens. Gates, in order: pending (409); has an action to apply
    (409); a person's interactive session (403); not a read-only company context (403);
    company AI egress on (403); the person's role can perform the action (403); the
    target still makes sense (409, the plan's reason); and ``expected_fingerprint`` --
    the list's ``apply_action.fingerprint`` -- still matches what would run (409).
    The recommendation is claimed before any write and the action runs as the clicking
    person in the same transaction; any refusal rolls all of it back.
    """
    service = AILearningService(db)
    try:
        recommendation = service.get_recommendation(recommendation_id, company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    refusal = apply_refusal(
        recommendation,
        current_user,
        egress_allowed=lambda: llm_client.ai_egress_allowed(company_id),
        summary=lambda: plan_for_recommendation(db, company_id, recommendation).summary,
    )
    if refusal is not None:
        raise HTTPException(status_code=refusal.status_code, detail=refusal.detail)

    # Claim the recommendation BEFORE any ERP write: a compare-and-set from pending, so a
    # losing double-click (or a concurrent accept/dismiss) never reaches a handler.
    try:
        recommendation = service.set_recommendation_status(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            status="accepted",
            expected_statuses={"pending"},
            reason=data.reason,
            event_note="Accepted with apply: the allowlisted action ran in this transaction as the accepting user.",
        )
    except RecommendationStateError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=ALREADY_DECIDED_ACCEPT_DETAIL.format(status=exc.current_status)
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    applier = AIActionApplier(db, company_id=company_id, user=current_user, audit=audit)
    try:
        apply_result = applier.apply(recommendation, expected_fingerprint=data.expected_fingerprint)
    except AIActionApplyError as exc:
        # A blocked plan, a changed fingerprint, or a refused handler: nothing ran, and
        # the claim is undone with it -- the card stays pending for the person to review.
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        # Anything else (e.g. a concurrent work-order edit's StaleDataError, which the
        # app-wide handler maps to 409) rolls back the claim and propagates.
        db.rollback()
        raise

    action_type = str(apply_result.get("action_type") or "")
    service.record_interaction(
        company_id=company_id,
        user=current_user,
        data=AIInteractionEventCreate(
            event_type="accepted",
            source_module=recommendation.source_module,
            ai_feature=recommendation.recommendation_type,
            entity_type=recommendation.target_entity_type,
            entity_id=recommendation.target_entity_id,
            recommendation_id=recommendation.id,
            context_summary=f"Applied allowlisted AI action: {apply_result.get('summary') or action_type}",
            event_payload={"applied": True, "result": apply_result},
            confidence_score=recommendation.confidence_score,
        ),
    )
    service.supersede_open_siblings(recommendation, current_user)
    db.commit()
    db.refresh(recommendation)

    # The apply is COMMITTED: a post-commit nudge must never turn it into an error
    # response (the client would say "Nothing was changed" and a retry would 409).
    try:
        _broadcast_applied(db, company_id=company_id, action_type=action_type, result=apply_result)
    except Exception:
        logger.warning(
            "Post-commit broadcast failed after applying AI recommendation %s", recommendation_id, exc_info=True
        )
    return AIRecommendationApplyResponse(recommendation=recommendation, applied=True, apply_result=apply_result)


@router.post("/recommendations/{recommendation_id}/dismiss", response_model=AIRecommendationResponse)
def dismiss_ai_recommendation(
    recommendation_id: int,
    data: Optional[AIRecommendationActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Dismiss a pending or snoozed recommendation and feed the reason back into learning telemetry."""
    service = AILearningService(db)
    try:
        recommendation = service.set_recommendation_status(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            status="dismissed",
            expected_statuses={"pending", "snoozed"},
            reason=data.reason if data else None,
        )
        db.commit()
        db.refresh(recommendation)
        return recommendation
    except RecommendationStateError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=ALREADY_DECIDED_DISMISS_DETAIL.format(status=exc.current_status)
        ) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/recommendations/{recommendation_id}/snooze", response_model=AIRecommendationResponse)
def snooze_ai_recommendation(
    recommendation_id: int,
    data: AIRecommendationSnoozeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Snooze a pending recommendation; the nightly learning sweep returns it to pending."""
    service = AILearningService(db)
    try:
        recommendation = service.snooze_recommendation(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            days=data.days,
            reason=data.reason,
        )
        db.commit()
        db.refresh(recommendation)
        return recommendation
    except RecommendationStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/recommendations/{recommendation_id}/feedback", response_model=AIInteractionEventResponse)
def record_ai_recommendation_feedback(
    recommendation_id: int,
    data: AIRecommendationFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Attach user feedback to a recommendation without changing its target entity."""
    service = AILearningService(db)
    try:
        event = service.record_feedback(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            data=data,
        )
        db.commit()
        db.refresh(event)
        return event
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/outcomes", response_model=AIOutcomeResponse)
def record_ai_outcome(
    data: AIOutcomeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Capture downstream outcomes such as actual cost, delivery, scrap, or quote result."""
    service = AILearningService(db)
    try:
        outcome = service.record_outcome(company_id=company_id, user=current_user, data=data)
        db.commit()
        db.refresh(outcome)
        return outcome
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/aggregate", response_model=AIAggregationSummary)
def aggregate_ai_learning(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Run the AI learning sweep for the active company: mints and sweeps recommendations.

    Never applies anything and makes no LLM call: it expires, wakes and retires cards
    and mints new ones from sensors and learners. Every card waits for a person.
    """
    if (
        current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR}
        and not current_user.is_superuser
    ):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    summary = AILearningService(db).aggregate_learning_signals(company_ids=[company_id])
    db.commit()
    return summary


@router.get("/context")
def get_ai_context(
    entity_type: Optional[str] = Query(None, max_length=80),
    entity_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return minimized tenant-scoped context for embedded copilots."""
    try:
        return AIContextService(db).compact_entity_context(
            company_id=company_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/governance")
def get_ai_governance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Expose current AI autonomy rules, approval posture, and trust metrics."""
    return AIGovernanceService(db).governance_snapshot(company_id=company_id)
