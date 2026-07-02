from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.models.ai_learning import AICorrection, AIInteractionEvent, AIOutcome, AIRecommendation
from app.models.company import Company
from app.models.user import User
from app.schemas.ai_learning import (
    AICorrectionCreate,
    AIInteractionEventCreate,
    AIOutcomeCreate,
    AIRecommendationCreate,
    AIRecommendationFeedbackRequest,
)

SENSITIVE_KEY_PARTS = {
    "authorization",
    "bearer",
    "cookie",
    "credit_card",
    "cui",
    "document_text",
    "drawing_text",
    "file_path",
    "password",
    "raw_text",
    "secret",
    "ssn",
    "token",
}

MAX_TEXT_LENGTH = 1000
MAX_LIST_ITEMS = 50

# --- Deterministic recommendation scoring (B0.3 Action Inbox) -------------------------------
# score = priority_weight x confidence x age_decay x impact_magnitude, computed at list time.
# No score column exists on purpose: the inputs (age, expiry) change continuously, so the
# value is derived on every read instead of persisted.
PRIORITY_SCORE_WEIGHTS: Dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.35, "info": 0.2}
DEFAULT_PRIORITY_WEIGHT = 0.2  # unknown priorities rank with "info", matching the old else_ ordering
DEFAULT_CONFIDENCE = 0.5
MIN_AGE_DECAY = 0.2  # decay value reached exactly at expires_at
NO_EXPIRY_DECAY_FLOOR = 0.5  # mild decay floor for recommendations without an expiry
NO_EXPIRY_DECAY_WINDOW_DAYS = 30.0  # days to glide from 1.0 down to the no-expiry floor
MIN_IMPACT_MAGNITUDE = 0.25
MAX_IMPACT_MAGNITUDE = 2.0
IMPACT_NUMERIC_KEYS = ("magnitude", "impact_score", "estimated_value", "estimated_savings", "value")
SCORING_FETCH_LIMIT = 500  # pre-ranked candidate window scored in Python before the final sort

SNOOZE_MIN_DAYS = 1
SNOOZE_MAX_DAYS = 30


class RecommendationStateError(Exception):
    """Raised when a recommendation is in a status that does not allow the requested transition."""


def _normalize_status(value: str) -> str:
    return (value or "").strip().lower()


def _as_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize tz-aware datetimes (Postgres) and naive ones (SQLite/utcnow) onto one axis."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _priority_weight(priority: Optional[str]) -> float:
    return PRIORITY_SCORE_WEIGHTS.get(_normalize_status(priority or ""), DEFAULT_PRIORITY_WEIGHT)


def _age_decay(created_at: Optional[datetime], expires_at: Optional[datetime], now: datetime) -> float:
    """1.0 when fresh, declining linearly to MIN_AGE_DECAY at expires_at; mild decay without expiry."""
    created = _as_utc_naive(created_at) or now
    expires = _as_utc_naive(expires_at)
    if expires is not None:
        if now >= expires:
            return MIN_AGE_DECAY
        total_seconds = (expires - created).total_seconds()
        if total_seconds <= 0:
            return 1.0
        elapsed = max((now - created).total_seconds(), 0.0)
        fraction = min(elapsed / total_seconds, 1.0)
        return 1.0 - (1.0 - MIN_AGE_DECAY) * fraction
    age_days = max((now - created).total_seconds(), 0.0) / 86400.0
    decay = 1.0 - (age_days / NO_EXPIRY_DECAY_WINDOW_DAYS) * (1.0 - NO_EXPIRY_DECAY_FLOOR)
    return max(NO_EXPIRY_DECAY_FLOOR, decay)


def _impact_magnitude(impact: Any) -> float:
    """Derive a multiplier from the impact JSON when it carries a numeric magnitude; default 1.0.

    Values in (0, 1] are treated as already-normalized fractions (floored at MIN_IMPACT_MAGNITUDE);
    values > 1 are log-scaled into (1.0, MAX_IMPACT_MAGNITUDE].
    """
    if not isinstance(impact, dict):
        return 1.0
    for key in IMPACT_NUMERIC_KEYS:
        raw = impact.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            # An unusable numeric (0, negative, NaN, inf) falls through to the next
            # candidate key, just like a non-numeric value does.
            continue
        if value <= 1.0:
            return max(MIN_IMPACT_MAGNITUDE, value)
        return min(MAX_IMPACT_MAGNITUDE, 1.0 + math.log10(value) / 3.0)
    return 1.0


