"""Load, read, edit and apply the company's production calendar (see ``production_calendar``).

``load_production_calendar`` is the ONE loader every day-boundary call site uses: three
tenant-scoped queries building a pure :class:`ProductionCalendar`, loaded once per request
or cron pass and passed down. A company with no ``production_calendar_settings`` row is
"unconfigured": its production day is its company-local calendar day and it has no shifts.

Edits are a whole-document replace (:func:`save_production_calendar`): compare-and-swap on
the settings ``version`` (409), whole-set validation (422), and a REQUIRED audit row in the
same transaction -- the router wraps it in ``app.api.audit_guard.required_audit`` (503,
nothing saved). A shift an OEE record references is retired (``is_active: false``), never
removed, and its code never changes, so every historical OEE label still names it.

:func:`sync_working_calendars` derives ``WorkingCalendar.weekly_hours`` from the shifts
(the sum of planned minutes of the active shifts starting each weekday) and writes them
through ``working_calendar_service.save_working_calendar`` -- the scheduling editor's own
writer -- keeping every dated override.

The attribution helpers at the bottom read time entries / downtime events for a range of
production days and attribute each row in Python (``entry_attribution``), reaching
``ENTRY_LOOKBACK`` before the window so an early clock-in that mostly overlaps the day's
first shift is counted on that day. SQL alone cannot express "greatest shift overlap".
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Query, Session

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.oee import OEERecord
from app.models.production_calendar import (
    ProductionCalendarSettings,
    ShiftDefinition,
    ShiftPattern,
    ShiftPatternWorkCenter,
)
from app.models.work_center import WorkCenter
from app.schemas.production_calendar import (
    ProductionCalendarResponse,
    ProductionCalendarUpdate,
    ProductionDayOut,
    ProductionMomentResponse,
    ShiftDefinitionOut,
    ShiftOccurrenceOut,
    ShiftPatternOut,
    WorkingCalendarSyncResponse,
    WorkingCalendarSyncRow,
)
from app.services.company_time import company_zone
from app.services.production_calendar import (
    MINUTES_PER_DAY,
    ProductionCalendar,
    ShiftDef,
    ShiftOccurrence,
    build_calendar,
    current_production_day,
    days_to_mask,
    entry_attribution,
    hhmm_to_minute,
    lookback_from,
    mask_to_days,
    minute_to_hhmm,
    occurrence_on,
    production_day,
    production_day_window_utc,
    production_days_window_or_empty,
    shift_minutes_from_wall_times,
    shifts_at,
    unconfigured_calendar,
    weekly_hours_for,
)
from app.services.working_calendar_service import (
    WorkingCalendarSave,
    audit_working_calendar_save,
    load_working_calendars,
    save_working_calendar,
)

AUDIT_RESOURCE_TYPE = "production_calendar"
AUDIT_IDENTIFIER = "Production calendar"
STALE_VERSION_DETAIL = "This production calendar changed. Reload it before saving."
SHIFT_NOT_FOUND_DETAIL = "Shift not found"
AT_OUT_OF_RANGE_DETAIL = "The instant is outside the supported range"
# ``_apply`` frees a changing unique key by renaming the row to ``~<id>`` mid-save, so a
# pattern name or shift code may not start with it (a user-named ``~5`` would collide).
RESERVED_PREFIX = "~"
UNKNOWN_SHIFT_DETAIL = (
    "Unknown shift '{label}'. Use a shift_id (or the code) of a shift defined in the production calendar "
    "(GET /production-calendar)."
)
SYNC_SOURCE = "production_calendar_sync"


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def _settings_row(db: Session, company_id: int, *, locked: bool = False) -> Optional[ProductionCalendarSettings]:
    query = tenant_query(db, ProductionCalendarSettings, company_id)
    if locked:
        query = query.with_for_update().populate_existing()
    return query.first()


def _shift_def(row: ShiftDefinition) -> ShiftDef:
    return ShiftDef(
        id=row.id,
        code=row.code,
        name=row.name,
        pattern_id=row.shift_pattern_id,
        start_minute=row.start_minute,
        end_minute=row.end_minute,
        days_mask=row.days_mask,
        planned_minutes=row.planned_minutes,
        sort_order=row.sort_order or 0,
    )


def load_production_calendar(db: Session, company_id: int) -> ProductionCalendar:
    """The company's calendar (active shifts only); unconfigured when it has no settings row."""
    tz = company_zone(db, company_id)
    settings = _settings_row(db, company_id)
    if settings is None:
        return unconfigured_calendar(tz, company_id)
    patterns = tenant_query(db, ShiftPattern, company_id).all()
    members: Dict[int, List[int]] = {}
    for member in tenant_query(db, ShiftPatternWorkCenter, company_id).all():
        members.setdefault(member.shift_pattern_id, []).append(member.work_center_id)
    shifts: Dict[int, List[ShiftDef]] = {}
    for row in tenant_query(db, ShiftDefinition, company_id).filter(ShiftDefinition.is_active == True).all():  # noqa
        shifts.setdefault(row.shift_pattern_id, []).append(_shift_def(row))
    return build_calendar(
        company_id=company_id,
        tz=tz,
        day_start_minute=settings.production_day_start_minute,
        version=settings.version,
        patterns=[(p.id, bool(p.is_default), members.get(p.id, []), shifts.get(p.id, [])) for p in patterns],
    )


