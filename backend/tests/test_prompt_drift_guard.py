"""GUARD: a change to what the model sees cannot land without a prompt version bump.

Every Anthropic call records ``prompt_version`` on its ``ai_usage_events`` row, and
the learning rows attribute their signal to it. That record is only honest if the
version moves whenever the request does. Before this guard nothing tied them
together, and drift had already happened: ``copilot_chat`` 1.6.0/1.7.0 and
``hank_document_intake`` 1.1.0/1.2.0 shipped with no CHANGELOG entry, and the
``estimate_drawing_extraction`` retirement was never recorded.

HOW IT WORKS. ``RENDER_CASES`` below calls each prompt's **real** request builder
with fixed tiny inputs, through ``tests.llm_capture.CaptureClient``, once per
branch that adds text the model sees (OCR notes, routing branches, the copilot
forced-final call and message wrappers, ...). The captured ``create`` kwargs are
reduced to ``system`` / ``messages`` / ``tools`` / ``tool_choice`` / ``extra_body``
(model id, ``max_tokens``, timeouts and retries are budgets, not layout; base64
document bytes become ``"<base64>"``), and the fingerprint is SHA-256 over
``{"text": Prompt.text, "variants": {name: [request]}}``. The router's default
model ids are fingerprinted too, under ``"models"``.

``app/services/prompts/prompt_lock.json`` holds an append-only history per id:
``[{"version", "sha256", "variants"}]`` (``variants`` maps each render variant to
its own digest, only so a failure can name what changed). The assertions:

(a) the current ``(version, fingerprint)`` of every registered prompt equals the
    last history entry; the current models fingerprint equals the last models entry;
(b) versions within a history strictly increase (semver);
(c) no version appears twice with different shas;
(d) ``CHANGELOG.md`` mentions every current version (and the current models version);
(e) no retired id is registered, and every retired id stays reserved in the
    ``app.services.prompts`` docstring;
(f) ``set(RENDER_CASES) == set(PROMPT_REGISTRY)``;
(g) every fingerprint is deterministic: twice in-process and once in a subprocess
    with a different ``PYTHONHASHSEED``;
(h) when ``PROMPT_LOCK_BASE_REF`` is set (CI fetches the base branch and sets it),
    every entry in the base branch's lock is still present, unchanged and in order,
    so the only way to go green after a change is to APPEND a higher version. In CI
    a missing or unresolvable ref fails; locally it skips with a message;
(i) for every render case, adding one character to the prompt text (or, for the
    text-less QMS prompt, its template) changes every one of its variants' captured
    requests, so no render case can silently capture nothing.

THE FIXED INPUTS ARE PART OF EVERY FINGERPRINT. Never edit an existing variant's
inputs: (h) makes the old entry immutable, so the edit would force a version bump
with no prompt change. To cover a new branch, add a new variant in the same change
that adds the branch (which changes the prompt anyway and needs a bump).

Unmarked on purpose: it runs in the main required suite and in a developer's plain
``pytest``. Rendering touches no database and no network.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import pkgutil
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ContextManager, Dict, Iterator, List, Mapping, Optional, Tuple
from unittest.mock import patch

import pytest

import app.services.prompts as prompts_package
from app.api.endpoints import search as search_endpoint
from app.models.user import UserRole
from app.schemas.hank_preferences import HankPreferenceValues
from app.services import (
    copilot_service,
    hank_intake_service,
    laser_nest_extraction_service,
    llm_model_router,
    llm_service,
    pdf_service,
    qms_extraction_service,
    routing_generation_service,
    sheet_stock_ai_resolver,
)
from app.services.pdf_service import DocumentExtractionResult
from app.services.prompts import PROMPT_REGISTRY
from app.services.sheet_stock_matcher import STATUS_AMBIGUOUS, CandidatePart, SheetSuggestion
from tests.llm_capture import (
    canonical_json,
    canonical_request,
    capture_request,
    prompt_fingerprint,
    sha256_hex,
    text_message,
    tool_use_message,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
PROMPTS_DIR = BACKEND_DIR / "app" / "services" / "prompts"
LOCK_PATH = PROMPTS_DIR / "prompt_lock.json"
CHANGELOG_PATH = PROMPTS_DIR / "CHANGELOG.md"
LOCK_REPO_PATH = "backend/app/services/prompts/prompt_lock.json"
BASE_REF_ENV = "PROMPT_LOCK_BASE_REF"
MODELS_KEY = "models"

COMPANY_ID = 1
MUTATION = "X"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


# ---------------------------------------------------------------------------
# Render cases: the real builders, fixed inputs, one variant per text branch
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RenderCase:
    """How to render one prompt id.

    ``variants`` maps a stable variant name to a zero-argument callable that returns
    the captured ``create`` kwargs. ``mutation`` returns a context manager that adds
    one character to the text that builder reads (assertion (i)).

    ``identical_variants`` names pairs that render the same request TODAY but are
    kept as sentinels: each pins an input that could grow a text branch later, so
    adding that branch changes the fingerprint instead of slipping past. Once the
    pair diverges, delete its entry.
    """

    variants: Mapping[str, Callable[[], Dict[str, Any]]]
    mutation: Callable[[], ContextManager[Any]]
    identical_variants: Tuple[Tuple[str, str], ...] = ()


@contextmanager
def _mutated_prompt(module: Any, attribute: str) -> Iterator[None]:
    """Replace ``module.attribute`` (a ``Prompt``) with a copy whose text has one more character."""
    original = getattr(module, attribute)
    with patch.object(module, attribute, dataclasses.replace(original, text=original.text + MUTATION)):
        yield


@contextmanager
def _mutated_string(module: Any, attribute: str) -> Iterator[None]:
    """Append one character to a template string constant the builder reads."""
    with patch.object(module, attribute, getattr(module, attribute) + MUTATION):
        yield


def _mutation(module: Any, attribute: str, *, template: bool = False) -> Callable[[], ContextManager[Any]]:
    return lambda: (_mutated_string if template else _mutated_prompt)(module, attribute)


# -- po_extraction / bom_extraction ------------------------------------------
PO_TEXT = "PURCHASE ORDER PO-1001\nVendor: Capture Supply Co.\n1  BRKT-100  Bracket  10 EA  $4.50  $45.00"
BOM_TEXT = "ASSEMBLY BRKT-ASSY-1 REV B\n1  BRKT-100  Bracket  2\n2  HW-1032  10-32 PEM nut  4"


def _po(is_ocr: bool) -> Dict[str, Any]:
    return capture_request(
        lambda: llm_service.extract_po_data_with_llm(PO_TEXT, is_ocr=is_ocr, document_type="po", company_id=COMPANY_ID)
    )


def _bom(is_ocr: bool) -> Dict[str, Any]:
    return capture_request(
        lambda: llm_service.extract_bom_data_with_llm(BOM_TEXT, is_ocr=is_ocr, company_id=COMPANY_ID)
    )


# -- laser_nest_extraction / verification / segmentation ----------------------
LASER_PDF_PATH = "/drift-guard/05749.pdf"
LASER_FILE_NAME = "05749.pdf"
LASER_PDF_BYTES = b"%PDF-1.4 drift-guard nest report"
LASER_FALLBACK_TEXT = "05749120x600.250\nERMAKSAN FIBERMAK 304 SS\nNo. of Sheets: 3"
LASER_PASS_ONE = {
    "cnc_number": "05749",
    "material": "304 SS",
    "thickness": "0.250",
    "sheet_size": "120 x 60",
    "planned_runs": 3,
    "confidence": {
        "cnc_number": "high",
        "material": "high",
        "thickness": "high",
        "sheet_size": "high",
        "planned_runs": "medium",
    },
    "extraction_confidence": "high",
}


def _ocr_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("The drift guard supplies fallback text directly; OCR must not run.")


def _laser_patches(*, native: bool, is_ocr: bool = False) -> List[Any]:
    """Pin the native/text-fallback decision and the fallback text, independent of tesseract."""
    patches = [
        patch.object(laser_nest_extraction_service, "_pdf_size_exceeds_native_cap", lambda path: not native),
        patch.object(laser_nest_extraction_service, "_read_pdf_bytes", lambda path: LASER_PDF_BYTES),
        # Belt and braces: the fallback text is supplied below, so a machine with
        # tesseract installed and CI without it render the same bytes.
        patch.object(pdf_service, "_extract_ocr_text", _ocr_must_not_run),
    ]
    if not native:
        patches.append(
            patch.object(
                laser_nest_extraction_service,
                "extract_text_from_document",
                lambda path: DocumentExtractionResult(text=LASER_FALLBACK_TEXT, is_ocr=is_ocr, page_count=1),
            )
        )
    return patches


def _laser_extraction(
    *, native: bool, is_ocr: bool = False, filename_is_cnc_hint: bool = True, cnc_hint: Optional[str] = None
) -> Dict[str, Any]:
    return capture_request(
        lambda: laser_nest_extraction_service.extract_nest_fields_from_pdf(
            LASER_PDF_PATH,
            LASER_FILE_NAME,
            company_id=COMPANY_ID,
            cnc_hint=cnc_hint,
            filename_is_cnc_hint=filename_is_cnc_hint,
        ),
        patches=_laser_patches(native=native, is_ocr=is_ocr),
    )


def _laser_verification(*, native: bool, is_ocr: bool = False, filename_is_cnc_hint: bool = True) -> Dict[str, Any]:
    # Pass 1 is scripted; pass 2 (the verification prompt) is the captured call.
    return capture_request(
        lambda: laser_nest_extraction_service.extract_nest_fields_from_pdf(
            LASER_PDF_PATH,
            LASER_FILE_NAME,
            company_id=COMPANY_ID,
            filename_is_cnc_hint=filename_is_cnc_hint,
        ),
        scripted=[text_message(json.dumps(LASER_PASS_ONE))],
        patches=_laser_patches(native=native, is_ocr=is_ocr),
    )


def _laser_segmentation() -> Dict[str, Any]:
    return capture_request(
        lambda: laser_nest_extraction_service.segment_nest_pdf(
            LASER_PDF_PATH, "miratech-package.pdf", page_count=3, company_id=COMPANY_ID
        ),
        patches=_laser_patches(native=True),
    )


# -- sheet_stock_disambiguation -----------------------------------------------
def _sheet_suggestions() -> Dict[str, SheetSuggestion]:
    """Three ambiguous rows forming two groups; together they reach every render branch."""

    def grade_shortlist() -> List[CandidatePart]:
        return [
            CandidatePart(
                part_id=11,
                part_number="SHT-304-250-120X60",
                part_name="304 SS sheet 0.250 x 120 x 60",
                unit_of_measure="EA",
                score=0.82,
                reason="Thickness 0.250 matches; grade 304 matches the nest.",
                on_hand=4,
                spec_thickness="0.250",
                spec_sheet_size="120 x 60",
            ),
            CandidatePart(
                part_id=12,
                part_number="SHT-316-250-120X60",
                part_name="",
                unit_of_measure=None,
                score=0.74,
                reason="",
                on_hand_known=False,
            ),
        ]

    size_shortlist = [
        CandidatePart(
            part_id=21,
            part_number="SHT-A36-125-96X48",
            part_name="A36 sheet 0.125 x 96 x 48",
            unit_of_measure="SHT",
            score=0.6,
            reason="Thickness 0.125 matches.",
            on_hand=2.5,
            spec_thickness="0.125",
            spec_sheet_size="96 x 48",
        )
    ]
    diagnostic = "Two sheets match 0.250 in.; the nest names no grade the catalog can confirm."
    return {
        "05749.pdf": SheetSuggestion(status=STATUS_AMBIGUOUS, candidates=grade_shortlist(), diagnostic=diagnostic),
        "05750.pdf": SheetSuggestion(status=STATUS_AMBIGUOUS, candidates=grade_shortlist(), diagnostic=diagnostic),
        "05751.pdf": SheetSuggestion(status=STATUS_AMBIGUOUS, candidates=size_shortlist, diagnostic=None),
    }


def _sheet_stock() -> Dict[str, Any]:
    return capture_request(
        lambda: sheet_stock_ai_resolver.resolve_ambiguous_sheet_matches(_sheet_suggestions(), company_id=COMPANY_ID)
    )


# -- routing_generation -------------------------------------------------------
ROUTING_TEXT = "TITLE: MOUNTING BRACKET\nMATERIAL: 304 SS 0.125\nFINISH: POWDER COAT BLACK\n4X DIA .250 THRU"
ROUTING_WORK_CENTER_TYPES = ["welding", "laser", "press_brake", "inspection", "shipping"]
ROUTING_GEOMETRY = {
    "cut_length": 42.0,
    "hole_count": 4,
    "bend_count": 2,
    "flat_area": 120.5,
    "bbox": {"min_x": 0.0, "max_x": 12.0, "min_y": 0.0, "max_y": 10.0},
}


def _routing(**overrides: Any) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "drawing_text": ROUTING_TEXT,
        "geometry": None,
        "work_center_types": ROUTING_WORK_CENTER_TYPES,
        "is_ocr": False,
        "part_context": None,
        "is_assembly": False,
        "learned_examples_context": None,
        "company_id": COMPANY_ID,
    }
    kwargs.update(overrides)
    return capture_request(lambda: routing_generation_service.extract_routing_data_with_llm(**kwargs))


# -- qms_clause_extraction ----------------------------------------------------
QMS_TEXT = "4 Context of the organization\n4.1 Understanding the organization and its context\nThe organization shall"


def _qms(version: Optional[str]) -> Dict[str, Any]:
    return capture_request(
        lambda: qms_extraction_service.extract_clauses_with_llm(QMS_TEXT, "AS9100", version, COMPANY_ID)
    )


# -- nl_search_intent ---------------------------------------------------------
def _nl_search() -> Dict[str, Any]:
    return capture_request(
        lambda: search_endpoint._llm_interpret_nl_search("late laser jobs waiting on material", company_id=COMPANY_ID)
    )


# -- hank_document_intake -----------------------------------------------------
def _hank_pdf() -> Dict[str, Any]:
    pages = ["PACKING SLIP PS-1001\nBRKT-100  Qty Shipped 12 EA  Qty Ordered 20"]
    return capture_request(
        lambda: hank_intake_service._analyze_document(b"%PDF-1.4 drift-guard slip", COMPANY_ID, pages, "pdf", [], [])
    )


def _hank_office() -> Dict[str, Any]:
    pages = ["[P1] PURCHASE ORDER PO-1001", "[T1R2] BRKT-100 | 10 | EA | 4.50"]
    labels = ["Paragraphs", "Table 1"]
    return capture_request(lambda: hank_intake_service._analyze_document(b"", COMPANY_ID, pages, "docx", labels, []))


# -- copilot_chat -------------------------------------------------------------
COPILOT_HISTORY = [
    {"role": "user", "content": "Hi Hank"},
    {"role": "assistant", "content": "Hi. What do you need?"},
    {"role": "user", "content": "What's the status of WO-1001?"},
]
COPILOT_CONTEXT_HINT = "Viewing work order WO-1001"
COPILOT_MANIFEST = [
    {
        "file_id": 41,
        "version": 2,
        "filename": "PS-1001.pdf",
        "source_format": "pdf",
        "classification": "packing_slip",
        "confidence": "medium",
        "summary": "Packing slip PS-1001 for BRKT-100.",
        "field_count": 3,
        "line_count": 1,
        "warnings": ["Quantities are suggestions until an employee reviews them."],
    }
]


def _copilot_service() -> copilot_service.CopilotService:
    service = copilot_service.CopilotService(
        SimpleNamespace(in_transaction=lambda: False),  # no database: the loop only asks this
        company_id=COMPANY_ID,
        user=SimpleNamespace(id=7, role=UserRole.ADMIN),
    )
    service.document_manifest = [dict(item) for item in COPILOT_MANIFEST]
    return service


def _copilot_patches(*, tool_rounds: int) -> List[Any]:
    return [
        # The saved-preferences read is the loop's only database access; an employee
        # with nothing saved gets the schema defaults, which is what is rendered here.
        patch.object(
            copilot_service, "get_hank_preference_values", lambda db, company_id, user_id: HankPreferenceValues()
        ),
        # Pinned rather than read from COPILOT_MAX_TOOL_ROUNDS, which an env var sets:
        # with 0 rounds the first call would itself be the forced-final call.
        patch.object(copilot_service, "COPILOT_MAX_TOOL_ROUNDS", tool_rounds),
    ]


def _fixed_tool_execution(self: Any, name: str, tool_input: Any) -> copilot_service.ToolExecution:
    return copilot_service.ToolExecution(tool=name, payload={"drift_guard": "fixed tool result"}, summary="fixed")


def _copilot_first_call() -> Dict[str, Any]:
    return capture_request(
        lambda: _copilot_service().stream_chat(messages=COPILOT_HISTORY, context_hint=COPILOT_CONTEXT_HINT),
        patches=_copilot_patches(tool_rounds=8),
    )


def _copilot_forced_final() -> Dict[str, Any]:
    # One tool round, then the forced final answer (tool_choice "none"): the call
    # that carries the assistant tool_use echo and the tool_result wrapper.
    return capture_request(
        lambda: _copilot_service().stream_chat(messages=COPILOT_HISTORY, context_hint=COPILOT_CONTEXT_HINT),
        scripted=[tool_use_message("lookup_work_order", {"work_order_number": "WO-1001"})],
        patches=[
            *_copilot_patches(tool_rounds=1),
            patch.object(copilot_service.CopilotService, "execute_tool", _fixed_tool_execution),
        ],
    )


RENDER_CASES: Dict[str, RenderCase] = {
    "po_extraction": RenderCase(
        variants={"text": lambda: _po(False), "ocr": lambda: _po(True)},
        mutation=_mutation(llm_service, "PO_EXTRACTION_PROMPT"),
    ),
    "bom_extraction": RenderCase(
        variants={"text": lambda: _bom(False), "ocr": lambda: _bom(True)},
        mutation=_mutation(llm_service, "BOM_EXTRACTION_PROMPT"),
    ),
    "laser_nest_extraction": RenderCase(
        variants={
            "native_pdf": lambda: _laser_extraction(native=True),
            "native_pdf_segment_hint": lambda: _laser_extraction(
                native=True, filename_is_cnc_hint=False, cnc_hint="05749"
            ),
            "native_pdf_no_hint": lambda: _laser_extraction(native=True, filename_is_cnc_hint=False),
            "text_fallback": lambda: _laser_extraction(native=False, is_ocr=False),
            "text_fallback_ocr": lambda: _laser_extraction(native=False, is_ocr=True),
        },
        mutation=_mutation(laser_nest_extraction_service, "LASER_NEST_EXTRACTION_PROMPT"),
    ),
    "laser_nest_verification": RenderCase(
        variants={
            "native_pdf": lambda: _laser_verification(native=True),
            "native_pdf_no_filename_hint": lambda: _laser_verification(native=True, filename_is_cnc_hint=False),
            "text_fallback": lambda: _laser_verification(native=False, is_ocr=False),
            "text_fallback_ocr": lambda: _laser_verification(native=False, is_ocr=True),
        },
        mutation=_mutation(laser_nest_extraction_service, "LASER_NEST_VERIFICATION_PROMPT"),
        # Pass 2 adds no OCR note today (is_ocr only reaches model routing); the OCR
        # variant stays so that adding one is caught.
        identical_variants=(("text_fallback", "text_fallback_ocr"),),
    ),
    "laser_nest_segmentation": RenderCase(
        variants={"native_pdf": _laser_segmentation},
        mutation=_mutation(laser_nest_extraction_service, "LASER_NEST_SEGMENTATION_PROMPT"),
    ),
    "sheet_stock_disambiguation": RenderCase(
        variants={"fixed_groups": _sheet_stock},
        mutation=_mutation(sheet_stock_ai_resolver, "SHEET_STOCK_DISAMBIGUATION_PROMPT"),
    ),
    "routing_generation": RenderCase(
        variants={
            "text": lambda: _routing(),
            "geometry": lambda: _routing(geometry=ROUTING_GEOMETRY),
            "learned_context": lambda: _routing(
                learned_examples_context="BRKT-090: Laser Cut (laser) -> Press Brake Form (press_brake) -> Inspect"
            ),
            "assembly": lambda: _routing(is_assembly=True),
            "ocr": lambda: _routing(is_ocr=True),
            "part_context": lambda: _routing(part_context="Part BRKT-100 rev B: sheet-metal mounting bracket"),
            "default_work_center_types": lambda: _routing(work_center_types=None),
        },
        mutation=_mutation(routing_generation_service, "ROUTING_GENERATION_PROMPT"),
    ),
    "qms_clause_extraction": RenderCase(
        variants={"with_version": lambda: _qms("Rev D"), "no_version": lambda: _qms(None)},
        # text=None: the prompt is the per-request template, so mutate that.
        mutation=_mutation(qms_extraction_service, "QMS_CLAUSE_EXTRACTION_TEMPLATE", template=True),
    ),
    "copilot_chat": RenderCase(
        variants={"first_call": _copilot_first_call, "forced_final": _copilot_forced_final},
        mutation=_mutation(copilot_service, "COPILOT_CHAT_PROMPT"),
    ),
    "nl_search_intent": RenderCase(
        variants={"query": _nl_search},
        # search._llm_interpret_nl_search imports the prompt from the package at call time.
        mutation=_mutation(prompts_package, "NL_SEARCH_INTENT_PROMPT"),
    ),
    "hank_document_intake": RenderCase(
        variants={"pdf": _hank_pdf, "office": _hank_office},
        mutation=_mutation(hank_intake_service, "HANK_INTAKE_PROMPT"),
    ),
}


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
def render_variants(prompt_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Canonical captured request(s) per variant, in ``RENDER_CASES`` order."""
    return {name: [canonical_request(render())] for name, render in RENDER_CASES[prompt_id].variants.items()}


