from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class AICorrectionResponse(BaseModel):
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


class AIInteractionEventResponse(BaseModel):
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


class AIRecommendationResponse(BaseModel):
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

    class Config:
        from_attributes = True


class AIRecommendationActionRequest(BaseModel):
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


class AIOutcomeResponse(BaseModel):
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
