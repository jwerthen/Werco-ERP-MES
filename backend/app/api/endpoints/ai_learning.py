from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User, UserRole
from app.schemas.ai_learning import (
    AIAggregationSummary,
    AIInteractionEventCreate,
    AIInteractionEventResponse,
    AIOutcomeCreate,
    AIOutcomeResponse,
    AIRecommendationActionRequest,
    AIRecommendationCreate,
    AIRecommendationFeedbackRequest,
    AIRecommendationResponse,
)
from app.services.ai_context_service import AIContextService
from app.services.ai_governance_service import AIGovernanceService
from app.services.ai_learning_service import AILearningService

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
    status: Optional[str] = Query("pending", pattern="^(pending|accepted|dismissed|stale)$"),
    source_module: Optional[str] = Query(None, max_length=80),
    target_entity_type: Optional[str] = Query(None, max_length=80),
    target_entity_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Return suggest-only AI recommendations for embedded copilots and the Action Inbox."""
    return AILearningService(db).list_recommendations(
        company_id=company_id,
        status=status,
        source_module=source_module,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        limit=limit,
    )


@router.post("/recommendations", response_model=AIRecommendationResponse)
def create_ai_recommendation(
    data: AIRecommendationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Create a suggest-only recommendation. Intended for admin and service workflows."""
    if current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR} and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    recommendation = AILearningService(db).create_recommendation(company_id=company_id, user=current_user, data=data)
    db.commit()
    db.refresh(recommendation)
    return recommendation


@router.post("/recommendations/{recommendation_id}/accept", response_model=AIRecommendationResponse)
def accept_ai_recommendation(
    recommendation_id: int,
    data: Optional[AIRecommendationActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Mark a recommendation accepted without applying changes to controlled records."""
    service = AILearningService(db)
    try:
        recommendation = service.set_recommendation_status(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            status="accepted",
            reason=data.reason if data else None,
        )
        db.commit()
        db.refresh(recommendation)
        return recommendation
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/recommendations/{recommendation_id}/dismiss", response_model=AIRecommendationResponse)
def dismiss_ai_recommendation(
    recommendation_id: int,
    data: Optional[AIRecommendationActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Dismiss a recommendation and feed the reason back into learning telemetry."""
    service = AILearningService(db)
    try:
        recommendation = service.set_recommendation_status(
            recommendation_id=recommendation_id,
            company_id=company_id,
            user=current_user,
            status="dismissed",
            reason=data.reason if data else None,
        )
        db.commit()
        db.refresh(recommendation)
        return recommendation
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
    """Run learning aggregation for the active tenant. Useful for admin verification."""
    if current_user.role not in {UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR} and not current_user.is_superuser:
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
