"""Structural guards for owner ruling 3 (2026-11-05): ONE door executes an AI recommendation.

Nothing AI-driven executes without a named person clicking Accept. The only executor is
``POST /api/v1/ai/recommendations/{id}/apply``; these guards fail CI if a second caller of the
applier appears, if the deleted auto-execute agent comes back, if the role bypass returns, or
if an auto tier can be written again.
"""

import inspect
from pathlib import Path

import pytest

from app.models.ai_learning import AIRecommendation
from app.services.ai_action_applier import AIActionApplier
from app.services.llm_model_router import TASK_MODEL_ENV
from app.services.prompts import PROMPT_REGISTRY

APP_DIR = Path(__file__).resolve().parents[1] / "app"
THE_DOOR = APP_DIR / "api" / "endpoints" / "ai_learning.py"


def _app_sources():
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


def test_applier_is_constructed_only_by_the_apply_route():
    constructors = [path for path, source in _app_sources() if "AIActionApplier(" in source]
    assert constructors == [THE_DOOR], constructors
    assert THE_DOOR.read_text(encoding="utf-8").count("AIActionApplier(") == 1


def test_auto_execute_agent_is_gone():
    assert not (APP_DIR / "services" / "ai_auto_execute_service.py").exists()
    assert not (APP_DIR / "services" / "prompts" / "auto_execute.py").exists()
    offenders = [path for path, source in _app_sources() if "ai_auto_execute_service" in source]
    assert offenders == []


def test_applier_has_no_bypass_or_allowlist_override():
    parameters = inspect.signature(AIActionApplier.__init__).parameters
    assert "bypass_role_checks" not in parameters
    assert "allowlist" not in parameters
    assert not hasattr(AIActionApplier, "can_apply")


@pytest.mark.parametrize("tier", ["auto_execute", "execute_controlled", "autonomous", ["apply_on_accept"], {"x": 1}, 3])
def test_an_auto_tier_cannot_be_written(tier):
    # A non-string (incl. an unhashable list/dict) is refused with ValueError, never a TypeError.
    with pytest.raises(ValueError, match="no auto-execute tier"):
        AIRecommendation(suggested_action={"type": "adjust_work_order_priority", "autonomy": tier})
    rec = AIRecommendation(suggested_action={"type": "x", "autonomy": "suggest_only"})
    with pytest.raises(ValueError):
        rec.suggested_action = {"type": "x", "autonomy": tier}


@pytest.mark.parametrize("tier", ["suggest_only", "apply_on_accept", None])
def test_canonical_tiers_construct(tier):
    action = {"type": "x"} if tier is None else {"type": "x", "autonomy": tier}
    assert AIRecommendation(suggested_action=action).suggested_action == action


def test_auto_execute_prompt_and_model_task_are_retired():
    assert "auto_execute_decision" not in PROMPT_REGISTRY
    assert "auto_execute" not in TASK_MODEL_ENV