def models_document() -> Dict[str, Any]:
    """The router's default model ids, as hashed under ``"models"``."""
    return {
        "DEFAULT_ANTHROPIC_MODELS": {
            tier.value: model for tier, model in llm_model_router.DEFAULT_ANTHROPIC_MODELS.items()
        },
        "DEFAULT_COPILOT_MODEL": llm_model_router.DEFAULT_COPILOT_MODEL,
    }


def current_fingerprints() -> Dict[str, Any]:
    """``{"prompts": {id: {"version", "sha256", "variants"}}, "models": {"sha256"}}`` for the working tree."""
    prompts: Dict[str, Any] = {}
    for prompt_id in sorted(RENDER_CASES):
        variants = render_variants(prompt_id)
        prompt = PROMPT_REGISTRY[prompt_id]
        prompts[prompt_id] = {
            "version": prompt.version,
            "sha256": prompt_fingerprint(prompt.text, variants),
            "variants": {name: sha256_hex(captures) for name, captures in variants.items()},
        }
    return {"prompts": prompts, MODELS_KEY: {"sha256": sha256_hex(models_document())}}


@pytest.fixture(scope="module")
def current() -> Dict[str, Any]:
    return current_fingerprints()


@pytest.fixture(scope="module")
def lock() -> Dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Failure messages
# ---------------------------------------------------------------------------
def _prompt_location(prompt_id: str) -> Tuple[str, str]:
    """(constant name, repo-relative module path) of the registered ``Prompt``."""
    target = PROMPT_REGISTRY[prompt_id]
    for module_info in pkgutil.iter_modules(prompts_package.__path__):
        module = importlib.import_module(f"{prompts_package.__name__}.{module_info.name}")
        for name, value in vars(module).items():
            if value is target and name.isupper():
                return name, f"app/services/prompts/{module_info.name}.py"
    return f"the '{prompt_id}' Prompt", "app/services/prompts/"


