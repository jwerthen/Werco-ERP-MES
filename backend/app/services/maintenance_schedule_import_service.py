"""Load a preventive-maintenance (PM) schedule sheet into ``MaintenanceSchedule`` rows.

The import template is ``maintenance-schedules``: ONE ROW PER TASK. Each row names exactly
one target -- a production work center (``work_center_code``, case-insensitive, inactive
allowed) or a non-production equipment asset (``asset_tag``) -- a ``task``, and a
``frequency``. Rows that share a target, a frequency (and, for ``custom``, the same
``frequency_days``) and a schedule name -- ``schedule_name`` when filled, else
``"<target> <Frequency> PM"`` -- become ONE schedule whose checklist is the ordered,
de-duplicated task list (a JSON array of strings). A machine whose sheet carries monthly and
yearly tasks therefore gets two schedules, because a schedule has one frequency.

Frequencies are the model's ``MaintenanceFrequency`` values (daily, weekly, biweekly,
monthly, quarterly, semi_annual, annual, custom) plus the spellings a PM sheet uses
("yearly" -> annual, "semi-annual", "bi-weekly"...). "As necessary" / "as needed" has no
cadence: such a row is SKIPPED with a warning, never guessed into a frequency.

Assets: an ``asset_tag`` that matches no asset of the company is CREATED on commit when the
row carries ``asset_name`` (and optionally ``asset_type``); the preview lists every asset it
will create. Without ``asset_name`` an unknown tag is a row error. An existing asset is never
renamed or retyped here.

Upsert: an existing ACTIVE schedule with the same target and the same name (case and
spacing ignored) is updated -- checklist replaced by the sheet's task list, frequency set,
and duration / shutdown / priority / next due date set only where the sheet fills them;
otherwise a schedule is created (next due date = the sheet's, else today, Central). A
re-run of the same sheet changes nothing and writes no audit row.

Open work orders: a changed checklist binds the schedule's NEXT work order. An open one opened since
migration 132 keeps the checklist snapshot it was opened with (a row warning says so); one opened
before it has no snapshot and is judged on the schedule's current checklist, so the warning says the
import changes its steps -- and a standing API token whose import takes steps away from such a
schedule gets a row error (the rule ``PUT /maintenance/schedules/{id}`` applies, 403 there).

All-or-nothing: any row error makes the commit refuse (409) having written nothing. A commit
writes one REQUIRED audit row per created asset and per created / updated schedule, plus a
summary row, in the caller's single transaction (``app.api.audit_guard.required_audit``).
``preview_pm_import`` takes no ``AuditService`` and writes nothing, structurally.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.time_utils import central_today
from app.db.locks import acquire_generator_lock
from app.models.maintenance import (
    OPEN_MAINTENANCE_STATUSES,
    MaintenanceAsset,
    MaintenanceFrequency,
    MaintenancePriority,
    MaintenanceSchedule,
    MaintenanceType,
    MaintenanceWorkOrder,
)
from app.models.work_center import WorkCenter
from app.services.audit_service import AuditService
from app.services.import_service import ParsedTable, parse_date_field, parse_import_file
from app.services.maintenance_checklist import API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL, removed_items

TEMPLATE_ENTITY = "maintenance-schedules"
IMPORT_SOURCE = "pm_import"
HEADER_DETECT = frozenset({"task", "frequency"})
REQUIRED_COLUMNS = {"task", "frequency"}

WORK_CENTER_COLUMNS = ("work_center_code", "work_center")
ASSET_TAG_COLUMNS = ("asset_tag",)
ASSET_NAME_COLUMNS = ("asset_name",)
ASSET_TYPE_COLUMNS = ("asset_type",)
SCHEDULE_NAME_COLUMNS = ("schedule_name",)
TASK_COLUMNS = ("task",)
FREQUENCY_COLUMNS = ("frequency",)
FREQUENCY_DAYS_COLUMNS = ("frequency_days",)
NEXT_DUE_COLUMNS = ("next_due_date", "next_due")
DURATION_COLUMNS = ("estimated_duration_hours", "duration_hours")
SHUTDOWN_COLUMNS = ("requires_shutdown",)
PRIORITY_COLUMNS = ("priority",)

MAX_TASK_LENGTH = 500
MAX_SCHEDULE_NAME_LENGTH = 255
MAX_DURATION_HOURS = 1000.0
MAX_FREQUENCY_DAYS = 3650

TARGET_WORK_CENTER = "work_center"
TARGET_ASSET = "asset"

_WHITESPACE = re.compile(r"\s+")
_FREQUENCY_SEPARATORS = re.compile(r"[-_/]+")

# The spellings a PM sheet uses, normalized (casefold, '-'/'_'/'/' read as spaces, spacing
# collapsed, a trailing '.' dropped). Deliberately absent: "bi-annual" / "biannual" -- it
# means twice a year to some and every two years to others, so it is a row error.
FREQUENCY_ALIASES: Dict[str, MaintenanceFrequency] = {
    "daily": MaintenanceFrequency.DAILY,
    "every day": MaintenanceFrequency.DAILY,
    "weekly": MaintenanceFrequency.WEEKLY,
    "every week": MaintenanceFrequency.WEEKLY,
    "biweekly": MaintenanceFrequency.BIWEEKLY,
    "bi weekly": MaintenanceFrequency.BIWEEKLY,
    "every 2 weeks": MaintenanceFrequency.BIWEEKLY,
    "every two weeks": MaintenanceFrequency.BIWEEKLY,
    "every other week": MaintenanceFrequency.BIWEEKLY,
    "monthly": MaintenanceFrequency.MONTHLY,
    "every month": MaintenanceFrequency.MONTHLY,
    "quarterly": MaintenanceFrequency.QUARTERLY,
    "every quarter": MaintenanceFrequency.QUARTERLY,
    "semi annual": MaintenanceFrequency.SEMI_ANNUAL,
    "semiannual": MaintenanceFrequency.SEMI_ANNUAL,
    "semi annually": MaintenanceFrequency.SEMI_ANNUAL,
    "semiannually": MaintenanceFrequency.SEMI_ANNUAL,
    "twice a year": MaintenanceFrequency.SEMI_ANNUAL,
    "every 6 months": MaintenanceFrequency.SEMI_ANNUAL,
    "annual": MaintenanceFrequency.ANNUAL,
    "annually": MaintenanceFrequency.ANNUAL,
    "yearly": MaintenanceFrequency.ANNUAL,
    "every year": MaintenanceFrequency.ANNUAL,
    "once a year": MaintenanceFrequency.ANNUAL,
    "custom": MaintenanceFrequency.CUSTOM,
}
AS_NEEDED_FREQUENCIES = frozenset(
    {"as necessary", "as needed", "as required", "when needed", "when necessary", "as req'd", "prn"}
)

FREQUENCY_LABELS: Dict[MaintenanceFrequency, str] = {
    MaintenanceFrequency.DAILY: "Daily",
    MaintenanceFrequency.WEEKLY: "Weekly",
    MaintenanceFrequency.BIWEEKLY: "Biweekly",
    MaintenanceFrequency.MONTHLY: "Monthly",
    MaintenanceFrequency.QUARTERLY: "Quarterly",
    MaintenanceFrequency.SEMI_ANNUAL: "Semi-Annual",
    MaintenanceFrequency.ANNUAL: "Annual",
    MaintenanceFrequency.CUSTOM: "Custom",
}

_TRUE = {"true", "yes", "y", "1", "x"}
_FALSE = {"false", "no", "n", "0"}

# The schedule fields a sheet may set at group level, in audit-diff order.
GROUP_FIELDS = ("next_due_date", "estimated_duration_hours", "requires_shutdown", "priority")


class AsNeededFrequency(ValueError):
    """The row's frequency has no cadence ("as necessary"): skip it, never guess."""


