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
from typing import FrozenSet, Optional, Tuple

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
READ_ONLY_FILE_REFUSED_DETAIL = "Read-only API token cannot download files."
EXPORT_CONTROL_FAILED_DETAIL = "Export-control check failed; nothing was sent."

# --------------------------------------------------------------------------- ledger vocabulary

# ``controlled_access_events.action``
ACTION_REDACTED = "ai_external_redacted"
ACTION_REFUSED = "ai_external_refused"

# ``controlled_access_events.denial_reason``
REASON_EXPORT_CONTROLLED_CUSTOMER = "export_controlled_customer"
REASON_UNLINKABLE_RECORD = "unlinkable_record"
REASON_AI_EGRESS_DISABLED = "ai_egress_disabled"
REASON_ROUTE_NOT_ALLOWLISTED = "route_not_allowlisted"
REASON_NON_JSON_RESPONSE = "non_json_response"

# ``controlled_access_events.data_classification`` values this feature writes (a subset of
# ``app.models.governance.DataClassification``; repeated, not imported, to stay pure).
CLASSIFICATION_ITAR = "itar_export_controlled"
CLASSIFICATION_CUI = "cui"
CLASSIFICATION_UNKNOWN = "unknown"

# --------------------------------------------------------------------------- the allowlist

# (METHOD, path template RELATIVE to ``settings.API_V1_PREFIX``) an AI-external token may
# call -- the brief's read set and nothing else: work orders, shop-floor operations,
# downtime, work centers, OEE, parts, routings, NCR / CAR / FAI, quotes, vendors, purchase
# orders, receiving history, maintenance work orders. GET only. There is no time-entry GET
# route; time entries reach the plugin through the work-order timeline.
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
        # Quotes
        ("GET", "/quotes/"),
        ("GET", "/quotes/{quote_id}"),
        # Purchasing and receiving
        ("GET", "/purchasing/vendors"),
        ("GET", "/purchasing/vendors/{vendor_id}"),
        ("GET", "/purchasing/purchase-orders"),
        ("GET", "/purchasing/purchase-orders/{po_id}"),
        ("GET", "/receiving/history"),
        ("GET", "/receiving/receipt/{receipt_id}"),
        # Maintenance work orders (work-center scoped: not customer data)
        ("GET", "/maintenance/work-orders"),
        ("GET", "/maintenance/work-orders/overdue"),
        ("GET", "/maintenance/work-orders/{wo_id}"),
    }
)


def ai_external_operations(api_prefix: str) -> FrozenSet[Tuple[str, str]]:
    """:data:`AI_EXTERNAL_ALLOWED_ROUTES` with ``api_prefix`` (``settings.API_V1_PREFIX``) applied."""
    prefix = api_prefix.rstrip("/")
    return frozenset((method, f"{prefix}{path}") for method, path in AI_EXTERNAL_ALLOWED_ROUTES)


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
