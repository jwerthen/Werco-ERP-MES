"""Learning helpers for drawing-based routing generation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.routing_learning import (
    RoutingGenerationSession,
    RoutingLearnedAlias,
    RoutingOperationPattern,
    RoutingWorkCenterPreference,
)
from app.models.work_center import WorkCenter
from app.services.routing_generation_service import _infer_part_info_from_drawing, normalize_work_center_type

STOP_ALIAS_WORDS = {
    "and",
    "assembly",
    "center",
    "complete",
    "final",
    "for",
    "from",
    "install",
    "operation",
    "part",
    "process",
    "run",
    "setup",
    "the",
    "to",
    "verify",
    "with",
}


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value.value if hasattr(value, "value") else value)


def _clean_feature(value: Any) -> str:
    normalized = normalize_work_center_type(str(value or ""))
    return normalized or "unknown"


def _count_bucket(value: Any) -> str:
    try:
        count = int(float(value or 0))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return "0"
    if count <= 5:
        return "1_5"
    if count <= 20:
        return "6_20"
    return "21_plus"


def build_feature_signature(
    part: Part,
    drawing_info: Optional[Dict[str, Any]] = None,
    geometry: Optional[Dict[str, Any]] = None,
    drawing_text: str = "",
) -> Dict[str, Any]:
    """Build a stable signature used to retrieve similar learned routings."""
    inferred = _infer_part_info_from_drawing(
        drawing_text, geometry, is_assembly=_enum_value(part.part_type) == "assembly"
    )
    info = {**inferred, **(drawing_info or {})}
    geometry = geometry or {}

    return {
        "part_type": _clean_feature(_enum_value(part.part_type)),
        "material": _clean_feature(info.get("material")),
        "thickness": _clean_feature(info.get("thickness")),
        "finish": _clean_feature(info.get("finish")),
        "has_cut": bool(geometry.get("cut_length")),
        "bend_bucket": _count_bucket(geometry.get("bend_count") or info.get("bend_count")),
        "hole_bucket": _count_bucket(geometry.get("hole_count") or info.get("hole_count")),
        "weld_required": bool(info.get("weld_required")),
        "assembly_required": bool(info.get("assembly_required")) or _enum_value(part.part_type) == "assembly",
    }


def feature_key(signature: Dict[str, Any]) -> str:
    return "|".join(f"{key}={signature[key]}" for key in sorted(signature.keys()))


def get_learned_routing_context(
    db: Session,
    *,
    company_id: int,
    part: Part,
    drawing_text: str,
    geometry: Optional[Dict[str, Any]],
    drawing_info: Optional[Dict[str, Any]] = None,
    max_aliases: int = 200,
    max_patterns: int = 3,
) -> Dict[str, Any]:
    """Fetch learned aliases, preferences, and similar approved patterns for a new drawing."""
    signature = build_feature_signature(part, drawing_info=drawing_info, geometry=geometry, drawing_text=drawing_text)
    key = feature_key(signature)

    aliases = (
        db.query(RoutingLearnedAlias)
        .filter(RoutingLearnedAlias.company_id == company_id)
        .order_by(RoutingLearnedAlias.usage_count.desc(), RoutingLearnedAlias.confidence_score.desc())
        .limit(max_aliases)
        .all()
    )

    exact_patterns = (
        db.query(RoutingOperationPattern)
        .filter(RoutingOperationPattern.company_id == company_id, RoutingOperationPattern.pattern_key == key)
        .order_by(RoutingOperationPattern.usage_count.desc(), RoutingOperationPattern.confidence_score.desc())
        .limit(max_patterns)
        .all()
    )
    patterns = list(exact_patterns)
    if len(patterns) < max_patterns:
        fallback_patterns = (
            db.query(RoutingOperationPattern)
            .filter(
                RoutingOperationPattern.company_id == company_id,
                RoutingOperationPattern.pattern_key != key,
                RoutingOperationPattern.part_type == signature["part_type"],
            )
            .order_by(RoutingOperationPattern.usage_count.desc(), RoutingOperationPattern.confidence_score.desc())
            .limit(max_patterns - len(patterns))
            .all()
        )
        patterns.extend(fallback_patterns)

    preferences = (
        db.query(RoutingWorkCenterPreference)
        .filter(RoutingWorkCenterPreference.company_id == company_id, RoutingWorkCenterPreference.feature_key == key)
        .order_by(RoutingWorkCenterPreference.usage_count.desc(), RoutingWorkCenterPreference.confidence_score.desc())
        .all()
    )
    if not preferences:
        preferences = (
            db.query(RoutingWorkCenterPreference)
            .filter(
                RoutingWorkCenterPreference.company_id == company_id,
                RoutingWorkCenterPreference.part_type == signature["part_type"],
            )
            .order_by(
                RoutingWorkCenterPreference.usage_count.desc(), RoutingWorkCenterPreference.confidence_score.desc()
            )
            .limit(20)
            .all()
        )
    preferred_work_center_ids: Dict[str, List[int]] = {}
    for preference in preferences:
        preferred_work_center_ids.setdefault(preference.work_center_type, []).append(preference.work_center_id)

    pattern_dicts = [
        {
            "pattern_key": pattern.pattern_key,
            "part_type": pattern.part_type,
            "material": pattern.material,
            "thickness": pattern.thickness,
            "finish": pattern.finish,
            "feature_signature": pattern.feature_signature or {},
            "operations": pattern.operations or [],
            "usage_count": pattern.usage_count or 0,
            "confidence_score": pattern.confidence_score or 0.0,
        }
        for pattern in patterns
    ]

    return {
        "feature_signature": signature,
        "feature_key": key,
        "aliases": [
            {
                "alias": alias.alias,
                "work_center_type": alias.work_center_type,
                "usage_count": alias.usage_count or 0,
                "confidence_score": alias.confidence_score or 0.0,
            }
            for alias in aliases
        ],
        "preferred_work_center_ids": preferred_work_center_ids,
        "patterns": pattern_dicts,
        "examples_prompt": format_learned_examples(pattern_dicts),
    }


def format_learned_examples(patterns: List[Dict[str, Any]]) -> str:
    if not patterns:
        return ""
    lines = []
    for index, pattern in enumerate(patterns, start=1):
        op_text = []
        for operation in pattern.get("operations") or []:
            op_text.append(
                f"{operation.get('sequence')}. {operation.get('operation_name')} -> {operation.get('work_center_type')}"
            )
        if op_text:
            lines.append(f"Example {index} ({pattern.get('usage_count', 0)} approvals): " + "; ".join(op_text))
    return "\n".join(lines)


def create_generation_session(
    db: Session,
    *,
    company_id: int,
    part_id: int,
    created_by: Optional[int],
    file_name: str,
    file_type: str,
    file_size: int,
    file_path: str,
    drawing_text: str,
    geometry: Optional[Dict[str, Any]],
    drawing_info: Dict[str, Any],
    proposed_operations: List[Dict[str, Any]],
    warnings: List[str],
    extraction_confidence: str,
    source_was_ocr: bool,
    learned_context: Optional[Dict[str, Any]] = None,
) -> RoutingGenerationSession:
    session = RoutingGenerationSession(
        company_id=company_id,
        part_id=part_id,
        created_by=created_by,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        file_path=file_path,
        drawing_text=(drawing_text or "")[:12000],
        geometry=geometry or {},
        drawing_info=drawing_info or {},
        proposed_operations=proposed_operations or [],
        warnings=warnings or [],
        extraction_confidence=extraction_confidence,
        source_was_ocr=source_was_ocr,
        learned_context=learned_context or {},
        status="proposed",
    )
    db.add(session)
    db.flush()
    return session


def learn_from_approved_generation(
    db: Session,
    *,
    generation_session: RoutingGenerationSession,
    approved_operations: List[Dict[str, Any]],
    part: Part,
    routing_id: int,
    approved_by: Optional[int],
    company_id: int,
) -> Dict[str, Any]:
    """Record the approved edits and update learned routing artifacts."""
    work_center_ids = [
        operation.get("work_center_id") for operation in approved_operations if operation.get("work_center_id")
    ]
    work_centers = (
        db.query(WorkCenter).filter(WorkCenter.company_id == company_id, WorkCenter.id.in_(work_center_ids)).all()
        if work_center_ids
        else []
    )
    work_center_by_id = {work_center.id: work_center for work_center in work_centers}
    proposed_operations = generation_session.proposed_operations or []

    approved_snapshots = _approved_operation_snapshots(approved_operations, work_center_by_id)
    summary = _summarize_corrections(proposed_operations, approved_snapshots)

    signature = build_feature_signature(
        part,
        drawing_info=generation_session.drawing_info or {},
        geometry=generation_session.geometry or {},
        drawing_text=generation_session.drawing_text or "",
    )
    key = feature_key(signature)

    aliases_learned = 0
    preferences_learned = 0
    for index, approved in enumerate(approved_snapshots):
        work_center_type = approved.get("work_center_type")
        if not work_center_type:
            continue

        proposed = _match_proposed_operation(proposed_operations, approved, index)
        aliases = _candidate_aliases(approved, proposed)
        for alias in aliases:
            _upsert_alias(db, company_id=company_id, alias=alias, work_center_type=work_center_type)
            aliases_learned += 1

        if approved.get("work_center_id"):
            _upsert_preference(
                db,
                company_id=company_id,
                signature=signature,
                key=key,
                work_center_type=work_center_type,
                work_center_id=approved["work_center_id"],
            )
            preferences_learned += 1

    _upsert_operation_pattern(
        db,
        company_id=company_id,
        signature=signature,
        key=key,
        operations=approved_snapshots,
    )

    summary["aliases_learned"] = aliases_learned
    summary["preferences_learned"] = preferences_learned
    summary["pattern_key"] = key

    generation_session.routing_id = routing_id
    generation_session.approved_operations = approved_snapshots
    generation_session.correction_summary = summary
    generation_session.status = "approved"
    generation_session.approved_by = approved_by
    generation_session.approved_at = datetime.utcnow()
    generation_session.updated_at = datetime.utcnow()
    db.flush()

    return summary


def _approved_operation_snapshots(
    approved_operations: List[Dict[str, Any]],
    work_center_by_id: Dict[int, WorkCenter],
) -> List[Dict[str, Any]]:
    snapshots = []
    for index, operation in enumerate(approved_operations):
        work_center = work_center_by_id.get(operation.get("work_center_id"))
        snapshots.append(
            {
                "sequence": operation.get("sequence") or (index + 1) * 10,
                "operation_name": operation.get("name") or operation.get("operation_name"),
                "description": operation.get("description"),
                "work_center_id": operation.get("work_center_id"),
                "work_center_name": work_center.name if work_center else None,
                "work_center_type": normalize_work_center_type(work_center.work_center_type if work_center else ""),
                "setup_hours": operation.get("setup_hours") or 0,
                "run_hours_per_unit": operation.get("run_hours_per_unit") or 0,
                "is_inspection_point": bool(operation.get("is_inspection_point")),
                "is_outside_operation": bool(operation.get("is_outside_operation")),
                "tooling_requirements": operation.get("tooling_requirements"),
                "work_instructions": operation.get("work_instructions"),
            }
        )
    return snapshots


def _match_proposed_operation(
    proposed_operations: List[Dict[str, Any]],
    approved_operation: Dict[str, Any],
    index: int,
) -> Optional[Dict[str, Any]]:
    sequence = approved_operation.get("sequence")
    for proposed in proposed_operations:
        if proposed.get("sequence") == sequence:
            return proposed
    if index < len(proposed_operations):
        return proposed_operations[index]
    return None


def _summarize_corrections(
    proposed_operations: List[Dict[str, Any]],
    approved_operations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    renamed = 0
    work_center_changed = 0
    instruction_changed = 0

    for index, approved in enumerate(approved_operations):
        proposed = _match_proposed_operation(proposed_operations, approved, index) or {}
        if normalize_work_center_type(proposed.get("operation_name") or "") != normalize_work_center_type(
            approved.get("operation_name") or ""
        ):
            renamed += 1
        if proposed.get("work_center_id") and proposed.get("work_center_id") != approved.get("work_center_id"):
            work_center_changed += 1
        if (proposed.get("work_instructions") or "") != (approved.get("work_instructions") or ""):
            instruction_changed += 1

    return {
        "proposed_count": len(proposed_operations),
        "approved_count": len(approved_operations),
        "added_count": max(0, len(approved_operations) - len(proposed_operations)),
        "removed_count": max(0, len(proposed_operations) - len(approved_operations)),
        "renamed_count": renamed,
        "work_center_changed_count": work_center_changed,
        "instruction_changed_count": instruction_changed,
    }


def _candidate_aliases(approved: Dict[str, Any], proposed: Optional[Dict[str, Any]]) -> List[str]:
    values: List[str] = [
        approved.get("operation_name") or "",
        approved.get("description") or "",
    ]
    if proposed:
        values.extend(
            [
                proposed.get("operation_name") or "",
                proposed.get("description") or "",
                proposed.get("work_center_type") or "",
            ]
        )

    aliases = []
    for value in values:
        normalized = normalize_work_center_type(value)
        if normalized and len(normalized) >= 3:
            aliases.append(normalized[:120])
        aliases.extend(_meaningful_tokens(normalized))

    return _dedupe(alias for alias in aliases if alias and alias not in STOP_ALIAS_WORDS)


def _meaningful_tokens(normalized_text: str) -> List[str]:
    return [
        token
        for token in normalized_text.split("_")
        if len(token) >= 3 and token not in STOP_ALIAS_WORDS and not token.isdigit()
    ]


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _confidence_from_usage(usage_count: int) -> float:
    return min(0.95, 0.5 + (usage_count * 0.05))


def _upsert_alias(db: Session, *, company_id: int, alias: str, work_center_type: str) -> None:
    learned = (
        db.query(RoutingLearnedAlias)
        .filter(
            RoutingLearnedAlias.company_id == company_id,
            RoutingLearnedAlias.alias == alias,
            RoutingLearnedAlias.work_center_type == work_center_type,
        )
        .first()
    )
    now = datetime.utcnow()
    if learned:
        learned.usage_count = (learned.usage_count or 0) + 1
        learned.confidence_score = _confidence_from_usage(learned.usage_count)
        learned.last_seen_at = now
        learned.updated_at = now
        return

    db.add(
        RoutingLearnedAlias(
            company_id=company_id,
            alias=alias,
            work_center_type=work_center_type,
            usage_count=1,
            confidence_score=_confidence_from_usage(1),
            last_seen_at=now,
        )
    )


def _upsert_preference(
    db: Session,
    *,
    company_id: int,
    signature: Dict[str, Any],
    key: str,
    work_center_type: str,
    work_center_id: int,
) -> None:
    preference = (
        db.query(RoutingWorkCenterPreference)
        .filter(
            RoutingWorkCenterPreference.company_id == company_id,
            RoutingWorkCenterPreference.feature_key == key,
            RoutingWorkCenterPreference.work_center_type == work_center_type,
            RoutingWorkCenterPreference.work_center_id == work_center_id,
        )
        .first()
    )
    now = datetime.utcnow()
    if preference:
        preference.usage_count = (preference.usage_count or 0) + 1
        preference.confidence_score = _confidence_from_usage(preference.usage_count)
        preference.last_seen_at = now
        preference.updated_at = now
        return

    db.add(
        RoutingWorkCenterPreference(
            company_id=company_id,
            feature_key=key,
            part_type=signature.get("part_type"),
            material=signature.get("material"),
            thickness=signature.get("thickness"),
            finish=signature.get("finish"),
            work_center_type=work_center_type,
            work_center_id=work_center_id,
            usage_count=1,
            confidence_score=_confidence_from_usage(1),
            last_seen_at=now,
        )
    )


def _upsert_operation_pattern(
    db: Session,
    *,
    company_id: int,
    signature: Dict[str, Any],
    key: str,
    operations: List[Dict[str, Any]],
) -> None:
    pattern = (
        db.query(RoutingOperationPattern)
        .filter(RoutingOperationPattern.company_id == company_id, RoutingOperationPattern.pattern_key == key)
        .first()
    )
    now = datetime.utcnow()
    pattern_operations = [
        {
            "sequence": operation.get("sequence"),
            "operation_name": operation.get("operation_name"),
            "description": operation.get("description"),
            "work_center_type": operation.get("work_center_type"),
            "setup_hours": operation.get("setup_hours"),
            "run_hours_per_unit": operation.get("run_hours_per_unit"),
            "is_inspection_point": operation.get("is_inspection_point"),
            "is_outside_operation": operation.get("is_outside_operation"),
            "tooling_requirements": operation.get("tooling_requirements"),
            "work_instructions": operation.get("work_instructions"),
        }
        for operation in operations
    ]

    if pattern:
        pattern.operations = pattern_operations
        pattern.usage_count = (pattern.usage_count or 0) + 1
        pattern.confidence_score = _confidence_from_usage(pattern.usage_count)
        pattern.last_used_at = now
        pattern.updated_at = now
        return

    db.add(
        RoutingOperationPattern(
            company_id=company_id,
            pattern_key=key,
            part_type=signature.get("part_type"),
            material=signature.get("material"),
            thickness=signature.get("thickness"),
            finish=signature.get("finish"),
            feature_signature=signature,
            operations=pattern_operations,
            usage_count=1,
            confidence_score=_confidence_from_usage(1),
            last_used_at=now,
        )
    )
