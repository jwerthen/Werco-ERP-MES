"""GUARD: every datetime an API response model serializes carries its UTC offset.

The contract (CLAUDE.md -> "store UTC, serve UTC (Z), display Central"; docs/API.md ->
Timestamps): a ``datetime`` in a JSON response is ISO-8601 UTC with a trailing ``Z``.
The schema stores NAIVE UTC, and a bare pydantic ``BaseModel`` serializes a naive
datetime with no offset at all (``2026-01-15T06:00:00``), which every consumer -- the
SPA, the MCP tools, the Claude plugin -- then has to guess the zone of.
``app.schemas.base.UTCModel`` carries ``json_encoders={datetime: to_utc_iso}``;
response schemas inherit it.

This guard walks every ``APIRoute.response_model`` in ``app.main.app``, recursing into
every pydantic model reachable through its fields (``List[...]``, ``Optional[...]``,
unions, dict values), and fails on a model with a ``datetime`` field that has neither
the UTC encoder in its config nor a ``field_serializer`` for that field.

Nesting is the trap it exists for: ``json_encoders`` do NOT reach into a nested bare
``BaseModel`` -- a ``UTCModel`` parent with a bare child still emits the child's
datetimes with no offset -- so each nested model must carry the encoder itself.
``test_nested_bare_model_inside_utc_model_is_caught`` pins that the walker sees it.

``KNOWN_NAIVE_MODELS`` is the allowlist, keyed ``module.QualName -> reason``. It is
EMPTY: the 2026-11 sweep converted all 30 offenders. It is stale-checked like the
tenant-scope guard -- an entry that no longer offends fails the test -- so it can only
shrink. Do not add to it; convert the model to ``UTCModel`` instead.

Not covered (by construction): routes that return a hand-built ``dict`` with no
``response_model`` -- those must run each datetime through ``to_utc_iso`` themselves
(``receiving.py``, ``traceability_service.trace_serial`` and ``reports.py`` do).
"""

import typing
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.time_utils import to_utc_iso
from app.main import app
from app.schemas.base import UTCModel

pytestmark = pytest.mark.unit

# module.QualName -> reason. Must stay empty unless a model genuinely cannot carry the
# encoder; a stale entry fails ``test_allowlist_is_not_stale``.
KNOWN_NAIVE_MODELS: Dict[str, str] = {}


def _contains_datetime(annotation) -> bool:
    if annotation is datetime:
        return True
    return any(_contains_datetime(arg) for arg in typing.get_args(annotation))


def _models_in(annotation, found: Set[type]) -> None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.add(annotation)
        return
    for arg in typing.get_args(annotation):
        _models_in(arg, found)


def _has_utc_encoder(model: type) -> bool:
    encoders = model.model_config.get("json_encoders") or {}
    return encoders.get(datetime) is to_utc_iso


def _serialized_fields(model: type) -> Set[str]:
    covered: Set[str] = set()
    for decorator in model.__pydantic_decorators__.field_serializers.values():
        covered.update(decorator.info.fields)
    return covered


def _key(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def naive_datetime_fields(roots: List[Tuple[type, str]]) -> Dict[str, Tuple[List[str], str]]:
    """``{model key: (offending field names, a route path that reaches it)}``."""
    offenders: Dict[str, Tuple[List[str], str]] = {}
    seen: Set[type] = set()

    def walk(model: type, via: str) -> None:
        if model in seen:
            return
        seen.add(model)
        encoder = _has_utc_encoder(model)
        serialized = _serialized_fields(model)
        bad = [
            name
            for name, field in model.model_fields.items()
            if _contains_datetime(field.annotation) and not encoder and name not in serialized
        ]
        if bad:
            offenders[_key(model)] = (sorted(bad), via)
        for field in model.model_fields.values():
            nested: Set[type] = set()
            _models_in(field.annotation, nested)
            for child in nested:
                walk(child, via)

    for root, via in roots:
        walk(root, via)
    return offenders


def _route_roots() -> List[Tuple[type, str]]:
    roots: List[Tuple[type, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.response_model is not None:
            found: Set[type] = set()
            _models_in(route.response_model, found)
            roots.extend((model, f"{sorted(route.methods)[0]} {route.path}") for model in found)
    return roots


def test_every_response_model_datetime_serializes_with_an_offset():
    offenders = {
        key: value for key, value in naive_datetime_fields(_route_roots()).items() if key not in KNOWN_NAIVE_MODELS
    }

    assert not offenders, (
        "Response models with datetime fields that serialize WITHOUT a UTC offset. Make each one "
        "inherit app.schemas.base.UTCModel (nested models too -- the encoder does not reach a "
        "nested bare BaseModel):\n"
        + "\n".join(f"  {key} {fields} (reached via {via})" for key, (fields, via) in sorted(offenders.items()))
    )


def test_allowlist_is_not_stale():
    offenders = naive_datetime_fields(_route_roots())
    stale = sorted(key for key in KNOWN_NAIVE_MODELS if key not in offenders)

    assert not stale, f"These KNOWN_NAIVE_MODELS entries no longer offend -- delete them: {stale}"


def test_the_walk_reaches_the_route_models():
    """Vacuity check: the walk must actually see the converted plugin-facing models."""
    seen = set()

    def collect(model: type) -> None:
        if model in seen:
            return
        seen.add(model)
        for field in model.model_fields.values():
            nested: Set[type] = set()
            _models_in(field.annotation, nested)
            for child in nested:
                collect(child)

    for model, _ in _route_roots():
        collect(model)
    names = {_key(model) for model in seen}

    for expected in (
        "app.api.endpoints.downtime.DowntimeEventResponse",
        "app.api.endpoints.oee.OEERecordResponse",
        "app.api.endpoints.quotes.QuoteResponse",
        "app.schemas.job_costing.JobCostResponse",
        "app.services.traceability_service.LotHistoryItem",
        "app.schemas.work_order_timeline.WorkOrderTimelineEntry",
    ):
        assert expected in names
    assert len(names) > 300


# --- the walker's own semantics -------------------------------------------------------


class _BareChild(BaseModel):
    at: datetime


class _UTCParentWithBareChild(UTCModel):
    at: datetime
    child: Optional[_BareChild] = None
    children: List[_BareChild] = []


class _UTCChild(UTCModel):
    at: datetime


class _UTCParentWithUTCChild(UTCModel):
    child: _UTCChild
    day: date


def test_nested_bare_model_inside_utc_model_is_caught():
    offenders = naive_datetime_fields([(_UTCParentWithBareChild, "test")])

    assert list(offenders) == [_key(_BareChild)]
    # ... and the encoder really does not reach it (why the guard recurses).
    dumped = _UTCParentWithBareChild(at=datetime(2026, 1, 15, 6), child=_BareChild(at=datetime(2026, 1, 15, 6)))
    payload = dumped.model_dump(mode="json")
    assert payload["at"] == "2026-01-15T06:00:00Z"
    assert payload["child"]["at"] == "2026-01-15T06:00:00"


def test_utc_models_all_the_way_down_pass():
    assert naive_datetime_fields([(_UTCParentWithUTCChild, "test")]) == {}
    payload = _UTCParentWithUTCChild(child=_UTCChild(at=datetime(2026, 7, 15, 5)), day=date(2026, 7, 15)).model_dump(
        mode="json"
    )
    assert payload == {"child": {"at": "2026-07-15T05:00:00Z"}, "day": "2026-07-15"}