def source_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _collapse(text: Any) -> str:
    return _WHITESPACE.sub(" ", str(text or "")).strip()


def _key(text: Any) -> str:
    return _collapse(text).casefold()


def parse_frequency(value: Any) -> MaintenanceFrequency:
    """A sheet frequency -> ``MaintenanceFrequency``. Raises ``AsNeededFrequency`` for a
    cadence-less value and ``ValueError`` (naming the allowed values) for anything else."""
    text = _collapse(_FREQUENCY_SEPARATORS.sub(" ", str(value or ""))).casefold().rstrip(".").strip()
    if not text:
        raise ValueError("frequency is required")
    if text in AS_NEEDED_FREQUENCIES:
        raise AsNeededFrequency(value)
    for member in MaintenanceFrequency:
        if text == member.value or text == member.value.replace("_", " "):
            return member
    found = FREQUENCY_ALIASES.get(text)
    if found is None:
        allowed = ", ".join(member.value for member in MaintenanceFrequency)
        raise ValueError(f"frequency '{value}' is not recognized; use one of {allowed} (or 'yearly')")
    return found


def frequency_label(frequency: MaintenanceFrequency, frequency_days: Optional[int]) -> str:
    if frequency == MaintenanceFrequency.CUSTOM and frequency_days:
        return f"Every {frequency_days} Days"
    return FREQUENCY_LABELS[frequency]


def parse_checklist(text: Optional[str]) -> List[str]:
    """A stored checklist as a list of items, tolerantly.

    The import writes a JSON array of strings; older UI rows hold one item per line. A JSON
    array of objects contributes each object's ``item`` / ``task`` / ``name`` / ``text``.
    """
    if not text or not str(text).strip():
        return []
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        loaded = None
    if isinstance(loaded, list):
        items: List[str] = []
        for entry in loaded:
            if isinstance(entry, dict):
                entry = next((entry[k] for k in ("item", "task", "name", "text") if entry.get(k)), "")
            item = _collapse(entry)
            if item:
                items.append(item)
        return items
    return [line for line in (_collapse(raw.lstrip("-*• \t")) for raw in str(text).splitlines()) if line]


def _cell(row: Dict[str, str], columns: Sequence[str]) -> str:
    for column in columns:
        value = row.get(column)
        if value is not None and value.strip():
            return value.strip()
    return ""


def _parse_bool(text: str, label: str) -> Optional[bool]:
    folded = text.strip().casefold()
    if not folded:
        return None
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise ValueError(f"{label} must be true or false")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------


