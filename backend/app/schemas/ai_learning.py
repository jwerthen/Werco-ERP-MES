from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.ai_learning import CANONICAL_AUTONOMY_TIERS
from app.schemas.base import UTCModel

AUTONOMY_REFUSED_DETAIL = (
    "suggested_action.autonomy must be suggest_only or apply_on_accept; there is no auto-execute tier."
)
APPLY_FLAG_REFUSED_DETAIL = (
    "apply is not accepted here: Accept only records your decision. To run the action, a person uses "
    "Accept & apply (POST /ai/recommendations/{id}/apply)."
)


class AIEventType(str, Enum):
    SUGGESTION_SHOWN = "suggestion_shown"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"
    IGNORED = "ignored"
    FEEDBACK = "feedback"
    OUTCOME_OBSERVED = "outcome_observed"


class AIRecommendationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    STALE = "stale"
    SNOOZED = "snoozed"


class AIRecommendationPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AICorrectionCreate(BaseModel):
    field_path: str = Field(..., min_length=1, max_length=255)
    proposed_value: Optional[Any] = None
    final_value: Optional[Any] = None
    correction_reason: Optional[str] = None
    confidence_score: Optional[float] = Field(None, ge=0, le=1)


class AICorrectionResponse(UTCModel):
    id: int
    event_id: Optional[int] = None
    recommendation_id: Optional[int] = None
    source_module: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    field_path: str
    proposed_value: Optional[Any] = None
    final_value: Optional[Any] = None
    correction_reason: Optional[str] = None
    confidence_score: Optional[float] = None
    created_by: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AIInteractionEventCreate(BaseModel):
    event_type: AIEventType
    source_module: str = Field(..., min_length=1, max_length=80)
    ai_feature: Optional[str] = Field(None, max_length=120)
    surface: Optional[str] = Field(None, max_length=120)
    entity_type: Optional[str] = Field(None, max_length=80)
    entity_id: Optional[int] = None
    recommendation_id: Optional[int] = None
    context_summary: Optional[str] = Field(None, max_length=4000)
    event_payload: Optional[Dict[str, Any]] = None
    confidence_score: Optional[float] = Field(None, ge=0, le=1)
    prompt_version: Optional[str] = Field(None, max_length=120)
    model_version: Optional[str] = Field(None, max_length=120)
    corrections: List[AICorrectionCreate] = Field(default_factory=list)


