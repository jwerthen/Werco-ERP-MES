"""Laser nest package parsing and import helpers."""

from __future__ import annotations

import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Iterable, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentType
from app.models.laser_nest import LaserNest, LaserNestPackage
from app.models.part import Part
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderType
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf
from app.services.laser_nest_text import (
    normalize_material,
    normalize_nest_descriptors,
    normalize_sheet_size,
    normalize_thickness,
)
from app.services.material_consumption_service import cancel_allocations_for_operations
from app.services.storage_service import get_storage, resolve_upload_dir, sanitize_ext
from app.services.work_center_type_service import get_work_center_group

if TYPE_CHECKING:
    from app.schemas.work_order import LaserNestManualCreate

CNC_EXTENSIONS = {
    ".cnc",
    ".eia",
    ".fgc",
    ".lcc",
    ".mpf",
    ".nc",
    ".ncc",
    ".ord",
    ".pgm",
    ".tap",
}

# Per-package cap on PDF nest sheets. AI-always extraction means each PDF costs
# one LLM call, so a runaway ZIP is both a latency and a cost concern -- the
# preview/import endpoints reject anything over this with a 400.
LASER_PDF_PACKAGE_MAX = 50


@dataclass(frozen=True)
class ParsedLaserNest:
    nest_name: str
    cnc_file_name: str
    cnc_file_path: Optional[str]
    planned_runs: int
    material: Optional[str] = None
    thickness: Optional[str] = None
    sheet_size: Optional[str] = None
    # PDF-package extras. cnc_number / confidence come from the AI extraction;
    # pdf_source_path is the absolute server path to the PDF bytes (used to
    # create + attach a Document on import) and is INTERNAL ONLY -- it is
    # deliberately kept out of as_dict() so it never leaks to the client.
    cnc_number: Optional[str] = None
    pdf_source_path: Optional[str] = None
    confidence: Optional[str] = None
    # Bare-multi-page-PDF extras (None for ZIP/CNC/folder rows). source_pages is
    # the segment's 1-based page list in the ORIGINAL uploaded PDF (a tuple --
    # the dataclass is frozen); field_confidence is the merged per-field
    # confidence dict from the two-pass extraction; warning surfaces a degraded
    # extraction / skipped verification; passes records 1 or 2 AI reads.
    source_pages: Optional[tuple[int, ...]] = None
    field_confidence: Optional[dict] = None
    warning: Optional[str] = None
    passes: Optional[int] = None
    # Per-row work-center override (PDF confirm-and-commit import): when set, the
    # nest's operation is created on THIS work center instead of the package-level
    # laser work center. IMPORT-SIDE INSTRUCTION only -- deliberately kept out of
    # as_dict() so it never appears in a preview response.
    work_center_id: Optional[int] = None
    # Per-row MATERIAL TIE (PDF confirm-and-commit import): the stock part this nest
    # consumes, and how much of it per completed run. When material_part_id is set the
    # build creates an operation-scoped WorkOrderMaterialAllocation on the nest's
    # operation, so the material is deducted when the laser WO finishes. Like
    # work_center_id these are IMPORT-SIDE INSTRUCTIONS only -- deliberately kept out
    # of as_dict() so they never appear in a preview response. There is NO fuzzy
    # auto-match from the AI-extracted ``material`` free text: a wrong auto-tie would
    # deplete the wrong heat lot into an as-built record, so the planner picks
    # explicitly or the nest ships untied.
    #
    # 2026-08-10: the PREVIEW now carries a server-computed ``sheet_suggestion``
    # (``services/sheet_stock_matcher.py``), and the reasoning above is exactly why
    # that is not the same thing. Nothing is matched by fuzzy string similarity, and
    # nothing is derived on the client at all. A suggestion is pre-filled only after
    # clearing an exact-thickness gate, a real alloy agreement, an 8-point ambiguity
    # margin and a sheet-form test -- and it reaches THIS field only once the planner
    # has confirmed it in the review grid. What lands here is still, always, a value
    # a human committed to.
    material_part_id: Optional[int] = None
    qty_per_run: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "nest_name": self.nest_name,
            "cnc_file_name": self.cnc_file_name,
            "cnc_file_path": self.cnc_file_path,
            "planned_runs": self.planned_runs,
            "material": self.material,
            "thickness": self.thickness,
            "sheet_size": self.sheet_size,
            "cnc_number": self.cnc_number,
            "confidence": self.confidence,
            # For PDFs this is the relative path within the package (the import
            # row key); for CNC-file nests it is the CNC file's relative path.
            "source_file": self.cnc_file_path,
            "source_pages": list(self.source_pages) if self.source_pages is not None else None,
            "field_confidence": self.field_confidence,
            "warning": self.warning,
            "passes": self.passes,
        }