@dataclass
class _NewAsset:
    tag: str
    name: str
    asset_type: Optional[str]
    rows: List[int] = field(default_factory=list)
    asset: Optional[MaintenanceAsset] = None  # set on commit


@dataclass
class _RowPlan:
    row: int
    task: str = ""
    schedule_name: Optional[str] = None
    target_type: Optional[str] = None
    target_code: Optional[str] = None
    frequency: Optional[MaintenanceFrequency] = None
    frequency_days: Optional[int] = None
    work_center: Optional[WorkCenter] = None
    asset: Optional[MaintenanceAsset] = None
    new_asset_key: Optional[str] = None
    values: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: bool = False
    group: Optional["_GroupPlan"] = None

    @property
    def outcome(self) -> str:
        if self.errors:
            return "error"
        if self.skipped:
            return "skipped"
        if self.group is not None:
            return self.group.outcome
        return "error"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "row": self.row,
            "target_type": self.target_type,
            "target_code": self.target_code,
            "schedule_name": self.group.name if self.group is not None else self.schedule_name,
            "task": self.task or None,
            "frequency": self.frequency.value if self.frequency is not None else None,
            "outcome": self.outcome,
            "warnings": self.warnings,
            "error": "; ".join(self.errors) if self.errors else None,
        }


@dataclass
class _GroupPlan:
    name: str
    target_type: str
    target_code: str
    target_name: Optional[str]
    work_center: Optional[WorkCenter]
    asset: Optional[MaintenanceAsset]
    new_asset_key: Optional[str]
    frequency: MaintenanceFrequency
    frequency_days: Optional[int]
    rows: List[_RowPlan] = field(default_factory=list)
    tasks: List[str] = field(default_factory=list)
    values: Dict[str, Any] = field(default_factory=dict)
    existing: Optional[MaintenanceSchedule] = None
    created: Optional[MaintenanceSchedule] = None  # set on commit
    changes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    errored: bool = False

    @property
    def outcome(self) -> str:
        if self.errored:
            return "error"
        if self.existing is None:
            return "create"
        return "update" if self.changes else "unchanged"

    @property
    def target_id(self) -> Optional[int]:
        if self.work_center is not None:
            return self.work_center.id
        if self.asset is not None:
            return self.asset.id
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": (self.existing or self.created).id if (self.existing or self.created) else None,
            "name": self.name,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_code": self.target_code,
            "target_name": self.target_name,
            "frequency": self.frequency.value,
            "frequency_days": self.frequency_days,
            "tasks": self.tasks,
            "next_due_date": self.values.get("next_due_date"),
            "estimated_duration_hours": self.values.get("estimated_duration_hours"),
            "requires_shutdown": self.values.get("requires_shutdown"),
            "priority": self.values["priority"].value if self.values.get("priority") is not None else None,
            "outcome": self.outcome,
            "changes": self.changes,
            "rows": [plan.row for plan in self.rows],
        }


class _Targets:
    def __init__(self, work_centers: Sequence[WorkCenter], assets: Sequence[MaintenanceAsset]):
        self.work_centers: Dict[str, List[WorkCenter]] = {}
        for wc in work_centers:
            self.work_centers.setdefault((wc.code or "").strip().upper(), []).append(wc)
        self.assets: Dict[str, List[MaintenanceAsset]] = {}
        for asset in assets:
            self.assets.setdefault((asset.asset_tag or "").strip().upper(), []).append(asset)
        self.new_assets: Dict[str, _NewAsset] = {}