def pattern_id_for_work_center(db: Session, company_id: int, work_center_id: Optional[int]) -> Optional[int]:
    """The pattern whose shifts apply at a work center: its explicit pattern, else the default."""
    if work_center_id is not None:
        member = (
            tenant_query(db, ShiftPatternWorkCenter, company_id)
            .filter(ShiftPatternWorkCenter.work_center_id == work_center_id)
            .first()
        )
        if member is not None:
            return member.shift_pattern_id
    default = tenant_query(db, ShiftPattern, company_id).filter(ShiftPattern.is_default == True).first()  # noqa
    return default.id if default is not None else None


# --------------------------------------------------------------------------------------
# Reading (GET / resolve)
# --------------------------------------------------------------------------------------


def _document(db: Session, company_id: int) -> Dict[str, Any]:
    """The stored calendar as a canonical, JSON-able dict (the audit snapshot and the GET body)."""
    settings = _settings_row(db, company_id)
    patterns = tenant_query(db, ShiftPattern, company_id).order_by(ShiftPattern.sort_order, ShiftPattern.id).all()
    members: Dict[int, List[int]] = {}
    for member in tenant_query(db, ShiftPatternWorkCenter, company_id).all():
        members.setdefault(member.shift_pattern_id, []).append(member.work_center_id)
    shifts: Dict[int, List[ShiftDefinition]] = {}
    for row in (
        tenant_query(db, ShiftDefinition, company_id)
        .order_by(ShiftDefinition.sort_order, ShiftDefinition.start_minute, ShiftDefinition.id)
        .all()
    ):
        shifts.setdefault(row.shift_pattern_id, []).append(row)
    return {
        "version": settings.version if settings is not None else 0,
        "production_day_start_minute": settings.production_day_start_minute if settings is not None else 0,
        "patterns": [
            {
                "id": pattern.id,
                "name": pattern.name,
                "is_default": bool(pattern.is_default),
                "sort_order": pattern.sort_order or 0,
                "work_center_ids": sorted(members.get(pattern.id, [])),
                "shifts": [
                    {
                        "id": row.id,
                        "code": row.code,
                        "name": row.name,
                        "start_minute": row.start_minute,
                        "end_minute": row.end_minute,
                        "days": mask_to_days(row.days_mask),
                        "planned_minutes": row.planned_minutes,
                        "is_active": bool(row.is_active),
                        "sort_order": row.sort_order or 0,
                    }
                    for row in shifts.get(pattern.id, [])
                ],
            }
            for pattern in patterns
        ],
    }


def _occurrence_out(occ: ShiftOccurrence) -> ShiftOccurrenceOut:
    return ShiftOccurrenceOut(
        shift_id=occ.shift.id,
        code=occ.shift.code,
        name=occ.shift.name,
        pattern_id=occ.shift.pattern_id,
        production_day=occ.production_day,
        start_utc=occ.start_utc,
        end_utc=occ.end_utc,
        planned_minutes=occ.shift.planned_minutes,
    )


