"""
Report Builder Service - Dynamic query execution for custom reports.

ONE catalog (``REPORT_SOURCES``) declares every data source and every field a report may
use: its column expression, label, type, enum options and any join it needs. Everything
else is derived from it -- the ``GET /analytics/data-sources`` catalog
(``data_source_catalog``), the legacy ``FIELD_MAPPINGS`` / ``DATA_SOURCE_MODELS`` maps,
validation (``definition_problems``) and execution (``ReportBuilderService``) -- so the
catalog a client is shown and the fields the query engine accepts can never disagree
again. (They did: the old hand-written catalog advertised two fields the engine silently
dropped and hid sixteen it supported.)

A field's ``type`` is DERIVED from its SQLAlchemy column type (``_field_type``), so a
column whose type changes changes its operators and value coercion with it.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type

from sqlalchemy import Date, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, Integer, Numeric, String, and_, asc, desc, func
from sqlalchemy.orm import Session

from app.core.time_utils import to_utc_iso
from app.core.unicode_text import contains_unencodable_text, is_utf8_encodable
from app.db.mixins import SoftDeleteMixin
from app.db.tenant_filter import tenant_filter
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.purchasing import PurchaseOrder, Vendor
from app.models.quality import NonConformanceReport
from app.models.quote import Quote
from app.models.work_order import WorkOrder
from app.schemas.analytics import (
    AggregateFunction,
    CustomReportColumnInfo,
    CustomReportNote,
    CustomReportRequest,
    CustomReportResult,
    ReportColumn,
    ReportDataSource,
    ReportFilter,
    ReportGroupBy,
    ReportSort,
)
from app.services.labor_cost_service import is_labor_cost_rollup_enabled

logger = logging.getLogger(__name__)

# WORK_ORDERS columns that are populated ONLY by the labor-cost rollup. When
# ``LABOR_COST_ROLLUP_ENABLED`` is OFF (the default) these render a literal 0 that means
# "not tracked", NOT a genuine $0/0-hours. ``labor_tracking_note`` annotates a report so
# a consumer can tell the two apart (G3-content), resolved via the SAME flag chokepoint
# (labor_cost_service.is_labor_cost_rollup_enabled) the completion paths + cost analysis use.
LABOR_DERIVED_WORK_ORDER_FIELDS = ("actual_hours", "actual_cost", "estimated_cost")
LABOR_NOT_TRACKED_NOTE_CODE = "labor_not_tracked"

FIELD_TYPES = ("string", "number", "date", "datetime", "enum")

OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "like", "between")
_COMPARISONS = ("eq", "ne", "gt", "gte", "lt", "lte")
OPERATORS_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    "string": ("eq", "ne", "in", "like"),
    "number": _COMPARISONS + ("in", "between"),
    "date": _COMPARISONS + ("between",),
    "datetime": _COMPARISONS + ("between",),
    "enum": ("eq", "ne", "in"),
}

# Which field types each aggregate accepts.
AGGREGATE_TYPES: Dict[AggregateFunction, Tuple[str, ...]] = {
    AggregateFunction.SUM: ("number",),
    AggregateFunction.AVG: ("number",),
    AggregateFunction.MIN: ("number", "date", "datetime", "string"),
    AggregateFunction.MAX: ("number", "date", "datetime", "string"),
    AggregateFunction.COUNT: FIELD_TYPES,
}
AGGREGATE_LABELS: Dict[AggregateFunction, str] = {
    AggregateFunction.SUM: "Sum of",
    AggregateFunction.AVG: "Average of",
    AggregateFunction.COUNT: "Count of",
    AggregateFunction.MIN: "Minimum of",
    AggregateFunction.MAX: "Maximum of",
}
_AGGREGATE_FUNCS: Dict[AggregateFunction, Callable[[Any], Any]] = {
    AggregateFunction.SUM: func.sum,
    AggregateFunction.AVG: func.avg,
    AggregateFunction.COUNT: func.count,
    AggregateFunction.MIN: func.min,
    AggregateFunction.MAX: func.max,
}

# ``in`` list cap, and the longest text value a filter may carry. A filter value is stored
# in the template and copied into immutable audit rows; no compared column is longer than
# 255 characters, so this refuses nothing that could ever match.
MAX_IN_VALUES = 500
MAX_TEXT_VALUE_LENGTH = 500
MAX_ALIAS_LENGTH = 100

# The widest integer any engine binds: a signed 64-bit value. SQLite raises OverflowError
# binding a larger Python int, and no column in the catalog holds a number that large, so
# a number filter outside this range is refused rather than allowed to fail at execution.
MIN_INTEGER_VALUE = -(2**63)
MAX_INTEGER_VALUE = 2**63 - 1

# The refusal for text a value or alias carries that UTF-8 cannot encode (an unpaired UTF-16
# surrogate such as JSON's ``"\\ud800"``). It never echoes the value: rendering it is the failure.
# Such text is accepted by ``json.loads``, ``Any`` and ``str`` alike and stored by a JSON
# column, and then every response that renders the template raises -- so ONE saved template
# carrying it 500'd the company's whole template list.
UNENCODABLE_TEXT_PROBLEM = "cannot contain characters that cannot be stored (an unpaired UTF-16 surrogate)"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# An instant: a date, then a time of at least hours and minutes; zone optional (none = UTC).
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class ReportValueError(ValueError):
    """A filter value or operator the field cannot take. The message is user-facing."""


@dataclass(frozen=True)
class ReportJoin:
    """A LEFT OUTER JOIN a field needs. ``on`` builds the ON clause for a company, so the
    joined table is scoped to the tenant inside the join itself."""

    key: str
    target: Any
    on: Callable[[int], Any]


@dataclass(frozen=True)
class ReportField:
    name: str
    label: str
    column: Any  # an ORM InstrumentedAttribute
    join: Optional[ReportJoin] = None
    type: str = field(init=False)
    enum_class: Optional[Type[Enum]] = field(init=False)

    def __post_init__(self) -> None:
        field_type, enum_class = _field_type(self.column)
        object.__setattr__(self, "type", field_type)
        object.__setattr__(self, "enum_class", enum_class)

    @property
    def options(self) -> Optional[List[str]]:
        if self.enum_class is None:
            return None
        return [str(member.value) for member in self.enum_class]


@dataclass(frozen=True)
class ReportSource:
    key: ReportDataSource
    label: str
    model: Any
    fields: Dict[str, ReportField]


def _sa_type(column: Any) -> Any:
    return column.property.columns[0].type


def _field_type(column: Any) -> Tuple[str, Optional[Type[Enum]]]:
    """Report type for a mapped column, from its SQLAlchemy type.

    Order matters: ``sqlalchemy.Enum`` subclasses ``String`` and ``Float`` subclasses
    ``Numeric``. An unsupported column type fails at import, so a field cannot be added
    to the catalog without the engine knowing how to compare it.
    """
    sa_type = _sa_type(column)
    if isinstance(sa_type, SAEnum):
        if sa_type.enum_class is None:
            raise TypeError(f"{column}: a report enum field needs a Python enum class")
        return "enum", sa_type.enum_class
    if isinstance(sa_type, DateTime):
        return "datetime", None
    if isinstance(sa_type, Date):
        return "date", None
    if isinstance(sa_type, (Integer, Float, Numeric)):
        return "number", None
    if isinstance(sa_type, String):
        return "string", None
    raise TypeError(f"{column}: unsupported column type {sa_type!r} for a report field")


def _fields(*specs: ReportField) -> Dict[str, ReportField]:
    return {spec.name: spec for spec in specs}


# The joined Part / Vendor are deliberately NOT filtered on ``is_deleted``: a lot's part
# and a PO's vendor are historical records that must keep naming who they named
# (CLAUDE.md invariant 3, "a historical record ... must keep naming who it named"). Only
# the report's BASE model is soft-delete filtered. Both joins are company-scoped in the
# ON clause, so a foreign tenant's row with a colliding id can never be joined in.
_INVENTORY_PART = ReportJoin(
    key="inventory_part",
    target=Part,
    on=lambda company_id: and_(Part.id == InventoryItem.part_id, Part.company_id == company_id),
)
_PURCHASE_ORDER_VENDOR = ReportJoin(
    key="purchase_order_vendor",
    target=Vendor,
    on=lambda company_id: and_(Vendor.id == PurchaseOrder.vendor_id, Vendor.company_id == company_id),
)


REPORT_SOURCES: Dict[ReportDataSource, ReportSource] = {
    source.key: source
    for source in (
        ReportSource(
            key=ReportDataSource.WORK_ORDERS,
            label="Work Orders",
            model=WorkOrder,
            fields=_fields(
                ReportField("work_order_number", "WO Number", WorkOrder.work_order_number),
                ReportField("status", "Status", WorkOrder.status),
                ReportField("quantity_ordered", "Qty Ordered", WorkOrder.quantity_ordered),
                ReportField("quantity_complete", "Qty Complete", WorkOrder.quantity_complete),
                ReportField("quantity_scrapped", "Qty Scrapped", WorkOrder.quantity_scrapped),
                ReportField("due_date", "Due Date", WorkOrder.due_date),
                ReportField("actual_start", "Start Date", WorkOrder.actual_start),
                ReportField("actual_end", "End Date", WorkOrder.actual_end),
                ReportField("customer_name", "Customer", WorkOrder.customer_name),
                ReportField("customer_po", "Customer PO", WorkOrder.customer_po),
                # G3-content: ``estimated_hours`` intentionally NOT exposed -- it has no writer
                # anywhere in the system (structurally 0 in every tenant), so a report column for
                # it only renders a misleading literal 0. Computing it needs a routing/operations
                # join (out of scope). Dropped rather than surfaced as a phantom column.
                ReportField("actual_hours", "Actual Hours", WorkOrder.actual_hours),
                ReportField("estimated_cost", "Est. Cost", WorkOrder.estimated_cost),
                ReportField("actual_cost", "Actual Cost", WorkOrder.actual_cost),
                ReportField("created_at", "Created", WorkOrder.created_at),
            ),
        ),
        ReportSource(
            key=ReportDataSource.PARTS,
            label="Parts",
            model=Part,
            fields=_fields(
                ReportField("part_number", "Part Number", Part.part_number),
                ReportField("name", "Name", Part.name),
                ReportField("part_type", "Type", Part.part_type),
                ReportField("standard_cost", "Std Cost", Part.standard_cost),
                ReportField("material_cost", "Material Cost", Part.material_cost),
                ReportField("labor_cost", "Labor Cost", Part.labor_cost),
                ReportField("lead_time_days", "Lead Time", Part.lead_time_days),
                ReportField("safety_stock", "Safety Stock", Part.safety_stock),
                ReportField("reorder_point", "Reorder Point", Part.reorder_point),
            ),
        ),
        ReportSource(
            key=ReportDataSource.INVENTORY,
            label="Inventory",
            model=InventoryItem,
            fields=_fields(
                ReportField("part_number", "Part Number", Part.part_number, join=_INVENTORY_PART),
                ReportField("quantity_on_hand", "Qty On Hand", InventoryItem.quantity_on_hand),
                ReportField("quantity_allocated", "Qty Allocated", InventoryItem.quantity_allocated),
                ReportField("quantity_available", "Qty Available", InventoryItem.quantity_available),
                ReportField("location", "Location", InventoryItem.location),
                ReportField("lot_number", "Lot Number", InventoryItem.lot_number),
                ReportField("unit_cost", "Unit Cost", InventoryItem.unit_cost),
                ReportField("status", "Status", InventoryItem.status),
            ),
        ),
        ReportSource(
            key=ReportDataSource.QUALITY,
            label="Quality (NCRs)",
            model=NonConformanceReport,
            fields=_fields(
                ReportField("ncr_number", "NCR Number", NonConformanceReport.ncr_number),
                ReportField("status", "Status", NonConformanceReport.status),
                ReportField("source", "Source", NonConformanceReport.source),
                ReportField("disposition", "Disposition", NonConformanceReport.disposition),
                ReportField("quantity_affected", "Qty Affected", NonConformanceReport.quantity_affected),
                ReportField("quantity_rejected", "Qty Rejected", NonConformanceReport.quantity_rejected),
                ReportField("detected_date", "Detected Date", NonConformanceReport.detected_date),
                ReportField("closed_date", "Closed Date", NonConformanceReport.closed_date),
                ReportField("estimated_cost", "Est. Cost", NonConformanceReport.estimated_cost),
                ReportField("actual_cost", "Actual Cost", NonConformanceReport.actual_cost),
            ),
        ),
        ReportSource(
            key=ReportDataSource.PURCHASING,
            label="Purchase Orders",
            model=PurchaseOrder,
            fields=_fields(
                ReportField("po_number", "PO Number", PurchaseOrder.po_number),
                ReportField("vendor_name", "Vendor", Vendor.name, join=_PURCHASE_ORDER_VENDOR),
                ReportField("status", "Status", PurchaseOrder.status),
                ReportField("order_date", "Order Date", PurchaseOrder.order_date),
                ReportField("required_date", "Required Date", PurchaseOrder.required_date),
                ReportField("subtotal", "Subtotal", PurchaseOrder.subtotal),
                ReportField("total", "Total", PurchaseOrder.total),
            ),
        ),
        ReportSource(
            key=ReportDataSource.QUOTES,
            label="Quotes",
            model=Quote,
            fields=_fields(
                ReportField("quote_number", "Quote Number", Quote.quote_number),
                ReportField("customer_name", "Customer", Quote.customer_name),
                ReportField("status", "Status", Quote.status),
                ReportField("quote_date", "Quote Date", Quote.quote_date),
                ReportField("subtotal", "Subtotal", Quote.subtotal),
                ReportField("total", "Total", Quote.total),
                ReportField("lead_time_days", "Lead Time", Quote.lead_time_days),
            ),
        ),
    )
}

# Derived views of the catalog, kept under their historical names (imported elsewhere).
DATA_SOURCE_MODELS: Dict[ReportDataSource, Any] = {key: source.model for key, source in REPORT_SOURCES.items()}
FIELD_MAPPINGS: Dict[ReportDataSource, Dict[str, Any]] = {
    key: {name: spec.column for name, spec in source.fields.items()} for key, source in REPORT_SOURCES.items()
}


def data_source_catalog() -> Dict[str, Dict[str, Any]]:
    """The ``GET /analytics/data-sources`` body, derived from ``REPORT_SOURCES``.

    ``options`` appears only on enum fields (the enum's values, which is what a filter on
    that field takes and what a report row shows).
    """
    catalog: Dict[str, Dict[str, Any]] = {}
    for key, source in REPORT_SOURCES.items():
        fields: List[Dict[str, Any]] = []
        for spec in source.fields.values():
            entry: Dict[str, Any] = {"name": spec.name, "label": spec.label, "type": spec.type}
            if spec.options is not None:
                entry["options"] = spec.options
            fields.append(entry)
        catalog[key.value] = {"label": source.label, "fields": fields}
    return catalog


# ---------------------------------------------------------------------------
# Validation: the ONE rule set
# ---------------------------------------------------------------------------


def _show(value: Any) -> str:
    """A short, JSON-looking rendering of a caller value for an error message."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError, RecursionError):
        try:
            # repr can fail too: an int past Python's digit limit raises ValueError.
            text = repr(value)
        except (TypeError, ValueError, RecursionError):
            text = f"a {type(value).__name__} value"
    return text if len(text) <= 60 else text[:57] + "..."


