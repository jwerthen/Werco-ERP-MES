"""The export-control redaction engine (Claude-plugin brief P0.3). PURE: no database, no I/O.

``app.middleware.export_control`` hands it a decoded JSON response and the request's
:class:`ControlledIndex` (built by ``export_control_service.build_controlled_index``) and
sends what comes back to an AI-external API token. The input is never mutated.

Two passes:

1. **Structural.** A JSON object is CONTROLLED when

   * one of its identifying keys (:data:`ID_KEYS`, :data:`NUMBER_KEYS`, :data:`NAME_KEYS`)
     names a record the index holds as controlled or unlinkable;
   * it is a top-level record of a route whose objects carry no such key for themselves
     (:data:`ROUTE_SHAPES`: a CAR is ``{"id": ...}`` and nothing else) and its ``id`` hits;
   * the route's own path parameter names such a record (:data:`PATH_PARAM_KEYS`: the
     whole response to ``/work-orders/{work_order_id}/timeline`` is that work order); or
   * it descends from a controlled object.

   Inside a controlled object a KEEP-allowlist applies -- ids, statuses, priority /
   sequence / version, dates and times, numbers, booleans, nulls and the business
   identifiers in :data:`KEPT_IDENTIFIER_KEYS` -- and every other string becomes
   ``"[export-controlled]"``. A denylist of "sensitive" keys would leak the next text
   column somebody adds; the allowlist cannot. ``"export_controlled": true`` is added to
   every redacted object. Counts, ids, statuses, dates and quantities therefore stay true.

2. **Residual text.** Every remaining string in the response -- cleared records, error
   ``detail`` text and object keys included -- has each sensitive token (the part numbers,
   aliases, customer part numbers and drawing numbers of controlled and unlinkable parts,
   controlled customers' names and codes, and every free-text customer name that resolves
   to no customer at all -- it may be a controlled one misspelled) replaced,
   case-insensitively. Only values
   that cannot carry text are exempt: ISO dates, numeric strings under non-identifier keys
   and numeric / UUID ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Pattern, Tuple

from app.core.export_control import (
    CLASSIFICATION_CUI,
    CLASSIFICATION_ITAR,
    CLASSIFICATION_UNKNOWN,
    REASON_EXPORT_CONTROLLED_CUSTOMER,
    REASON_UNLINKABLE_RECORD,
    REDACTED_MARKER_KEY,
    REDACTED_VALUE,
)

# Sensitive tokens shorter than this are not matched by the text pass: a 1-3 character
# part number would rewrite every word that happens to contain it.
MIN_SENSITIVE_TOKEN_LENGTH = 4


# --------------------------------------------------------------------------- the index


@dataclass(frozen=True)
class Control:
    """Why a record is withheld, and under which customer / contract."""

    reason: str  # REASON_EXPORT_CONTROLLED_CUSTOMER | REASON_UNLINKABLE_RECORD
    classification: str  # CLASSIFICATION_ITAR | CLASSIFICATION_CUI | CLASSIFICATION_UNKNOWN
    customer_id: Optional[int] = None
    contract_id: Optional[int] = None

    @property
    def rank(self) -> int:
        """Severity for ``worst``: ITAR > CUI > unlinkable."""
        if self.classification == CLASSIFICATION_ITAR:
            return 3
        if self.classification == CLASSIFICATION_CUI:
            return 2
        return 1


# The control every record the ERP cannot tie to a customer gets (positive clearance).
UNLINKABLE = Control(reason=REASON_UNLINKABLE_RECORD, classification=CLASSIFICATION_UNKNOWN)


def customer_control(classification: str, customer_id: Optional[int], contract_id: Optional[int]) -> Control:
    return Control(
        reason=REASON_EXPORT_CONTROLLED_CUSTOMER,
        classification=classification,
        customer_id=customer_id,
        contract_id=contract_id,
    )


def worst(*controls: Optional[Control]) -> Optional[Control]:
    """The most severe of ``controls`` (None = cleared); ties keep the first."""
    best: Optional[Control] = None
    for control in controls:
        if control is not None and (best is None or control.rank > best.rank):
            best = control
    return best


def normalize_key(value: Any) -> str:
    """The comparison key for names and record numbers: casefold, strip, collapse whitespace."""
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


@dataclass(frozen=True)
class ControlledIndex:
    """One company's export-control picture, as of one request.

    ``controlled[type][id]`` -- every record of ``type`` that is withheld, and why.
    ``known[type]`` -- every id of ``type`` the index enumerated (soft-deleted included).
      An id of an indexed type that is in neither is a record the index has never seen
      and is treated as UNLINKABLE: positive clearance, never "absent means fine".
    ``numbers[type][normalized number]`` -- the business number of each withheld record.
    ``customer_names[normalized name or code]`` -- controlled customers only.
    ``sensitive_tokens`` -- what the residual text pass removes.
    """

    company_id: int
    controlled: Mapping[str, Mapping[int, Control]] = field(default_factory=dict)
    known: Mapping[str, FrozenSet[int]] = field(default_factory=dict)
    numbers: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    customer_names: Mapping[str, Tuple[int, Control]] = field(default_factory=dict)
    sensitive_tokens: Tuple[str, ...] = ()

    def by_id(self, record_type: str, record_id: Any) -> Optional[Control]:
        """The control for ``record_type`` #``record_id``; None when it is cleared or not an indexed type."""
        if record_type not in self.known or isinstance(record_id, bool):
            return None
        try:
            key = int(record_id)
        except (TypeError, ValueError):
            return None
        control = self.controlled.get(record_type, {}).get(key)
        if control is not None:
            return control
        if key not in self.known[record_type]:
            return UNLINKABLE
        return None

    def by_number(self, record_type: str, number: Any) -> Optional[Tuple[int, Control]]:
        """``(id, control)`` when ``number`` is a withheld record's business number, else None."""
        if not isinstance(number, str):
            return None
        record_id = self.numbers.get(record_type, {}).get(normalize_key(number))
        if record_id is None:
            return None
        control = self.controlled.get(record_type, {}).get(record_id)
        return (record_id, control) if control is not None else None

    def by_customer_name(self, name: Any) -> Optional[Tuple[int, Control]]:
        if not isinstance(name, str):
            return None
        return self.customer_names.get(normalize_key(name))

    def sensitive_pattern(self) -> Optional[Pattern[str]]:
        return compile_sensitive_pattern(self.sensitive_tokens)