def production_day_out(day: date, cal: ProductionCalendar, shifts: Iterable[ShiftDef]) -> ProductionDayOut:
    """One production day's window and every one of ``shifts`` that starts on it."""
    start_utc, end_utc = production_day_window_utc(day, cal)
    seen: Dict[int, ShiftOccurrence] = {}
    for shift in shifts:
        if shift.id not in seen and shift.runs_on(day.weekday()):
            seen[shift.id] = occurrence_on(shift, day, cal)
    occurrences = sorted(seen.values(), key=lambda occ: (occ.start_utc, occ.shift.sort_order, occ.shift.id))
    return ProductionDayOut(
        production_day=day,
        start_utc=start_utc,
        end_utc=end_utc,
        occurrences=[_occurrence_out(occ) for occ in occurrences],
    )


def calendar_response(db: Session, company_id: int, *, now: Optional[datetime] = None) -> ProductionCalendarResponse:
    settings = _settings_row(db, company_id)
    doc = _document(db, company_id)
    cal = load_production_calendar(db, company_id)
    tz = company_zone(db, company_id)
    patterns = [
        ShiftPatternOut(
            id=pattern["id"],
            name=pattern["name"],
            is_default=pattern["is_default"],
            sort_order=pattern["sort_order"],
            work_center_ids=pattern["work_center_ids"],
            shifts=[
                ShiftDefinitionOut(
                    id=shift["id"],
                    pattern_id=pattern["id"],
                    code=shift["code"],
                    name=shift["name"],
                    start=minute_to_hhmm(shift["start_minute"]),
                    end=minute_to_hhmm(shift["end_minute"]),
                    start_minute=shift["start_minute"],
                    end_minute=shift["end_minute"],
                    ends_next_day=shift["end_minute"] > MINUTES_PER_DAY,
                    days=shift["days"],
                    planned_minutes=shift["planned_minutes"],
                    is_active=shift["is_active"],
                    sort_order=shift["sort_order"],
                )
                for shift in pattern["shifts"]
            ],
        )
        for pattern in doc["patterns"]
    ]
    return ProductionCalendarResponse(
        configured=settings is not None,
        version=doc["version"],
        timezone=tz.key,
        production_day_start=minute_to_hhmm(doc["production_day_start_minute"]),
        production_day_start_minute=doc["production_day_start_minute"],
        updated_at=settings.updated_at if settings is not None else None,
        updated_by=settings.updated_by if settings is not None else None,
        patterns=patterns,
        # Every ACTIVE shift of every pattern -- including a pattern no work center is in yet.
        today=production_day_out(
            current_production_day(cal, now),
            cal,
            [
                ShiftDef(
                    id=shift["id"],
                    code=shift["code"],
                    name=shift["name"],
                    pattern_id=pattern["id"],
                    start_minute=shift["start_minute"],
                    end_minute=shift["end_minute"],
                    days_mask=days_to_mask(shift["days"]),
                    planned_minutes=shift["planned_minutes"],
                    sort_order=shift["sort_order"],
                )
                for pattern in doc["patterns"]
                for shift in pattern["shifts"]
                if shift["is_active"]
            ],
        ),
    )


def resolve_moment(
    db: Session, company_id: int, at: Optional[datetime], work_center_id: Optional[int]
) -> ProductionMomentResponse:
    """Which production day and shift an instant belongs to at a work center (404 for a foreign WC)."""
    if work_center_id is not None:
        exists = tenant_query(db, WorkCenter, company_id).filter(WorkCenter.id == work_center_id).first()
        if exists is None:
            raise HTTPException(404, "Work center not found")
    cal = load_production_calendar(db, company_id)
    if at is None:
        instant = datetime.now(timezone.utc).replace(tzinfo=None)
    elif at.tzinfo is None:
        instant = at  # a naive instant is UTC, like every stored timestamp
    else:
        instant = at.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        day = production_day(instant, cal, work_center_id)
        start_utc, end_utc = production_day_window_utc(day, cal)
        running = shifts_at(instant, cal, work_center_id)
    except (ValueError, OverflowError) as exc:
        # An instant within a day of the ends of the datetime range has no window.
        raise _unprocessable(AT_OUT_OF_RANGE_DETAIL) from exc
    return ProductionMomentResponse(
        at=instant,
        work_center_id=work_center_id,
        pattern_id=pattern_id_for_work_center(db, company_id, work_center_id) if cal.configured else None,
        production_day=day,
        production_day_start_utc=start_utc,
        production_day_end_utc=end_utc,
        shift=_occurrence_out(running[0]) if running else None,
        overlapping=[_occurrence_out(occ) for occ in running],
        configured=cal.configured,
        calendar_version=cal.version,
    )