def _a(field_type: str) -> str:
    """``field_type`` with its indefinite article, for messages ("an enum", "a number")."""
    return f"an {field_type}" if field_type[:1] in "aeiou" else f"a {field_type}"


def coerce_value(spec: ReportField, value: Any) -> Any:
    """Coerce ONE non-null filter value to the Python type its column binds.

    Used by both ``definition_problems`` (to judge) and the executor (to bind), so the
    two cannot disagree. Enum fields take the enum's VALUE and bind the enum MEMBER:
    most of these columns are non-``values_callable`` ``sqlalchemy.Enum`` types that
    store the member NAME (``IN_PROGRESS``), so binding the bare value string
    (``in_progress``) silently matched nothing.

    Every way a value can fail -- here OR later, when the engine binds it -- must surface
    as a ``ReportValueError``: anything else escapes ``definition_problems`` and the request
    validator, so a request answers 500 instead of 422, and one stored template carrying
    such a value would 500 the template list. Hence the integer range, the NUL and
    unencodable-text refusals and the guarded UTC conversion below.
    """
    if isinstance(value, str) and "\x00" in value:
        # Postgres text cannot hold NUL (the driver raises at bind), and CPython's
        # ``fromisoformat`` quietly ignores a trailing one -- so it is refused for every type.
        raise ReportValueError("a value cannot contain a NUL (0x00) character")
    if isinstance(value, str) and not is_utf8_encodable(value):
        # Refused for every type, like NUL. Nothing would match it, and it would be stored.
        raise ReportValueError(f"a value {UNENCODABLE_TEXT_PROBLEM}")
    if spec.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReportValueError(f"{_show(value)} is not a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ReportValueError(f"{_show(value)} is not a finite number")
        if isinstance(value, int) and not MIN_INTEGER_VALUE <= value <= MAX_INTEGER_VALUE:
            raise ReportValueError(f"{_show(value)} is out of range for a number filter")
        return value
    if spec.type == "string":
        if not isinstance(value, str):
            raise ReportValueError(f"{_show(value)} is not text")
        if len(value) > MAX_TEXT_VALUE_LENGTH:
            raise ReportValueError(f"a text value may be at most {MAX_TEXT_VALUE_LENGTH} characters")
        return value
    if spec.type == "date":
        if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
            raise ReportValueError(f"{_show(value)} is not a date (use YYYY-MM-DD)")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ReportValueError(f"{_show(value)} is not a valid date")
    if spec.type == "datetime":
        if not isinstance(value, str) or not _DATETIME_RE.match(value):
            raise ReportValueError(
                f"{_show(value)} is not a date and time (use ISO-8601, e.g. 2026-01-31T14:00:00Z; " "no zone means UTC)"
            )
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                # Timestamps are stored as naive UTC. The conversion is inside the guard:
                # an instant at the calendar's edge (0001-01-01T00:00+05:00,
                # 9999-12-31T23:59-01:00) has no UTC equivalent and raises OverflowError.
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except OverflowError:
            raise ReportValueError(f"{_show(value)} is outside the supported range of dates and times")
        except ValueError:
            raise ReportValueError(f"{_show(value)} is not a valid date and time")
        return parsed
    if spec.type == "enum":
        options = spec.options or []
        if not isinstance(value, str) or value not in options:
            raise ReportValueError(f"{_show(value)} is not one of: {', '.join(options)}")
        assert spec.enum_class is not None
        return spec.enum_class(value)
    raise ReportValueError(f"unsupported field type {spec.type}")  # pragma: no cover - catalog fails at import


def coerce_filter(spec: ReportField, operator: Any, value: Any, value2: Any) -> Tuple[Any, Any]:
    """Validate one filter and return its bound ``(value, value2)``.

    ``value`` null means IS NULL / IS NOT NULL and is accepted only with ``eq`` / ``ne``.
    ``value2`` is read only by ``between``, and a ``value2`` of 0 is a real bound (the old
    engine's truthiness check dropped it). An unread ``value2`` is still STORED with the
    filter, so text in it that cannot be stored is refused whatever the operator.
    """
    if contains_unencodable_text(value2):
        raise ReportValueError(f"value2 {UNENCODABLE_TEXT_PROBLEM}")
    allowed = OPERATORS_BY_TYPE[spec.type]
    if operator not in OPERATORS:
        raise ReportValueError(f"unknown operator {_show(operator)}; use one of: {', '.join(allowed)}")
    if operator not in allowed:
        raise ReportValueError(
            f'operator "{operator}" does not apply to {_a(spec.type)} field; use one of: {", ".join(allowed)}'
        )
    if operator in ("eq", "ne"):
        return (None if value is None else coerce_value(spec, value)), None
    if value is None:
        raise ReportValueError(f'operator "{operator}" needs a value')
    if operator == "in":
        if not isinstance(value, list):
            raise ReportValueError('operator "in" needs a list of values')
        if not value:
            raise ReportValueError('operator "in" needs at least one value')
        if len(value) > MAX_IN_VALUES:
            raise ReportValueError(f'operator "in" takes at most {MAX_IN_VALUES} values')
        if any(item is None for item in value):
            raise ReportValueError('an "in" list cannot contain null; use "eq" with a null value to match empty fields')
        return [coerce_value(spec, item) for item in value], None
    if operator == "between":
        if value2 is None:
            raise ReportValueError('operator "between" needs a second value (value2)')
        return coerce_value(spec, value), coerce_value(spec, value2)
    return coerce_value(spec, value), None


def output_key(column: ReportColumn) -> str:
    """A column's key in the result rows: its alias, else its field."""
    return column.alias or column.field


def definition_problems(
    data_source: Any,
    columns: Sequence[ReportColumn],
    filters: Sequence[ReportFilter],
    group_by: Sequence[ReportGroupBy],
    sort: Sequence[ReportSort],
) -> List[str]:
    """Every reason this report definition cannot run, as user-facing sentences.

    THE rule set for a report definition. The request schemas run it in a validator (a
    non-empty list is one 422), and a stored template is judged by building the same
    request model from its JSON -- so ``definition_errors`` on a listed template and the
    409 its run answers come from here too. It takes the SHARED models and never mutates
    them; nothing here may be moved onto those models (see ``app/schemas/analytics.py``).
    """
    source = REPORT_SOURCES.get(data_source)
    if source is None:
        if data_source == ReportDataSource.PRODUCTION:
            return ['The "production" data source is no longer available; choose another data source']
        return [f"Unknown data source {_show(data_source)}"]

    problems: List[str] = []
    of_source = f"a {source.label} field"
    if not columns:
        problems.append("Select at least one column")

    output_keys: Dict[str, int] = {}
    plain_columns: List[Tuple[int, ReportField]] = []
    aggregated = False
    for index, column in enumerate(columns, start=1):
        spec = source.fields.get(column.field)
        if spec is None:
            problems.append(f"Column {index}: {_show(column.field)} is not {of_source}")
        if column.alias is not None and len(column.alias) > MAX_ALIAS_LENGTH:
            problems.append(f"Column {index}: an alias may be at most {MAX_ALIAS_LENGTH} characters")
        if column.alias is not None and "\x00" in column.alias:
            # The output key is the SELECT label, so it reaches the SQL text itself.
            problems.append(f"Column {index}: an alias cannot contain a NUL (0x00) character")
        if column.alias is not None and not is_utf8_encodable(column.alias):
            problems.append(f"Column {index}: an alias {UNENCODABLE_TEXT_PROBLEM}")
        key = output_key(column)
        if key in output_keys:
            problems.append(
                f"Columns {output_keys[key]} and {index} both produce the output {_show(key)}; "
                "give one of them a different alias"
            )
        else:
            output_keys[key] = index
        if column.aggregate is not None:
            aggregated = True
            if spec is not None and spec.type not in AGGREGATE_TYPES[column.aggregate]:
                kinds = " or ".join(AGGREGATE_TYPES[column.aggregate])
                problems.append(
                    f"Column {index} ({spec.label}): {column.aggregate.value} works only on {kinds} fields, "
                    f"and this is {_a(spec.type)} field"
                )
        elif spec is not None:
            plain_columns.append((index, spec))

    for index, report_filter in enumerate(filters, start=1):
        spec = source.fields.get(report_filter.field)
        if spec is None:
            problems.append(f"Filter {index}: {_show(report_filter.field)} is not {of_source}")
            continue
        try:
            coerce_filter(spec, report_filter.operator, report_filter.value, report_filter.value2)
        except ReportValueError as exc:
            problems.append(f"Filter {index} ({spec.label}): {exc}")

    group_fields = set()
    for index, group in enumerate(group_by, start=1):
        spec = source.fields.get(group.field)
        if spec is None:
            problems.append(f"Group by {index}: {_show(group.field)} is not {of_source}")
        else:
            group_fields.add(spec.name)
        if group.granularity is not None:
            problems.append(f'Group by {index}: date granularity is not supported yet; remove "granularity"')

    grouped = bool(group_by) or aggregated
    if grouped:
        for index, spec in plain_columns:
            if spec.name not in group_fields:
                problems.append(
                    f"Column {index} ({spec.label}): the report is grouped, so this column must be aggregated "
                    "or added to group by"
                )

    for index, report_sort in enumerate(sort, start=1):
        direction = report_sort.direction.lower() if isinstance(report_sort.direction, str) else None
        if direction not in ("asc", "desc"):
            problems.append(f'Sort {index}: direction must be "asc" or "desc", not {_show(report_sort.direction)}')
        targets_output = report_sort.field in output_keys
        if not targets_output and report_sort.field not in source.fields:
            problems.append(
                f"Sort {index}: {_show(report_sort.field)} is neither {of_source} nor a column of this report"
            )
            continue
        if grouped and not targets_output and report_sort.field not in group_fields:
            problems.append(
                f"Sort {index}: the report is grouped, so sort by a group-by field or by one of the report's columns"
            )
    return problems


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlannedColumn:
    key: str
    label: str
    type: str
    expression: Any


def _plan_columns(source: ReportSource, columns: Sequence[ReportColumn]) -> List[_PlannedColumn]:
    planned: List[_PlannedColumn] = []
    for column in columns:
        spec = source.fields[column.field]
        expression: Any = spec.column
        if column.aggregate is None:
            label, col_type = spec.label, spec.type
        else:
            expression = _AGGREGATE_FUNCS[column.aggregate](spec.column)
            label = f"{AGGREGATE_LABELS[column.aggregate]} {spec.label}"
            col_type = spec.type if column.aggregate in (AggregateFunction.MIN, AggregateFunction.MAX) else "number"
        planned.append(
            _PlannedColumn(key=output_key(column), label=column.alias or label, type=col_type, expression=expression)
        )
    return planned


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so the value matches literally (paired with ``escape="\\\\"``)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_condition(spec: ReportField, operator: str, value: Any, value2: Any) -> Any:
    column = spec.column
    if operator == "eq":
        return column.is_(None) if value is None else column == value
    if operator == "ne":
        return column.is_not(None) if value is None else column != value
    if operator == "gt":
        return column > value
    if operator == "gte":
        return column >= value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "in":
        return column.in_(value)
    if operator == "like":
        return column.ilike(f"%{_escape_like(value)}%", escape="\\")
    if operator == "between":
        return column.between(value, value2)
    raise ReportValueError(f"unknown operator {operator}")  # pragma: no cover - validated upstream


class ReportDefinitionError(ValueError):
    """Raised when a report definition that skipped schema validation reaches the engine."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class ReportBuilderService:
    def __init__(self, db: Session):
        self.db = db

    def labor_tracking_note(self, request: CustomReportRequest, company_id: int) -> Optional[Dict[str, Any]]:
        """Flag-OFF annotation for labor-derived report columns (G3-content).

        When ``LABOR_COST_ROLLUP_ENABLED`` is OFF (default) for this company AND the
        report selects any of the WORK_ORDERS labor-derived columns
        (``actual_hours`` / ``actual_cost`` / ``estimated_cost``), those columns render a
        literal 0 that means "not tracked", not a genuine zero. Returns a small metadata
        dict that ``execute_report`` turns into the ``labor_not_tracked`` note in the
        result body, so a consumer can tell the two apart -- mirroring the documented
        flag-OFF stance of ``analytics_service.get_cost_analysis``. Returns ``None`` when
        the flag is ON, the data source isn't WORK_ORDERS, or no labor-derived column was
        selected. Resolves the flag via the SAME chokepoint
        (``labor_cost_service.is_labor_cost_rollup_enabled``).
        """
        if request.data_source != ReportDataSource.WORK_ORDERS:
            return None
        if is_labor_cost_rollup_enabled(company_id):
            return None
        selected = {col.field for col in request.columns}
        not_tracked = [f for f in LABOR_DERIVED_WORK_ORDER_FIELDS if f in selected]
        if not_tracked:
            return {
                "labor_cost_rollup_enabled": False,
                "not_tracked_fields": not_tracked,
                "note": (
                    "Labor-cost rollup is disabled for this company, so "
                    f"{', '.join(not_tracked)} are reported as 0 (not tracked), not a measured $0."
                ),
            }
        return None

    def execute_report(
        self, request: CustomReportRequest, company_id: int, *, limit: Optional[int] = None
    ) -> CustomReportResult:
        """Run a validated report for ``company_id`` and return a ``CustomReportResult``.

        * Tenant isolation (G3-scope): the base model is ALWAYS restricted to
          ``company_id`` before any user filter, and every joined table is
          company-scoped in its ON clause.
        * Soft delete (invariant 3): a soft-deletable base model (work orders, parts,
          NCRs -- voided NCRs included -- and purchase orders) is filtered to
          ``is_deleted == False``. Joined tables are not (see ``_INVENTORY_PART``).
        * Limits: ``limit`` (default ``request.limit``) rows are returned; one extra row
          is fetched so ``truncated`` says exactly whether more rows matched.
        """
        source = REPORT_SOURCES.get(request.data_source)
        if source is None:
            raise ReportDefinitionError([f"Unknown data source {_show(request.data_source)}"])
        problems = definition_problems(
            request.data_source, request.columns, request.filters, request.group_by, request.sort
        )
        if problems:
            raise ReportDefinitionError(problems)

        row_limit = request.limit if limit is None else limit
        planned = _plan_columns(source, request.columns)
        by_key = {column.key: column for column in planned}

        # Every field the definition touches decides which joins the query needs.
        referenced = (
            [c.field for c in request.columns]
            + [f.field for f in request.filters]
            + [g.field for g in request.group_by]
            + [s.field for s in request.sort if s.field not in by_key]
        )
        joins: Dict[str, ReportJoin] = {}
        for name in referenced:
            join = source.fields[name].join
            if join is not None:
                joins.setdefault(join.key, join)

        query = self.db.query(*[column.expression.label(column.key) for column in planned]).select_from(source.model)
        for join in joins.values():
            query = query.outerjoin(join.target, join.on(company_id))

        # G3-scope: tenant isolation -- restrict to the active company BEFORE any
        # user-supplied filters/group-by/sort.
        query = tenant_filter(query, source.model, company_id)
        if issubclass(source.model, SoftDeleteMixin):
            query = query.filter(source.model.is_deleted == False)  # noqa: E712

        for report_filter in request.filters:
            spec = source.fields[report_filter.field]
            value, value2 = coerce_filter(spec, report_filter.operator, report_filter.value, report_filter.value2)
            query = query.filter(_filter_condition(spec, report_filter.operator, value, value2))

        if request.group_by:
            query = query.group_by(*[source.fields[g.field].column for g in request.group_by])

        for report_sort in request.sort:
            target = by_key.get(report_sort.field)
            expression = target.expression if target is not None else source.fields[report_sort.field].column
            query = query.order_by(desc(expression) if report_sort.direction.lower() == "desc" else asc(expression))
        grouped = bool(request.group_by) or any(c.aggregate is not None for c in request.columns)
        if not grouped:
            # Deterministic order (and so a deterministic first ``limit`` rows) when the
            # caller's sort leaves ties.
            query = query.order_by(source.model.id)

        fetched = query.limit(row_limit + 1).all()
        truncated = len(fetched) > row_limit
        rows = [
            {column.key: self._format_value(row[index]) for index, column in enumerate(planned)}
            for row in fetched[:row_limit]
        ]

        notes: List[CustomReportNote] = []
        labor = self.labor_tracking_note(request, company_id)
        if labor is not None:
            notes.append(
                CustomReportNote(
                    code=LABOR_NOT_TRACKED_NOTE_CODE, message=labor["note"], fields=labor["not_tracked_fields"]
                )
            )
        return CustomReportResult(
            columns=[CustomReportColumnInfo(key=c.key, label=c.label, type=c.type) for c in planned],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            notes=notes,
        )

    def _format_value(self, value: Any) -> Any:
        """Format a value for JSON serialization."""
        if value is None:
            return None
        if isinstance(value, Decimal):  # e.g. Postgres avg() over an integer column
            return round(float(value), 2)
        if hasattr(value, 'value'):  # Enum
            return value.value
        if isinstance(value, datetime):  # datetime -> UTC ISO with trailing Z
            return to_utc_iso(value)
        if hasattr(value, 'isoformat'):  # date (day-only) -> bare YYYY-MM-DD
            return value.isoformat()
        if isinstance(value, float):
            return round(value, 2)
        return value