def parse_laser_nest_folder(source_path: str) -> list[ParsedLaserNest]:
    root = Path(source_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Source path must be an existing folder")
    return _parse_entries(_iter_cnc_files(root))


def parse_laser_nest_zip(zip_path: str) -> list[ParsedLaserNest]:
    with TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract_zip(archive, Path(temp_dir))
        return parse_laser_nest_folder(temp_dir)


def package_has_pdfs(folder: str) -> bool:
    """True if the folder contains at least one PDF (recursively).

    Detection switch for the import/preview endpoints: PDFs and ``CNC_EXTENSIONS``
    are disjoint, so a package is treated as a PDF nest-report package iff it
    contains any ``*.pdf``; otherwise it falls back to the CNC-program path.
    """
    root = Path(folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return False
    return any(p.is_file() for p in root.rglob("*.pdf"))


def parse_laser_nest_pdf_package(folder: str, company_id: int) -> list[ParsedLaserNest]:
    """Parse a folder of laser-nest report PDFs via AI extraction.

    Globs ``*.pdf`` (recursively, sorted) and runs ``extract_nest_fields_from_pdf``
    on each, building one ``ParsedLaserNest`` per file keyed by relative path.

    The AI calls here run SEQUENTIALLY -- the extraction function is sync and
    blocking. The async preview endpoint parallelizes the per-PDF extraction with
    bounded concurrency; this sync helper is the simple/offline path and is also
    handy in tests. Enforces ``LASER_PDF_PACKAGE_MAX``.

    Raises ``ValueError`` on an empty package or one over the cap (the endpoints
    translate that to a 400).
    """
    root = Path(folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("Source path must be an existing folder")

    pdf_paths = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    if not pdf_paths:
        raise ValueError("No PDF files found in package")
    if len(pdf_paths) > LASER_PDF_PACKAGE_MAX:
        raise ValueError(
            f"Package has {len(pdf_paths)} PDFs; the limit is {LASER_PDF_PACKAGE_MAX}. "
            "Split the package into smaller batches."
        )

    nests: list[ParsedLaserNest] = []
    for path in pdf_paths:
        rel_path = str(path.relative_to(root))
        result = extract_nest_fields_from_pdf(str(path), path.name, company_id)
        nests.append(build_parsed_nest_from_extraction(result, abs_path=str(path), rel_path=rel_path))
    return nests


def _coerce_planned_runs(value: object) -> int:
    """Coerce a model-supplied ``planned_runs`` to a sane int, flooring at 1.

    Defensive: the extraction result is AI output, so ``planned_runs`` may be a
    non-numeric string, a float-ish string, or junk. Anything unreadable falls
    back to 1 so a bad model value can never ``ValueError`` -> 400 the whole
    preview batch.

    THE FLOOR IS NOT A READ. ``planned_runs`` is a non-optional ``int`` on the
    wire, so a nest whose run count could not be found and a nest that genuinely
    runs once are the SAME 1 in the preview response. Only
    ``field_confidence["planned_runs"]`` tells them apart, which is why the
    wizard counts the low-confidence ones out loud rather than presenting 40
    identical-looking 1s as read values. Do not "simplify" that signal away.

    Accepted shapes, widened deliberately: the previous rule was int-or-digit-
    string ONLY, so every near-miss a model actually emits for this field --
    ``3.0`` (JSON has one number type), ``"3"`` with stray whitespace, ``"3
    sheets"``, ``"x3"`` -- landed on the same silent 1 as genuine junk. Each of
    those is an unambiguous 3, and reading it is strictly better than defaulting
    it. A LEADING integer is required for the free-text case: ``"3 of 5"`` is 3,
    while ``"sheet 4"`` stays 1 rather than reading a label as a count.

    A FRACTIONAL count is refused on BOTH the number and the string path, and
    the two must not drift apart: half a run is not a sheet count, and rounding
    one would invent a quantity nobody wrote. ``2.5`` and ``"2.5"`` therefore
    both fall back to 1 -- reading the string form as 2 would make the answer
    depend on whether the model happened to quote its JSON number, which is the
    one thing a caller can neither see nor control.
    """
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float):
        return max(1, int(value)) if value.is_integer() else 1
    if isinstance(value, str):
        text = value.strip()
        # NOTE: there is deliberately no `text.isdigit()` fast path here, and
        # the character class is `[0-9]` rather than `\d`. Both guard the same
        # hazard -- this function promises never to raise, and a raise here
        # would 400 a whole 50-nest batch over one odd row.
        #
        # `str.isdigit()` is True for superscripts and other non-ASCII digits
        # ('³'), and `int()` rejects exactly those, so that fast path was
        # the one surviving ValueError route. It was also redundant: the regex
        # reads "007", "12" and "0" identically. `\d` is Unicode-aware under
        # `re` for str patterns and would re-open the same hole, so the class is
        # spelled out.
        #
        # The trailing \b keeps this conservative: "3 sheets" and "3 of 5" read
        # as 3, but "3abc" does not -- a digit glued to a word is not a count.
        match = re.match(r"^[xX]?\s*([0-9]+)(\.[0-9]+)?\b", text)
        if match:
            fraction = match.group(2)
            if fraction and float(fraction) != 0:
                return 1
            return max(1, int(match.group(1)))
    return 1


def _coerce_field_confidence(value: object) -> Optional[dict]:
    """Sanitize the extraction result's per-field ``confidence`` dict for rows.

    Defensive: on the two-pass merge path this is a well-formed
    ``{field: "high"|"medium"|"low"}`` dict, but when verification is skipped
    the pass-1 dict is raw AI output. Only string-ish entries survive; junk
    shapes collapse to None so one odd response can't 500 a preview batch.
    """
    if not isinstance(value, dict):
        return None
    coerced = {str(key): str(entry) for key, entry in value.items() if isinstance(entry, (str, int, float))}
    return coerced or None


def build_parsed_nest_from_extraction(result: dict, *, abs_path: str, rel_path: str) -> ParsedLaserNest:
    """Map an ``extract_nest_fields_from_pdf`` result dict to a ``ParsedLaserNest``.

    Shared by the sync package parser and the async (parallelized) preview path
    so both assemble rows identically. ``planned_runs`` floors at 1.
    """
    cnc_number = result.get("cnc_number")
    file_name = Path(abs_path).name
    warning = result.get("warning")
    passes = result.get("passes")
    # Canonicalize the sheet descriptors HERE, at the earliest point they exist,
    # rather than only at the DB write: the wizard shows this row to the planner
    # and echoes it back on commit, so normalizing later would mean the planner
    # confirmed one spelling and a different one was stored.
    material, thickness, sheet_size = normalize_nest_descriptors(
        result.get("material"), result.get("thickness"), result.get("sheet_size")
    )
    return ParsedLaserNest(
        nest_name=cnc_number or Path(file_name).stem,
        cnc_file_name=file_name,
        cnc_file_path=rel_path,
        planned_runs=_coerce_planned_runs(result.get("planned_runs")),
        material=material,
        thickness=thickness,
        sheet_size=sheet_size,
        cnc_number=cnc_number,
        pdf_source_path=abs_path,
        confidence=result.get("extraction_confidence"),
        field_confidence=_coerce_field_confidence(result.get("confidence")),
        warning=warning if isinstance(warning, str) else None,
        passes=passes if isinstance(passes, int) and not isinstance(passes, bool) else None,
    )


def preview_laser_nest_package(source_path: Optional[str] = None, zip_path: Optional[str] = None) -> list[dict]:
    if zip_path:
        nests = parse_laser_nest_zip(zip_path)
    elif source_path:
        nests = parse_laser_nest_folder(source_path)
    else:
        raise ValueError("Provide a zipped package or source folder")
    return [nest.as_dict() for nest in nests]


def copy_laser_nest_folder(source_path: str, destination: str) -> None:
    source = Path(source_path).expanduser().resolve()
    dest = Path(destination).resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError("Source path must be an existing folder")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)


def extract_laser_nest_zip(zip_path: str, destination: str) -> None:
    dest = Path(destination).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract_zip(archive, dest)


def _create_nest_document(
    db: Session,
    *,
    nest: ParsedLaserNest,
    work_order: WorkOrder,
    company_id: int,
    created_by: Optional[int],
    saved_storage_keys: Optional[list[str]] = None,
) -> Optional[Document]:
    """Persist a nest's source PDF as a DRAWING Document and return it.

    Mirrors ``documents.upload_document`` storage handling: tenant-prefixed
    object key on remote storage, legacy ``UPLOAD_DIR/{uuid}{ext}`` layout
    locally. The drawing is scoped to ``work_order`` -- the PARENT assembly WO
    in the classic flow (matching the manual-modal attach path), or the
    standalone laser-cutting WO itself when there is no parent. Returns None
    when the nest has no PDF source.

    ``storage.save`` writes a REAL blob (disk/S3) BEFORE the surrounding
    transaction commits. Every reference it returns is appended to
    ``saved_storage_keys`` (when supplied) so the caller can reap orphaned blobs
    if the transaction later rolls back -- these blobs live outside the temp
    package dir, so they are not reaped by the package-dir cleanup.
    """
    if not nest.pdf_source_path:
        return None

    with open(nest.pdf_source_path, "rb") as handle:
        content = handle.read()

    storage = get_storage()
    if storage.is_remote:
        key = f"{company_id}/documents/{uuid.uuid4()}{sanitize_ext(nest.cnc_file_name)}"
    else:
        file_ext = os.path.splitext(nest.cnc_file_name or "")[1] or ".pdf"
        key = os.path.join(resolve_upload_dir(), f"{uuid.uuid4()}{file_ext}")
    file_path = storage.save(content, key=key)
    if saved_storage_keys is not None:
        # Record the STORED reference (what delete()/delete_ref() expects back):
        # an ``s3://...`` ref on remote, the filesystem path locally.
        saved_storage_keys.append(file_path)

    # Local import avoids importing the documents endpoint module at service load.
    from app.api.endpoints.documents import generate_document_number

    document = Document(
        document_number=generate_document_number(db, "drawing"),
        revision="A",
        title=nest.cnc_number or nest.nest_name,
        document_type=DocumentType.DRAWING,
        work_order_id=work_order.id,
        file_name=nest.cnc_file_name,
        file_path=file_path,
        file_size=len(content),
        mime_type="application/pdf",
        status="released",
        created_by=created_by,
        company_id=company_id,
    )
    db.add(document)
    db.flush()
    return document


def _uom_value(part: Part) -> str:
    """``Part.unit_of_measure`` as the lowercase enum VALUE the snapshot column stores.

    Mirrors ``api/endpoints/work_order_materials._uom_value`` so a nest-created tie and
    a hand-created one snapshot the unit identically (``Part.unit_of_measure`` passes
    ``values_callable``, so it persists the lowercase value).
    """
    uom = part.unit_of_measure
    return getattr(uom, "value", uom) or "each"


def create_nest_material_allocation(
    db: Session,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    part: Part,
    qty_per_run: Optional[float],
    planned_runs: int,
    company_id: int,
    created_by: Optional[int],
    audit: AuditService,
) -> WorkOrderMaterialAllocation:
    """Create the OPERATION-scoped material tie for one laser nest.

    The single tie-creation seam shared by the package import
    (``build_laser_nest_child_work_order``) and the manual single-nest endpoint, so both
    produce byte-identical rows AND identical hash-chain entries -- the same field set,
    the same UoM snapshot, and the same ``log_create`` resource type / description /
    ``extra_data`` shape as ``POST /work-orders/{id}/material-allocations``
    (``api/endpoints/work_order_materials``). Keep the three in lock-step.

    ``operation`` must already be flushed (its ``id`` is the tie's scope). ``part`` is
    resolved and tenant-validated by the CALLER -- a tie naming another company's part is
    a security defect, and the caller is the layer that can answer it with a 404.

    **This seam does NOT re-check the terminal-work-order refusal that
    ``POST .../material-allocations`` enforces (409, "a tie that can never consume is a
    lie").** It does not need to, but the reason is a COUPLING rather than a guard, so it
    is written down here: both nest callers force ``work_order.status = RELEASED`` inside
    the same transaction before reaching this function (the import at
    ``work_orders._run_laser_nest_import``, the manual route at
    ``create_manual_laser_nest_endpoint``), so a terminal work order is unreachable by
    construction. That coupling is invisible from this signature -- if a third caller is
    ever added, or either existing one stops forcing the status, this function will
    happily create a tie that can never consume. Add the explicit check then.

    Ships UNPINNED by design: no ``pinned_inventory_item_id``, so FIFO picks the lot at
    consume time. Operator/planner lot-picking is deliberately deferred.

    ``qty_per_run`` defaults to 1.0 when the planner named a part but no quantity (one
    sheet per run -- the headline nest case). ``qty_planned`` is the run-scaled total:
    the engine is reconcile-to-target, so this is planning demand, not a commitment.

    ``audit`` is REQUIRED (invariant #2): creating a tie is a state change on a tenant
    table, so there is no caller for whom an unaudited tie is correct. ``AuditService.log``
    only flushes, so the row commits atomically with the allocation.

    NOTE ON TIMING: at the import call site this MUST run after
    ``cancel_allocations_for_operations`` and the operation wipe. A superseded tie is
    CANCELLED (never deleted), so the partial unique index
    ``uq_wo_material_alloc_open_op (company_id, work_order_operation_id, part_id)
    WHERE status = 'OPEN'`` only ever sees one OPEN row per key -- and the new operation
    id is fresh anyway. Creating before the cancel would be a collision waiting to happen.
    """
    effective_qty_per_run = float(qty_per_run) if qty_per_run is not None else 1.0
    allocation = WorkOrderMaterialAllocation(
        company_id=company_id,
        work_order_id=work_order.id,
        work_order_operation_id=operation.id,
        part_id=part.id,
        source=AllocationSource.NEST,
        status=AllocationStatus.OPEN,
        qty_per_run=effective_qty_per_run,
        qty_planned=effective_qty_per_run * float(planned_runs),
        # Snapshot so the tie stays readable after the part's UoM is changed.
        unit_of_measure=_uom_value(part),
        qty_consumed=0.0,
        # No lot pin: FIFO selects at consume time.
        pinned_inventory_item_id=None,
        pinned_lot_number=None,
        notes=None,
        created_by=created_by,
    )
    db.add(allocation)
    db.flush()

    audit.log_create(
        "work_order_material_allocation",
        allocation.id,
        f"WO {work_order.work_order_number} / part {part.part_number}",
        new_values=allocation,
        description=(
            f"Tied {allocation.qty_planned} {allocation.unit_of_measure} of part {part.part_number} "
            f"to work order {work_order.work_order_number}"
            f" operation {operation.operation_number or operation.id}"
        ),
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": allocation.work_order_operation_id,
            "part_id": part.id,
            "source": allocation.source.value,
            "pinned_inventory_item_id": allocation.pinned_inventory_item_id,
        },
    )
    return allocation