def compile_sensitive_pattern(tokens: Iterable[str]) -> Optional[Pattern[str]]:
    """One case-insensitive alternation, longest token first, or None when there is nothing to remove."""
    cleaned = {token.strip() for token in tokens if isinstance(token, str)}
    usable = sorted(
        (token for token in cleaned if len(token) >= MIN_SENSITIVE_TOKEN_LENGTH), key=lambda t: (-len(t), t)
    )
    if not usable:
        return None
    return re.compile("|".join(re.escape(token) for token in usable), re.IGNORECASE)


# --------------------------------------------------------------------------- identifying keys

# key -> record type, for keys whose value is a record ID
ID_KEYS: Mapping[str, str] = {
    "work_order_id": "work_order",
    "parent_work_order_id": "work_order",
    "part_id": "part",
    "component_part_id": "part",
    "customer_id": "customer",
    "operation_id": "operation",
    "routing_id": "routing",
    "ncr_id": "ncr",
    "car_id": "car",
    "fai_id": "fai",
    "quote_id": "quote",
    "po_id": "purchase_order",
    "purchase_order_id": "purchase_order",
    "receipt_id": "receipt",
}

# key -> record type, for keys whose value is a record's business NUMBER
NUMBER_KEYS: Mapping[str, str] = {
    "work_order_number": "work_order",
    "wo_number": "work_order",
    "part_number": "part",
    "component_part_number": "part",
    "ncr_number": "ncr",
    "car_number": "car",
    "fai_number": "fai",
    "quote_number": "quote",
    "po_number": "purchase_order",
    "receipt_number": "receipt",
}