# --------------------------------------------------------------------------------------
# Editing (PUT)
# --------------------------------------------------------------------------------------


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


@dataclass
class _DesiredShift:
    id: Optional[int]
    code: str
    name: str
    start_minute: int
    end_minute: int
    days_mask: int
    planned_minutes: int
    is_active: bool
    sort_order: int


@dataclass
class _DesiredPattern:
    id: Optional[int]
    name: str
    is_default: bool
    sort_order: int
    work_center_ids: List[int]
    shifts: List[_DesiredShift]


def _validate(db: Session, company_id: int, payload: ProductionCalendarUpdate) -> Tuple[int, List[_DesiredPattern]]:
    """Whole-document validation; every refusal is a 422 raised before the first write."""
    day_start = hhmm_to_minute(payload.production_day_start)
    patterns: List[_DesiredPattern] = []
    names: Dict[str, str] = {}
    codes: Dict[str, str] = {}
    wc_owner: Dict[int, str] = {}
    pattern_ids: List[int] = []
    shift_ids: List[int] = []
    defaults = [p.name for p in payload.patterns if p.is_default]
    if len(defaults) > 1:
        raise _unprocessable(f"Only one shift pattern can be the default; got {', '.join(defaults)}.")
    for pattern in payload.patterns:
        if pattern.name.startswith(RESERVED_PREFIX):
            raise _unprocessable(f"Shift pattern name '{pattern.name}' cannot start with '{RESERVED_PREFIX}'.")
        key = pattern.name.casefold()
        if key in names:
            raise _unprocessable(f"Shift pattern name '{pattern.name}' is used twice.")
        names[key] = pattern.name
        if pattern.id is not None:
            pattern_ids.append(pattern.id)
        for wc_id in pattern.work_center_ids:
            if wc_id in wc_owner:
                raise _unprocessable(
                    f"Work center {wc_id} is in both '{wc_owner[wc_id]}' and '{pattern.name}'; "
                    "a work center belongs to at most one shift pattern."
                )
            wc_owner[wc_id] = pattern.name
        shifts: List[_DesiredShift] = []
        for shift in pattern.shifts:
            if shift.code.startswith(RESERVED_PREFIX):
                raise _unprocessable(f"Shift code '{shift.code}' cannot start with '{RESERVED_PREFIX}'.")
            code_key = shift.code.casefold()
            if code_key in codes:
                raise _unprocessable(f"Shift code '{shift.code}' is used twice; codes are unique per company.")
            codes[code_key] = shift.code
            if shift.id is not None:
                shift_ids.append(shift.id)
            start_minute, end_minute = shift_minutes_from_wall_times(shift.start, shift.end)
            length = end_minute - start_minute
            if shift.planned_minutes > length:
                raise _unprocessable(
                    f"Shift '{shift.code}' plans {shift.planned_minutes} minutes but is only {length} minutes long."
                )
            if start_minute < day_start or end_minute > day_start + MINUTES_PER_DAY:
                raise _unprocessable(
                    f"Shift '{shift.code}' ({shift.start}-{shift.end}) must start at or after the production day "
                    f"start ({payload.production_day_start}) and end by the next day's start, so it belongs to "
                    "the production day it starts on."
                )
            shifts.append(
                _DesiredShift(
                    id=shift.id,
                    code=shift.code,
                    name=shift.name,
                    start_minute=start_minute,
                    end_minute=end_minute,
                    days_mask=days_to_mask(shift.days),
                    planned_minutes=shift.planned_minutes,
                    is_active=shift.is_active,
                    sort_order=shift.sort_order,
                )
            )
        patterns.append(
            _DesiredPattern(
                id=pattern.id,
                name=pattern.name,
                is_default=pattern.is_default,
                sort_order=pattern.sort_order,
                work_center_ids=sorted(set(pattern.work_center_ids)),
                shifts=shifts,
            )
        )
    if len(set(pattern_ids)) != len(pattern_ids):
        raise _unprocessable("A shift pattern id appears twice.")
    if len(set(shift_ids)) != len(shift_ids):
        raise _unprocessable("A shift id appears twice.")
    if wc_owner:
        known = {
            row_id
            for (row_id,) in tenant_query(db, WorkCenter, company_id)
            .with_entities(WorkCenter.id)
            .filter(WorkCenter.id.in_(list(wc_owner)))
            .all()
        }
        unknown = sorted(set(wc_owner) - known)
        if unknown:
            raise _unprocessable(f"Unknown work center id(s): {', '.join(str(i) for i in unknown)}.")
    return day_start, patterns