def build_laser_nest_child_work_order(
    db: Session,
    *,
    parent_work_order: Optional[WorkOrder],
    child_work_order: WorkOrder,
    package_name: str,
    package_source_path: Optional[str],
    nests: list[ParsedLaserNest],
    laser_work_center: WorkCenter,
    company_id: int,
    created_by: Optional[int],
    saved_storage_keys: Optional[list[str]] = None,
    row_work_centers: Optional[dict[int, WorkCenter]] = None,
    row_material_parts: Optional[dict[int, Part]] = None,
    audit: AuditService,
) -> LaserNestPackage:
    """Replace a laser WO's nest tasks with the supplied package plan.

    ``parent_work_order`` is the assembly WO in the classic child-laser-WO flow.
    It is ``None`` for a STANDALONE nest WO (part-less laser-cutting WO with no
    parent): the package then carries ``parent_work_order_id IS NULL``, no
    parent linkage is written onto ``child_work_order``, and nest-PDF Documents
    attach to ``child_work_order`` itself instead of a parent.

    ``saved_storage_keys`` (when supplied) collects every storage reference this
    build writes for nest-PDF Documents. ``storage.save`` writes the blob BEFORE
    the surrounding ``atomic_transaction`` commits, so on rollback the caller must
    reap these refs (they live outside the temp package dir). On commit they are
    durable Documents and must NOT be deleted.

    ``row_work_centers`` resolves per-nest work-center overrides: a nest whose
    ``work_center_id`` is set lands on that work center (management may spread a
    package's nests across multiple lasers); nests without one land on the
    package-level ``laser_work_center``. The CALLER validates each distinct
    override as an active, company-scoped work center and hands the resolved
    rows in here -- an override missing from the mapping is a caller bug and
    raises ``ValueError`` rather than silently falling back.

    ``row_material_parts`` resolves per-nest MATERIAL TIES the same way: a nest whose
    ``material_part_id`` is set gets an operation-scoped ``WorkOrderMaterialAllocation``
    on its freshly-created operation, so that material is deducted when the laser WO
    finishes. The CALLER validates each distinct part as a non-deleted, company-scoped
    part (404 on a miss -- never 403) and hands the resolved rows in here; a
    ``material_part_id`` missing from the mapping is a caller bug and raises
    ``ValueError`` rather than tying nothing. Nests without one are untied and stay
    byte-identical to their pre-feature behavior -- no allocation row, no audit row.

    ``audit`` is REQUIRED and records the material-tie cancellations the operation wipe
    forces (invariant #2 -- cancelling a tie is a state change on a tenant table, so
    there is no caller for whom an unaudited wipe is correct). Raises
    ``MaterialAllocationConsumedError`` -- which the endpoint maps to HTTP 409 -- if any
    allocation on a to-be-wiped operation has already consumed material; nothing is
    deleted in that case. Surviving ties are cancelled AND detached from the operation
    (their ``work_order_operation_id`` is cleared): that FK carries no ``ON DELETE``, so
    without the detach the ``db.delete(operation)`` below FK-violates on Postgres.
    """

    # IMPORT REPLACES EVERYTHING (by design). Importing a laser package wipes ALL
    # existing packages, LASER operations, and nests on this child WO and rebuilds
    # them from the package plan -- including any MANUALLY-entered nests. This is
    # intentional: the product decision is "manual OR import per job", the two
    # paths are never mixed, so an import is the authoritative source of truth and
    # cleanly supersedes prior manual entry. Do not soften this into coexistence.
    #
    # Capture the operation ids behind this WO's nests BEFORE the packages (and
    # their nests, via cascade) are deleted: ops now derive operation_group from
    # THEIR work center, so a nest op on an unusually-named work center may not
    # carry group "LASER" -- the id list keeps the wipe exhaustive regardless.
    nest_backed_operation_ids = [
        row[0]
        for row in (
            db.query(LaserNest.work_order_operation_id)
            .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
            .filter(
                LaserNest.company_id == company_id,
                WorkOrderOperation.work_order_id == child_work_order.id,
            )
            .all()
        )
        if row[0] is not None
    ]
    operation_wipe_filter = WorkOrderOperation.operation_group == "LASER"
    if nest_backed_operation_ids:
        operation_wipe_filter = or_(operation_wipe_filter, WorkOrderOperation.id.in_(nest_backed_operation_ids))

    # Resolve the operations this rebuild will destroy BEFORE anything is deleted, so
    # the material-tie guard can run first. Deleting an operation out from under posted
    # consumption would orphan the ISSUE rows that carry its lot genealogy, so a wipe
    # touching a CONSUMED allocation raises MaterialAllocationConsumedError (-> 409) and
    # nothing is destroyed; untouched ties are CANCELLED with an audit row. Computing
    # the ids here rather than after the package delete is equivalent -- the package
    # cascade removes nests, never operations.
    wipe_operation_ids = [
        row[0]
        for row in db.query(WorkOrderOperation.id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == child_work_order.id,
            operation_wipe_filter,
        )
        .all()
    ]
    cancel_allocations_for_operations(
        db,
        work_order=child_work_order,
        operation_ids=wipe_operation_ids,
        company_id=company_id,
        audit=audit,
    )

    existing_packages = (
        db.query(LaserNestPackage)
        .filter(
            LaserNestPackage.company_id == company_id,
            LaserNestPackage.child_work_order_id == child_work_order.id,
        )
        .all()
    )
    for package in existing_packages:
        db.delete(package)
    db.flush()

    existing_operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.id.in_(wipe_operation_ids),
        )
        .all()
        if wipe_operation_ids
        else []
    )
    for operation in existing_operations:
        db.delete(operation)
    db.flush()

    package = LaserNestPackage(
        company_id=company_id,
        parent_work_order_id=parent_work_order.id if parent_work_order is not None else None,
        child_work_order_id=child_work_order.id,
        package_name=package_name,
        source_path=package_source_path,
        import_status="imported",
        created_by=created_by,
    )
    db.add(package)
    db.flush()

    overrides = row_work_centers or {}
    material_parts = row_material_parts or {}
    for index, nest in enumerate(nests, start=1):
        sequence = index * 10
        if nest.work_center_id:
            op_work_center = overrides.get(nest.work_center_id)
            if op_work_center is None:
                raise ValueError(f"Unresolved nest work-center override: {nest.work_center_id}")
        else:
            op_work_center = laser_work_center
        operation = WorkOrderOperation(
            company_id=company_id,
            work_order_id=child_work_order.id,
            work_center_id=op_work_center.id,
            sequence=sequence,
            operation_number=f"Nest {index}",
            name=f"Laser Cut - {nest.nest_name}",
            description=_laser_operation_description(nest),
            component_quantity=float(nest.planned_runs),
            setup_time_hours=0.0,
            run_time_hours=0.0,
            run_time_per_piece=0.0,
            # Laser WOs are DISPATCH POOLS, not routings: every nest is startable
            # (and kiosk-visible) immediately, so all nest ops are born READY.
            # The distinct sequence values stay -- labels ("Nest N") and stable
            # ordering depend on them -- but carry no precedence semantics (see
            # work_order_state_service.is_laser_dispatch_work_order).
            status=OperationStatus.READY,
            operation_group=get_work_center_group(op_work_center),
        )
        db.add(operation)
        db.flush()

        # Optional MATERIAL TIE for this nest. Runs after the operation flush (the
        # tie is scoped to operation.id) and, critically, after the cancel+wipe
        # above, so the OPEN partial unique index can never see two live rows for
        # the same (company, operation, part). A nest with no material_part_id
        # creates NOTHING -- no allocation row, no audit row.
        if nest.material_part_id:
            material_part = material_parts.get(nest.material_part_id)
            if material_part is None:
                raise ValueError(f"Unresolved nest material part: {nest.material_part_id}")
            create_nest_material_allocation(
                db,
                work_order=child_work_order,
                operation=operation,
                part=material_part,
                qty_per_run=nest.qty_per_run,
                planned_runs=nest.planned_runs,
                company_id=company_id,
                created_by=created_by,
                audit=audit,
            )

        # PDF nests carry their source bytes: store them as a DRAWING Document
        # and attach it via document_id. Scoped to the parent WO in the classic
        # flow; to the standalone laser WO itself when there is no parent.
        # CNC-file nests have no pdf_source_path, so this is a no-op for the
        # legacy import path.
        document = _create_nest_document(
            db,
            nest=nest,
            work_order=parent_work_order if parent_work_order is not None else child_work_order,
            company_id=company_id,
            created_by=created_by,
            saved_storage_keys=saved_storage_keys,
        )

        # Re-normalized rather than trusted: these rows can arrive from the
        # planner-EDITED import payload, not only from the extraction mapper
        # (which already normalized). Cheap, idempotent, and it closes the one
        # path by which a hand-typed spelling could still reach the column.
        nest_material, nest_thickness, nest_sheet_size = normalize_nest_descriptors(
            nest.material, nest.thickness, nest.sheet_size
        )
        db.add(
            LaserNest(
                company_id=company_id,
                package_id=package.id,
                work_order_operation_id=operation.id,
                nest_name=nest.nest_name,
                cnc_number=nest.cnc_number,
                cnc_file_name=nest.cnc_file_name,
                cnc_file_path=nest.cnc_file_path,
                document_id=document.id if document is not None else None,
                planned_runs=nest.planned_runs,
                completed_runs=0,
                material=nest_material,
                thickness=nest_thickness,
                sheet_size=nest_sheet_size,
            )
        )

    # Flush so EVERY just-added nest is persisted before the caller queries them
    # back (the PDF-import path SELECTs the package's nests to write one audit
    # CREATE row each). The session uses autoflush=False, so without this the
    # SELECT would miss the last nest -- silently dropping its audit row.
    db.flush()

    total_runs = sum(nest.planned_runs for nest in nests)
    child_work_order.quantity_ordered = float(total_runs or 1)
    # Standalone nest WOs stay parent-less: never write a parent linkage (and
    # never self-reference) when there is no parent assembly WO.
    if parent_work_order is not None:
        child_work_order.parent_work_order_id = parent_work_order.id
    child_work_order.work_order_type = WorkOrderType.LASER_CUTTING.value
    return package