# keys whose (string) value is a customer name
NAME_KEYS: FrozenSet[str] = frozenset({"customer_name", "customer"})

# Allowlisted routes (relative to the API prefix) whose top-level objects are records of
# one type identified by ``id`` -- including the ones that also carry a number key, so a
# record the index has never seen fails closed rather than passing on a missing number.
# Value: (record type, wrapper key). A list response's items are the records; a dict
# response with an ``id`` is the record; a dict WITHOUT one is a wrapper whose
# ``wrapper key`` list holds the records (None: the wrapper holds none by ``id``).
ROUTE_SHAPES: Mapping[str, Tuple[str, Optional[str]]] = {
    "/work-orders/": ("work_order", None),
    "/work-orders/browse": ("work_order", "items"),
    "/work-orders/{work_order_id}": ("work_order", None),
    "/work-orders/by-number/{wo_number}": ("work_order", None),
    "/shop-floor/operations": ("operation", "operations"),
    "/parts/": ("part", None),
    "/parts/{part_id}": ("part", None),
    "/parts/by-number/{part_number}": ("part", None),
    "/routing/": ("routing", None),
    "/routing/{routing_id}": ("routing", None),
    "/routing/by-part/{part_id}": ("routing", None),
    "/quality/ncr": ("ncr", None),
    "/quality/ncr/{ncr_id}": ("ncr", None),
    "/quality/car": ("car", None),
    "/quality/car/{car_id}": ("car", None),
    "/quality/fai": ("fai", None),
    "/quality/fai/{fai_id}": ("fai", None),
    "/quotes/": ("quote", None),
    "/quotes/{quote_id}": ("quote", None),
    "/purchasing/purchase-orders": ("purchase_order", None),
    "/purchasing/purchase-orders/{po_id}": ("purchase_order", None),
    "/receiving/history": ("receipt", None),
    "/receiving/receipt/{receipt_id}": ("receipt", None),
}

# path parameter -> (record type, "id" | "number"). A hit makes the WHOLE response
# controlled: every byte of /work-orders/{work_order_id}/timeline is that work order's.
PATH_PARAM_KEYS: Mapping[str, Tuple[str, str]] = {
    "work_order_id": ("work_order", "id"),
    "wo_number": ("work_order", "number"),
    "part_id": ("part", "id"),
    "part_number": ("part", "number"),
    "operation_id": ("operation", "id"),
    "routing_id": ("routing", "id"),
    "ncr_id": ("ncr", "id"),
    "car_id": ("car", "id"),
    "fai_id": ("fai", "id"),
    "quote_id": ("quote", "id"),
    "po_id": ("purchase_order", "id"),
    "receipt_id": ("receipt", "id"),
}

# --------------------------------------------------------------------------- the keep-allowlist

KEPT_IDENTIFIER_KEYS: FrozenSet[str] = frozenset(
    {
        "work_order_number",
        "wo_number",
        "operation_number",
        "ncr_number",
        "car_number",
        "fai_number",
        "quote_number",
        "po_number",
        "receipt_number",
        "lot_number",
        "work_center_code",
        "work_center_name",
    }
)
_STATUS_KEYS = frozenset({"status", "state", "priority", "sequence", "version", "date"})
_DATE_SUFFIXES = ("_at", "_date", "_time")
# A key containing any of these names text, even when its value looks like a number: a
# purely numeric part number must still be withheld.
_TEXTUAL_KEY_FRAGMENTS = (
    "number",
    "name",
    "code",
    "desc",
    "note",
    "spec",
    "drawing",
    "title",
    "label",
    "text",
    "instruction",
    "comment",
    "reason",
    "customer",
    "part",
    "serial",
    "heat",
    "rev",
    "file",
    "path",
    "url",
    "email",
    "phone",
    "address",
    "material",
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_NUMERIC = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
_ID_VALUE = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def _is_id_key(key: str) -> bool:
    return key == "id" or key.endswith("_id") or key.endswith("_ids")


def _is_status_key(key: str) -> bool:
    return key in _STATUS_KEYS or key.endswith("_status")


def _is_date_key(key: str) -> bool:
    return key.endswith(_DATE_SUFFIXES)


def _is_textual_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _TEXTUAL_KEY_FRAGMENTS)


