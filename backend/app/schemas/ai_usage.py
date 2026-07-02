"""Response contracts for the AI usage telemetry endpoints."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.base import UTCModel


class AIUsageAggregate(BaseModel):
    """Aggregated usage for one bucket (a task, a model, or the whole window)."""

    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    estimated_cost_usd: Optional[float] = None  # None when no priced calls in the bucket
    avg_latency_ms: Optional[float] = None
    error_rate: float  # failed calls / total calls, 0.0-1.0


class AIUsageTaskSummary(AIUsageAggregate):
    task: str


class AIUsageModelSummary(AIUsageAggregate):
    model: str


class AIUsageSummaryResponse(UTCModel):
    window_days: int
    since: datetime
    totals: AIUsageAggregate
    by_task: List[AIUsageTaskSummary]
    by_model: List[AIUsageModelSummary]