def sync_laser_nest_from_operation(operation: WorkOrderOperation) -> None:
    if operation.laser_nest:
        operation.laser_nest.completed_runs = float(operation.quantity_complete or 0)


def active_laser_nest(operation: WorkOrderOperation) -> Optional[LaserNest]:
    """Return the operation's laser nest only if it is not soft-deleted.

    ``WorkOrderOperation.laser_nest`` is a ``uselist=False`` relationship that
    eagerly loads whatever row points at the operation -- including a
    soft-deleted one. Serialization paths that surface a nest to a
    WorkOrderResponse or to the operator queue MUST route through this accessor
    so a soft-deleted manual nest never leaks back into the UI, the operator
    queue, or quantity rollups.
    """
    nest = operation.laser_nest
    if nest is None or getattr(nest, "is_deleted", False):
        return None
    return nest


def _recompute_child_quantity_ordered(db: Session, child_work_order: WorkOrder, company_id: int) -> float:
    """Set the child laser WO's ``quantity_ordered`` to the sum of planned runs.

    Sums ``planned_runs`` over the child's NON-deleted nests only (soft-deleted
    nests must not contribute to the rollup). Floors at 1 so a child WO never
    drops to a zero ordered quantity. Returns the new value.
    """
    # Flush pending in-memory nest changes (planned_runs edit / soft-delete flag)
    # so the aggregate SELECT below reflects them even when autoflush is off.
    db.flush()
    total = (
        db.query(func.coalesce(func.sum(LaserNest.planned_runs), 0))
        .join(WorkOrderOperation, LaserNest.work_order_operation_id == WorkOrderOperation.id)
        .filter(
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == False,  # noqa: E712
            WorkOrderOperation.work_order_id == child_work_order.id,
        )
        .scalar()
    )
    child_work_order.quantity_ordered = float(total or 0) or 1.0
    return child_work_order.quantity_ordered