def _structurally_inert(key: Optional[str], value: str) -> bool:
    """A string that cannot carry technical data: an ISO date, a numeric id, or a number under a non-text key."""
    if _ISO_DATE.match(value):
        return True
    if key is not None and _is_id_key(key) and _ID_VALUE.match(value):
        return True
    if _NUMERIC.match(value) and (key is None or not _is_textual_key(key)):
        return True
    return False


def _kept_string(key: Optional[str], value: str) -> bool:
    """Inside a controlled object: may this string stay?"""
    if key is None:
        return _structurally_inert(None, value)
    if key in KEPT_IDENTIFIER_KEYS:
        return True
    if _is_id_key(key):
        return bool(_ID_VALUE.match(value))
    if _is_status_key(key):
        return True
    if _is_date_key(key):
        return True
    return _structurally_inert(key, value)


# --------------------------------------------------------------------------- the walk


@dataclass(frozen=True)
class RedactionHit:
    """One withheld record a response touched: one ``controlled_access_events`` row per (type, id)."""

    record_type: str
    record_id: Optional[int]
    control: Control


@dataclass(frozen=True)
class RedactionResult:
    payload: Any
    hits: Tuple[RedactionHit, ...]


class _Walker:
    def __init__(self, index: ControlledIndex, pattern: Optional[Pattern[str]]) -> None:
        self.index = index
        self.pattern = pattern
        self.hits: Dict[Tuple[str, Optional[int]], RedactionHit] = {}

    def record(self, record_type: str, record_id: Optional[int], control: Control) -> None:
        key = (record_type, record_id)
        if key not in self.hits:
            self.hits[key] = RedactionHit(record_type, record_id, control)

    # -- identification

    def identify(self, obj: Mapping[str, Any], shape: Optional[str]) -> Optional[Control]:
        """The control that makes ``obj`` itself withheld (recording every hit), or None."""
        found: List[Control] = []
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                continue
            id_type = ID_KEYS.get(key)
            if id_type is not None and value is not None:
                control = self.index.by_id(id_type, value)
                if control is not None:
                    self.record(id_type, _as_int(value), control)
                    found.append(control)
                continue
            number_type = NUMBER_KEYS.get(key)
            if number_type is not None:
                hit = self.index.by_number(number_type, value)
                if hit is not None:
                    self.record(number_type, hit[0], hit[1])
                    found.append(hit[1])
                continue
            if key in NAME_KEYS:
                named = self.index.by_customer_name(value)
                if named is not None:
                    self.record("customer", named[0], named[1])
                    found.append(named[1])
        if shape is not None and "id" in obj and not isinstance(obj["id"], (dict, list)):
            control = self.index.by_id(shape, obj["id"])
            if control is not None:
                self.record(shape, _as_int(obj["id"]), control)
                found.append(control)
        return worst(*found)

    # -- the structural pass

    def walk(self, value: Any, *, controlled: bool, key: Optional[str] = None, shape: Optional[str] = None) -> Any:
        if isinstance(value, dict):
            return self.walk_object(value, controlled=controlled, shape=shape)
        if isinstance(value, list):
            return [self.walk(item, controlled=controlled, key=key, shape=shape) for item in value]
        if isinstance(value, str):
            if controlled and not _kept_string(key, value):
                return REDACTED_VALUE
            return self.scrub(key, value)
        return value

    def walk_object(self, obj: Mapping[str, Any], *, controlled: bool, shape: Optional[str]) -> Dict[str, Any]:
        own = self.identify(obj, shape)
        withheld = controlled or own is not None
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            new_key = self.scrub_key(key, out)
            out[new_key] = self.walk(value, controlled=withheld, key=key)
        if withheld:
            out[REDACTED_MARKER_KEY] = True
        return out

    # -- the residual text pass

    def scrub(self, key: Optional[str], value: str) -> str:
        if self.pattern is None or _structurally_inert(key, value):
            return value
        return self.pattern.sub(REDACTED_VALUE, value)

    def scrub_key(self, key: Any, taken: Mapping[str, Any]) -> Any:
        if not isinstance(key, str) or self.pattern is None:
            return key
        scrubbed = self.pattern.sub(REDACTED_VALUE, key)
        candidate, n = scrubbed, 2
        while candidate in taken:  # two keys collapsing to one must not drop a value
            candidate, n = f"{scrubbed}#{n}", n + 1
        return candidate

    def scrub_tree(self, value: Any, key: Optional[str] = None) -> Any:
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for k, v in value.items():
                out[self.scrub_key(k, out)] = self.scrub_tree(v, k if isinstance(k, str) else None)
            return out
        if isinstance(value, list):
            return [self.scrub_tree(item, key) for item in value]
        if isinstance(value, str):
            return self.scrub(key, value)
        return value


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _path_param_control(walker: _Walker, path_params: Optional[Mapping[str, Any]]) -> Optional[Control]:
    found: List[Control] = []
    for name, raw in (path_params or {}).items():
        spec = PATH_PARAM_KEYS.get(name)
        if spec is None:
            continue
        record_type, kind = spec
        if kind == "id":
            control = walker.index.by_id(record_type, raw)
            if control is not None:
                walker.record(record_type, _as_int(raw), control)
                found.append(control)
        else:
            hit = walker.index.by_number(record_type, raw)
            if hit is not None:
                walker.record(record_type, hit[0], hit[1])
                found.append(hit[1])
    return worst(*found)