def _next_patch(version: str) -> str:
    match = _SEMVER.match(version)
    if not match:
        return "<new version>"
    major, minor, patch_level = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch_level + 1}"


def _short(sha: str) -> str:
    return f"{sha[:12]}…"


def _changed_variants(locked: Mapping[str, str], now: Mapping[str, str]) -> str:
    changed = [name for name in now if locked.get(name) != now[name]]
    removed = [name for name in locked if name not in now]
    parts = []
    if changed:
        parts.append(", ".join(changed))
    if removed:
        parts.append("removed: " + ", ".join(removed))
    return "; ".join(parts) or "none (Prompt.text only)"


def _drift_message(prompt_id: str, locked: Mapping[str, Any], now: Mapping[str, Any]) -> str:
    constant, module_path = _prompt_location(prompt_id)
    new_version = now["version"] if now["version"] != locked["version"] else _next_patch(locked["version"])
    entry = {"version": new_version, "sha256": now["sha256"], "variants": now["variants"]}
    if now["version"] == locked["version"]:
        headline = f"Prompt '{prompt_id}' changed what the model sees, but its version did not change."
        step_one = (
            f"  1. Bump {constant}.version in {module_path}\n"
            "     (patch = wording/layout only, minor = behavior, major = output contract)."
        )
    else:
        headline = (
            f"Prompt '{prompt_id}' is now version {now['version']}, but prompt_lock.json's last entry for it "
            f"is {locked['version']}."
        )
        step_one = f"  1. ({constant}.version in {module_path} is already bumped to {now['version']}.)"
    return (
        f"{headline}\n"
        f"  locked: {locked['version']}  sha256={_short(locked['sha256'])}   "
        f"now: {now['version']}  sha256={_short(now['sha256'])}   "
        f"variant(s) that changed: {_changed_variants(locked.get('variants') or {}, now['variants'])}\n"
        "If this change is deliberate:\n"
        f"{step_one}\n"
        f"  2. Add a CHANGELOG.md line:  - `{prompt_id}` {locked['version']} → {new_version} — "
        "<what changed and why>\n"
        f'  3. APPEND to app/services/prompts/prompt_lock.json under "{prompt_id}":\n'
        f"       {json.dumps(entry)}\n"
        "     Never edit an existing entry: CI compares the lock with the base branch.\n"
        "If you did not mean to change the prompt, the request layout, a schema or the tool list, revert the edit."
    )