def save_production_calendar(
    db: Session, company_id: int, user_id: int, payload: ProductionCalendarUpdate, audit
) -> ProductionCalendarResponse:
    """Replace the whole calendar after a version check; flushes and audits, the caller commits."""
    acquire_generator_lock(db, "production_calendar", company_id)
    settings = _settings_row(db, company_id, locked=True)
    current_version = settings.version if settings is not None else 0
    if payload.expected_version != current_version:
        raise HTTPException(409, STALE_VERSION_DETAIL)
    day_start, desired = _validate(db, company_id, payload)

    existing_patterns = {row.id: row for row in tenant_query(db, ShiftPattern, company_id).all()}
    existing_shifts = {row.id: row for row in tenant_query(db, ShiftDefinition, company_id).all()}
    for pattern in desired:
        if pattern.id is not None and pattern.id not in existing_patterns:
            raise _unprocessable(f"Shift pattern {pattern.id} not found; omit the id to create a new pattern.")
        for shift in pattern.shifts:
            if shift.id is not None and shift.id not in existing_shifts:
                raise _unprocessable(f"Shift {shift.id} not found; omit the id to create a new shift.")

    kept_shift_ids = {shift.id for pattern in desired for shift in pattern.shifts if shift.id is not None}
    referenced = (
        {
            shift_id
            for (shift_id,) in db.query(OEERecord.shift_id)
            .filter(OEERecord.company_id == company_id, OEERecord.shift_id.in_(list(existing_shifts)))
            .distinct()
            .all()
        }
        if existing_shifts
        else set()
    )
    for shift_id, row in existing_shifts.items():
        if shift_id in referenced and shift_id not in kept_shift_ids:
            raise _unprocessable(
                f"Shift '{row.code}' is used by OEE records; retire it (is_active: false) instead of removing it."
            )
    for pattern in desired:
        for shift in pattern.shifts:
            if shift.id in referenced and existing_shifts[shift.id].code != shift.code:
                raise _unprocessable(
                    f"Shift '{existing_shifts[shift.id].code}' is used by OEE records, so its code cannot change."
                )

    before = _document(db, company_id)
    if settings is not None and _same_document(before, day_start, desired):
        return calendar_response(db, company_id)

    _apply(db, company_id, user_id, settings, day_start, desired, existing_patterns, existing_shifts)
    after = _document(db, company_id)
    settings = _settings_row(db, company_id)
    assert settings is not None
    audit.log_required(
        "CREATE" if current_version == 0 else "UPDATE",
        AUDIT_RESOURCE_TYPE,
        resource_id=settings.id,
        resource_identifier=AUDIT_IDENTIFIER,
        description=f"{'Created' if current_version == 0 else 'Updated'} the production calendar (v{after['version']})",
        old_values=before if current_version else None,
        new_values=after,
        extra_data={"source": AUDIT_RESOURCE_TYPE},
    )
    db.flush()
    return calendar_response(db, company_id)


def _same_document(before: Dict[str, Any], day_start: int, desired: List[_DesiredPattern]) -> bool:
    if before["production_day_start_minute"] != day_start:
        return False
    if any(p.id is None or any(s.id is None for s in p.shifts) for p in desired):
        return False
    wanted = [
        {
            "id": p.id,
            "name": p.name,
            "is_default": p.is_default,
            "sort_order": p.sort_order,
            "work_center_ids": p.work_center_ids,
            "shifts": sorted(
                [
                    {
                        "id": s.id,
                        "code": s.code,
                        "name": s.name,
                        "start_minute": s.start_minute,
                        "end_minute": s.end_minute,
                        "days": mask_to_days(s.days_mask),
                        "planned_minutes": s.planned_minutes,
                        "is_active": s.is_active,
                        "sort_order": s.sort_order,
                    }
                    for s in p.shifts
                ],
                key=lambda s: s["id"],
            ),
        }
        for p in desired
    ]
    stored = [dict(p, shifts=sorted(p["shifts"], key=lambda s: s["id"])) for p in before["patterns"]]
    return sorted(wanted, key=lambda p: p["id"]) == sorted(stored, key=lambda p: p["id"])


