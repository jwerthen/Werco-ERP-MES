"""Audit trail for bulk data exports.

A bulk export is a **disclosure event**, not a domain read. A domain read returns
one record through the UI; an export hands an entire dataset over as a file in a
single request -- the parts master with ``standard_cost``, the full inventory
valuation, every PO line with ``unit_price`` and vendor, every quote with its
customer contacts. That difference is why exports are gated and audited as their
own category even though the underlying reads are deliberately read-broad
(``docs/RBAC_PERMISSIONS.md`` -> Access enforcement model).

The row records **the request, never the payload**: which dataset, how many rows,
which format, which columns were disclosed and which filters selected them.
Reconstructing the file's contents is the ledger's job, not the audit log's.

Per compliance invariant 2 this writes through ``AuditService`` and never to the
``audit_log`` table directly. The shape follows the visitor-log export
(``app/api/endpoints/visitor_logs.py``), the existing audited exporter:
``action="EXPORT"``, ``resource_type`` = the dataset, row count in the
description, committed **before** the file streams so the disclosure is on record
even if the client abandons the download.
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from app.services.audit_service import AuditService

# Hard cap on any one recorded filter value, and on each filter key.
#
# The endpoints bound their own free-text filters at the edge (``max_length`` on
# the ``Query``), which is where a caller gets a useful 422 instead of a silently
# altered audit row. This is the seam's own backstop, so a future exporter that
# forgets one cannot widen the chain: an ``audit_log`` row is un-UPDATE-able and
# un-DELETE-able (the 008/060 triggers) and is covered by the integrity hash, so
# caller-supplied text is either bounded on the way in or bounded nowhere.
#
# The cap sits at the widest column any filter compares against
# (``Quote.customer_name``, String(255)), so a value that could actually match a
# row is never truncated -- only text that was never going to match anything is.
_MAX_FILTER_VALUE_CHARS = 255
_MAX_FILTER_KEY_CHARS = 64
_TRUNCATION_MARKER = "...[truncated]"


def _cap(text: str) -> str:
    """Bound one recorded string, marking it when it had to be shortened."""
    if len(text) <= _MAX_FILTER_VALUE_CHARS:
        return text
    return text[:_MAX_FILTER_VALUE_CHARS] + _TRUNCATION_MARKER


def _json_safe(value: Any) -> Any:
    """Coerce a filter value to something the JSON ``new_values`` column can hold."""
    # Enum first: the status/type filters are ``str``-backed enums, so the ``str``
    # branch below would otherwise swallow them and log the repr.
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return _cap(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # A stored report template's ``filters`` is a JSON structure; recurse so it is
    # recorded as structure rather than flattened into one unqueryable string.
    # Keys are capped too -- in that structure the key is caller-authored as well.
    if isinstance(value, Mapping):
        return {str(k)[:_MAX_FILTER_KEY_CHARS]: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return _cap(str(value))


def log_export(
    db: Session,
    audit: AuditService,
    *,
    dataset: str,
    label: str,
    row_count: int,
    export_format: str,
    columns: Sequence[str],
    known_columns: Optional[Sequence[str]] = None,
    filters: Optional[Mapping[str, Any]] = None,
    resource_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
) -> None:
    """Record one bulk export, then commit so the row lands before the file streams.

    ``dataset`` is the ``resource_type`` (snake-case, e.g. ``purchase_order_line``);
    ``label`` is its human form for the description (e.g. ``purchase order line``).

    ``known_columns``, when given, is the endpoint's recognized column set and
    ``columns`` is what the request actually exported. Only the intersection is
    recorded -- ``columns`` is a caller-supplied query parameter, and an audit row
    is immutable and undeletable, so unrecognized caller text must not be able to
    write itself into the chain. Omit ``known_columns`` when the column list comes
    from a trusted source (a stored report template) rather than the query string.

    ``filters`` values are caller-supplied for the same reason and get the same
    treatment, by length rather than by allowlist: a filter's *value* is free text
    by definition (a warehouse code, a customer-name fragment), so there is no set
    to intersect against -- ``_json_safe`` caps each one at
    ``_MAX_FILTER_VALUE_CHARS``. Callers should still bound their own free-text
    query parameters with ``max_length``; that is where a too-long value earns an
    honest 422 rather than a quietly shortened audit row.

    The detail goes in ``new_values`` rather than ``extra_data`` because
    ``new_values`` is covered by the integrity hash (``compute_audit_hash``) and
    ``extra_data`` is not -- for a disclosure record, *what was asked for* is
    exactly the part that must be tamper-evident.

    **Call this before building the file, not after.** ``/exports/*`` commits the
    row and then generates the workbook, so a generator failure would leave a row
    for a file that never shipped; ``analytics.py`` deliberately builds first
    because ``_export_csv`` refuses an empty result set (400) and a refusal must
    leave no row. The asymmetry is intended: the only error direction available
    here is **over**-recording, which for a disclosure log is the safe one --
    a row without a download is a false positive an auditor can dismiss, whereas
    a download without a row is the failure this seam exists to prevent.
    """
    if known_columns is None:
        disclosed = list(columns)
    else:
        requested = set(columns)
        disclosed = [c for c in known_columns if c in requested]

    applied = {k: _json_safe(v) for k, v in (filters or {}).items() if v is not None}

    audit.log(
        action="EXPORT",
        resource_type=dataset,
        resource_id=resource_id,
        resource_identifier=resource_identifier,
        description=f"Exported {row_count} {label} record(s) to {export_format.upper()}",
        new_values={"format": export_format, "columns": disclosed, "filters": applied},
    )
    db.commit()