def _models_message(locked: Mapping[str, Any], now_sha: str) -> str:
    major, minor = (int(part) for part in locked["version"].split(".")[:2])
    new_version = f"{major}.{minor + 1}.0"
    entry = {"version": new_version, "sha256": now_sha}
    return (
        "The router's default model ids changed (llm_model_router.DEFAULT_ANTHROPIC_MODELS / "
        "DEFAULT_COPILOT_MODEL), but prompt_lock.json was not updated.\n"
        f"  locked: models {locked['version']}  sha256={_short(locked['sha256'])}   now: sha256={_short(now_sha)}\n"
        f"  current defaults: {canonical_json(models_document())}\n"
        "A default-model change is the largest behavior change a prompt can see. If it is deliberate:\n"
        f"  1. Add a CHANGELOG.md line:  - `models` {locked['version']} → {new_version} — <which model and why>\n"
        f'  2. APPEND to app/services/prompts/prompt_lock.json under "models":\n'
        f"       {json.dumps(entry)}\n"
        "     Never edit an existing entry: CI compares the lock with the base branch."
    )


# ---------------------------------------------------------------------------
# (a) the working tree matches the last lock entry
# ---------------------------------------------------------------------------
def test_every_prompt_matches_its_last_lock_entry(current: Dict[str, Any], lock: Dict[str, Any]) -> None:
    problems = []
    for prompt_id, now in current["prompts"].items():
        history = lock["prompts"].get(prompt_id)
        if not history:
            problems.append(
                f"Prompt '{prompt_id}' has no history in prompt_lock.json. APPEND under \"{prompt_id}\":\n"
                f"       {json.dumps({'version': now['version'], 'sha256': now['sha256'], 'variants': now['variants']})}"
            )
            continue
        locked = history[-1]
        if (locked["version"], locked["sha256"]) != (now["version"], now["sha256"]):
            problems.append(_drift_message(prompt_id, locked, now))
    assert not problems, "\n\n".join(problems)