def _apply(
    db: Session,
    company_id: int,
    user_id: int,
    settings: Optional[ProductionCalendarSettings],
    day_start: int,
    desired: List[_DesiredPattern],
    existing_patterns: Dict[int, ShiftPattern],
    existing_shifts: Dict[int, ShiftDefinition],
) -> None:
    """Write the validated document. Ordered so no unique key collides mid-flush."""
    now = datetime.utcnow()
    if settings is None:
        settings = ProductionCalendarSettings(
            company_id=company_id, production_day_start_minute=day_start, version=1, updated_by=user_id, updated_at=now
        )
        db.add(settings)
    else:
        settings.production_day_start_minute = day_start
        settings.version = (settings.version or 0) + 1
        settings.updated_by = user_id
        settings.updated_at = now

    kept_pattern_ids = {p.id for p in desired if p.id is not None}
    kept_shift_ids = {s.id for p in desired for s in p.shifts if s.id is not None}

    # 1. Membership is rebuilt wholesale; 2. removed shifts go (none is referenced).
    for member in tenant_query(db, ShiftPatternWorkCenter, company_id).all():
        db.delete(member)
    for shift_id, row in existing_shifts.items():
        if shift_id not in kept_shift_ids:
            db.delete(row)
    db.flush()

    # 3. Free every unique key that changes: renamed / removed patterns, recoded shifts, defaults.
    desired_by_pattern = {p.id: p for p in desired if p.id is not None}
    desired_by_shift = {s.id: s for p in desired for s in p.shifts if s.id is not None}
    for pattern_id, row in existing_patterns.items():
        target = desired_by_pattern.get(pattern_id)
        if target is None or target.name != row.name:
            row.name = f"{RESERVED_PREFIX}{pattern_id}"
        row.is_default = False
    for shift_id, row in existing_shifts.items():
        target_shift = desired_by_shift.get(shift_id)
        if target_shift is not None and target_shift.code != row.code:
            row.code = f"{RESERVED_PREFIX}{shift_id}"
    db.flush()

    # 4. New patterns; 5. final pattern values and every shift.
    rows_by_desired: Dict[int, ShiftPattern] = {}
    for index, pattern in enumerate(desired):
        if pattern.id is None:
            row = ShiftPattern(
                company_id=company_id, name=pattern.name, is_default=False, sort_order=pattern.sort_order
            )
            db.add(row)
        else:
            row = existing_patterns[pattern.id]
            row.name = pattern.name
            row.sort_order = pattern.sort_order
        rows_by_desired[index] = row
    db.flush()
    for index, pattern in enumerate(desired):
        pattern_row = rows_by_desired[index]
        for shift in pattern.shifts:
            shift_row = existing_shifts[shift.id] if shift.id is not None else ShiftDefinition(company_id=company_id)
            shift_row.shift_pattern_id = pattern_row.id
            shift_row.code = shift.code
            shift_row.name = shift.name
            shift_row.start_minute = shift.start_minute
            shift_row.end_minute = shift.end_minute
            shift_row.days_mask = shift.days_mask
            shift_row.planned_minutes = shift.planned_minutes
            shift_row.is_active = shift.is_active
            shift_row.sort_order = shift.sort_order
            if shift.id is None:
                db.add(shift_row)
    db.flush()

    # 6. Removed patterns (their shifts are gone or moved); 7. the default; 8. membership.
    for pattern_id, row in existing_patterns.items():
        if pattern_id not in kept_pattern_ids:
            db.delete(row)
    db.flush()
    for index, pattern in enumerate(desired):
        if pattern.is_default:
            rows_by_desired[index].is_default = True
    db.flush()
    for index, pattern in enumerate(desired):
        for wc_id in pattern.work_center_ids:
            db.add(
                ShiftPatternWorkCenter(
                    company_id=company_id, shift_pattern_id=rows_by_desired[index].id, work_center_id=wc_id
                )
            )
    db.flush()


# --------------------------------------------------------------------------------------
# OEE shift references
# --------------------------------------------------------------------------------------


