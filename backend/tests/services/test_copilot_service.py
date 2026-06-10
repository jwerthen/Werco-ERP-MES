"""Unit tests for CopilotService — tool registry, tenant injection, bounded loop.

No live Anthropic calls: ``run_llm_task`` is either replaced with a scripted
fake (loop behavior tests) or driven through a fake Anthropic client
(telemetry tests), matching the approach in tests/services/test_llm_client.py.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy.orm import Session

import app.services.copilot_service as copilot_service
import app.services.llm_client as llm_client
from app.models.ai_learning import AIInteractionEvent
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.services.copilot_service import (
    COPILOT_MAX_OUTPUT_TOKENS,
    COPILOT_MAX_TOOL_ROUNDS,
    TOOL_REGISTRY,
    CopilotService,
    CopilotToolSpec,
    anthropic_tool_definitions,
)
from app.services.prompts import COPILOT_CHAT_PROMPT


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, tool_input: Dict[str, Any], block_id: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


class FakeLLMResult:
    """Mimics the LLMTaskResult fields CopilotService reads (model, raw_response)."""

    def __init__(self, blocks: List[Any]):
        self.model = "claude-sonnet-4-6"
        self.tier = "default"
        self.raw_response = SimpleNamespace(content=blocks)
        self.text = next((b.text for b in blocks if getattr(b, "type", None) == "text"), "")


class ScriptedLLM:
    """Returns scripted responses in order; records every call's kwargs."""

    def __init__(self, responses: List[FakeLLMResult]):
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, ctx, **kwargs):
        self.calls.append({"ctx": ctx, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class FakeUsage:
    def __init__(self):
        self.input_tokens = 1000
        self.output_tokens = 100
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class FakeAnthropicResponse:
    def __init__(self, blocks: List[Any]):
        self.content = blocks
        self.usage = FakeUsage()


class FakeAnthropicClient:
    def __init__(self, responses: List[FakeAnthropicResponse]):
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def with_options(self, **kwargs):
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class RecordingSession:
    def __init__(self):
        self.added: List[Any] = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def other_company(db_session: Session) -> Company:
    company = db_session.query(Company).filter(Company.id == 2).first()
    if not company:
        company = Company(id=2, name="Other Corp", slug="other-corp", is_active=True)
        db_session.add(company)
        db_session.commit()
    return company


@pytest.fixture
def service(db_session: Session, test_user: User) -> CopilotService:
    return CopilotService(db_session, company_id=1, user=test_user)


# ---------------------------------------------------------------------------
# Tool registry — tenant injection
# ---------------------------------------------------------------------------
class TestTenantInjection:
    def test_model_supplied_company_id_is_dropped(
        self, db_session: Session, service: CopilotService, test_work_order: WorkOrder, other_company: Company
    ):
        """A model-supplied company_id must never override the session tenant."""
        foreign_wo = WorkOrder(
            work_order_number="WO-FOREIGN-001",
            customer_name="Other Corp Customer",
            part_id=test_work_order.part_id,
            quantity_ordered=5,
            status="released",
            company_id=other_company.id,
        )
        db_session.add(foreign_wo)
        db_session.commit()

        execution = service.execute_tool(
            "lookup_work_order",
            {"number_or_id": "WO-FOREIGN-001", "company_id": other_company.id, "tenant_id": other_company.id},
        )
        assert execution.is_error is False
        assert execution.payload.get("found") is False

    def test_own_tenant_work_order_found(self, service: CopilotService, test_work_order: WorkOrder):
        execution = service.execute_tool("lookup_work_order", {"number_or_id": test_work_order.work_order_number})
        assert execution.payload.get("found") is True
        assert execution.references[0]["label"] == test_work_order.work_order_number

    def test_no_tool_schema_declares_a_tenant_field(self):
        for spec in TOOL_REGISTRY:
            properties = spec.input_schema.get("properties") or {}
            assert "company_id" not in properties, spec.name
            assert "tenant_id" not in properties, spec.name

    def test_soft_deleted_work_order_not_returned(
        self, db_session: Session, service: CopilotService, test_work_order: WorkOrder
    ):
        test_work_order.is_deleted = True
        db_session.commit()
        execution = service.execute_tool("lookup_work_order", {"number_or_id": test_work_order.work_order_number})
        assert execution.payload.get("found") is False

    def test_search_erp_scoped_to_tenant(
        self, db_session: Session, service: CopilotService, test_work_order: WorkOrder, other_company: Company
    ):
        foreign_wo = WorkOrder(
            work_order_number="WO-FOREIGN-SEARCH",
            customer_name="Foreign",
            part_id=test_work_order.part_id,
            quantity_ordered=1,
            status="released",
            company_id=other_company.id,
        )
        db_session.add(foreign_wo)
        db_session.commit()

        execution = service.execute_tool("search_erp", {"query": "WO-FOREIGN-SEARCH"})
        assert execution.payload["total"] == 0


# ---------------------------------------------------------------------------
# Tool registry — RBAC and robustness
# ---------------------------------------------------------------------------
class TestToolRegistry:
    def test_unknown_tool_is_error_not_crash(self, service: CopilotService):
        execution = service.execute_tool("not_a_tool", {})
        assert execution.is_error is True
        assert "unknown tool" in execution.payload["error"]

    def test_role_restricted_tool_hidden_and_refused(self, monkeypatch, db_session: Session, operator_user: User):
        restricted = CopilotToolSpec(
            name="admin_only_probe",
            description="test-only restricted tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda **kwargs: {"data": {"ok": True}, "summary": "ran"},
            allowed_roles=frozenset({UserRole.ADMIN}),
        )
        monkeypatch.setattr(copilot_service, "TOOL_REGISTRY", TOOL_REGISTRY + [restricted])

        svc = CopilotService(db_session, company_id=1, user=operator_user)
        assert all(spec.name != "admin_only_probe" for spec in svc.tool_specs_for_user())
        execution = svc.execute_tool("admin_only_probe", {})
        assert execution.is_error is True
        assert "not available" in execution.payload["error"]

    def test_handler_exception_returns_error_payload(self, monkeypatch, service: CopilotService):
        def boom(**kwargs):
            raise RuntimeError("db exploded")

        broken = CopilotToolSpec(
            name="broken_tool",
            description="always fails",
            input_schema={"type": "object", "properties": {}},
            handler=boom,
        )
        monkeypatch.setattr(copilot_service, "TOOL_REGISTRY", TOOL_REGISTRY + [broken])
        execution = service.execute_tool("broken_tool", {})
        assert execution.is_error is True
        assert "RuntimeError" in execution.payload["error"]
        assert "db exploded" not in json.dumps(execution.payload)  # no internals leaked

    def test_tool_definitions_are_deterministic(self, service: CopilotService):
        specs = service.tool_specs_for_user()
        defs_a = anthropic_tool_definitions(specs)
        defs_b = anthropic_tool_definitions(specs)
        assert json.dumps(defs_a, sort_keys=False) == json.dumps(defs_b, sort_keys=False)
        assert [d["name"] for d in defs_a] == [s.name for s in specs]


# ---------------------------------------------------------------------------
# Chat loop — bounded, read-only, recorded
# ---------------------------------------------------------------------------
class TestChatLoop:
    def test_single_tool_round_then_answer(self, monkeypatch, db_session: Session, service: CopilotService):
        scripted = ScriptedLLM(
            [
                FakeLLMResult([_tool_use_block("company_snapshot", {})]),
                FakeLLMResult([_text_block("All quiet: no open blockers.")]),
            ]
        )
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        events = list(service.stream_chat(messages=[{"role": "user", "content": "how are we doing?"}]))
        final = events[-1]

        assert [e["type"] for e in events if e["type"] == "tool_use"] == ["tool_use"]
        assert any(e["type"] == "delta" for e in events)
        assert final["type"] == "final"
        assert final["answer"] == "All quiet: no open blockers."
        assert final["rounds"] == 1
        assert final["truncated"] is False
        assert final["tool_trace"] == [{"tool": "company_snapshot", "summary": "pulled company snapshot"}]
        assert len(scripted.calls) == 2

    def test_iteration_cap_honored_and_final_forced(self, monkeypatch, db_session: Session, service: CopilotService):
        """A model that never stops calling tools is cut off at the round cap."""
        scripted = ScriptedLLM([FakeLLMResult([_tool_use_block("company_snapshot", {})])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        final = service.run_chat(messages=[{"role": "user", "content": "loop forever"}])

        assert final["rounds"] == COPILOT_MAX_TOOL_ROUNDS
        assert final["truncated"] is True
        assert len(scripted.calls) == COPILOT_MAX_TOOL_ROUNDS + 1  # 8 tool rounds + 1 forced answer
        assert scripted.calls[-1]["tool_choice"] == {"type": "none"}
        assert all(call["tool_choice"] is None for call in scripted.calls[:-1])
        assert final["answer"]  # fallback summary text, never empty

    def test_output_token_cap_on_every_call(self, monkeypatch, service: CopilotService):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("done")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)
        service.run_chat(messages=[{"role": "user", "content": "hi"}])
        assert all(call["max_tokens"] == COPILOT_MAX_OUTPUT_TOKENS for call in scripted.calls)

    def test_chat_turn_is_read_only(
        self, monkeypatch, db_session: Session, service: CopilotService, test_work_order: WorkOrder
    ):
        """A chat turn must not write audit rows or mutate domain data."""
        scripted = ScriptedLLM(
            [
                FakeLLMResult(
                    [_tool_use_block("lookup_work_order", {"number_or_id": test_work_order.work_order_number})]
                ),
                FakeLLMResult([_text_block("Found it.")]),
            ]
        )
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)
        wo_count_before = db_session.query(WorkOrder).count()
        status_before = test_work_order.status

        service.run_chat(messages=[{"role": "user", "content": f"where is {test_work_order.work_order_number}"}])
        db_session.flush()

        assert db_session.query(AuditLog).count() == 0
        assert db_session.query(WorkOrder).count() == wo_count_before
        db_session.refresh(test_work_order)
        assert test_work_order.status == status_before

    def test_every_turn_records_interaction_event(self, monkeypatch, db_session: Session, service: CopilotService):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("Nothing is blocked right now.")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        final = service.run_chat(messages=[{"role": "user", "content": "what's blocked?"}])

        event = db_session.query(AIInteractionEvent).filter(AIInteractionEvent.id == final["interaction_id"]).one()
        assert event.company_id == 1
        assert event.source_module == "copilot"
        assert event.ai_feature == "copilot_chat"
        assert event.prompt_version == COPILOT_CHAT_PROMPT.version
        assert event.event_payload["answer_preview"].startswith("Nothing is blocked")

    def test_interaction_recording_failure_does_not_break_answer(
        self, monkeypatch, db_session: Session, service: CopilotService
    ):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("answer text")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)

        def explode(self, **kwargs):
            raise RuntimeError("learning db down")

        monkeypatch.setattr(copilot_service.AILearningService, "record_interaction", explode)
        final = service.run_chat(messages=[{"role": "user", "content": "q"}])
        assert final["answer"] == "answer text"
        assert final["interaction_id"] is None

    def test_system_blocks_carry_cache_control(self, monkeypatch, service: CopilotService):
        scripted = ScriptedLLM([FakeLLMResult([_text_block("ok")])])
        monkeypatch.setattr(copilot_service, "run_llm_task", scripted)
        service.run_chat(messages=[{"role": "user", "content": "hi"}])
        system_blocks = scripted.calls[0]["system"]
        assert system_blocks[-1]["cache_control"] == {"type": "ephemeral"}
        assert system_blocks[-1]["text"] == COPILOT_CHAT_PROMPT.text
        assert scripted.calls[0]["tools"]  # tool defs sent on every call

    def test_rejects_conversation_without_user_message(self, service: CopilotService):
        with pytest.raises(ValueError):
            service.run_chat(messages=[{"role": "assistant", "content": "hello"}])


# ---------------------------------------------------------------------------
# Telemetry — one AIUsageEvent per loop iteration through the real llm_client
# ---------------------------------------------------------------------------
class TestUsageTelemetry:
    def test_usage_event_per_iteration(self, monkeypatch, db_session: Session, service: CopilotService):
        fake_client = FakeAnthropicClient(
            [
                FakeAnthropicResponse([_tool_use_block("company_snapshot", {})]),
                FakeAnthropicResponse([_text_block("Snapshot says all good.")]),
            ]
        )
        recording = RecordingSession()
        monkeypatch.setattr(llm_client, "get_anthropic_client", lambda: fake_client)
        monkeypatch.setattr(llm_client, "_usage_session_factory", lambda: recording)

        final = service.run_chat(messages=[{"role": "user", "content": "how are we doing?"}])

        assert final["answer"] == "Snapshot says all good."
        assert len(recording.added) == 2  # one row per loop iteration
        for event in recording.added:
            assert event.company_id == 1
            assert event.task == "copilot_chat"
            assert event.feature == "copilot_panel"
            assert event.prompt_version == COPILOT_CHAT_PROMPT.version
            assert event.success is True
        # The real client received the cached prefix: tools + system w/ cache_control
        for call in fake_client.calls:
            assert call["tools"][0]["name"] == TOOL_REGISTRY[0].name
            assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}
            assert call["max_tokens"] == COPILOT_MAX_OUTPUT_TOKENS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_chunk_text_reassembles(self):
        text = "word " * 200
        chunks = copilot_service._chunk_text(text.strip())
        assert "".join(chunks).split() == text.split()
        assert len(chunks) > 1

    def test_chunk_text_empty(self):
        assert copilot_service._chunk_text("") == []

    def test_escape_like_neutralizes_wildcards(self):
        assert "%" not in copilot_service._escape_like("100% laser_cut")
        assert "_" not in copilot_service._escape_like("100% laser_cut")

    def test_context_hint_goes_into_last_user_message(self, service: CopilotService):
        messages = service._build_messages(
            [{"role": "user", "content": "what about this one?"}], "viewing /work-orders/12"
        )
        blocks = messages[-1]["content"]
        assert isinstance(blocks, list)
        assert blocks[0]["text"].startswith("<context_hint>")
        assert blocks[1]["text"] == "what about this one?"