def test_default_models_match_the_last_models_entry(current: Dict[str, Any], lock: Dict[str, Any]) -> None:
    history = lock[MODELS_KEY]
    assert history, "prompt_lock.json has no 'models' history."
    locked = history[-1]
    assert locked["sha256"] == current[MODELS_KEY]["sha256"], _models_message(locked, current[MODELS_KEY]["sha256"])


def test_lock_has_no_orphan_histories(lock: Dict[str, Any]) -> None:
    """A history belongs to a registered id or a retired one; histories are never deleted."""
    orphans = sorted(set(lock["prompts"]) - set(PROMPT_REGISTRY) - set(lock["retired"]))
    assert not orphans, (
        f"prompt_lock.json has histories for ids that are neither registered nor retired: {orphans}. "
        "If a prompt was retired, add it to the lock's \"retired\" map (and the CHANGELOG and the registry "
        "docstring); keep its history."
    )


# ---------------------------------------------------------------------------
# (b) / (c) history shape
# ---------------------------------------------------------------------------
def _histories(lock: Dict[str, Any]) -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
    yield MODELS_KEY, lock[MODELS_KEY]
    yield from sorted(lock["prompts"].items())


def _semver(version: str) -> Tuple[int, int, int]:
    match = _SEMVER.match(version)
    assert match, f"'{version}' is not a MAJOR.MINOR.PATCH version"
    major, minor, patch_level = (int(part) for part in match.groups())
    return major, minor, patch_level


