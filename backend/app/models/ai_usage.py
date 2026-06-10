"""Tenant-scoped telemetry for every Anthropic API call made by the platform.

AIUsageEvent rows are written by ``app.services.llm_client.run_llm_task`` on a
short-lived dedicated session (fire-and-forget). They power the
``/api/v1/ai-usage/summary`` cost/latency dashboard and are intentionally NOT
audit records: no AuditService involvement, no controlled-record mutation.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, Numeric, String

from app.db.database import Base
from app.db.mixins import TenantMixin


class AIUsageEvent(Base, TenantMixin):
    """One row per LLM API call: tokens, estimated cost, latency, outcome."""

    __tablename__ = "ai_usage_events"
    __table_args__ = (Index("ix_ai_usage_company_task_created", "company_id", "task", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)

    # What ran
    task = Column(String(80), nullable=False, index=True)  # e.g. po_extraction, routing_generation
    model = Column(String(120), nullable=False)  # exact model id used
    tier = Column(String(20), nullable=True)  # fast | default | reasoning
    feature = Column(String(120), nullable=True)  # product surface, e.g. po_upload
    prompt_version = Column(String(120), nullable=True)  # from app.services.prompts registry

    # Usage / cost
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Numeric(12, 6), nullable=True)  # NULL when model not in price table
    latency_ms = Column(Integer, nullable=True)

    # Outcome
    success = Column(Boolean, nullable=False, default=True)
    error_type = Column(String(120), nullable=True)  # exception class name on failure

    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