def _resolve_target(plan: _RowPlan, row: Dict[str, str], targets: _Targets) -> None:
    code = _cell(row, WORK_CENTER_COLUMNS)
    tag = _cell(row, ASSET_TAG_COLUMNS)
    if code and tag:
        plan.errors.append("fill work_center_code OR asset_tag, not both")
        return
    if not code and not tag:
        plan.errors.append("work_center_code or asset_tag is required")
        return
    if code:
        plan.target_type = TARGET_WORK_CENTER
        plan.target_code = code
        hits = targets.work_centers.get(code.upper(), [])
        if len(hits) > 1:
            plan.errors.append(
                f"work center code '{code}' matches {len(hits)} work centers (codes differ only in case)"
            )
        elif not hits:
            plan.errors.append(f"no work center with code '{code}'")
        else:
            plan.work_center = hits[0]
            plan.target_code = hits[0].code
            if hits[0].is_active is False:
                plan.warnings.append(f"Work center {hits[0].code} is inactive; its schedule is loaded anyway.")
            if _cell(row, ASSET_NAME_COLUMNS) or _cell(row, ASSET_TYPE_COLUMNS):
                plan.warnings.append("asset_name / asset_type are ignored on a work-center row.")
        return

    plan.target_type = TARGET_ASSET
    plan.target_code = tag
    if len(tag) > 50:
        plan.errors.append("asset_tag is longer than 50 characters")
        return
    asset_name = _collapse(_cell(row, ASSET_NAME_COLUMNS))
    asset_type = _collapse(_cell(row, ASSET_TYPE_COLUMNS)) or None
    hits = targets.assets.get(tag.upper(), [])
    if len(hits) > 1:
        plan.errors.append(f"asset tag '{tag}' matches {len(hits)} assets (tags differ only in case)")
        return
    if hits:
        asset = hits[0]
        plan.asset = asset
        plan.target_code = asset.asset_tag
        if asset_name and _key(asset_name) != _key(asset.name):
            plan.warnings.append(
                f"asset_name '{asset_name}' differs from asset {asset.asset_tag}'s name '{asset.name}'; "
                "this import never renames an asset."
            )
        if asset_type and _key(asset_type) != _key(asset.asset_type):
            plan.warnings.append(
                f"asset_type '{asset_type}' differs from asset {asset.asset_tag}'s; it is not changed."
            )
        if asset.is_active is False:
            plan.warnings.append(f"Asset {asset.asset_tag} is retired (inactive); its schedule is loaded anyway.")
        return

    key = tag.upper()
    pending = targets.new_assets.get(key)
    if pending is None:
        if not asset_name:
            plan.errors.append(
                f"no asset tagged '{tag}'; fill asset_name (and optionally asset_type) to create it, "
                "or create the asset first"
            )
            return
        if len(asset_name) > 255 or (asset_type and len(asset_type) > 50):
            plan.errors.append("asset_name (255) or asset_type (50) is too long")
            return
        pending = _NewAsset(tag=tag, name=asset_name, asset_type=asset_type)
        targets.new_assets[key] = pending
    else:
        if asset_name and _key(asset_name) != _key(pending.name):
            plan.errors.append(
                f"asset_name '{asset_name}' differs from row {pending.rows[0]}'s '{pending.name}' for the same new "
                f"asset tag '{tag}'"
            )
            return
        if asset_type and pending.asset_type and _key(asset_type) != _key(pending.asset_type):
            plan.errors.append(
                f"asset_type '{asset_type}' differs from row {pending.rows[0]}'s '{pending.asset_type}' for the same "
                f"new asset tag '{tag}'"
            )
            return
        if asset_type and not pending.asset_type:
            pending.asset_type = asset_type
    pending.rows.append(plan.row)
    plan.new_asset_key = key
    plan.target_code = pending.tag


def _parse_row_values(plan: _RowPlan, row: Dict[str, str]) -> None:
    days_text = _cell(row, FREQUENCY_DAYS_COLUMNS)
    if plan.frequency == MaintenanceFrequency.CUSTOM:
        if not days_text:
            plan.errors.append("frequency_days is required when frequency is custom")
        else:
            try:
                days = int(float(days_text))
                if days != float(days_text) or not 1 <= days <= MAX_FREQUENCY_DAYS:
                    raise ValueError
                plan.frequency_days = days
            except ValueError:
                plan.errors.append(f"frequency_days must be a whole number of days from 1 to {MAX_FREQUENCY_DAYS}")
    elif days_text and plan.frequency is not None:
        plan.warnings.append("frequency_days is used only with a custom frequency; ignored.")

    values: Dict[str, Any] = {}
    due_text = _cell(row, NEXT_DUE_COLUMNS)
    if due_text:
        try:
            values["next_due_date"] = parse_date_field(due_text, "next_due_date")
        except ValueError as exc:
            plan.errors.append(str(exc))
    duration_text = _cell(row, DURATION_COLUMNS)
    if duration_text:
        try:
            duration = float(duration_text)
            if not 0 <= duration <= MAX_DURATION_HOURS:
                raise ValueError
            values["estimated_duration_hours"] = duration
        except ValueError:
            plan.errors.append(f"estimated_duration_hours must be a number from 0 to {MAX_DURATION_HOURS:g}")
    shutdown_text = _cell(row, SHUTDOWN_COLUMNS)
    if shutdown_text:
        try:
            values["requires_shutdown"] = _parse_bool(shutdown_text, "requires_shutdown")
        except ValueError as exc:
            plan.errors.append(str(exc))
    priority_text = _cell(row, PRIORITY_COLUMNS)
    if priority_text:
        try:
            values["priority"] = MaintenancePriority(priority_text.strip().casefold())
        except ValueError:
            allowed = ", ".join(member.value for member in MaintenancePriority)
            plan.errors.append(f"priority '{priority_text}' is not one of {allowed}")
    plan.values = values


def _plan_row(row_number: int, row: Dict[str, str], targets: _Targets) -> _RowPlan:
    plan = _RowPlan(row=row_number)
    plan.task = _collapse(_cell(row, TASK_COLUMNS))
    schedule_name = _collapse(_cell(row, SCHEDULE_NAME_COLUMNS))
    plan.schedule_name = schedule_name or None
    try:
        plan.frequency = parse_frequency(_cell(row, FREQUENCY_COLUMNS))
    except AsNeededFrequency:
        plan.skipped = True
        plan.target_code = _cell(row, WORK_CENTER_COLUMNS) or _cell(row, ASSET_TAG_COLUMNS) or None
        plan.warnings.append(
            f"Frequency '{_cell(row, FREQUENCY_COLUMNS)}' has no cadence; this task is not scheduled. "
            "Give it a frequency (e.g. monthly) to load it."
        )
        return plan
    except ValueError as exc:
        plan.errors.append(str(exc))
    if not plan.task:
        plan.errors.append("task is required")
    elif len(plan.task) > MAX_TASK_LENGTH:
        plan.errors.append(f"task is longer than {MAX_TASK_LENGTH} characters")
    if schedule_name and len(schedule_name) > MAX_SCHEDULE_NAME_LENGTH:
        plan.errors.append(f"schedule_name is longer than {MAX_SCHEDULE_NAME_LENGTH} characters")
    _resolve_target(plan, row, targets)
    _parse_row_values(plan, row)
    return plan