def test_versions_strictly_increase_within_each_history(lock: Dict[str, Any]) -> None:
    problems = []
    for history_id, history in _histories(lock):
        versions = [entry["version"] for entry in history]
        parsed = [_semver(version) for version in versions]
        if any(later <= earlier for earlier, later in zip(parsed, parsed[1:])):
            problems.append(f"{history_id}: {versions}")
    assert not problems, "Versions must strictly increase (append a HIGHER version):\n  " + "\n  ".join(problems)


def test_no_version_is_locked_twice_with_different_shas(lock: Dict[str, Any]) -> None:
    problems = []
    for history_id, history in _histories(lock):
        shas_by_version: Dict[str, set] = {}
        for entry in history:
            shas_by_version.setdefault(entry["version"], set()).add(entry["sha256"])
        problems.extend(f"{history_id} {version}" for version, shas in shas_by_version.items() if len(shas) > 1)
    assert not problems, f"One version, two fingerprints: {problems}. Bump the version instead."


# ---------------------------------------------------------------------------
# (d) the CHANGELOG records every current version
# ---------------------------------------------------------------------------
def _normalized_changelog() -> str:
    text = CHANGELOG_PATH.read_text(encoding="utf-8").replace("`", "").replace("*", "")
    return re.sub(r"\s+", " ", text)


def _changelog_mentions(changelog: str, history_id: str, version: str) -> bool:
    pattern = rf"\b{re.escape(history_id)}\s+(?:\d+\.\d+\.\d+\s*(?:→|->)\s*)?{re.escape(version)}(?![\d.])"
    return re.search(pattern, changelog) is not None


def test_changelog_mentions_every_current_version(lock: Dict[str, Any]) -> None:
    changelog = _normalized_changelog()
    wanted = [(prompt_id, prompt.version) for prompt_id, prompt in sorted(PROMPT_REGISTRY.items())]
    wanted.append((MODELS_KEY, lock[MODELS_KEY][-1]["version"]))
    missing = [
        f"{history_id} {version}"
        for history_id, version in wanted
        if not _changelog_mentions(changelog, history_id, version)
    ]
    assert not missing, (
        f"app/services/prompts/CHANGELOG.md has no entry for: {missing}. Add a line such as "
        "`- `<id>` <old> → <new> — <what changed and why>`."
    )


def test_changelog_matcher_rejects_a_neighbouring_id() -> None:
    """The match is per id: one id's version on a shared line must not satisfy another id."""
    line = "laser_nest_extraction 1.0.0 → 1.1.0 and laser_nest_verification 1.0.0 → 1.0.1"
    assert _changelog_mentions(line, "laser_nest_extraction", "1.1.0")
    assert _changelog_mentions(line, "laser_nest_verification", "1.0.1")
    assert not _changelog_mentions(line, "laser_nest_verification", "1.1.0")
    assert not _changelog_mentions("copilot_chat 1.7.10", "copilot_chat", "1.7.1")


# ---------------------------------------------------------------------------
# (e) retired ids stay retired and reserved
# ---------------------------------------------------------------------------
def test_retired_ids_are_not_registered_and_stay_reserved(lock: Dict[str, Any]) -> None:
    retired = set(lock["retired"])
    assert retired >= {"auto_execute_decision", "estimate_drawing_extraction"}
    reused = sorted(retired & set(PROMPT_REGISTRY))
    assert not reused, f"Retired prompt ids are registered again: {reused}. Retired ids are never reused."
    docstring = prompts_package.__doc__ or ""
    unreserved = sorted(prompt_id for prompt_id in retired if prompt_id not in docstring)
    assert not unreserved, f"Retired ids missing from the app.services.prompts docstring's reserved list: {unreserved}"
    changelog = _normalized_changelog()
    unrecorded = sorted(prompt_id for prompt_id in retired if not re.search(rf"\b{prompt_id}\b RETIRED", changelog))
    assert not unrecorded, f"Retired ids with no 'RETIRED' CHANGELOG entry: {unrecorded}"


