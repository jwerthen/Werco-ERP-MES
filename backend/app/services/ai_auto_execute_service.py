"""Claude-driven auto-execute of allowlisted Action Inbox recommendations.

Uses the **existing** Anthropic stack only:
  ``run_llm_task`` + ``LLMTaskContext(task=\"auto_execute\")`` + versioned
  ``AUTO_EXECUTE_PROMPT`` — no new AI providers or SDKs.

Flow (per tenant, after nightly sensors/learners):
  1. Collect pending recommendations whose ``suggested_action.type`` is
     allowlisted and autonomy is auto_execute / apply_on_accept / execute_controlled.
  2. Ask Claude which ids to execute (JSON).
  3. Apply each via ``AIActionApplier`` as the company system actor (admin).
  4. Mark accepted with audit/telemetry.

If AI egress is off or Claude is unavailable, falls back to deterministic
auto-execute for high-confidence allowlisted items so the plant still improves
without a human prompt.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.user import User, UserRole
from app.schemas.ai_learning import AIInteractionEventCreate
from app.services.ai_action_applier import (
    DEFAULT_APPLY_ALLOWLIST,
    AIActionApplier,
    AIActionApplyError,
)
from app.services.ai_learning_service import AILearningService
from app.services.audit_service import AuditService
from app.services.llm_client import (
    LLMEgressDisabledError,
    LLMNotConfiguredError,
    run_llm_task,
)
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts import AUTO_EXECUTE_PROMPT

logger = logging.getLogger(__name__)

# Env knobs (all optional; defaults ship auto-execute ON).
AUTO_EXECUTE_ENABLED = os.getenv("AI_AUTO_EXECUTE_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MIN_CONFIDENCE_FOR_CANDIDATE = float(os.getenv("AI_AUTO_EXECUTE_MIN_CONFIDENCE", "0.55"))
# When Claude is unavailable, still auto-run these if confidence is high enough.
FALLBACK_MIN_CONFIDENCE = float(os.getenv("AI_AUTO_EXECUTE_FALLBACK_MIN_CONFIDENCE", "0.75"))
MAX_BATCH = int(os.getenv("AI_AUTO_EXECUTE_MAX_BATCH", "25"))
LLM_TIMEOUT_SECONDS = float(os.getenv("AI_AUTO_EXECUTE_LLM_TIMEOUT", "45"))

AUTO_AUTONOMY = frozenset({"auto_execute", "apply_on_accept", "execute_controlled"})
# Informational types never auto-execute even if mis-tagged.
SKIP_TYPES = frozenset(
    {
        "morning_brief",
        "workflow_friction",
        "correction_pattern",
        "learned_preference",
        "standard_update",
        "estimate_calibration",
    }
)


def resolve_system_actor(db: Session, company_id: int) -> Optional[User]:
    """Pick an active admin/manager/supervisor to own auto-applied mutations."""
    for role in (UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR):
        user = (
            db.query(User)
            .filter(User.company_id == company_id, User.is_active == True, User.role == role)  # noqa: E712
            .order_by(User.id.asc())
            .first()
        )
        if user:
            return user
    return (
        db.query(User)
        .filter(User.company_id == company_id, User.is_active == True)  # noqa: E712
        .order_by(User.id.asc())
        .first()
    )


def _candidate_query(db: Session, company_id: int) -> List[AIRecommendation]:
    rows = (
        db.query(AIRecommendation)
        .filter(AIRecommendation.company_id == company_id, AIRecommendation.status == "pending")
        .order_by(AIRecommendation.confidence_score.desc(), AIRecommendation.created_at.asc())
        .limit(MAX_BATCH * 3)
        .all()
    )
    candidates: List[AIRecommendation] = []
    for rec in rows:
        if rec.recommendation_type in SKIP_TYPES:
            continue
        action = rec.suggested_action or {}
        action_type = str(action.get("type") or "")
        if action_type not in DEFAULT_APPLY_ALLOWLIST:
            continue
        autonomy = str(action.get("autonomy") or "suggest_only")
        if autonomy not in AUTO_AUTONOMY:
            continue
        conf = float(rec.confidence_score or 0.0)
        if conf < MIN_CONFIDENCE_FOR_CANDIDATE:
            continue
        candidates.append(rec)
        if len(candidates) >= MAX_BATCH:
            break
    return candidates


def _serialize_for_llm(recs: Sequence[AIRecommendation]) -> List[Dict[str, Any]]:
    payload = []
    for rec in recs:
        action = rec.suggested_action or {}
        payload.append(
            {
                "id": rec.id,
                "recommendation_type": rec.recommendation_type,
                "source_module": rec.source_module,
                "priority": rec.priority,
                "title": rec.title,
                "summary": (rec.summary or "")[:500],
                "confidence_score": rec.confidence_score,
                "target_entity_type": rec.target_entity_type,
                "target_entity_id": rec.target_entity_id,
                "action_type": action.get("type"),
                "autonomy": action.get("autonomy"),
                "evidence": (rec.evidence or [])[:5],
                "impact": rec.impact or {},
            }
        )
    return payload


def _parse_llm_json(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())


def _claude_decide_ids(db: Session, company_id: int, candidates: Sequence[AIRecommendation]) -> Optional[Set[int]]:
    """Ask Claude which recommendation ids to execute. None = LLM unavailable."""
    payload = _serialize_for_llm(candidates)
    user_content = (
        "Pending allowlisted recommendations (JSON array). Choose execute vs skip for each id:\n\n"
        + json.dumps(payload, default=str)
    )
    try:
        result = run_llm_task(
            LLMTaskContext(task="auto_execute", input_chars=len(user_content), max_output_tokens=2048),
            messages=[{"role": "user", "content": user_content}],
            system=AUTO_EXECUTE_PROMPT.text,
            max_tokens=2048,
            company_id=company_id,
            feature="ai_auto_execute",
            prompt_version=AUTO_EXECUTE_PROMPT.version,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except (LLMEgressDisabledError, LLMNotConfiguredError) as exc:
        logger.info("auto_execute Claude unavailable for company %s: %s", company_id, exc)
        return None
    except Exception:
        logger.exception("auto_execute Claude call failed for company %s", company_id)
        return None

    try:
        parsed = _parse_llm_json(result.text)
        execute = parsed.get("execute") or []
        ids: Set[int] = set()
        for item in execute:
            if isinstance(item, dict) and item.get("id") is not None:
                ids.add(int(item["id"]))
            elif isinstance(item, int):
                ids.add(item)
        # Only allow ids that were in the candidate set
        allowed = {r.id for r in candidates}
        return {i for i in ids if i in allowed}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("auto_execute Claude JSON parse failed company %s: %s", company_id, exc)
        return None


def _fallback_ids(candidates: Sequence[AIRecommendation]) -> Set[int]:
    """Deterministic high-confidence auto-execute when Claude cannot run."""
    return {
        r.id
        for r in candidates
        if float(r.confidence_score or 0.0) >= FALLBACK_MIN_CONFIDENCE and r.priority in {"high", "medium"}
    }


def auto_execute_pending_recommendations(db: Session, company_id: int) -> Dict[str, int]:
    """Run Claude-driven auto-execute for one tenant. Returns counters."""
    summary = {
        "candidates": 0,
        "claude_selected": 0,
        "executed": 0,
        "failed": 0,
        "skipped": 0,
        "used_fallback": 0,
    }
    if not AUTO_EXECUTE_ENABLED:
        return summary

    candidates = _candidate_query(db, company_id)
    summary["candidates"] = len(candidates)
    if not candidates:
        return summary

    actor = resolve_system_actor(db, company_id)
    if not actor:
        logger.warning("auto_execute: no active user for company %s", company_id)
        return summary

    selected = _claude_decide_ids(db, company_id, candidates)
    if selected is None:
        selected = _fallback_ids(candidates)
        summary["used_fallback"] = 1
    summary["claude_selected"] = len(selected)
    summary["skipped"] = max(0, len(candidates) - len(selected))

    learning = AILearningService(db)
    audit = AuditService(db, actor)
    # System actor is treated as full apply authority for allowlisted actions.
    applier = AIActionApplier(
        db,
        company_id=company_id,
        user=actor,
        audit=audit,
        allowlist=set(DEFAULT_APPLY_ALLOWLIST),
        bypass_role_checks=True,
    )

    for rec in candidates:
        if rec.id not in selected:
            continue
        try:
            apply_result = applier.apply(rec)
            learning.set_recommendation_status(
                recommendation_id=rec.id,
                company_id=company_id,
                user=actor,
                status="accepted",
                reason="Auto-executed by Claude always-on agent",
            )
            learning.record_interaction(
                company_id=company_id,
                user=actor,
                data=AIInteractionEventCreate(
                    event_type="accepted",
                    source_module=rec.source_module,
                    ai_feature="auto_execute",
                    surface="nightly_agent",
                    entity_type=rec.target_entity_type,
                    entity_id=rec.target_entity_id,
                    recommendation_id=rec.id,
                    context_summary="Claude always-on auto-execute",
                    event_payload={
                        "auto_executed": True,
                        "apply_result": apply_result,
                        "used_fallback": bool(summary["used_fallback"]),
                        "prompt_version": AUTO_EXECUTE_PROMPT.version,
                    },
                    confidence_score=rec.confidence_score,
                    prompt_version=AUTO_EXECUTE_PROMPT.version,
                ),
            )
            summary["executed"] += 1
        except AIActionApplyError as exc:
            logger.info("auto_execute skip rec %s: %s", rec.id, exc)
            summary["failed"] += 1
        except Exception:
            logger.exception("auto_execute failed rec %s company %s", rec.id, company_id)
            summary["failed"] += 1

    return summary