def _target_key(plan: _RowPlan) -> Tuple[str, str]:
    if plan.work_center is not None:
        return (TARGET_WORK_CENTER, str(plan.work_center.id))
    if plan.asset is not None:
        return (TARGET_ASSET, str(plan.asset.id))
    return (TARGET_ASSET, f"new:{plan.new_asset_key}")


def _schedule_name(plan: _RowPlan) -> str:
    if plan.schedule_name:
        return plan.schedule_name
    assert plan.frequency is not None
    return f"{plan.target_code} {frequency_label(plan.frequency, plan.frequency_days)} PM"


def _group_rows(plans: Sequence[_RowPlan], targets: _Targets) -> List[_GroupPlan]:
    groups: Dict[Tuple[Any, ...], _GroupPlan] = {}
    for plan in plans:
        if plan.skipped or plan.errors or plan.frequency is None:
            continue
        name = _schedule_name(plan)
        key = (*_target_key(plan), plan.frequency.value, plan.frequency_days, _key(name))
        group = groups.get(key)
        if group is None:
            pending = targets.new_assets.get(plan.new_asset_key) if plan.new_asset_key else None
            target_name: Optional[str]
            if plan.work_center is not None:
                target_name = plan.work_center.name
            elif plan.asset is not None:
                target_name = plan.asset.name
            else:
                target_name = pending.name if pending is not None else None
            group = _GroupPlan(
                name=name,
                target_type=plan.target_type or TARGET_ASSET,
                target_code=plan.target_code or "",
                target_name=target_name,
                work_center=plan.work_center,
                asset=plan.asset,
                new_asset_key=plan.new_asset_key,
                frequency=plan.frequency,
                frequency_days=plan.frequency_days,
            )
            groups[key] = group
        group.rows.append(plan)
        plan.group = group
    return list(groups.values())


def _fill_group(group: _GroupPlan) -> None:
    """Ordered de-duplicated tasks and the schedule-level values (conflicts are row errors)."""
    seen: Dict[str, int] = {}
    for plan in group.rows:
        task_key = _key(plan.task)
        if task_key in seen:
            plan.warnings.append(f"Duplicate of row {seen[task_key]}'s task in this schedule; listed once.")
            continue
        seen[task_key] = plan.row
        group.tasks.append(plan.task)
    for name in GROUP_FIELDS:
        first_row: Optional[int] = None
        for plan in group.rows:
            if name not in plan.values:
                continue
            value = plan.values[name]
            if first_row is None:
                first_row = plan.row
                group.values[name] = value
            elif value != group.values[name]:
                plan.errors.append(
                    f"{name} '{_jsonable(value)}' conflicts with row {first_row}'s '{_jsonable(group.values[name])}' "
                    f"for the same schedule '{group.name}'"
                )


def _existing_schedules(schedules: Sequence[MaintenanceSchedule]) -> Dict[Tuple[str, str, str], List[Any]]:
    index: Dict[Tuple[str, str, str], List[Any]] = {}
    for schedule in schedules:
        if schedule.work_center_id is not None:
            target = (TARGET_WORK_CENTER, str(schedule.work_center_id))
        elif schedule.asset_id is not None:
            target = (TARGET_ASSET, str(schedule.asset_id))
        else:
            continue
        index.setdefault((*target, _key(schedule.name)), []).append(schedule)
    return index


def _schedule_snapshot(schedule: Any) -> Dict[str, Any]:
    return {
        "frequency": _jsonable(schedule.frequency),
        "frequency_days": schedule.frequency_days,
        "checklist": parse_checklist(schedule.checklist),
        "estimated_duration_hours": schedule.estimated_duration_hours,
        "requires_shutdown": bool(schedule.requires_shutdown),
        "priority": _jsonable(schedule.priority),
        "next_due_date": _jsonable(schedule.next_due_date),
    }


def _target_values(group: _GroupPlan) -> Dict[str, Any]:
    """What the sheet says the schedule's fields should be (only the fields it sets)."""
    target: Dict[str, Any] = {"frequency": group.frequency.value, "checklist": list(group.tasks)}
    if group.frequency == MaintenanceFrequency.CUSTOM:
        target["frequency_days"] = group.frequency_days
    for name in GROUP_FIELDS:
        if name in group.values:
            target[name] = _jsonable(group.values[name])
    return target


_ScheduleIndex = Dict[Tuple[str, str, str], List[MaintenanceSchedule]]
_OpenWorkOrders = Dict[int, List[MaintenanceWorkOrder]]


