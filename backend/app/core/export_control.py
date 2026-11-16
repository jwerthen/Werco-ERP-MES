"""Export-control policy constants for AI-external API tokens (Claude-plugin brief P0.3).

An API token minted ``ai_external`` (migration ``119_api_token_scope``; always read-only)
answers to an outside AI service. Three layers act on it, and each reads its constants
from HERE so the API layer, the response middleware and the MCP door can never disagree:

* ``app.api.deps.get_current_user`` -- gate 1 (company AI egress off -> 403), gate 4 (a
  route outside :data:`AI_EXTERNAL_ALLOWED_ROUTES` -> 403) and gate 5 (the
  :class:`ExportControlMarker` written into ``request.scope["state"]``);
* ``app.middleware.export_control`` -- reads the marker: refuses file bytes to every
  read-only token, redacts records of export-controlled (or unlinkable) customers for an
  AI-external one, and writes the ``controlled_access_events`` ledger;
* ``app.mcp`` -- the door refuses an AI-external token outright when its company's
  egress is off, and lists / dispatches only tools whose every route is allowlisted.

Pure: imports nothing from the app, so every layer (``app.mcp`` included) can use it.
The refusal texts are quoted verbatim by docs/API.md, docs/MCP.md and
docs/EXPORT_CONTROL_AI_EGRESS.md -- change them together.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import FrozenSet, Iterable, Mapping, Optional, Tuple

# ``scope["state"]`` key the marker is written under (``request.state`` is backed by it).
EXPORT_CONTROL_STATE_KEY = "export_control"

# The value every redacted string becomes.
REDACTED_VALUE = "[export-controlled]"
# The key added (True) to every redacted object, so a consumer can tell "withheld" from "empty".
REDACTED_MARKER_KEY = "export_controlled"

# --------------------------------------------------------------------------- refusal texts

AI_EGRESS_DISABLED_DETAIL = (
    "AI egress is disabled for this company: an AI-external API token cannot be used until an Admin enables it."
)
AI_EXTERNAL_ROUTE_DENIED_DETAIL = "This resource is not available to an AI-external API token."
# Gate 4, second half: a query parameter (or value) off :data:`AI_EXTERNAL_QUERY_PARAMS`.
# Fixed text, never naming the parameter or echoing its value (see the map's comment).
AI_EXTERNAL_QUERY_DENIED_DETAIL = (
    "This query parameter is not available to an AI-external API token: free-text search, "
    "customer filters and text sort keys are refused."
)
READ_ONLY_FILE_REFUSED_DETAIL = "Read-only API token cannot download files."
EXPORT_CONTROL_FAILED_DETAIL = "Export-control check failed; nothing was sent."

# --------------------------------------------------------------------------- ledger vocabulary

# ``controlled_access_events.action``
ACTION_REDACTED = "ai_external_redacted"
ACTION_REFUSED = "ai_external_refused"
# A masked lookup (:data:`AI_EXTERNAL_MASKED_LOOKUPS`) whose number matched NOTHING. Recorded
# so that a miss does the same work as a withheld hit (index build + ledger write + commit):
# the two answer byte-identical 404s, and unequal work would let latency tell them apart.
ACTION_LOOKUP_MISS = "ai_external_lookup_miss"

# ``controlled_access_events.denial_reason``
REASON_EXPORT_CONTROLLED_CUSTOMER = "export_controlled_customer"
REASON_UNLINKABLE_RECORD = "unlinkable_record"
REASON_AI_EGRESS_DISABLED = "ai_egress_disabled"
REASON_ROUTE_NOT_ALLOWLISTED = "route_not_allowlisted"
REASON_NON_JSON_RESPONSE = "non_json_response"
REASON_QUERY_NOT_ALLOWLISTED = "query_parameter_not_allowlisted"
REASON_NOT_FOUND = "not_found"

# ``controlled_access_events.data_classification`` values this feature writes (a subset of
# ``app.models.governance.DataClassification``; repeated, not imported, to stay pure).
CLASSIFICATION_ITAR = "itar_export_controlled"
CLASSIFICATION_CUI = "cui"
CLASSIFICATION_UNKNOWN = "unknown"

# --------------------------------------------------------------------------- the allowlist

# (METHOD, path template RELATIVE to ``settings.API_V1_PREFIX``) an AI-external token may
# call -- the brief's read set and nothing else: work orders, shop-floor operations,
# downtime, work centers, OEE, parts, routings, NCR / CAR / FAI (and the FAI log of record,
# P2.5), quotes, vendors (and the overdue supplier re-evaluation list, P2.2), purchase
# orders, receiving history, maintenance work orders -- plus the production calendar (P1.2:
# the production-day start and the shifts, so the plugin buckets its own figures by the
# shop's production day). GET only. There is no time-entry GET route; time entries reach the
# plugin through the work-order timeline.
#
# EVERY entry needs a canary conformance case in tests/api/test_ai_external_token.py
# (seed a controlled record whose text carries a canary, call the route as an AI-external
# token, prove the canary is absent from the raw bytes); a guard test there fails CI when
# an entry has none.
AI_EXTERNAL_ALLOWED_ROUTES: FrozenSet[Tuple[str, str]] = frozenset(
    {
        # Work orders
        ("GET", "/work-orders/"),
        ("GET", "/work-orders/browse"),
        ("GET", "/work-orders/{work_order_id}"),
        ("GET", "/work-orders/by-number/{wo_number}"),
        ("GET", "/work-orders/{work_order_id}/timeline"),
        # Shop-floor operations (and their actuals)
        ("GET", "/shop-floor/operations"),
        ("GET", "/shop-floor/operations/{operation_id}"),
        # Downtime. NOT /downtime/summary: its Pareto ``top_reasons`` falls back to an event's
        # free-text ``description`` in an object carrying no record key, so a controlled job's
        # downtime text cannot be linked and withheld there -- refuse rather than leak (the
        # brief). The per-event reads below carry ``work_order_id`` and are redacted.
        ("GET", "/downtime/"),
        ("GET", "/downtime/active"),
        ("GET", "/downtime/by-work-center"),
        ("GET", "/downtime/reason-codes"),
        ("GET", "/downtime/{event_id}"),
        # Work centers
        ("GET", "/work-centers/"),
        ("GET", "/work-centers/types"),
        ("GET", "/work-centers/{work_center_id}"),
        # OEE
        ("GET", "/oee/records"),
        ("GET", "/oee/records/{record_id}"),
        ("GET", "/oee/dashboard"),
        ("GET", "/oee/trends"),
        ("GET", "/oee/six-big-losses/{work_center_id}"),
        ("GET", "/oee/targets"),
        # Parts and routings
        ("GET", "/parts/"),
        ("GET", "/parts/{part_id}"),
        ("GET", "/parts/by-number/{part_number}"),
        ("GET", "/routing/"),
        ("GET", "/routing/{routing_id}"),
        ("GET", "/routing/by-part/{part_id}"),
        # Quality
        ("GET", "/quality/ncr"),
        ("GET", "/quality/ncr/{ncr_id}"),
        ("GET", "/quality/car"),
        ("GET", "/quality/car/{car_id}"),
        ("GET", "/quality/fai"),
        ("GET", "/quality/fai/{fai_id}"),
        # The FAI log of record (P2.5): one row per FAI, each judged like the FAI it names
        # (by ``fai_id``, ``part_id`` and ``work_order_id``) -- a withheld row keeps its ids,
        # FAI #, job #, status, dates and counts, and nothing else.
        ("GET", "/quality/fai/log"),
        # Quotes
        ("GET", "/quotes/"),
        ("GET", "/quotes/{quote_id}"),
        # Purchasing and receiving
        ("GET", "/purchasing/vendors"),
        # The overdue supplier re-evaluation list (P2.2, AS9100D 8.4): vendor reference data
        # like the two vendor reads beside it -- a vendor names no customer, part or job.
        ("GET", "/purchasing/vendors/reevaluation-due"),
        ("GET", "/purchasing/vendors/{vendor_id}"),
        ("GET", "/purchasing/purchase-orders"),
        ("GET", "/purchasing/purchase-orders/{po_id}"),
        ("GET", "/receiving/history"),
        ("GET", "/receiving/receipt/{receipt_id}"),
        # Maintenance work orders (work-center scoped: not customer data)
        ("GET", "/maintenance/work-orders"),
        ("GET", "/maintenance/work-orders/overdue"),
        ("GET", "/maintenance/work-orders/{wo_id}"),
        # Production calendar: reference data with no customer linkage -- the day start, the
        # shift patterns (names, codes, wall times, work-center ids) and the instant -> day /
        # shift resolver. Nothing in it names a work order, part or customer, so it is always
        # served cleared. The two writes (PUT /production-calendar and the working-calendar
        # sync POST) stay refused: a read-only token never writes.
        ("GET", "/production-calendar"),
        ("GET", "/production-calendar/resolve"),
    }
)


def ai_external_operations(api_prefix: str) -> FrozenSet[Tuple[str, str]]:
    """:data:`AI_EXTERNAL_ALLOWED_ROUTES` with ``api_prefix`` (``settings.API_V1_PREFIX``) applied."""
    prefix = api_prefix.rstrip("/")
    return frozenset((method, f"{prefix}{path}") for method, path in AI_EXTERNAL_ALLOWED_ROUTES)


# Per allowlisted route, the query parameters an AI-external token may send (None = any
# value; a set = only those values). Anything else -- an undeclared parameter included --
# is refused 403 at gate 4 (``AI_EXTERNAL_QUERY_DENIED_DETAIL``), ledgered
# ``query_parameter_not_allowlisted``. Code review P0, finding 1.
#
# WHY: a filter is an oracle on the field it filters. ``/parts/?search=`` matches the part
# number, name and description the redaction then hides, so "1 row" vs "0 rows" works the
# withheld text out one substring at a time; an exact ``customer=`` filter confirms a
# controlled customer's name; sorting or grouping by part or customer orders withheld text
# against cleared text. A filter over a field the redaction KEEPS (ids, statuses, dates,
# counts, booleans, pagination, closed-vocabulary categories) reveals nothing the response
# does not already show, so those stay. Free text is refused, not stripped: silently
# ignoring a filter would hand an agent the wrong answer as if it were the right one.
#
# A new parameter on an allowlisted route is refused until it is classified here;
# ``tests/api/test_ai_external_token.py`` fails CI until it is (allowed here, or named in
# that test's refused list).
_ANY = None
AI_EXTERNAL_QUERY_PARAMS: Mapping[str, Mapping[str, Optional[FrozenSet[str]]]] = {
    "/work-orders/": {
        "skip": _ANY,
        "limit": _ANY,
        "status": _ANY,
        "include_deleted": _ANY,
        "deleted_only": _ANY,
    },
    "/work-orders/browse": {
        "skip": _ANY,
        "limit": _ANY,
        "status": _ANY,
        "hide_cots": _ANY,
        "scope": _ANY,
        "work_order_type": _ANY,
        # Not "part" / "customer": ordering by withheld text places it among cleared text.
        "sort": frozenset({"work_order_number", "due_date", "priority", "status"}),
        "direction": _ANY,
        # Not "customer" / "part": group keys ARE the withheld text.
        "group": frozenset({"none", "status"}),
    },
    "/work-orders/{work_order_id}": {},
    "/work-orders/by-number/{wo_number}": {},
    "/work-orders/{work_order_id}/timeline": {
        "category": _ANY,
        "actor_id": _ANY,
        "start_at": _ANY,
        "end_at": _ANY,
        "cursor": _ANY,
        "limit": _ANY,
    },
    "/shop-floor/operations": {
        "work_center_id": _ANY,
        "status": _ANY,
        "due_today": _ANY,
        "page": _ANY,
        "page_size": _ANY,
    },
    "/shop-floor/operations/{operation_id}": {},
    "/downtime/": {
        "skip": _ANY,
        "limit": _ANY,
        "work_center_id": _ANY,
        "category": _ANY,
        "planned_type": _ANY,
        "date_from": _ANY,
        "date_to": _ANY,
        "active_only": _ANY,
    },
    "/downtime/active": {},
    "/downtime/by-work-center": {"date_from": _ANY, "date_to": _ANY},
    "/downtime/reason-codes": {"category": _ANY, "active_only": _ANY},
    "/downtime/{event_id}": {},
    "/work-centers/": {"skip": _ANY, "limit": _ANY, "active_only": _ANY},
    "/work-centers/types": {},
    "/work-centers/{work_center_id}": {},
    "/oee/records": {
        "work_center_id": _ANY,
        "date_from": _ANY,
        "date_to": _ANY,
        "shift": _ANY,
        # A defined shift's id (P1.2): a filter over a kept id.
        "shift_id": _ANY,
        "skip": _ANY,
        "limit": _ANY,
    },
    "/oee/records/{record_id}": {},
    "/oee/dashboard": {"period": _ANY, "date_from": _ANY, "date_to": _ANY},
    "/oee/trends": {"work_center_id": _ANY, "period": _ANY, "date_from": _ANY, "date_to": _ANY},
    "/oee/six-big-losses/{work_center_id}": {"period": _ANY},
    "/oee/targets": {},
    "/parts/": {
        "skip": _ANY,
        "limit": _ANY,
        "part_type": _ANY,
        "item_group": _ANY,
        "active_only": _ANY,
        "include_bom_components": _ANY,
        "include_deleted": _ANY,
    },
    "/parts/{part_id}": {},
    "/parts/by-number/{part_number}": {},
    "/routing/": {
        "skip": _ANY,
        "limit": _ANY,
        "part_id": _ANY,
        "status": _ANY,
        "active_only": _ANY,
        "include_bom_components": _ANY,
    },
    "/routing/{routing_id}": {},
    "/routing/by-part/{part_id}": {},
    "/quality/ncr": {"skip": _ANY, "limit": _ANY, "status": _ANY, "part_id": _ANY},
    "/quality/ncr/{ncr_id}": {},
    "/quality/car": {"skip": _ANY, "limit": _ANY, "status": _ANY},
    "/quality/car/{car_id}": {},
    "/quality/fai": {"skip": _ANY, "limit": _ANY, "status": _ANY, "part_id": _ANY},
    "/quality/fai/{fai_id}": {},
    # Not ``customer`` (a customer-name substring) or ``search`` (FAI #, part number /
    # description, job #, customer PO, serial): both match text the redaction withholds.
    # The log-date range, status and the two ids filter kept fields.
    "/quality/fai/log": {
        "start_date": _ANY,
        "end_date": _ANY,
        "status": _ANY,
        "part_id": _ANY,
        "work_order_id": _ANY,
        "limit": _ANY,
        "offset": _ANY,
    },
    "/quotes/": {"status": _ANY, "limit": _ANY, "offset": _ANY},
    "/quotes/{quote_id}": {},
    "/purchasing/vendors": {"active_only": _ANY, "approved_only": _ANY, "deleted_only": _ANY},
    # A day count and a boolean: neither filters withheld text.
    "/purchasing/vendors/reevaluation-due": {"within_days": _ANY, "include_inactive": _ANY},
    "/purchasing/vendors/{vendor_id}": {},
    "/purchasing/purchase-orders": {
        "status": _ANY,
        "vendor_id": _ANY,
        "deleted_only": _ANY,
        "limit": _ANY,
        "offset": _ANY,
    },
    "/purchasing/purchase-orders/{po_id}": {},
    "/receiving/history": {"days": _ANY, "status": _ANY},
    "/receiving/receipt/{receipt_id}": {},
    "/maintenance/work-orders": {
        "status": _ANY,
        "work_center_id": _ANY,
        # An equipment asset's id (P2.3): a filter over a kept id.
        "asset_id": _ANY,
        "maintenance_type": _ANY,
        "start_date": _ANY,
        "end_date": _ANY,
    },
    "/maintenance/work-orders/overdue": {},
    "/maintenance/work-orders/{wo_id}": {},
    "/production-calendar": {},
    # ``at`` is an instant and ``work_center_id`` a kept id: neither filters withheld text.
    "/production-calendar/resolve": {"at": _ANY, "work_center_id": _ANY},
}


def ai_external_query_allowed(route_path: Optional[str], query: Iterable[Tuple[str, str]]) -> bool:
    """Gate 4, second half: may an AI-external token send ``query`` (name, value pairs) to ``route_path``?

    ``route_path`` is the matched template RELATIVE to the API prefix. A route with no entry
    in :data:`AI_EXTERNAL_QUERY_PARAMS` accepts no query parameter at all (fail closed).
    """
    allowed = AI_EXTERNAL_QUERY_PARAMS.get(route_path or "", {})
    for name, value in query:
        if name not in allowed:
            return False
        values = allowed[name]
        if values is not None and value not in values:
            return False
    return True


# Business-number lookups whose number is WITHHELD text (a part number is; a work-order
# number is a kept identifier, so ``/work-orders/by-number/...`` is not here). When the
# number names a withheld record, the AI-external answer is the same 404 a number nobody
# carries gets -- a 200 (even fully redacted) would confirm the number exists. The two 404s
# also COST the same: the route's own miss builds the index and commits one
# ``ai_external_lookup_miss`` ledger row, as a withheld hit builds it and commits its
# ``ai_external_redacted`` rows. Otherwise latency alone would re-open the oracle (code
# review P0 round 2, finding 1).
# route (relative) -> (path parameter, record type)
AI_EXTERNAL_MASKED_LOOKUPS: Mapping[str, Tuple[str, str]] = {
    "/parts/by-number/{part_number}": ("part_number", "part"),
}


def ai_external_error_detail(status_code: int) -> str:
    """The ONE error text an AI-external token gets for a route's own error (not a gate refusal).

    A route error can echo the request (a 422's ``input``, a 404 naming the number asked
    for), and the residual text pass would then rewrite an echoed controlled part number to
    ``[export-controlled]`` -- which tells the caller the probe matched. A fixed text per
    status code cannot. Gate refusals keep their own fixed texts.
    """
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "Error"
    return f"Request failed ({status_code} {phrase}); details are withheld from AI-external API tokens."


def relative_route_path(path: Optional[str], api_prefix: str) -> Optional[str]:
    """``path`` with ``api_prefix`` stripped (``/api/v1/parts/`` -> ``/parts/``); None stays None."""
    if path is None:
        return None
    prefix = api_prefix.rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return path[len(prefix) :] or "/"
    return path


# --------------------------------------------------------------------------- the marker


@dataclass(frozen=True)
class ExportControlMarker:
    """What ``get_current_user`` tells the response middleware about a NARROWED API token.

    Written into ``request.scope["state"][EXPORT_CONTROL_STATE_KEY]`` for every read-only
    API token that passed the gates (``refusal_reason`` None), and for an AI-external one a
    gate refused (``refusal_reason`` set: the middleware records the refusal in the
    ledger). No marker -- a person, a full token, a kiosk badge -- means the middleware
    passes the response through untouched. All values come from the api_tokens ROW.
    """

    company_id: int
    user_id: Optional[int]
    api_token_id: Optional[int]
    jti_prefix: Optional[str]
    label: Optional[str]
    read_only: bool = True
    ai_external: bool = False
    route: Optional[str] = None
    refusal_reason: Optional[str] = None

    def credential(self) -> dict:
        """The ``extra_data.credential`` shape ``AuditService`` uses for a token, plus ``kind``."""
        return {
            "kind": "api_token",
            "api_token_id": self.api_token_id,
            "jti_prefix": self.jti_prefix,
            "label": self.label,
        }