def score_recommendation(recommendation: AIRecommendation, *, now: Optional[datetime] = None) -> float:
    """Deterministic Action Inbox ranking score; read-path only, never persisted."""
    current = now or _now()
    weight = _priority_weight(recommendation.priority)
    confidence = _clamp_confidence(recommendation.confidence_score, default=DEFAULT_CONFIDENCE)
    if confidence is None:  # defensive: _clamp_confidence returns the default for None inputs
        confidence = DEFAULT_CONFIDENCE
    decay = _age_decay(recommendation.created_at, recommendation.expires_at, current)
    impact = _impact_magnitude(recommendation.impact)
    return round(weight * confidence * decay * impact, 4)


def _now() -> datetime:
    return datetime.utcnow()


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_learning_payload(value: Any, *, key_hint: str = "") -> Any:
    """Minimize learning payloads before they are stored for product improvement."""
    if key_hint and _is_sensitive_key(key_hint):
        return "[redacted]"

    if isinstance(value, dict):
        return {str(key): redact_learning_payload(item, key_hint=str(key)) for key, item in value.items()}

    if isinstance(value, list):
        return [redact_learning_payload(item) for item in value[:MAX_LIST_ITEMS]]

    if isinstance(value, tuple):
        return [redact_learning_payload(item) for item in value[:MAX_LIST_ITEMS]]

    if isinstance(value, str):
        if len(value) > MAX_TEXT_LENGTH:
            return f"{value[:MAX_TEXT_LENGTH]}...[truncated]"
        return value

    return value