def _flag_open_work_orders(
    group: _GroupPlan, open_work_orders: Sequence[MaintenanceWorkOrder], api_token: bool
) -> None:
    """What a checklist change does to the schedule's OPEN work orders -- the same probe for the
    preview and the commit, so the two cannot disagree.

    A work order opened since migration 132 keeps the checklist snapshot it was opened with: the
    import changes only the next one (a warning, so the planner knows). One opened BEFORE it has no
    snapshot and is judged on the schedule's current checklist, so the import changes the steps it
    must tick or explain: a warning for a person, and -- when the import takes steps away -- a row
    error for a standing API token, exactly as ``PUT /maintenance/schedules/{id}`` refuses it (403
    ``API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL``). Row errors make the commit refuse (409) before its first
    write."""
    change = group.changes.get("checklist")
    if change is None:
        return
    takes_steps_away = bool(removed_items(change["old"] or [], change["new"] or []))
    for work_order in open_work_orders:
        if work_order.schedule_checklist_snapshot is not None:
            group.rows[0].warnings.append(
                f"Schedule '{group.name}' has open maintenance work order {work_order.wo_number}; it keeps the "
                "checklist it was opened with, and the new checklist applies from the next work order."
            )
        elif api_token and takes_steps_away:
            for plan in group.rows:
                plan.errors.append(
                    f"{API_TOKEN_SCHEDULE_CHECKLIST_REMOVAL} (Open maintenance work order {work_order.wo_number} "
                    f"on schedule '{group.name}' is judged on the schedule's current checklist.)"
                )
        else:
            group.rows[0].warnings.append(
                f"Open maintenance work order {work_order.wo_number} was opened before checklist snapshots and is "
                f"judged on schedule '{group.name}''s current checklist: this import changes the steps it must "
                "tick or give a reason for."
            )


def _diff_group(
    group: _GroupPlan,
    active: _ScheduleIndex,
    inactive: _ScheduleIndex,
    open_work_orders: Optional[_OpenWorkOrders] = None,
    api_token: bool = False,
) -> None:
    if group.work_center is not None:
        target = (TARGET_WORK_CENTER, str(group.work_center.id))
    elif group.asset is not None:
        target = (TARGET_ASSET, str(group.asset.id))
    else:
        return  # a new asset has no schedules yet
    key = (*target, _key(group.name))
    hits = active.get(key, [])
    if len(hits) > 1:
        ids = ", ".join(str(schedule.id) for schedule in hits)
        for plan in group.rows:
            plan.errors.append(
                f"{len(hits)} active schedules on {group.target_code} are named '{group.name}' (ids {ids}); "
                "deactivate the extra ones or give this one a distinct schedule_name"
            )
        return
    if not hits:
        if inactive.get(key):
            group.rows[0].warnings.append(
                f"An inactive schedule named '{group.name}' exists on {group.target_code}; a new one is created."
            )
        return
    group.existing = hits[0]
    before = _schedule_snapshot(hits[0])
    for name, value in _target_values(group).items():
        if before.get(name) != value:
            group.changes[name] = {"old": before.get(name), "new": value}
    _flag_open_work_orders(group, (open_work_orders or {}).get(hits[0].id, []), api_token)


def _name_conflicts(groups: Sequence[_GroupPlan]) -> None:
    """The upsert key is (target, name): one name on one target cannot carry two frequencies."""
    by_name: Dict[Tuple[Any, ...], List[_GroupPlan]] = {}
    for group in groups:
        target = group.target_id if group.target_id is not None else f"new:{group.new_asset_key}"
        by_name.setdefault((group.target_type, target, _key(group.name)), []).append(group)
    for clashing in by_name.values():
        if len(clashing) < 2:
            continue
        cadences = ", ".join(frequency_label(g.frequency, g.frequency_days) for g in clashing)
        for group in clashing:
            for plan in group.rows:
                plan.errors.append(
                    f"schedule '{group.name}' on {group.target_code} is given more than one frequency ({cadences}); "
                    "give each cadence its own schedule_name"
                )


def build_plan(
    table: ParsedTable,
    *,
    work_centers: Sequence[WorkCenter],
    assets: Sequence[MaintenanceAsset],
    schedules: Sequence[MaintenanceSchedule],
    open_work_orders: Sequence[MaintenanceWorkOrder] = (),
    api_token: bool = False,
) -> Tuple[List[_RowPlan], List[_GroupPlan], List[_NewAsset]]:
    """Validate, resolve, group and diff every sheet row. Pure: writes nothing. ``open_work_orders``
    are the company's open maintenance work orders on a PM schedule; ``api_token`` is whether a
    standing API token is importing (see ``_flag_open_work_orders``)."""
    targets = _Targets(work_centers, assets)
    plans = [_plan_row(row_number, row, targets) for row_number, row in table.iter_rows()]
    groups = _group_rows(plans, targets)
    for group in groups:
        _fill_group(group)
    _name_conflicts(groups)
    active = _existing_schedules([s for s in schedules if s.is_active])
    inactive = _existing_schedules([s for s in schedules if not s.is_active])
    open_by_schedule: _OpenWorkOrders = {}
    for work_order in open_work_orders:
        if work_order.schedule_id is not None:
            open_by_schedule.setdefault(work_order.schedule_id, []).append(work_order)
    for group in groups:
        if not any(plan.errors for plan in group.rows):
            _diff_group(group, active, inactive, open_by_schedule, api_token)
    for group in groups:
        failing = [plan.row for plan in group.rows if plan.errors]
        if failing:
            group.errored = True
            group.changes = {}
            for plan in group.rows:
                if not plan.errors:
                    plan.errors.append(
                        f"not loaded: row(s) {', '.join(str(r) for r in failing)} of the same schedule have errors"
                    )
    needed = {group.new_asset_key for group in groups if group.new_asset_key and not group.errored}
    new_assets = [pending for key, pending in targets.new_assets.items() if key in needed]
    return plans, groups, new_assets