def resolve_shift_for_write(
    db: Session,
    company_id: int,
    work_center_id: int,
    *,
    shift_id: Optional[int],
    shift_label: Optional[str],
    current_shift_id: Optional[int] = None,
) -> Optional[ShiftDefinition]:
    """The defined shift an OEE write names, validated tenant-scoped; ``None`` for no shift.

    ``shift_id`` wins; a legacy ``shift`` text is accepted only when it equals a defined
    code (it then resolves to that shift). A foreign or unknown id is 404, an unknown
    text 422, a retired shift or one outside the work center's pattern 422 -- unless it
    is the shift the record already carries (``current_shift_id``).
    """
    label = (shift_label or "").strip() or None
    if shift_id is None and label is None:
        return None
    if shift_id is not None:
        definition = tenant_query(db, ShiftDefinition, company_id).filter(ShiftDefinition.id == shift_id).first()
        if definition is None:
            raise HTTPException(404, SHIFT_NOT_FOUND_DETAIL)
        if label is not None and label != definition.code:
            raise _unprocessable(f"shift '{label}' does not match shift_id {shift_id} ('{definition.code}').")
    else:
        definition = tenant_query(db, ShiftDefinition, company_id).filter(ShiftDefinition.code == label).first()
        if definition is None:
            raise _unprocessable(UNKNOWN_SHIFT_DETAIL.format(label=label))
    if definition.id != current_shift_id:
        if not definition.is_active:
            raise _unprocessable(f"Shift '{definition.code}' is retired.")
        if pattern_id_for_work_center(db, company_id, work_center_id) != definition.shift_pattern_id:
            raise _unprocessable(f"Shift '{definition.code}' is not in this work center's shift pattern.")
    return definition


def shift_names_by_id(db: Session, company_id: int, shift_ids: Iterable[Optional[int]]) -> Dict[int, str]:
    ids = sorted({shift_id for shift_id in shift_ids if shift_id is not None})
    if not ids:
        return {}
    return {
        row.id: row.name
        for row in tenant_query(db, ShiftDefinition, company_id).filter(ShiftDefinition.id.in_(ids)).all()
    }


def shift_def_for(definition: ShiftDefinition) -> ShiftDef:
    return _shift_def(definition)


# --------------------------------------------------------------------------------------
# Working-calendar sync
# --------------------------------------------------------------------------------------


def sync_working_calendars(
    db: Session,
    company_id: int,
    user_id: int,
    *,
    dry_run: bool,
    expected_version: Optional[int],
    audit,
) -> Tuple[WorkingCalendarSyncResponse, List[WorkingCalendarSave]]:
    """Derive each covered work center's weekly hours from its shifts; write them unless ``dry_run``.

    A dry run writes nothing. A commit first takes the production calendar's own lock
    (the one ``PUT /production-calendar`` takes), so the hours are derived from -- and the
    audit rows stamped with -- the calendar version that is current when they are written,
    and a concurrent save waits for the sync rather than slipping in between the version
    check and the writes. It then locks the active work centers (ascending id) BEFORE the
    first write, writes every changed calendar through ``save_working_calendar``
    (overrides kept, version bumped), then writes ONE required audit row per work center
    -- after the last row lock, because the audit chain lock is global. The caller wraps
    the call in ``required_audit`` and commits.

    A work center whose pattern has NO active shift is not covered: an empty (or wholly
    retired) pattern defines no hours, and writing ``[0] * 7`` would take the work center
    out of scheduling capacity altogether.
    """
    if not dry_run:
        acquire_generator_lock(db, "production_calendar", company_id)
    cal = load_production_calendar(db, company_id)
    if expected_version is not None and expected_version != cal.version:
        raise HTTPException(409, STALE_VERSION_DETAIL)
    patterns = {row.id: row for row in tenant_query(db, ShiftPattern, company_id).all()}
    explicit = {
        member.work_center_id: member.shift_pattern_id
        for member in tenant_query(db, ShiftPatternWorkCenter, company_id).all()
    }
    default_id = next((pid for pid, row in patterns.items() if row.is_default), None)
    centers_query = (
        tenant_query(db, WorkCenter, company_id).filter(WorkCenter.is_active == True).order_by(WorkCenter.id)  # noqa
    )
    if not dry_run:
        centers_query = centers_query.with_for_update().populate_existing()
    centers = centers_query.all()
    covered = [(wc, explicit.get(wc.id, default_id)) for wc in centers]
    covered = [
        (wc, pid) for wc, pid in covered if pid is not None and pid in patterns and cal.shifts_for_work_center(wc.id)
    ]
    current = load_working_calendars(db, company_id, [wc.id for wc, _ in covered]) if covered else {}

    rows: List[WorkingCalendarSyncRow] = []
    to_write: List[Tuple[WorkCenter, List[float], Dict[str, Any]]] = []
    for wc, pattern_id in covered:
        now_calendar = current[wc.id]
        new_hours = weekly_hours_for(cal, wc.id)
        current_hours = [float(value) for value in now_calendar["weekly_hours"]]
        changed = now_calendar["version"] == 0 or current_hours != new_hours
        rows.append(
            WorkingCalendarSyncRow(
                work_center_id=wc.id,
                work_center_code=wc.code,
                work_center_name=wc.name,
                pattern_id=pattern_id,
                pattern_name=patterns[pattern_id].name,
                current_version=now_calendar["version"],
                current_weekly_hours=current_hours,
                new_weekly_hours=new_hours,
                overrides_kept=len(now_calendar["overrides"] or []),
                changed=changed,
            )
        )
        if changed:
            to_write.append((wc, new_hours, now_calendar))

    saves: List[WorkingCalendarSave] = []
    if not dry_run:
        for wc, new_hours, now_calendar in to_write:
            saves.append(
                save_working_calendar(
                    db,
                    company_id,
                    wc,
                    weekly_hours=new_hours,
                    overrides=list(now_calendar["overrides"] or []),
                    expected_version=now_calendar["version"],
                    user_id=user_id,
                )
            )
        for saved in saves:
            audit_working_calendar_save(
                audit,
                saved,
                required=True,
                extra_data={"source": SYNC_SOURCE, "production_calendar_version": cal.version},
            )
        db.flush()

    response = WorkingCalendarSyncResponse(
        dry_run=dry_run,
        configured=cal.configured,
        calendar_version=cal.version,
        changed_count=sum(1 for row in rows if row.changed),
        rows=rows,
    )
    return response, saves


