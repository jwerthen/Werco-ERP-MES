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
   sequence / version, dates and times, booleans, nulls, the business identifiers in
   :data:`KEPT_IDENTIFIER_KEYS`, the closed-vocabulary values in :data:`KEPT_ENUM_VALUES`,
   and numbers -- JSON numbers and numeric strings alike --
   ONLY under a key naming an id, count, quantity, duration, money amount, rate, percentage
   or sequence (:func:`kept_numeric_key`) and never under one naming a dimension or
   measurement of the article (:data:`DIMENSION_KEY_FRAGMENTS`: a nominal, tolerance,
   measured value, length or area is technical data) -- and every other value becomes
   ``"[export-controlled]"``. A denylist of "sensitive" keys would leak the next text
   column somebody adds; the allowlist cannot -- and that holds for numbers too: a number
   under a key nobody listed (``flat_area``, an AI estimate's free-form ``assumed_inches``)
   is withheld. ``"export_controlled": true`` is added to every redacted object. Counts,
   ids, statuses, dates, quantities, money and OEE figures therefore stay true.

2. **Residual text.** Every remaining string in the response -- cleared records, error
   ``detail`` text and object keys included -- has each sensitive token (the part numbers,
   aliases, customer part numbers and drawing numbers of controlled and unlinkable parts,
   controlled customers' names and codes, and every free-text customer name that resolves
   to no customer at all -- it may be a controlled one misspelled) replaced,
   case-insensitively. A whole value EQUAL to one of those tokens is replaced whatever its
   length (a three-letter customer code is withheld too); the substring match needs
   :data:`MIN_SENSITIVE_TOKEN_LENGTH` so a short token cannot rewrite ordinary words. Only
   values that cannot carry text are exempt: ISO dates, numeric strings under
   non-identifier keys and numeric / UUID ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Pattern, Tuple

from app.core.export_control import (
    AI_EXTERNAL_MASKED_LOOKUPS,
    CLASSIFICATION_CUI,
    CLASSIFICATION_ITAR,
    CLASSIFICATION_UNKNOWN,
    REASON_EXPORT_CONTROLLED_CUSTOMER,
    REASON_UNLINKABLE_RECORD,
    REDACTED_MARKER_KEY,
    REDACTED_VALUE,
    ai_external_error_detail,
)

# Sensitive tokens shorter than this are not matched INSIDE a longer string by the text
# pass: a 1-3 character part number would rewrite every word that happens to contain it.
# A string that IS such a token (whole value, normalized) is still replaced -- code review
# P0, finding 2: a customer coded "BAE" otherwise went out as "BAE".
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

    def sensitive_values(self) -> FrozenSet[str]:
        """Every sensitive token and controlled customer name / code, normalized: matched as a WHOLE value."""
        values = {normalize_key(token) for token in self.sensitive_tokens}
        values.update(self.customer_names)
        values.discard("")
        return frozenset(values)


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

# (route, list key) -> record type, for NESTED record lists whose items identify themselves
# by nothing but ``id`` (or a link) -- the top-level ROUTE_SHAPES cannot see them. Compliance
# review P0, finding 3: ``all_operations`` on an operation's detail lists its SIBLINGS by
# ``id`` alone, so a withheld sibling (its component part unlinkable) went out in full.
# ``timeline_operation``: a work-order timeline item names its operation only in its
# ``source_url`` fragment (``#operation-12``); every other item is the work order's own.
NESTED_SHAPES: Mapping[Tuple[str, str], str] = {
    ("/shop-floor/operations/{operation_id}", "all_operations"): "operation",
    ("/work-orders/{work_order_id}/timeline", "items"): "timeline_operation",
}
_TIMELINE_OPERATION = re.compile(r"#operation-(\d+)$")


def _shape_reference(shape: str, obj: Mapping[str, Any]) -> Optional[Tuple[str, Any]]:
    """``(record type, id)`` the ``shape`` says ``obj`` is, or None when it names no record."""
    if shape == "timeline_operation":
        url = obj.get("source_url")
        match = _TIMELINE_OPERATION.search(url) if isinstance(url, str) else None
        return ("operation", match.group(1)) if match else None
    record_id = obj.get("id")
    if record_id is None or isinstance(record_id, (dict, list)):
        return None
    return (shape, record_id)


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

# Inside a withheld object a NUMBER -- a JSON number, or a numeric string (a Decimal
# serializes as one) -- stays only under a key naming an id, count, quantity, duration,
# money amount, rate, percentage or sequence: an ALLOWLIST, like the one for text. Compliance
# review P0, finding 1 (blocker): numeric strings under any non-text key passed, and an ITAR
# FAI's dimensions went out. Code review P0 round 2, finding 2: JSON numbers were still a
# DENYLIST (withheld only under a listed measurement key), so a legacy AI estimate's
# ``flat_area`` and every number in its free-form ``assumptions`` (``assumed_inches``) went
# out true. Every numeric property of every allowlisted route's response schema is pinned
# kept-or-withheld by ``tests/test_export_control_redaction.py``, so a new count that this
# list does not name fails CI rather than going quietly missing.
NUMERIC_KEEP_KEY_FRAGMENTS = (
    "quantity",
    "qty",
    "count",
    "hours",
    "minutes",
    "seconds",
    "days",
    "duration",
    "time",
    "cost",
    "price",
    "total",
    "amount",
    "tax",
    "shipping",
    "stock",
    "reorder",
    "percent",
    "pct",
    "rate",
    "sequence",
    "version",
    "priority",
    "runs",
    "piece",
    "parts",
    "order",
    "rank",
    "rating",
    "score",
    "confidence",
    "efficiency",
    "passed",
    "failed",
    "complete",
    "short",
)
# Whole keys (not fragments) whose number is a count, a position or pagination.
NUMERIC_KEEP_KEYS = frozenset(
    {
        "char_number",
        "line_number",
        "step_number",
        "balloon_number",
        "skip",
        "limit",
        "offset",
        "page",
        "page_size",
        "in_progress",
        "overdue",
        "due_today",
        # The production calendar (P1.2): a wall time as minutes after local midnight -- a
        # time-of-day position, not a dimension ("minutes" is a fragment, "minute" is not).
        "start_minute",
        "end_minute",
        "production_day_start_minute",
    }
)
# Inside a withheld object, a STRING under one of these keys stays only when it is one of the
# key's closed-vocabulary values: the value is a server-derived category the response schema
# types as a ``Literal``, so it cannot carry the record's text. Any other value under the key
# is withheld like every other string. ``hours_source`` (P1.4): where an operation's actual
# hours come from -- ``tests/test_export_control_redaction.py`` pins this set to the schema's
# ``Literal`` so the two cannot drift.
KEPT_ENUM_VALUES: Mapping[str, FrozenSet[str]] = {
    "hours_source": frozenset({"time_entries", "manual", "standard", "none"}),
}
# A key naming a dimension or measurement of the article: its number is technical data and is
# withheld even when the key ALSO names a count / total (``total_length``,
# ``tolerance_percent``). "value", "limit" and "target" are not here: on their own they keep
# nothing (no keep fragment), and with one they are money, pagination or an OEE target
# (``total_value``, ``target_quantity``, ``target_oee_pct``).
DIMENSION_KEY_FRAGMENTS = (
    "nominal",
    "toleran",
    "measur",
    "dimension",
    "diameter",
    "thickness",
    "length",
    "width",
    "height",
    "depth",
    "angle",
    "radius",
    "area",
    "perimeter",
    "volume",
    "weight",
    "mass",
    "gauge",
    "inch",
    "torque",
    "pressure",
    "temperature",
    "hardness",
    "tensile",
)
# Unit tokens that name a dimension only as a whole word (``bend_mm``; never ``comments``).
DIMENSION_KEY_TOKENS = frozenset({"mm", "cm"})
_KEY_TOKENS = re.compile(r"[a-z0-9]+")

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


def names_a_dimension(key: str) -> bool:
    """Does ``key`` name a dimension or measurement of the article (see DIMENSION_KEY_FRAGMENTS)?"""
    lowered = key.lower()
    if any(fragment in lowered for fragment in DIMENSION_KEY_FRAGMENTS):
        return True
    return any(token in DIMENSION_KEY_TOKENS for token in _KEY_TOKENS.findall(lowered))


def kept_numeric_key(key: Optional[str]) -> bool:
    """Inside a withheld object, may a NUMBER under ``key`` stay? The allowlist, for numbers.

    Yes for an id (``*_id``, ``*_ids``, ``id``, and the ``*_by`` user ids), a status-like or
    kept-identifier key, a whole key in :data:`NUMERIC_KEEP_KEYS`, or a key naming a count /
    quantity / duration / money / rate / percentage / sequence
    (:data:`NUMERIC_KEEP_KEY_FRAGMENTS`) -- unless it also names a dimension
    (:func:`names_a_dimension`). No for everything else, a key nobody listed included, and for
    a number with no key at all.
    """
    if key is None:
        return False
    lowered = key.lower()
    if (
        _is_id_key(lowered)
        or lowered.endswith("_by")
        or _is_status_key(lowered)
        or lowered in KEPT_IDENTIFIER_KEYS
        or lowered in NUMERIC_KEEP_KEYS
    ):
        return True
    if names_a_dimension(lowered):
        return False
    return any(fragment in lowered for fragment in NUMERIC_KEEP_KEY_FRAGMENTS)


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
    if _ISO_DATE.match(value):
        return True
    if key is None:
        return False
    if key in KEPT_IDENTIFIER_KEYS:
        return True
    if value in KEPT_ENUM_VALUES.get(key, frozenset()):
        return True
    if _is_id_key(key):
        return bool(_ID_VALUE.match(value))
    if _is_status_key(key):
        return True
    if _is_date_key(key):
        return True
    # A number as text: the same allowlist as a JSON number, and never under a text key (a
    # purely numeric part number is still a part number).
    return bool(_NUMERIC.match(value)) and kept_numeric_key(key) and not _is_textual_key(key)


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
    def __init__(
        self, index: ControlledIndex, pattern: Optional[Pattern[str]], route_path: Optional[str] = None
    ) -> None:
        self.index = index
        self.pattern = pattern
        self.whole_values = index.sensitive_values()
        self.route_path = route_path or ""
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
        reference = _shape_reference(shape, obj) if shape is not None else None
        if reference is not None:
            record_type, record_id = reference
            control = self.index.by_id(record_type, record_id)
            if control is not None:
                self.record(record_type, _as_int(record_id), control)
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
        if controlled and isinstance(value, (int, float)) and not isinstance(value, bool) and not kept_numeric_key(key):
            return REDACTED_VALUE
        return value

    def walk_object(self, obj: Mapping[str, Any], *, controlled: bool, shape: Optional[str]) -> Dict[str, Any]:
        own = self.identify(obj, shape)
        withheld = controlled or own is not None
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            new_key = self.scrub_key(key, out)
            nested = NESTED_SHAPES.get((self.route_path, key)) if isinstance(key, str) else None
            out[new_key] = self.walk(value, controlled=withheld, key=key, shape=nested)
        if withheld:
            out[REDACTED_MARKER_KEY] = True
        return out

    # -- the residual text pass

    def scrub(self, key: Optional[str], value: str) -> str:
        if _structurally_inert(key, value):
            return value
        if self.whole_values and normalize_key(value) in self.whole_values:
            return REDACTED_VALUE
        if self.pattern is None:
            return value
        return self.pattern.sub(REDACTED_VALUE, value)

    def scrub_key(self, key: Any, taken: Mapping[str, Any]) -> Any:
        if not isinstance(key, str):
            return key
        if self.whole_values and normalize_key(key) in self.whole_values:
            scrubbed = REDACTED_VALUE
        elif self.pattern is not None:
            scrubbed = self.pattern.sub(REDACTED_VALUE, key)
        else:
            return key
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
    walker = _Walker(index, index.sensitive_pattern(), route_path)
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
            nested = NESTED_SHAPES.get((walker.route_path, key)) if isinstance(key, str) else None
            out[new_key] = walker.walk(value, controlled=controlled, key=key, shape=nested)
    if controlled:
        out[REDACTED_MARKER_KEY] = True
    return out


def scrub_text(payload: Any, index: ControlledIndex) -> Any:
    """The residual text pass alone (an error response): a new payload, the input untouched."""
    walker = _Walker(index, index.sensitive_pattern())
    return walker.scrub_tree(payload)


def is_masked_lookup(route_path: Optional[str], result: RedactionResult) -> bool:
    """Must this 2xx answer a by-number lookup with the not-found 404 instead (code review P0, finding 1)?

    True when ``route_path`` is in :data:`~app.core.export_control.AI_EXTERNAL_MASKED_LOOKUPS`
    (its number is withheld text) and the record it returned was withheld: a 200 -- redacted
    or not -- would confirm that a probed part number exists.
    """
    if (route_path or "") not in AI_EXTERNAL_MASKED_LOOKUPS:
        return False
    payload = result.payload
    return isinstance(payload, dict) and payload.get(REDACTED_MARKER_KEY) is True


# 422 ``detail`` item keys kept for an AI-external token: where and what kind of error, never
# the request's own value (``input``), the validator's context or message. A parameter NAME is
# kept only where it is the route's own declared name (an undeclared query parameter never
# gets this far: gate 4 refuses it); a body location keeps no key -- that could be the caller's.
_NAMED_VALIDATION_LOCATIONS = frozenset({"path", "query", "header", "cookie"})
_VALIDATION_LOCATIONS = _NAMED_VALIDATION_LOCATIONS | {"body"}
VALIDATION_ERROR_MESSAGE = "Invalid value."


def ai_external_error_body(status_code: int, payload: Any) -> Dict[str, Any]:
    """The body an AI-external token gets for a route's own error -- fixed text, no echo of the request.

    The residual text pass over an error that ECHOES the request (a 422's ``input``, a 404
    naming the number asked for) would turn a probed controlled part number into
    ``[export-controlled]`` and so confirm the probe (code review P0, finding 1). So no
    route error text reaches the token at all: one fixed detail per status code, and for a
    422 the location and type of each error -- schema names -- and nothing the caller sent.
    """
    body: Dict[str, Any] = {"detail": ai_external_error_detail(status_code)}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if status_code == 422 and isinstance(detail, list):
        errors: List[Dict[str, Any]] = []
        for error in detail:
            if not isinstance(error, dict):
                continue
            loc = error.get("loc")
            parts = list(loc) if isinstance(loc, (list, tuple)) else []
            if parts and parts[0] in _NAMED_VALIDATION_LOCATIONS and len(parts) >= 2:
                location = [str(parts[0]), str(parts[1])]
            elif parts and parts[0] in _VALIDATION_LOCATIONS:
                location = [str(parts[0])]
            else:
                location = []
            error_type = error.get("type")
            errors.append(
                {
                    "type": str(error_type) if isinstance(error_type, str) else "invalid",
                    "loc": location,
                    "msg": VALIDATION_ERROR_MESSAGE,
                }
            )
        body["errors"] = errors
    return body