def _summary(plans: Sequence[_RowPlan], groups: Sequence[_GroupPlan], new_assets: Sequence[_NewAsset]) -> Dict:
    outcomes = [group.outcome for group in groups]
    return {
        "total_rows": len(plans),
        "task_rows": sum(1 for plan in plans if not plan.skipped),
        "skipped_rows": sum(1 for plan in plans if plan.skipped),
        "error_count": sum(1 for plan in plans if plan.errors),
        "warning_count": sum(len(plan.warnings) for plan in plans),
        "schedules_to_create": outcomes.count("create"),
        "schedules_to_update": outcomes.count("update"),
        "schedules_unchanged": outcomes.count("unchanged"),
        "assets_to_create": len(new_assets),
    }


def _response(
    filename: Optional[str],
    content_hash: str,
    plans: Sequence[_RowPlan],
    groups: Sequence[_GroupPlan],
    new_assets: Sequence[_NewAsset],
    *,
    committed: bool,
) -> Dict[str, Any]:
    return {
        "filename": filename,
        "source_hash": content_hash,
        "committed": committed,
        "summary": _summary(plans, groups, new_assets),
        "rows": [plan.as_dict() for plan in plans],
        "schedules": [group.as_dict() for group in groups],
        "assets_to_create": [
            {
                "asset_id": pending.asset.id if pending.asset is not None else None,
                "asset_tag": pending.tag,
                "name": pending.name,
                "asset_type": pending.asset_type,
                "rows": pending.rows,
            }
            for pending in new_assets
        ],
    }


def parse_pm_file(filename: Optional[str], content: bytes) -> ParsedTable:
    """Parse the template (or a CSV / workbook with a title block above the header).
    Raises ``ImportFileError`` (-> 400)."""
    return parse_import_file(filename, content, required_columns=REQUIRED_COLUMNS, header_detect=HEADER_DETECT)


def _load(db: Session, company_id: int, *, lock: bool) -> Dict[str, Any]:
    schedules = db.query(MaintenanceSchedule).filter(MaintenanceSchedule.company_id == company_id)
    assets = db.query(MaintenanceAsset).filter(MaintenanceAsset.company_id == company_id)
    if lock:
        schedules = schedules.with_for_update().populate_existing()
        assets = assets.with_for_update().populate_existing()
    loaded = {
        "work_centers": db.query(WorkCenter).filter(WorkCenter.company_id == company_id).order_by(WorkCenter.id).all(),
        "assets": assets.order_by(MaintenanceAsset.id).all(),
        "schedules": schedules.order_by(MaintenanceSchedule.id).all(),
    }
    # Read AFTER the schedules (row-locked on commit): opening a PM work order takes its schedule's row
    # lock first, so no work order this probe misses can be opened against a schedule being imported.
    loaded["open_work_orders"] = (
        db.query(MaintenanceWorkOrder)
        .filter(
            MaintenanceWorkOrder.company_id == company_id,
            MaintenanceWorkOrder.schedule_id.isnot(None),
            MaintenanceWorkOrder.status.in_(OPEN_MAINTENANCE_STATUSES),
        )
        .order_by(MaintenanceWorkOrder.id)
        .all()
    )
    return loaded


def preview_pm_import(
    db: Session, *, company_id: int, filename: Optional[str], content: bytes, api_token: bool = False
) -> Dict[str, Any]:
    """Dry run: parse, resolve, group and diff. Writes nothing (takes no AuditService). ``api_token``
    previews the row errors a standing API token's commit would hit."""
    table = parse_pm_file(filename, content)
    plans, groups, new_assets = build_plan(table, **_load(db, company_id, lock=False), api_token=api_token)
    return _response(filename, source_hash(content), plans, groups, new_assets, committed=False)


def _create_asset(
    db: Session, pending: _NewAsset, *, company_id: int, audit: AuditService, extra: Dict[str, Any]
) -> MaintenanceAsset:
    asset = MaintenanceAsset(
        company_id=company_id,
        asset_tag=pending.tag,
        name=pending.name,
        asset_type=pending.asset_type,
        is_active=True,
        version=1,
    )
    db.add(asset)
    db.flush()
    audit.log_required(
        AuditService.ACTIONS["CREATE"],
        "maintenance_asset",
        resource_id=asset.id,
        resource_identifier=asset.asset_tag,
        description=f"PM import created equipment asset {asset.asset_tag} ({asset.name})",
        new_values={"asset_tag": asset.asset_tag, "name": asset.name, "asset_type": asset.asset_type},
        extra_data={**extra, "rows": pending.rows},
        company_id=company_id,
    )
    return asset