def _manual_operation_description(
    *,
    cnc_number: Optional[str],
    planned_runs: int,
    material: Optional[str],
    thickness: Optional[str],
    sheet_size: Optional[str],
) -> str:
    """Mirror ``_laser_operation_description`` for a manually-keyed nest."""
    parts = []
    if cnc_number:
        parts.append(f"CNC#: {cnc_number}")
    parts.append(f"Planned runs: {planned_runs}")
    if material:
        parts.append(f"Material: {material}")
    if thickness:
        parts.append(f"Thickness: {thickness}")
    if sheet_size:
        parts.append(f"Sheet: {sheet_size}")
    return " | ".join(parts)


def create_manual_laser_nest(
    db: Session,
    *,
    parent_work_order: Optional[WorkOrder],
    child_work_order: WorkOrder,
    laser_work_center: WorkCenter,
    data: "LaserNestManualCreate | dict",
    company_id: int,
    user_id: Optional[int],
) -> LaserNest:
    """Append one manually-keyed laser nest (and its shop-floor operation).

    Standalone creation path -- it does NOT touch existing import behavior. The
    caller (a thin endpoint) has already resolved the child laser WO and the
    laser work center via the endpoint-local helpers and hands them in here.
    ``parent_work_order`` is ``None`` when the target is a standalone nest WO
    (a part-less laser-cutting WO with no parent assembly).

    All manual nests for a parent (or for one standalone nest WO) live under
    ONE reusable "Manual entry" package (``source_path IS NULL``); each call
    appends one operation + one nest to the laser WO and re-derives its
    ordered quantity.
    """
    payload = data if isinstance(data, dict) else data.model_dump()
    cnc_number = (payload.get("cnc_number") or "").strip()
    planned_runs = int(payload.get("planned_runs") or 1)
    nest_name = (payload.get("nest_name") or "").strip() or cnc_number
    material, thickness, sheet_size = normalize_nest_descriptors(
        payload.get("material"), payload.get("thickness"), payload.get("sheet_size")
    )

    # Find or create the single reusable "Manual entry" package on this parent/child.
    # Standalone nest WOs carry parent_work_order_id IS NULL on their packages.
    parent_filter = (
        LaserNestPackage.parent_work_order_id == parent_work_order.id
        if parent_work_order is not None
        else LaserNestPackage.parent_work_order_id.is_(None)
    )
    package = (
        db.query(LaserNestPackage)
        .filter(
            LaserNestPackage.company_id == company_id,
            parent_filter,
            LaserNestPackage.child_work_order_id == child_work_order.id,
            LaserNestPackage.package_name == "Manual entry",
            LaserNestPackage.source_path.is_(None),
        )
        .first()
    )
    if package is None:
        package = LaserNestPackage(
            company_id=company_id,
            parent_work_order_id=parent_work_order.id if parent_work_order is not None else None,
            child_work_order_id=child_work_order.id,
            package_name="Manual entry",
            source_path=None,
            import_status="imported",
            created_by=user_id,
        )
        db.add(package)
        db.flush()

    # Next LASER sequence on the child = current max LASER sequence + 10 (default 10).
    max_sequence = (
        db.query(func.max(WorkOrderOperation.sequence))
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == child_work_order.id,
            WorkOrderOperation.operation_group == "LASER",
        )
        .scalar()
    )
    sequence = int(max_sequence or 0) + 10

    operation = WorkOrderOperation(
        company_id=company_id,
        work_order_id=child_work_order.id,
        work_center_id=laser_work_center.id,
        sequence=sequence,
        operation_number=f"Nest {sequence // 10}",
        name=f"Laser Cut - {nest_name}",
        description=_manual_operation_description(
            cnc_number=cnc_number,
            planned_runs=planned_runs,
            material=material,
            thickness=thickness,
            sheet_size=sheet_size,
        ),
        component_quantity=float(planned_runs),
        setup_time_hours=0.0,
        run_time_hours=0.0,
        run_time_per_piece=0.0,
        # Laser WOs are DISPATCH POOLS, not routings: every nest -- manual ones
        # included -- is startable (and kiosk-visible) immediately, so nest ops
        # are born READY regardless of how many nests already exist (see
        # work_order_state_service.is_laser_dispatch_work_order).
        status=OperationStatus.READY,
        operation_group="LASER",
    )
    db.add(operation)
    db.flush()

    nest = LaserNest(
        company_id=company_id,
        package_id=package.id,
        work_order_operation_id=operation.id,
        nest_name=nest_name,
        cnc_number=cnc_number or None,
        cnc_file_name=None,
        cnc_file_path=None,
        document_id=None,
        planned_runs=planned_runs,
        completed_runs=0,
        material=material,
        thickness=thickness,
        sheet_size=sheet_size,
    )
    db.add(nest)
    db.flush()

    _recompute_child_quantity_ordered(db, child_work_order, company_id)
    return nest


