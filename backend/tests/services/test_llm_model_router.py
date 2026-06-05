from app.services.llm_model_router import (
    LLMModelTier,
    LLMTaskContext,
    select_anthropic_model,
)


def test_short_clean_po_uses_fast_tier(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_PO_MODEL", raising=False)

    decision = select_anthropic_model(LLMTaskContext(task="po_extraction", input_chars=1200, is_ocr=False))

    assert decision.tier == LLMModelTier.FAST
    assert decision.model == "claude-haiku-4-5-20251001"


def test_standard_routing_uses_default_tier(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_ROUTING_MODEL", raising=False)

    decision = select_anthropic_model(
        LLMTaskContext(task="routing_generation", input_chars=2500, geometry={"hole_count": 4})
    )

    assert decision.tier == LLMModelTier.DEFAULT
    assert decision.model == "claude-sonnet-4-6"


def test_large_qms_extraction_uses_reasoning_tier(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_QMS_MODEL", raising=False)

    decision = select_anthropic_model(
        LLMTaskContext(task="qms_clause_extraction", input_chars=100_000, max_output_tokens=16_000)
    )

    assert decision.tier == LLMModelTier.REASONING
    assert decision.model == "claude-opus-4-8"


def test_normal_qms_extraction_uses_default_tier(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_QMS_MODEL", raising=False)

    decision = select_anthropic_model(
        LLMTaskContext(task="qms_clause_extraction", input_chars=20_000, max_output_tokens=16_000)
    )

    assert decision.tier == LLMModelTier.DEFAULT
    assert decision.model == "claude-sonnet-4-6"


def test_task_override_wins(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.setenv("ANTHROPIC_ROUTING_MODEL", "claude-sonnet-4-6")

    decision = select_anthropic_model(LLMTaskContext(task="routing_generation", input_chars=100_000, is_ocr=True))

    assert decision.model == "claude-sonnet-4-6"
    assert decision.reason == "routing_generation override"


def test_global_tier_override_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL_SELECTION", "reasoning")

    decision = select_anthropic_model(LLMTaskContext(task="po_extraction", input_chars=1200, is_ocr=False))

    assert decision.tier == LLMModelTier.REASONING
    assert decision.model == "claude-opus-4-8"
