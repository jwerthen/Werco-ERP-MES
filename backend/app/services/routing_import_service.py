"""Routing CSV/XLSX importer (A0.2 Excel-migration family).

This is the routing wizard's server side: it ingests a parsed CSV/XLSX upload
(already normalized by :func:`app.services.import_service.parse_import_file`) and
turns it into draft routings. Concretely it:

* groups data rows by ``part_number`` (case-insensitive) into ONE draft
  :class:`~app.models.routing.Routing` plus its
  :class:`~app.models.routing.RoutingOperation` rows, preserving first-seen file
  order;
* runs each routing inside a SAVEPOINT — ``dry_run`` rolls the savepoint back so
  a preview is guaranteed to write nothing (including audit rows), while commit
  mode commits routing-by-routing so one bad routing never poisons the rest
  (same partial-success contract as the open-WO / open-PO migration loaders);
* resolves each ``work_center_code`` to an active, tenant-scoped
  :class:`~app.models.work_center.WorkCenter`;
* requires the part to PRE-EXIST and be a manufactured/assembly (engineering)
  part, not soft-deleted — it never creates parts;
* refuses a duplicate part+revision (a routing already at that revision) and
  NEVER mutates an existing routing: a new draft revision is created alongside
  any existing revisions instead (compliance: preserve historical records,
  prefer new revisions over mutating shipped data);
* audit-logs exactly one CREATE per routing, summarizing its operations in
  ``extra_data`` — never writing the tamper-evident audit_log table directly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import func

from app.db.tenant_filter import tenant_query
from app.models.part import ENGINEERING_PART_TYPES
from app.models.routing import Routing, RoutingOperation
from app.models.user import User
from app.models.work_center import WorkCenter
from app.schemas.routing_import import (
    RoutingImportError,
    RoutingImportResponse,
    RoutingImportRowResult,
)
from app.services.audit_service import AuditService
from app.services.import_service import ParsedTable
from app.services.migration_import_service import (
    _expunge_rolled_back,
    _find_part,
    _required,
    _rollback_failed_row,
)

logger = logging.getLogger(__name__)

IMPORT_SOURCE = "import"

_TRUE_VALUES = {"y", "yes", "true", "1", "t"}
_FALSE_VALUES = {"n", "no", "false", "0", "f", ""}


def _parse_hours(value, field_name: str) -> float:
    """Parse an optional non-negative hours cell. Blank/None -> 0.0.

    Unlike the migration service's ``_parse_non_negative_float`` this defaults a
    blank cell to 0.0 (setup/run hours are optional and legitimately zero), while
    still rejecting NaN/inf and negative values.
    """
    text = (str(value) if value is not None else "").strip()
    if not text:
        return 0.0
    try:
        parsed = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field_name} must be a non-negative number")
    return parsed


def _parse_int(value, field_name: str) -> int:
    """Parse a REQUIRED whole-number cell, rejecting blanks/NaN/inf/non-integers."""
    text = (str(value) if value is not None else "").strip()
    try:
        parsed = float(text)
        if not math.isfinite(parsed):  # int(float("inf")) raises OverflowError -> 500
            raise ValueError(f"{field_name} must be a whole number")
        return int(parsed)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a whole number") from exc


def _parse_bool(value) -> bool:
    """Coerce Y/N/true/false/1/0/yes/no (case-insensitive) -> bool; blank -> False.

    ``parse_import_file`` already coerces Excel python bools to the strings
    ``"true"``/``"false"``, so those land here as text and are accepted too.
    """
    text = (str(value) if value is not None else "").strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError(f"value '{value}' is not a yes/no (true/false) value")


@dataclass
class _RoutingOperationRow:
    row_number: int
    part_number: str
    routing_revision: str
    routing_description: Optional[str]
    sequence: int
    operation_name: str
    work_center_code: str
    setup_hours: float
    run_hours_per_unit: float
    description: Optional[str]
    is_inspection_point: bool
    is_outside_operation: bool


def _parse_routing_operation_row(row_number: int, row: Dict[str, str]) -> _RoutingOperationRow:
    return _RoutingOperationRow(
        row_number=row_number,
        part_number=_required(row, "part_number"),
        routing_revision=(row.get("routing_revision") or "").strip() or "A",
        routing_description=(row.get("routing_description") or "").strip() or None,
        sequence=_parse_int(_required(row, "sequence"), "sequence"),
        operation_name=_required(row, "operation_name"),
        work_center_code=_required(row, "work_center_code"),
        setup_hours=_parse_hours(row.get("setup_hours", ""), "setup_hours"),
        run_hours_per_unit=_parse_hours(row.get("run_hours_per_unit", ""), "run_hours_per_unit"),
        description=(row.get("description") or "").strip() or None,
        is_inspection_point=_parse_bool(row.get("is_inspection_point", "")),
        is_outside_operation=_parse_bool(row.get("is_outside_operation", "")),
    )


def import_routings(
    db,
    *,
    table: ParsedTable,
    current_user: User,
    company_id: int,
    audit: AuditService,
    dry_run: bool,
) -> RoutingImportResponse:
    """Create draft routings (one per part) with their operations from a parsed upload."""
    # Lazy import: ``calculate_routing_totals`` lives with the routing router
    # (which itself imports services), so a module-level import here would be
    # circular. Same precedent as migration_import_service reusing router helpers.
    from app.api.endpoints.routing import calculate_routing_totals

    errors: List[RoutingImportError] = []
    results: List[RoutingImportRowResult] = []
    created_ids: List[int] = []
    total_rows = 0
    total_operations = 0

    # Phase 1 — parse every row, then group rows into routings by part_number.
    groups: Dict[str, List[_RoutingOperationRow]] = {}
    group_order: List[str] = []
    failed_groups: Dict[str, int] = {}  # group key -> row that broke it

    for row_number, row in table.iter_rows():
        total_rows += 1
        raw_part_number = (row.get("part_number") or "").strip()
        key = raw_part_number.upper()
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        try:
            groups[key].append(_parse_routing_operation_row(row_number, row))
        except ValueError as exc:
            failed_groups.setdefault(key, row_number)
            errors.append(
                RoutingImportError(
                    row=row_number,
                    part_number=raw_part_number or None,
                    reason=str(exc),
                )
            )

    # Phase 2 — create one routing per group.
    for key in group_order:
        lines = groups[key]

        if key in failed_groups:
            # A routing must import whole: skip sibling rows of a failed group.
            for line in lines:
                errors.append(
                    RoutingImportError(
                        row=line.row_number,
                        part_number=line.part_number,
                        reason=f"skipped: row {failed_groups[key]} in the same routing failed validation",
                    )
                )
            continue

        part_number = lines[0].part_number
        revision = lines[0].routing_revision
        routing_description = next((line.routing_description for line in lines if line.routing_description), None)

        nested = db.begin_nested()
        row_instances: List[object] = []
        try:
            # Duplicate-sequence guard (within the group) — a routing can't have
            # two operations at the same sequence.
            seen_sequences: set[int] = set()
            for line in lines:
                if line.sequence in seen_sequences:
                    raise ValueError(f"duplicate sequence {line.sequence} for part '{part_number}'")
                seen_sequences.add(line.sequence)

            part = _find_part(db, company_id, part_number)
            if part is None:
                raise ValueError(f"part '{part_number}' not found")
            # part_type may be an enum or a plain string depending on the loader;
            # compare by value to be safe.
            pt = part.part_type.value if hasattr(part.part_type, "value") else part.part_type
            if pt not in {p.value for p in ENGINEERING_PART_TYPES}:
                raise ValueError(
                    f"part '{part_number}' is not a manufactured or assembly part " "— routings only apply to those"
                )

            # Existing-routing / revision handling: never mutate an existing
            # routing. Same revision is a conflict; any other revision is fine
            # (a new draft revision is created alongside).
            existing_routings = (
                tenant_query(db, Routing, company_id)
                .filter(Routing.part_id == part.id, Routing.is_deleted == False)  # noqa: E712
                .all()
            )
            for existing in existing_routings:
                if (existing.revision or "").upper() == revision.upper():
                    raise ValueError(
                        f"part '{part_number}' already has a routing at revision '{revision}' "
                        "— choose a new revision"
                    )

            routing = Routing(
                part_id=part.id,
                revision=revision,
                description=routing_description,
                status="draft",
                created_by=current_user.id,
            )
            routing.company_id = company_id
            db.add(routing)
            db.flush()
            row_instances.append(routing)

            for op in lines:
                wc = (
                    tenant_query(db, WorkCenter, company_id)
                    .filter(
                        func.upper(WorkCenter.code) == op.work_center_code.upper(),
                        WorkCenter.is_active == True,  # noqa: E712
                    )
                    .first()
                )
                if wc is None:
                    raise ValueError(
                        f"work center '{op.work_center_code}' not found or inactive "
                        f"(operation '{op.operation_name}', sequence {op.sequence})"
                    )
                operation = RoutingOperation(
                    routing_id=routing.id,
                    company_id=company_id,
                    sequence=op.sequence,
                    operation_number=f"Op {op.sequence}",
                    name=op.operation_name,
                    description=op.description,
                    work_center_id=wc.id,
                    setup_hours=op.setup_hours,
                    run_hours_per_unit=op.run_hours_per_unit,
                    is_inspection_point=op.is_inspection_point,
                    is_outside_operation=op.is_outside_operation,
                    work_instructions=op.description,
                )
                db.add(operation)
                row_instances.append(operation)

            db.flush()
            db.refresh(routing)
            # Compute totals EXACTLY as the interactive create flow does.
            calculate_routing_totals(routing, db)
            db.flush()

            operation_count = len(lines)
            result = RoutingImportRowResult(
                rows=[line.row_number for line in lines],
                part_number=part.part_number,
                routing_revision=revision,
                routing_id=None if dry_run else routing.id,
                operation_count=operation_count,
                total_setup_hours=routing.total_setup_hours,
                total_run_hours_per_unit=routing.total_run_hours_per_unit,
                status="draft",
            )

            if dry_run:
                nested.rollback()
                _expunge_rolled_back(db, row_instances)
            else:
                audit.log_create(
                    resource_type="routing",
                    resource_id=routing.id,
                    resource_identifier=part.part_number,
                    new_values=routing,
                    description=f"Imported routing Rev {revision} for {part.part_number} from CSV/XLSX upload",
                    extra_data={
                        "source": IMPORT_SOURCE,
                        "part_number": part.part_number,
                        "routing_revision": revision,
                        "operation_count": operation_count,
                        "operation_sequences": [op.sequence for op in lines],
                    },
                )
                nested.commit()
                db.commit()
                created_ids.append(routing.id)

            total_operations += operation_count
            results.append(result)
        except ValueError as exc:
            _rollback_failed_row(db, nested, row_instances)
            for line in lines:
                errors.append(
                    RoutingImportError(
                        row=line.row_number,
                        part_number=part_number,
                        reason=str(exc),
                    )
                )
        except Exception:
            _rollback_failed_row(db, nested, row_instances)
            logger.exception("routing import failed for part %s", part_number)
            for line in lines:
                errors.append(
                    RoutingImportError(
                        row=line.row_number,
                        part_number=part_number,
                        reason="Failed to create routing due to a database error",
                    )
                )

    if dry_run:
        # Belt and braces: nothing from a preview may ever reach the database.
        db.rollback()

    rows_imported = sum(len(result.rows) for result in results)
    return RoutingImportResponse(
        dry_run=dry_run,
        total_rows=total_rows,
        parts_detected=len(group_order),
        routings_created=len(results),
        total_operations=total_operations,
        skipped_count=total_rows - rows_imported,
        created_ids=created_ids,
        results=results,
        errors=errors,
    )