def sync_laser_nest_to_operation(db: Session, nest: LaserNest) -> None:
    """Reverse of ``sync_laser_nest_from_operation``: push planned_runs forward.

    On a planned_runs edit, set the backing operation's ``component_quantity`` to
    the new planned run count and re-derive the child laser WO's ordered quantity
    over its non-deleted nests.
    """
    operation = nest.operation
    if operation is None:
        return
    operation.component_quantity = float(nest.planned_runs or 0)

    child_work_order = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == operation.work_order_id, WorkOrder.company_id == nest.company_id)
        .first()
    )
    if child_work_order is not None:
        _recompute_child_quantity_ordered(db, child_work_order, nest.company_id)


def manual_nest_response_dict(nest: LaserNest) -> dict:
    """Serialize a nest into the LaserNestManualResponse shape.

    Returns the nest id + its backing operation (id + status) so the client can
    render the nest as a clock-in-able operation, plus document attachment state.
    """
    operation = nest.operation
    return {
        "id": nest.id,
        "nest_name": nest.nest_name,
        "cnc_number": nest.cnc_number,
        "planned_runs": nest.planned_runs,
        "completed_runs": float(nest.completed_runs or 0),
        "remaining_runs": nest.remaining_runs,
        "material": nest.material,
        "thickness": nest.thickness,
        "sheet_size": nest.sheet_size,
        "work_order_operation_id": nest.work_order_operation_id,
        "operation_status": operation.status if operation is not None else None,
        "document_id": nest.document_id,
        "has_document": bool(nest.document_id),
        "document_file_name": nest.document.file_name if nest.document else None,
    }


