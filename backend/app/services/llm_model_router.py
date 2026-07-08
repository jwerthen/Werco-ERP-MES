"""
Cost-aware Anthropic model selection for ERP/MES AI workloads.

The router keeps model choices centralized so extraction, routing, and QMS
features can move to newer model families without hardcoding IDs everywhere.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class LLMModelTier(str, Enum):
    FAST = "fast"
    DEFAULT = "default"
    REASONING = "reasoning"


DEFAULT_ANTHROPIC_MODELS = {
    LLMModelTier.FAST: "claude-haiku-4-5-20251001",
    LLMModelTier.DEFAULT: "claude-sonnet-4-6",
    LLMModelTier.REASONING: "claude-opus-4-8",
}


@dataclass(frozen=True)
class LLMTaskContext:
    task: str
    input_chars: int
    is_ocr: bool = False
    has_pdf_document: bool = False
    max_output_tokens: int = 4096
    document_type: Optional[str] = None
    geometry: Optional[Dict[str, Any]] = None
    learned_examples: bool = False
    is_assembly: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMModelDecision:
    model: str
    tier: LLMModelTier
    reason: str


TASK_MODEL_ENV = {
    "bom_extraction": "ANTHROPIC_BOM_MODEL",
    "po_extraction": "ANTHROPIC_PO_MODEL",
    "routing_generation": "ANTHROPIC_ROUTING_MODEL",
    "qms_clause_extraction": "ANTHROPIC_QMS_MODEL",
    "laser_nest_extraction": "ANTHROPIC_LASER_NEST_MODEL",
    "copilot_chat": "ANTHROPIC_COPILOT_MODEL",
    "nl_search": "ANTHROPIC_NL_SEARCH_MODEL",
    # Reuses the same Anthropic client; optional override only.
    "auto_execute": "ANTHROPIC_AUTO_EXECUTE_MODEL",
}


def get_anthropic_model(tier: LLMModelTier) -> str:
    env_name = {
        LLMModelTier.FAST: "ANTHROPIC_FAST_MODEL",
        LLMModelTier.DEFAULT: "ANTHROPIC_DEFAULT_MODEL",
        LLMModelTier.REASONING: "ANTHROPIC_REASONING_MODEL",
    }[tier]
    return os.getenv(env_name, DEFAULT_ANTHROPIC_MODELS[tier])


def next_tier(tier: LLMModelTier) -> Optional[LLMModelTier]:
    if tier == LLMModelTier.FAST:
        return LLMModelTier.DEFAULT
    if tier == LLMModelTier.DEFAULT:
        return LLMModelTier.REASONING
    return None


def model_decision_for_tier(tier: LLMModelTier, reason: str) -> LLMModelDecision:
    return LLMModelDecision(model=get_anthropic_model(tier), tier=tier, reason=reason)


def select_anthropic_model(context: LLMTaskContext) -> LLMModelDecision:
    """
    Pick the lowest-cost model tier likely to be reliable for the workload.

    Manual controls:
    - ANTHROPIC_MODEL_SELECTION=auto|fast|default|reasoning|<model-id>
    - Task-specific model env vars such as ANTHROPIC_ROUTING_MODEL.
    """
    task = context.task.strip().lower()
    task_override = os.getenv(TASK_MODEL_ENV.get(task, ""))
    if task_override and task_override.lower() != "auto":
        tier = tier_for_model(task_override)
        return LLMModelDecision(model=task_override, tier=tier, reason=f"{task} override")

    mode = os.getenv("ANTHROPIC_MODEL_SELECTION", "auto").strip().lower()
    if mode in {tier.value for tier in LLMModelTier}:
        tier = LLMModelTier(mode)
        return model_decision_for_tier(tier, f"forced {tier.value} tier")
    if mode and mode != "auto":
        return LLMModelDecision(model=mode, tier=LLMModelTier.DEFAULT, reason="forced model id")

    complexity_score = _complexity_score(context)

    if task == "nl_search":
        return model_decision_for_tier(LLMModelTier.FAST, "NL search intent parse is a cheap classification")

    if task == "auto_execute":
        # Batch approve/skip of already-structured allowlisted actions — cheap JSON decision.
        return model_decision_for_tier(LLMModelTier.FAST, "auto-execute decision over structured recommendations")

    if task == "copilot_chat":
        if complexity_score >= 5:
            return model_decision_for_tier(LLMModelTier.REASONING, "long multi-tool copilot conversation")
        return model_decision_for_tier(LLMModelTier.DEFAULT, "copilot chat needs reliable multi-step tool use")

    if task == "qms_clause_extraction":
        if context.input_chars >= 85_000 or (context.input_chars >= 50_000 and context.max_output_tokens >= 16_000):
            return model_decision_for_tier(LLMModelTier.REASONING, "large QMS extraction")
        return model_decision_for_tier(LLMModelTier.DEFAULT, "QMS extraction needs complete clause coverage")

    if task == "routing_generation":
        if complexity_score >= 5:
            return model_decision_for_tier(LLMModelTier.REASONING, "complex manufacturing routing")
        return model_decision_for_tier(LLMModelTier.DEFAULT, "routing generation requires process judgment")

    if task in {"po_extraction", "bom_extraction"}:
        if complexity_score >= 5:
            return model_decision_for_tier(LLMModelTier.REASONING, "large or noisy extraction")
        if complexity_score <= 1:
            return model_decision_for_tier(LLMModelTier.FAST, "short clean extraction")
        return model_decision_for_tier(LLMModelTier.DEFAULT, "standard extraction")

    if complexity_score >= 5:
        return model_decision_for_tier(LLMModelTier.REASONING, "high complexity")
    if complexity_score <= 1:
        return model_decision_for_tier(LLMModelTier.FAST, "low complexity")
    return model_decision_for_tier(LLMModelTier.DEFAULT, "standard complexity")


def tier_for_model(model: str) -> LLMModelTier:
    for tier in LLMModelTier:
        if model == get_anthropic_model(tier) or model == DEFAULT_ANTHROPIC_MODELS[tier]:
            return tier
    return LLMModelTier.DEFAULT


def _complexity_score(context: LLMTaskContext) -> int:
    score = 0

    if context.input_chars >= 60_000:
        score += 4
    elif context.input_chars >= 20_000:
        score += 3
    elif context.input_chars >= 8_000:
        score += 2
    elif context.input_chars >= 4_000:
        score += 1

    if context.is_ocr:
        score += 2
    if context.has_pdf_document:
        # A native-PDF call carries input_chars~=0 (the bytes ride in a document
        # block, not the prompt text), so without this bump it would score 0 ->
        # FAST/Haiku. +2 lifts it to "standard"/DEFAULT (Sonnet): layout-aware
        # extraction over a rendered 2-D nest sheet warrants the stronger tier.
        score += 2
    if context.max_output_tokens > 8_000:
        score += 2
    if context.learned_examples:
        score += 1
    if context.is_assembly:
        score += 1

    geometry = context.geometry or {}
    if geometry.get("bend_count", 0) or geometry.get("hole_count", 0):
        score += 1
    if (geometry.get("bend_count") or 0) >= 8 or (geometry.get("hole_count") or 0) >= 40:
        score += 1
    if geometry.get("cut_length", 0) and geometry.get("cut_length", 0) >= 1_000:
        score += 1

    return score