def redact_payload(
    payload: Any,
    index: ControlledIndex,
    *,
    route_path: Optional[str] = None,
    path_params: Optional[Mapping[str, Any]] = None,
) -> RedactionResult:
    """Both passes over a SUCCESSFUL response; returns a new payload and every withheld record it touched.

    ``route_path`` is the matched route template relative to the API prefix (it selects a
    :data:`ROUTE_SHAPES` entry); ``path_params`` are the request's path parameters.
    """
    walker = _Walker(index, index.sensitive_pattern())
    shape, wrapper_key = ROUTE_SHAPES.get(route_path or "", (None, None))
    whole = _path_param_control(walker, path_params) is not None

    if isinstance(payload, list):
        result: Any = [walker.walk(item, controlled=whole, shape=shape) for item in payload]
    elif isinstance(payload, dict) and ("id" in payload or shape is None):
        result = walker.walk_object(payload, controlled=whole, shape=shape)
    elif isinstance(payload, dict):
        # A wrapper ({"operations": [...], "total": n}): its ``wrapper_key`` list holds the records.
        result = _walk_wrapper(walker, payload, controlled=whole, shape=shape, wrapper_key=wrapper_key)
    else:
        result = walker.walk(payload, controlled=whole)
    return RedactionResult(payload=result, hits=tuple(walker.hits.values()))


def _walk_wrapper(
    walker: _Walker, payload: Mapping[str, Any], *, controlled: bool, shape: Optional[str], wrapper_key: Optional[str]
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        new_key = walker.scrub_key(key, out)
        if key == wrapper_key and isinstance(value, list):
            out[new_key] = [walker.walk(item, controlled=controlled, key=key, shape=shape) for item in value]
        else:
            out[new_key] = walker.walk(value, controlled=controlled, key=key)
    if controlled:
        out[REDACTED_MARKER_KEY] = True
    return out


def scrub_text(payload: Any, index: ControlledIndex) -> Any:
    """The residual text pass alone (an error response): a new payload, the input untouched."""
    walker = _Walker(index, index.sensitive_pattern())
    return walker.scrub_tree(payload)
