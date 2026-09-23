import pytest

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


def test_short_clean_laser_nest_uses_fast_tier(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_LASER_NEST_MODEL", raising=False)

    decision = select_anthropic_model(LLMTaskContext(task="laser_nest_extraction", input_chars=900, is_ocr=False))

    assert decision.tier == LLMModelTier.FAST
    assert decision.model == "claude-haiku-4-5-20251001"


def test_laser_nest_task_override_wins(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.setenv("ANTHROPIC_LASER_NEST_MODEL", "claude-sonnet-4-6")

    decision = select_anthropic_model(LLMTaskContext(task="laser_nest_extraction", input_chars=900, is_ocr=False))

    assert decision.model == "claude-sonnet-4-6"
    assert decision.reason == "laser_nest_extraction override"


def test_global_tier_override_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL_SELECTION", "reasoning")

    decision = select_anthropic_model(LLMTaskContext(task="po_extraction", input_chars=1200, is_ocr=False))

    assert decision.tier == LLMModelTier.REASONING
    assert decision.model == "claude-opus-4-8"


def test_native_pdf_laser_nest_uses_default_tier(monkeypatch):
    """A native-PDF laser-nest call carries input_chars=0 (the bytes ride in a
    document block, not the prompt). Without the has_pdf_document bump it would
    score 0 -> FAST. The +2 complexity bump must lift it to DEFAULT (Sonnet) so
    layout-aware extraction over the rendered 2-D sheet gets the stronger tier."""
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_LASER_NEST_MODEL", raising=False)

    decision = select_anthropic_model(
        LLMTaskContext(task="laser_nest_extraction", input_chars=0, has_pdf_document=True)
    )

    assert decision.tier == LLMModelTier.DEFAULT
    assert decision.model == "claude-sonnet-4-6"


def test_text_path_laser_nest_without_pdf_document_uses_fast_tier(monkeypatch):
    """The same task/size WITHOUT has_pdf_document scores 0 -> FAST. Pairing this
    with the native-PDF case above proves the +2 bump is what moves the tier."""
    monkeypatch.delenv("ANTHROPIC_MODEL_SELECTION", raising=False)
    monkeypatch.delenv("ANTHROPIC_LASER_NEST_MODEL", raising=False)

    decision = select_anthropic_model(
        LLMTaskContext(task="laser_nest_extraction", input_chars=0, has_pdf_document=False)
    )

    assert decision.tier == LLMModelTier.FAST
    assert decision.model == "claude-haiku-4-5-20251001"


@pytest.mark.parametrize('input_chars,is_ocr', [(0, True), (1000, False), (12000, False), (60000, True)])
def test_hank_document_intake_uses_default_tier_even_for_long_pdfs(monkeypatch, input_chars, is_ocr):
    monkeypatch.delenv('ANTHROPIC_MODEL_SELECTION', raising=False)
    monkeypatch.delenv('ANTHROPIC_HANK_INTAKE_MODEL', raising=False)
    decision = select_anthropic_model(
        LLMTaskContext(
            task='hank_document_intake',
            input_chars=input_chars,
            is_ocr=is_ocr,
            has_pdf_document=True,
            max_output_tokens=6000,
        )
    )
    assert decision.tier == LLMModelTier.DEFAULT
    assert decision.model == 'claude-sonnet-4-6'


def test_hank_document_intake_respects_task_override_before_global_tier(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_MODEL_SELECTION', 'fast')
    monkeypatch.setenv('ANTHROPIC_HANK_INTAKE_MODEL', 'claude-opus-4-8')
    decision = select_anthropic_model(LLMTaskContext(task='hank_document_intake', input_chars=1000))
    assert decision.tier == LLMModelTier.REASONING
    assert decision.model == 'claude-opus-4-8'
    assert decision.reason == 'hank_document_intake override'


def test_hank_document_intake_respects_global_override_when_task_is_auto(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_MODEL_SELECTION', 'reasoning')
    monkeypatch.setenv('ANTHROPIC_HANK_INTAKE_MODEL', 'auto')
    decision = select_anthropic_model(LLMTaskContext(task='hank_document_intake', input_chars=1000))
    assert decision.tier == LLMModelTier.REASONING