def soft_delete_laser_nest(db: Session, nest: LaserNest, user_id: Optional[int]) -> None:
    """Soft-delete a manual nest and deactivate its operation.

    Sets the backing operation to ON_HOLD: ``OperationStatus`` has no op-level
    CANCELLED, and ON_HOLD is the closest inactive/terminal state. ON_HOLD
    removes the op from the work-center queue, which filters
    ``status.in_([READY, IN_PROGRESS])``. Never hard-deletes -- traceability and
    the package's run history must survive.
    """
    nest.soft_delete(user_id)
    operation = nest.operation
    if operation is not None:
        operation.status = OperationStatus.ON_HOLD

    child_work_order = None
    if operation is not None:
        child_work_order = (
            db.query(WorkOrder)
            .filter(WorkOrder.id == operation.work_order_id, WorkOrder.company_id == nest.company_id)
            .first()
        )
    if child_work_order is not None:
        _recompute_child_quantity_ordered(db, child_work_order, nest.company_id)


def _iter_cnc_files(root: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in CNC_EXTENSIONS:
            continue
        rel_path = str(path.relative_to(root))
        yield path, rel_path


def _parse_entries(entries: Iterable[tuple[Path, str]]) -> list[ParsedLaserNest]:
    nests = []
    for path, rel_path in entries:
        nests.append(_parse_filename(path.name, rel_path))
    if not nests:
        raise ValueError("No CNC files found in package")
    return nests


def _parse_filename(file_name: str, rel_path: str) -> ParsedLaserNest:
    stem = Path(file_name).stem
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    planned_runs = _infer_planned_runs(cleaned)
    nest_name = _infer_nest_name(cleaned)
    return ParsedLaserNest(
        nest_name=nest_name,
        cnc_file_name=file_name,
        cnc_file_path=rel_path,
        planned_runs=planned_runs,
        # The filename-inference fallback goes through the same canonicalizer as
        # the AI path, so a CNC-file nest and a PDF nest describing the same
        # sheet land on one grouping key.
        material=normalize_material(_infer_material(cleaned)),
        thickness=normalize_thickness(_infer_thickness(cleaned)),
        sheet_size=normalize_sheet_size(_infer_sheet_size(cleaned)),
    )


def _infer_planned_runs(text: str) -> int:
    patterns = [
        r"(?:^|\s)(?:runs?|qty|quantity|sheets?)\s*[:#-]?\s*(\d{1,4})(?:\s|$)",
        r"(?:^|\s)(\d{1,4})\s*x(?:\s|$)",
        r"(?:^|\s)x\s*(\d{1,4})(?:\s|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))
    return 1


def _infer_nest_name(text: str) -> str:
    name = re.sub(r"(?:runs?|qty|quantity|sheets?)\s*[:#-]?\s*\d{1,4}", "", text, flags=re.IGNORECASE)
    name = re.sub(r"(?:^|\s)\d{1,4}\s*x(?:\s|$)", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"(?:^|\s)x\s*\d{1,4}(?:\s|$)", " ", name, flags=re.IGNORECASE)
    name = " ".join(name.split())
    return name or text or "Laser Nest"


def _infer_material(text: str) -> Optional[str]:
    material_patterns = [
        r"\b(A36|A572|A514|AR400|AR500|SS304|SS316|304SS|316SS|AL5052|AL6061|CRS|HRS)\b",
        r"\b(Aluminum|Aluminium|Stainless|Mild Steel|Carbon Steel)\b",
    ]
    for pattern in material_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _infer_thickness(text: str) -> Optional[str]:
    match = re.search(r"\b(\d+(?:\.\d+)?\s*(?:ga|gauge|in|inch|mm))\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "")
    return None


def _infer_sheet_size(text: str) -> Optional[str]:
    match = re.search(r"\b(\d{2,3})\s*[xX]\s*(\d{2,3})\b", text)
    if match:
        return f"{match.group(1)}x{match.group(2)}"
    return None


def _laser_operation_description(nest: ParsedLaserNest) -> str:
    parts = [f"CNC: {nest.cnc_file_name}", f"Planned runs: {nest.planned_runs}"]
    if nest.material:
        parts.append(f"Material: {nest.material}")
    if nest.thickness:
        parts.append(f"Thickness: {nest.thickness}")
    if nest.sheet_size:
        parts.append(f"Sheet: {nest.sheet_size}")
    return " | ".join(parts)


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ValueError("Zip package contains an unsafe path") from exc
    archive.extractall(destination)