# --------------------------------------------------------------------------------------
# Attribution over production-day ranges
# --------------------------------------------------------------------------------------


def attribute_time_entries(
    query: Query,
    cal: ProductionCalendar,
    first_day: date,
    last_day: date,
    *,
    clock_in_column: Any,
    now: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[Tuple[Any, date, Optional[ShiftOccurrence]]]:
    """``(row, production_day, shift)`` for every row of ``query`` attributed to ``first_day..last_day``.

    ``query`` is the caller's tenant-scoped time-entry query; it must select rows that
    carry ``clock_in`` / ``clock_out`` / ``work_center_id``. The SQL window is the
    production-day range padded by ``ENTRY_LOOKBACK`` before its start (``until`` caps
    clock-ins at "now" for a today-so-far read); attribution happens here, per row.
    Open entries run to ``now`` (default: no open-entry extension).
    """
    start_utc, end_utc = production_days_window_or_empty(first_day, last_day, cal)
    if start_utc >= end_utc:
        return []
    query = query.filter(clock_in_column >= lookback_from(start_utc), clock_in_column < end_utc)
    if until is not None:
        query = query.filter(clock_in_column <= until)
    attributed = []
    for row in query.all():
        day, occ = entry_attribution(row.clock_in, row.clock_out, cal, row.work_center_id, now=now)
        if first_day <= day <= last_day:
            attributed.append((row, day, occ))
    return attributed


def attribute_downtime_events(
    query: Query,
    cal: ProductionCalendar,
    first_day: date,
    last_day: date,
    *,
    start_column: Any,
) -> List[Tuple[Any, date, Optional[ShiftOccurrence]]]:
    """``(row, production_day, shift)`` for downtime rows (``start_time`` / ``end_time`` /
    ``duration_minutes`` / ``work_center_id``) attributed to ``first_day..last_day``.

    An event belongs to the shift it overlaps most (its end is ``end_time``, else start +
    ``duration_minutes``), like a time entry; one overlapping no shift belongs to the
    production day it started on.
    """
    start_utc, end_utc = production_days_window_or_empty(first_day, last_day, cal)
    if start_utc >= end_utc:
        return []
    query = query.filter(start_column >= lookback_from(start_utc), start_column < end_utc)
    attributed = []
    for row in query.all():
        end = row.end_time
        if end is None and row.duration_minutes:
            end = row.start_time + timedelta(minutes=float(row.duration_minutes))
        day, occ = entry_attribution(row.start_time, end, cal, row.work_center_id)
        if first_day <= day <= last_day:
            attributed.append((row, day, occ))
    return attributed