def _clamp_confidence(value: Optional[float], default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    return max(0.0, min(float(value), 1.0))


class AILearningService:
    """Shared AI feedback fabric for suggest-only learning loops."""

    def __init__(self, db: Session):
        self.db = db

    def record_interaction(
        self,
        *,
        company_id: int,
        user: Optional[User],
        data: AIInteractionEventCreate,
    ) -> AIInteractionEvent:
        if data.recommendation_id:
            self._get_recommendation_or_raise(data.recommendation_id, company_id)

        event = AIInteractionEvent(
            company_id=company_id,
            event_type=data.event_type.value,
            source_module=data.source_module,
            ai_feature=data.ai_feature,
            surface=data.surface,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            recommendation_id=data.recommendation_id,
            context_summary=redact_learning_payload(data.context_summary),
            event_payload=redact_learning_payload(data.event_payload),
            confidence_score=_clamp_confidence(data.confidence_score),
            prompt_version=data.prompt_version,
            model_version=data.model_version,
            created_by=user.id if user else None,
        )
        self.db.add(event)
        self.db.flush()

        for correction_data in data.corrections:
            self._create_correction(
                company_id=company_id,
                user=user,
                source_module=data.source_module,
                entity_type=data.entity_type,
                entity_id=data.entity_id,
                event_id=event.id,
                recommendation_id=data.recommendation_id,
                data=correction_data,
            )

        self.db.flush()
        return event

    def create_recommendation(
        self,
        *,
        company_id: int,
        user: Optional[User],
        data: AIRecommendationCreate,
    ) -> AIRecommendation:
        recommendation = AIRecommendation(
            company_id=company_id,
            source_module=data.source_module,
            recommendation_type=data.recommendation_type,
            priority=data.priority.value,
            title=data.title,
            summary=data.summary,
            rationale=data.rationale,
            target_entity_type=data.target_entity_type,
            target_entity_id=data.target_entity_id,
            suggested_action=redact_learning_payload(data.suggested_action),
            evidence=redact_learning_payload(data.evidence),
            impact=redact_learning_payload(data.impact),
            confidence_score=_clamp_confidence(data.confidence_score, default=0.5),
            prompt_version=data.prompt_version,
            model_version=data.model_version,
            expires_at=data.expires_at,
            created_by=user.id if user else None,
        )
        self.db.add(recommendation)
        self.db.flush()
        return recommendation

    def list_recommendations(
        self,
        *,
        company_id: int,
        status: Optional[str] = "pending",
        source_module: Optional[str] = None,
        target_entity_type: Optional[str] = None,
        target_entity_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[AIRecommendation]:
        query = self.db.query(AIRecommendation).filter(AIRecommendation.company_id == company_id)

        if status:
            query = query.filter(AIRecommendation.status == _normalize_status(status))
        if source_module:
            query = query.filter(AIRecommendation.source_module == source_module)
        if target_entity_type:
            query = query.filter(AIRecommendation.target_entity_type == target_entity_type)
        if target_entity_id is not None:
            query = query.filter(AIRecommendation.target_entity_id == target_entity_id)

        # Pre-rank in SQL (priority, confidence, recency) and cap the candidate window, then
        # compute the deterministic score in Python and return the top `limit` by score.
        recommendations = (
            query.order_by(
                case(
                    (AIRecommendation.priority == "high", 0),
                    (AIRecommendation.priority == "medium", 1),
                    (AIRecommendation.priority == "low", 2),
                    else_=3,
                ),
                AIRecommendation.confidence_score.desc(),
                AIRecommendation.created_at.desc(),
            )
            .limit(max(limit, SCORING_FETCH_LIMIT))
            .all()
        )

        now = _now()
        for recommendation in recommendations:
            setattr(recommendation, "score", score_recommendation(recommendation, now=now))

        recommendations.sort(
            key=lambda rec: (
                float(getattr(rec, "score", 0.0)),
                _as_utc_naive(rec.created_at) or now,
                rec.id or 0,
            ),
            reverse=True,
        )
        return recommendations[:limit]

    def set_recommendation_status(
        self,
        *,
        recommendation_id: int,
        company_id: int,
        user: User,
        status: str,
        reason: Optional[str] = None,
    ) -> AIRecommendation:
        recommendation = self._get_recommendation_or_raise(recommendation_id, company_id)
        normalized = _normalize_status(status)
        recommendation.status = normalized
        recommendation.status_reason = redact_learning_payload(reason)
        recommendation.acted_at = _now()
        recommendation.updated_at = _now()
        if normalized == "accepted":
            recommendation.accepted_by = user.id
        elif normalized == "dismissed":
            recommendation.dismissed_by = user.id
        self.db.flush()

        event_type = "accepted" if normalized == "accepted" else "rejected"
        self.record_interaction(
            company_id=company_id,
            user=user,
            data=AIInteractionEventCreate(
                event_type=event_type,
                source_module=recommendation.source_module,
                ai_feature=recommendation.recommendation_type,
                entity_type=recommendation.target_entity_type,
                entity_id=recommendation.target_entity_id,
                recommendation_id=recommendation.id,
                context_summary=reason,
                event_payload={
                    "status": normalized,
                    "note": "Suggest-only status change; no controlled ERP record was mutated.",
                },
                confidence_score=recommendation.confidence_score,
            ),
        )
        return recommendation

    def snooze_recommendation(
        self,
        *,
        recommendation_id: int,
        company_id: int,
        user: User,
        days: int,
        reason: Optional[str] = None,
    ) -> AIRecommendation:
        """Snooze a pending recommendation for `days`; the nightly sweep returns it to pending.

        Implemented purely with the existing plain-string `status` column plus the interaction
        event trail (the wake-up time lives in the snooze event payload) — no schema change.
        """
        if days < SNOOZE_MIN_DAYS or days > SNOOZE_MAX_DAYS:
            # Unreachable via the API (the request schema enforces ge=1/le=30 with a 422);
            # plain ValueError keeps a defensive guard for internal callers without posing as
            # a 409-shaped recommendation-state conflict.
            raise ValueError(f"Snooze duration must be between {SNOOZE_MIN_DAYS} and {SNOOZE_MAX_DAYS} days")
        recommendation = self._get_recommendation_or_raise(recommendation_id, company_id)
        if _normalize_status(recommendation.status) != "pending":
            raise RecommendationStateError("Only pending recommendations can be snoozed")

        now = _now()
        snoozed_until = now + timedelta(days=days)
        status_reason = f"Snoozed until {to_utc_iso(snoozed_until)}"
        if reason:
            status_reason = f"{status_reason} — {reason}"
        recommendation.status = "snoozed"
        recommendation.status_reason = redact_learning_payload(status_reason)
        recommendation.acted_at = now
        recommendation.updated_at = now
        self.db.flush()

        self.record_interaction(
            company_id=company_id,
            user=user,
            data=AIInteractionEventCreate(
                event_type="ignored",
                source_module=recommendation.source_module,
                ai_feature=recommendation.recommendation_type,
                entity_type=recommendation.target_entity_type,
                entity_id=recommendation.target_entity_id,
                recommendation_id=recommendation.id,
                context_summary=reason,
                event_payload={
                    "status": "snoozed",
                    "snoozed_until": to_utc_iso(snoozed_until),
                    "snooze_days": days,
                    "note": "Suggest-only status change; no controlled ERP record was mutated.",
                },
                confidence_score=recommendation.confidence_score,
            ),
        )
        return recommendation

    def record_feedback(
        self,
        *,
        recommendation_id: int,
        company_id: int,
        user: User,
        data: AIRecommendationFeedbackRequest,
    ) -> AIInteractionEvent:
        recommendation = self._get_recommendation_or_raise(recommendation_id, company_id)
        payload = dict(data.event_payload or {})
        payload["feedback"] = data.feedback
        if data.rating is not None:
            payload["rating"] = data.rating

        return self.record_interaction(
            company_id=company_id,
            user=user,
            data=AIInteractionEventCreate(
                event_type="feedback",
                source_module=recommendation.source_module,
                ai_feature=recommendation.recommendation_type,
                entity_type=recommendation.target_entity_type,
                entity_id=recommendation.target_entity_id,
                recommendation_id=recommendation.id,
                context_summary=data.feedback,
                event_payload=payload,
                confidence_score=recommendation.confidence_score,
            ),
        )

    def record_outcome(
        self,
        *,
        company_id: int,
        user: Optional[User],
        data: AIOutcomeCreate,
    ) -> AIOutcome:
        if data.recommendation_id:
            self._get_recommendation_or_raise(data.recommendation_id, company_id)

        outcome = AIOutcome(
            company_id=company_id,
            recommendation_id=data.recommendation_id,
            source_module=data.source_module,
            outcome_type=data.outcome_type,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            metric_name=data.metric_name,
            metric_value=data.metric_value,
            baseline_value=data.baseline_value,
            target_value=data.target_value,
            outcome_payload=redact_learning_payload(data.outcome_payload),
            observed_at=data.observed_at or _now(),
            created_by=user.id if user else None,
        )
        self.db.add(outcome)
        self.db.flush()

        if data.recommendation_id and data.metric_value is not None and data.baseline_value is not None:
            self._nudge_recommendation_confidence(
                data.recommendation_id, company_id, data.metric_value, data.baseline_value
            )

        return outcome

    def aggregate_learning_signals(self, *, company_ids: Optional[Iterable[int]] = None) -> Dict[str, int]:
        now = _now()
        companies = list(company_ids) if company_ids is not None else self._company_ids_with_learning_data()

        created = 0
        stale_count = 0
        woken_count = 0
        for company_id in companies:
            # Tenant-scoped fan-out: each sweep only ever touches the company being processed,
            # so a tenant-triggered run (/ai/aggregate) cannot write across tenants.
            stale_count += self._expire_recommendations_for_company(company_id, now)
            woken_count += self._wake_snoozed_recommendations_for_company(company_id, now)
            created += self._recommend_from_recent_friction(company_id)
            created += self._recommend_from_repeated_corrections(company_id)

        self.db.flush()
        return {
            "companies_processed": len(companies),
            "recommendations_created": created,
            "stale_recommendations": stale_count,
            "snoozed_recommendations_woken": woken_count,
        }

    def _expire_recommendations_for_company(self, company_id: int, now: datetime) -> int:
        """Mark pending/snoozed recommendations past expires_at as stale for one tenant."""
        count = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.status.in_(["pending", "snoozed"]),
                AIRecommendation.expires_at.isnot(None),
                AIRecommendation.expires_at < now,
            )
            .update({"status": "stale", "updated_at": now}, synchronize_session=False)
        )
        return int(count or 0)

    def _wake_snoozed_recommendations_for_company(self, company_id: int, now: datetime) -> int:
        """Return snoozed recommendations to pending once their snooze window has elapsed."""
        woken = 0
        snoozed = (
            self.db.query(AIRecommendation)
            .filter(AIRecommendation.company_id == company_id, AIRecommendation.status == "snoozed")
            .all()
        )
        if not snoozed:
            return woken
        wake_times = self._snoozed_until_by_recommendation(company_id, [rec.id for rec in snoozed])
        for recommendation in snoozed:
            snoozed_until = wake_times.get(recommendation.id)
            if snoozed_until is not None and snoozed_until > now:
                continue
            recommendation.status = "pending"
            recommendation.status_reason = None
            recommendation.updated_at = now
            woken += 1
        return woken

    def _snoozed_until_by_recommendation(self, company_id: int, recommendation_ids: List[int]) -> Dict[int, datetime]:
        """Batch-read wake-up times from snooze interaction events (one query for the whole sweep).

        Semantics match the old per-recommendation lookup: events are scanned newest-first and the
        first parseable `snoozed_until` wins per recommendation; a recommendation with no parseable
        snooze payload stays absent from the map, which the sweep treats as wake-now.
        """
        wake_times: Dict[int, datetime] = {}
        if not recommendation_ids:
            return wake_times
        events = (
            self.db.query(AIInteractionEvent)
            .filter(
                AIInteractionEvent.company_id == company_id,
                AIInteractionEvent.recommendation_id.in_(recommendation_ids),
                AIInteractionEvent.event_type == "ignored",
            )
            .order_by(AIInteractionEvent.created_at.desc(), AIInteractionEvent.id.desc())
            .all()
        )
        for event in events:
            recommendation_id = event.recommendation_id
            if recommendation_id is None or recommendation_id in wake_times:
                continue
            payload = event.event_payload or {}
            raw = payload.get("snoozed_until")
            if not isinstance(raw, str):
                continue
            try:
                parsed = _as_utc_naive(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
            if parsed is not None:
                wake_times[recommendation_id] = parsed
        return wake_times

    def _create_correction(
        self,
        *,
        company_id: int,
        user: Optional[User],
        source_module: str,
        entity_type: Optional[str],
        entity_id: Optional[int],
        event_id: Optional[int],
        recommendation_id: Optional[int],
        data: AICorrectionCreate,
    ) -> AICorrection:
        correction = AICorrection(
            company_id=company_id,
            event_id=event_id,
            recommendation_id=recommendation_id,
            source_module=source_module,
            entity_type=entity_type,
            entity_id=entity_id,
            field_path=data.field_path,
            proposed_value=redact_learning_payload(data.proposed_value, key_hint=data.field_path),
            final_value=redact_learning_payload(data.final_value, key_hint=data.field_path),
            correction_reason=redact_learning_payload(data.correction_reason),
            confidence_score=_clamp_confidence(data.confidence_score),
            created_by=user.id if user else None,
        )
        self.db.add(correction)
        return correction

    def _get_recommendation_or_raise(self, recommendation_id: int, company_id: int) -> AIRecommendation:
        recommendation = (
            self.db.query(AIRecommendation)
            .filter(AIRecommendation.id == recommendation_id, AIRecommendation.company_id == company_id)
            .first()
        )
        if not recommendation:
            raise ValueError("AI recommendation not found")
        return recommendation

    def _company_ids_with_learning_data(self) -> List[int]:
        event_company_ids = self.db.query(AIInteractionEvent.company_id)
        recommendation_company_ids = self.db.query(AIRecommendation.company_id)
        known_company_ids = {
            company_id
            for (company_id,) in event_company_ids.union(recommendation_company_ids).distinct().all()
            if company_id is not None
        }
        if known_company_ids:
            return sorted(known_company_ids)
        return [
            company_id
            for (company_id,) in self.db.query(Company.id).filter(Company.is_active == True).all()
            if company_id is not None
        ]

    def _recommend_from_recent_friction(self, company_id: int) -> int:
        cutoff = _now() - timedelta(days=14)
        rows = (
            self.db.query(AIInteractionEvent.source_module, func.count(AIInteractionEvent.id).label("event_count"))
            .filter(
                AIInteractionEvent.company_id == company_id,
                AIInteractionEvent.created_at >= cutoff,
                AIInteractionEvent.event_type.in_(["edited", "rejected"]),
            )
            .group_by(AIInteractionEvent.source_module)
            .having(func.count(AIInteractionEvent.id) >= 3)
            .all()
        )

        created = 0
        for source_module, event_count in rows:
            if self._open_recommendation_exists(company_id, "workflow_friction", source_module, "source_module", None):
                continue
            priority = "high" if event_count >= 10 else "medium"
            self.db.add(
                AIRecommendation(
                    company_id=company_id,
                    source_module=source_module,
                    recommendation_type="workflow_friction",
                    priority=priority,
                    title=f"Review repeated AI corrections in {source_module}",
                    summary=(
                        f"Users edited or rejected {event_count} AI suggestions in this workflow over the last 14 days. "
                        "Review the prompt, defaults, or module-specific learning rules before increasing automation."
                    ),
                    rationale="Frequent correction is a high-signal source of product improvement.",
                    target_entity_type="source_module",
                    target_entity_id=None,
                    suggested_action={
                        "type": "review_learning_signals",
                        "source_module": source_module,
                        "autonomy": "suggest_only",
                    },
                    evidence=[{"event_count": int(event_count), "window_days": 14}],
                    impact={"expected": "Reduce repeated edits and make future suggestions feel more natural."},
                    confidence_score=min(0.9, 0.45 + (float(event_count) * 0.04)),
                    expires_at=_now() + timedelta(days=30),
                )
            )
            created += 1
        return created

    def _recommend_from_repeated_corrections(self, company_id: int) -> int:
        cutoff = _now() - timedelta(days=30)
        rows = (
            self.db.query(
                AICorrection.source_module,
                AICorrection.field_path,
                func.count(AICorrection.id).label("correction_count"),
            )
            .filter(AICorrection.company_id == company_id, AICorrection.created_at >= cutoff)
            .group_by(AICorrection.source_module, AICorrection.field_path)
            .having(func.count(AICorrection.id) >= 3)
            .order_by(func.count(AICorrection.id).desc())
            .limit(7)
            .all()
        )

        created = 0
        for source_module, field_path, correction_count in rows:
            target_key = f"{source_module}:{field_path}"
            if self._open_recommendation_exists(
                company_id, "correction_pattern", source_module, "field_path", None, target_key
            ):
                continue
            self.db.add(
                AIRecommendation(
                    company_id=company_id,
                    source_module=source_module,
                    recommendation_type="correction_pattern",
                    priority="medium",
                    title=f"Teach AI the preferred value for {field_path}",
                    summary=(
                        f"Users corrected `{field_path}` {correction_count} times in the last 30 days. "
                        "Capture the preferred rule or default so similar future suggestions need fewer edits."
                    ),
                    rationale="Repeated field-level edits are a concrete training signal.",
                    target_entity_type="field_path",
                    target_entity_id=None,
                    suggested_action={
                        "type": "add_preference_or_prompt_rule",
                        "source_module": source_module,
                        "field_path": field_path,
                        "dedupe_key": target_key,
                        "autonomy": "suggest_only",
                    },
                    evidence=[{"correction_count": int(correction_count), "window_days": 30, "field_path": field_path}],
                    impact={"expected": "Improve future suggestions for this field."},
                    confidence_score=min(0.95, 0.5 + (float(correction_count) * 0.05)),
                    expires_at=_now() + timedelta(days=45),
                )
            )
            created += 1
        return created

    def _open_recommendation_exists(
        self,
        company_id: int,
        recommendation_type: str,
        source_module: str,
        target_entity_type: Optional[str],
        target_entity_id: Optional[int],
        dedupe_key: Optional[str] = None,
    ) -> bool:
        """True when a matching recommendation is still open (pending or snoozed).

        Snoozed counts as open: the user deferred the item, so the nightly aggregation must
        not re-create an identical pending duplicate while the original sleeps.
        """
        query = self.db.query(AIRecommendation.id).filter(
            AIRecommendation.company_id == company_id,
            AIRecommendation.recommendation_type == recommendation_type,
            AIRecommendation.source_module == source_module,
            AIRecommendation.status.in_(["pending", "snoozed"]),
        )
        if target_entity_type:
            query = query.filter(AIRecommendation.target_entity_type == target_entity_type)
        if target_entity_id is not None:
            query = query.filter(AIRecommendation.target_entity_id == target_entity_id)

        if not dedupe_key:
            return query.first() is not None

        for (recommendation_id,) in query.all():
            recommendation = self.db.query(AIRecommendation).filter(AIRecommendation.id == recommendation_id).first()
            action = recommendation.suggested_action or {}
            if action.get("dedupe_key") == dedupe_key:
                return True
        return False

    def _nudge_recommendation_confidence(
        self,
        recommendation_id: int,
        company_id: int,
        metric_value: float,
        baseline_value: float,
    ) -> None:
        recommendation = self._get_recommendation_or_raise(recommendation_id, company_id)
        improved = metric_value >= baseline_value
        delta = 0.03 if improved else -0.05
        recommendation.confidence_score = _clamp_confidence((recommendation.confidence_score or 0.5) + delta, 0.5)
        recommendation.updated_at = _now()