class AIInteractionEventResponse(UTCModel):
    id: int
    event_type: str
    source_module: str
    ai_feature: Optional[str] = None
    surface: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    recommendation_id: Optional[int] = None
    context_summary: Optional[str] = None
    event_payload: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: Optional[float] = None
    prompt_version: Optional[str] = None
    model_version: Optional[str] = None
    created_by: Optional[int] = None
    created_at: datetime
    corrections: List[AICorrectionResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class AIRecommendationCreate(BaseModel):
    source_module: str = Field(..., min_length=1, max_length=80)
    recommendation_type: str = Field(..., min_length=1, max_length=80)
    priority: AIRecommendationPriority = AIRecommendationPriority.MEDIUM
    title: str = Field(..., min_length=1, max_length=255)
    summary: str = Field(..., min_length=1)
    rationale: Optional[str] = None
    target_entity_type: Optional[str] = Field(None, max_length=80)
    target_entity_id: Optional[int] = None
    suggested_action: Optional[Dict[str, Any]] = None
    evidence: Optional[List[Dict[str, Any]]] = None
    impact: Optional[Dict[str, Any]] = None
    confidence_score: float = Field(0.5, ge=0, le=1)
    prompt_version: Optional[str] = Field(None, max_length=120)
    model_version: Optional[str] = Field(None, max_length=120)
    expires_at: Optional[datetime] = None

    @field_validator("suggested_action")
    @classmethod
    def _no_auto_tier(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Owner ruling 3: an agent- or admin-authored card may never carry an auto tier."""
        if value is not None:
            autonomy = value.get("autonomy")
            # isinstance first: an unhashable value (a list, a dict) would make the set
            # lookup raise TypeError, which Pydantic does not turn into a 422.
            if autonomy is not None and (not isinstance(autonomy, str) or autonomy not in CANONICAL_AUTONOMY_TIERS):
                raise ValueError(AUTONOMY_REFUSED_DETAIL)
        return value


BlockedKind = Literal["credential", "read_only", "egress", "role", "target"]


class AIRecommendationApplyAction(BaseModel):
    """What a person's Accept & apply would do RIGHT NOW (list rows only, pending action cards).

    ``summary`` and ``warnings`` are resolved server-side from live records -- never from the
    stored action's labels. ``fingerprint`` must be sent back as ``expected_fingerprint`` on
    ``POST /ai/recommendations/{id}/apply``; a 409 means the plan changed in between.
    ``can_apply`` is true only when nothing blocks the apply. ``can_mark_handled`` says
    whether this person may close the card with a record-only Accept ("Mark handled").
    ``blocked_kind`` is the first refusal: credential | read_only | egress | role | target.
    """

    action_type: str
    summary: str
    warnings: List[str] = Field(default_factory=list)
    fingerprint: str
    can_apply: bool
    can_mark_handled: bool
    blocked_reason: Optional[str] = None
    blocked_kind: Optional[BlockedKind] = None


class AIRecommendationResponse(UTCModel):
    id: int
    source_module: str
    recommendation_type: str
    status: str
    priority: str
    title: str
    summary: str
    rationale: Optional[str] = None
    target_entity_type: Optional[str] = None
    target_entity_id: Optional[int] = None
    suggested_action: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    impact: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: float
    prompt_version: Optional[str] = None
    model_version: Optional[str] = None
    status_reason: Optional[str] = None
    created_by: Optional[int] = None
    accepted_by: Optional[int] = None
    dismissed_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    acted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Deterministic Action Inbox ranking score, computed at list time (never persisted).
    # Only populated by the list endpoint; None on single-recommendation responses.
    score: Optional[float] = None
    # List endpoint only (None elsewhere): the resolved Accept & apply plan for a pending
    # action card, and the server-computed link to the record the card is about.
    apply_action: Optional[AIRecommendationApplyAction] = None
    target_href: Optional[str] = None

    class Config:
        from_attributes = True


class AIRecommendationActionRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class AIRecommendationAcceptRequest(AIRecommendationActionRequest):
    """Record-only Accept. ``apply=true`` is refused (422): applying is its own door."""

    apply: bool = Field(
        False,
        description=(
            "Must be false (true is refused with 422). Accept only records your decision; running the "
            "action is a person's Accept & apply in the Werco app, which agents and API tokens cannot do."
        ),
    )

    @field_validator("apply")
    @classmethod
    def _refuse_apply(cls, value: bool) -> bool:
        if value:
            raise ValueError(APPLY_FLAG_REFUSED_DETAIL)
        return value


class AIRecommendationApplyRequest(BaseModel):
    """Accept & apply. ``expected_fingerprint`` is the list's ``apply_action.fingerprint``."""

    reason: Optional[str] = Field(None, max_length=2000)
    expected_fingerprint: str = Field(..., min_length=8, max_length=64)


class AIRecommendationApplyResponse(UTCModel):
    """Accept (record-only, ``applied`` false) or Accept & apply (``applied`` true) result."""

    recommendation: AIRecommendationResponse
    applied: bool = False
    apply_result: Optional[Dict[str, Any]] = None
    # Always null since ruling 3: a failed apply is a 403/409 that rolls the accept back,
    # never a 200 that records "accepted" while nothing ran. Kept for client compatibility.
    apply_error: Optional[str] = None


class AIRecommendationSnoozeRequest(BaseModel):
    days: int = Field(..., ge=1, le=30)
    reason: Optional[str] = Field(None, max_length=2000)


class AIRecommendationFeedbackRequest(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=4000)
    rating: Optional[int] = Field(None, ge=1, le=5)
    event_payload: Dict[str, Any] = Field(default_factory=dict)


class AIOutcomeCreate(BaseModel):
    recommendation_id: Optional[int] = None
    source_module: str = Field(..., min_length=1, max_length=80)
    outcome_type: str = Field(..., min_length=1, max_length=80)
    entity_type: Optional[str] = Field(None, max_length=80)
    entity_id: Optional[int] = None
    metric_name: Optional[str] = Field(None, max_length=120)
    metric_value: Optional[float] = None
    baseline_value: Optional[float] = None
    target_value: Optional[float] = None
    outcome_payload: Optional[Dict[str, Any]] = None
    observed_at: Optional[datetime] = None


class AIOutcomeResponse(UTCModel):
    id: int
    recommendation_id: Optional[int] = None
    source_module: str
    outcome_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    baseline_value: Optional[float] = None
    target_value: Optional[float] = None
    outcome_payload: Dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime
    created_by: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AIAggregationSummary(BaseModel):
    companies_processed: int
    recommendations_created: int
    stale_recommendations: int
    snoozed_recommendations_woken: int = 0
    sensor_recommendations_created: int = 0
    learner_recommendations_created: int = 0
    # Open cards retired because their blocker closed or their work order finished/was deleted.
    closed_target_recommendations_staled: int = 0