def _apply_group(
    db: Session,
    group: _GroupPlan,
    *,
    company_id: int,
    audit: AuditService,
    extra: Dict[str, Any],
    new_assets: Dict[str, _NewAsset],
) -> Optional[MaintenanceSchedule]:
    rows = [plan.row for plan in group.rows]
    checklist = json.dumps(group.tasks)
    if group.existing is None:
        asset = group.asset
        if asset is None and group.new_asset_key:
            asset = new_assets[group.new_asset_key].asset
            group.asset = asset  # the response then names the created asset's id
        schedule = MaintenanceSchedule(
            company_id=company_id,
            work_center_id=group.work_center.id if group.work_center is not None else None,
            asset_id=asset.id if asset is not None and group.work_center is None else None,
            name=group.name,
            maintenance_type=MaintenanceType.PREVENTIVE,
            frequency=group.frequency,
            frequency_days=group.frequency_days if group.frequency == MaintenanceFrequency.CUSTOM else None,
            estimated_duration_hours=group.values.get("estimated_duration_hours", 1.0),
            priority=group.values.get("priority", MaintenancePriority.MEDIUM),
            checklist=checklist,
            requires_shutdown=bool(group.values.get("requires_shutdown", False)),
            next_due_date=group.values.get("next_due_date") or central_today(),
            is_active=True,
        )
        db.add(schedule)
        db.flush()
        audit.log_required(
            AuditService.ACTIONS["CREATE"],
            "maintenance_schedule",
            resource_id=schedule.id,
            resource_identifier=schedule.name[:255],
            description=f"PM import created schedule '{schedule.name}' on {group.target_code}",
            new_values={
                "target_type": group.target_type,
                "target_code": group.target_code,
                **_schedule_snapshot(schedule),
            },
            extra_data={**extra, "rows": rows},
            company_id=company_id,
        )
        return schedule

    if not group.changes:
        return None
    schedule = group.existing
    target = _target_values(group)
    old: Dict[str, Any] = {}
    new: Dict[str, Any] = {}
    for name in group.changes:
        old[name] = group.changes[name]["old"]
        new[name] = target[name]
        if name == "frequency":
            schedule.frequency = MaintenanceFrequency(target[name])
        elif name == "checklist":
            schedule.checklist = checklist
        elif name == "priority":
            schedule.priority = MaintenancePriority(target[name])
        elif name == "next_due_date":
            schedule.next_due_date = group.values["next_due_date"]
        else:
            setattr(schedule, name, target[name])
    db.flush()
    audit.log_required(
        AuditService.ACTIONS["UPDATE"],
        "maintenance_schedule",
        resource_id=schedule.id,
        resource_identifier=schedule.name[:255],
        description=f"PM import updated schedule '{schedule.name}' on {group.target_code}",
        old_values=old,
        new_values=new,
        extra_data={**extra, "rows": rows},
        company_id=company_id,
    )
    return schedule


def commit_pm_import(
    db: Session,
    *,
    company_id: int,
    filename: Optional[str],
    content: bytes,
    expected_source_hash: str,
    audit: AuditService,
    api_token: bool = False,
) -> Dict[str, Any]:
    """Apply the sheet. All-or-nothing; the CALLER commits (inside ``required_audit``).

    The file must be the one the preview read (``expected_source_hash``, 409 otherwise).
    Concurrent imports for one company are serialized (a transaction-scoped advisory lock on
    PostgreSQL) and every probe re-runs against the database as it is now.
    """
    content_hash = source_hash(content)
    if (expected_source_hash or "").strip().lower() != content_hash:
        raise HTTPException(
            status_code=409,
            detail="This file is not the one that was previewed. Preview it again, then commit.",
        )
    table = parse_pm_file(filename, content)
    acquire_generator_lock(db, "maintenance_schedule_import", company_id)
    plans, groups, new_assets = build_plan(table, **_load(db, company_id, lock=True), api_token=api_token)
    failing = [plan for plan in plans if plan.errors]
    if failing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{len(failing)} row(s) have errors; nothing was imported. Fix them and preview again.",
                "errors": [
                    {"row": plan.row, "target": plan.target_code, "task": plan.task, "error": "; ".join(plan.errors)}
                    for plan in failing
                ],
            },
        )

    extra = {"source": IMPORT_SOURCE, "filename": filename, "source_hash": content_hash}
    pending_by_key = {pending.tag.upper(): pending for pending in new_assets}
    for pending in new_assets:
        pending.asset = _create_asset(db, pending, company_id=company_id, audit=audit, extra=extra)

    created: List[int] = []
    updated: List[int] = []
    for group in groups:
        schedule = _apply_group(db, group, company_id=company_id, audit=audit, extra=extra, new_assets=pending_by_key)
        if schedule is None:
            continue
        if group.existing is None:
            group.created = schedule
            created.append(schedule.id)
        else:
            updated.append(schedule.id)

    response = _response(filename, content_hash, plans, groups, new_assets, committed=True)

    if created or updated or new_assets:
        audit.log_required(
            AuditService.ACTIONS["IMPORT"],
            "maintenance_schedule_import",
            resource_identifier=(filename or TEMPLATE_ENTITY)[:255],
            description=(
                f"PM schedule import: {len(created)} schedule(s) created, {len(updated)} updated, "
                f"{len(new_assets)} asset(s) created"
            ),
            new_values=response["summary"],
            extra_data={
                **extra,
                "created_schedule_ids": created,
                "updated_schedule_ids": updated,
                "created_asset_ids": [pending.asset.id for pending in new_assets if pending.asset is not None],
            },
            company_id=company_id,
        )
    return response