# ---------------------------------------------------------------------------
# (f) every registered prompt is rendered
# ---------------------------------------------------------------------------
def test_every_registered_prompt_has_a_render_case() -> None:
    assert set(RENDER_CASES) == set(PROMPT_REGISTRY), (
        f"Registered but not rendered: {sorted(set(PROMPT_REGISTRY) - set(RENDER_CASES))}; "
        f"rendered but not registered: {sorted(set(RENDER_CASES) - set(PROMPT_REGISTRY))}. "
        "A new prompt needs a RENDER_CASES entry (one variant per branch that adds text) and a lock history."
    )


# ---------------------------------------------------------------------------
# (g) fingerprints are deterministic
# ---------------------------------------------------------------------------
_SUBPROCESS_MARKER = "DRIFT_GUARD_FINGERPRINTS="


def test_fingerprints_are_deterministic_in_process_and_across_hash_seeds(current: Dict[str, Any]) -> None:
    again = current_fingerprints()
    assert again == current, "Rendering the same prompts twice in one process gave different fingerprints."

    seed = "2" if os.environ.get("PYTHONHASHSEED") == "1" else "1"
    code = (
        "import json\n"
        "from tests.test_prompt_drift_guard import current_fingerprints\n"
        f"print({_SUBPROCESS_MARKER!r} + json.dumps(current_fingerprints()))\n"
    )
    python_path = os.pathsep.join(filter(None, [str(BACKEND_DIR), os.environ.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, f"Fingerprint subprocess failed:\n{completed.stderr[-4000:]}"
    payload = [line for line in completed.stdout.splitlines() if line.startswith(_SUBPROCESS_MARKER)]
    assert payload, f"Fingerprint subprocess printed no result:\n{completed.stdout[-2000:]}"
    other = json.loads(payload[-1][len(_SUBPROCESS_MARKER) :])
    differing = sorted(
        prompt_id
        for prompt_id in current["prompts"]
        if other["prompts"].get(prompt_id) != current["prompts"][prompt_id]
    )
    assert not differing and other[MODELS_KEY] == current[MODELS_KEY], (
        f"Fingerprints differ under PYTHONHASHSEED={seed}: {differing or ['models']}. A render depends on set "
        "or hash ordering; fix the builder (or the render case) so the request bytes are stable."
    )


# ---------------------------------------------------------------------------
# (h) the lock is append-only against the base branch
# ---------------------------------------------------------------------------
def append_only_violations(base: Mapping[str, Any], head: Mapping[str, Any]) -> List[str]:
    """Every base entry must still be in ``head``, unchanged and in order; only appends are allowed."""
    problems: List[str] = []

    def compare(history_id: str, base_history: List[Any], head_history: Optional[List[Any]]) -> None:
        if head_history is None:
            problems.append(f"'{history_id}': the history was removed")
            return
        for index, base_entry in enumerate(base_history):
            if index >= len(head_history):
                problems.append(f"'{history_id}': entry {index} ({base_entry.get('version')}) was removed")
                return
            if head_history[index] != base_entry:
                problems.append(
                    f"'{history_id}': entry {index} ({base_entry.get('version')}) was edited in place. "
                    f"Base: {json.dumps(base_entry)}  Now: {json.dumps(head_history[index])}"
                )
                return

    compare(MODELS_KEY, base.get(MODELS_KEY) or [], head.get(MODELS_KEY))
    for prompt_id, base_history in sorted((base.get("prompts") or {}).items()):
        compare(prompt_id, base_history, (head.get("prompts") or {}).get(prompt_id))
    for prompt_id, retired_on in sorted((base.get("retired") or {}).items()):
        if (head.get("retired") or {}).get(prompt_id) != retired_on:
            problems.append(f"retired id '{prompt_id}' was removed from or changed in the \"retired\" map")
    return problems


def _ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False)


def _unavailable(reason: str) -> None:
    if _ci():
        pytest.fail(f"{reason} CI must check the prompt lock against the base branch; see ci-cd.yml backend-test.")
    pytest.skip(f"{reason} Skipped locally; set {BASE_REF_ENV}=origin/main (after `git fetch origin main`) to run it.")


def _check_lock_against_base_ref(lock: Dict[str, Any]) -> None:
    ref = os.environ.get(BASE_REF_ENV, "").strip()
    if not ref:
        _unavailable(f"{BASE_REF_ENV} is not set.")
    if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        _unavailable(f"{BASE_REF_ENV}={ref} does not resolve to a commit (was the base branch fetched?).")
    shown = _git("show", f"{ref}:{LOCK_REPO_PATH}")
    if shown.returncode != 0:
        # The ref exists but has no lock yet: the PR that introduces the lock.
        if _git("cat-file", "-e", f"{ref}:{LOCK_REPO_PATH}").returncode != 0:
            return
        pytest.fail(f"Could not read {LOCK_REPO_PATH} at {ref}: {shown.stderr.strip()}")
    problems = append_only_violations(json.loads(shown.stdout), lock)
    assert not problems, (
        f"prompt_lock.json is append-only, and these entries differ from {ref}:\n  "
        + "\n  ".join(problems)
        + "\nRestore them exactly, and APPEND a higher version for the change instead."
    )


def test_lock_is_append_only_against_the_base_branch(lock: Dict[str, Any]) -> None:
    _check_lock_against_base_ref(lock)


def test_append_only_check_rejects_edits_and_accepts_appends(lock: Dict[str, Any]) -> None:
    """Self-test for (h), on a copy of the real lock."""
    assert append_only_violations(lock, lock) == []

    prompt_id = "copilot_chat"
    last = lock["prompts"][prompt_id][-1]

    tampered = json.loads(json.dumps(lock))
    tampered["prompts"][prompt_id][-1]["sha256"] = "0" * 64
    assert any("edited in place" in problem for problem in append_only_violations(lock, tampered))

    removed = json.loads(json.dumps(lock))
    removed["prompts"][prompt_id].pop()
    assert any("removed" in problem for problem in append_only_violations(lock, removed))

    tampered_models = json.loads(json.dumps(lock))
    tampered_models[MODELS_KEY][-1]["sha256"] = "0" * 64
    assert any("'models'" in problem for problem in append_only_violations(lock, tampered_models))

    unretired = json.loads(json.dumps(lock))
    unretired["retired"].pop("estimate_drawing_extraction")
    assert any("estimate_drawing_extraction" in problem for problem in append_only_violations(lock, unretired))

    appended = json.loads(json.dumps(lock))
    appended["prompts"][prompt_id].append({**last, "version": _next_patch(last["version"]), "sha256": "f" * 64})
    assert append_only_violations(lock, appended) == []


def test_ci_without_a_base_ref_fails_rather_than_skips(monkeypatch: pytest.MonkeyPatch, lock: Dict[str, Any]) -> None:
    monkeypatch.delenv(BASE_REF_ENV, raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        _check_lock_against_base_ref(lock)
    monkeypatch.setenv(BASE_REF_ENV, "refs/drift-guard/no-such-ref")
    with pytest.raises(pytest.fail.Exception):
        _check_lock_against_base_ref(lock)
    monkeypatch.setenv("CI", "")
    with pytest.raises(pytest.skip.Exception):
        _check_lock_against_base_ref(lock)


# ---------------------------------------------------------------------------
# (i) every render case captures the text it claims to lock
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prompt_id", sorted(RENDER_CASES))
def test_one_character_change_reaches_every_variant(prompt_id: str) -> None:
    baseline = render_variants(prompt_id)
    with RENDER_CASES[prompt_id].mutation():
        mutated = render_variants(prompt_id)
    unchanged = [name for name in baseline if canonical_json(baseline[name]) == canonical_json(mutated[name])]
    assert not unchanged, (
        f"Adding one character to '{prompt_id}''s prompt text left these variants' requests unchanged: {unchanged}. "
        "The render case is not capturing the text the builder sends (wrong patch target, or the builder "
        "returned before calling run_llm_task)."
    )


@pytest.mark.parametrize("prompt_id", sorted(RENDER_CASES))
def test_each_variant_renders_a_distinct_request(prompt_id: str) -> None:
    """A variant identical to a sibling is not exercising the branch it is named for.

    The only exceptions are the declared ``identical_variants`` sentinels, and each
    of those must still be identical: once the pair diverges, its declaration is stale.
    """
    case = RENDER_CASES[prompt_id]
    rendered = {name: canonical_json(captures) for name, captures in render_variants(prompt_id).items()}
    declared = {frozenset(pair) for pair in case.identical_variants}
    stale = sorted(tuple(sorted(pair)) for pair in declared if len({rendered[name] for name in pair}) != 1)
    assert not stale, f"'{prompt_id}' identical_variants now differ, so delete the declaration: {stale}"
    duplicates = [
        (first, second)
        for index, first in enumerate(rendered)
        for second in list(rendered)[index + 1 :]
        if rendered[first] == rendered[second] and frozenset((first, second)) not in declared
    ]
    assert not duplicates, f"'{prompt_id}' variants render identical requests: {duplicates}"


# ---------------------------------------------------------------------------
# The canonical form itself
# ---------------------------------------------------------------------------
def test_canonical_request_keeps_layout_and_drops_budgets() -> None:
    request = {
        "model": "claude-x",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "QUJD"}},
                    {"type": "text", "text": "hi"},
                ],
            }
        ],
        "system": "sys",
        "tool_choice": {"type": "none"},
    }
    canonical = canonical_request(request)
    assert list(canonical) == ["system", "messages", "tool_choice"]
    assert canonical["messages"][0]["content"][0]["source"]["data"] == "<base64>"
    assert request["messages"][0]["content"][0]["source"]["data"] == "QUJD", "the capture must not be mutated"


def test_canonical_request_refuses_an_unclassified_parameter() -> None:
    with pytest.raises(ValueError, match="temperature"):
        canonical_request({"model": "m", "max_tokens": 1, "messages": [], "temperature": 0.2})


def test_canonical_json_refuses_unknown_objects_but_maps_sdk_sentinels() -> None:
    import anthropic

    assert canonical_json({"a": anthropic.NOT_GIVEN}) == '{"a":"<NOT_GIVEN>"}'
    with pytest.raises(TypeError):
        canonical_json({"a": object()})


def test_capture_fails_when_the_builder_never_calls_the_model() -> None:
    from tests.llm_capture import CaptureNotReached

    with pytest.raises(CaptureNotReached):
        capture_request(lambda: None)
    # The scripted reply ends the builder's only call, so the capture is never reached.
    with pytest.raises(CaptureNotReached, match="1 call"):
        capture_request(
            lambda: llm_service.extract_po_data_with_llm(PO_TEXT, company_id=COMPANY_ID), scripted=[text_message("{}")]
        )
