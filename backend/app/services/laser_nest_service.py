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
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderType
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf
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
    non-numeric string, a float-ish string, or junk. Only an int or a digit
    string is honored (floored at 1); anything else falls back to 1 so a bad
    model value can never ``ValueError`` -> 400 the whole preview batch.
    """
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, str) and value.strip().isdigit():
        return max(1, int(value.strip()))
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
    return ParsedLaserNest(
        nest_name=cnc_number or Path(file_name).stem,
        cnc_file_name=file_name,
        cnc_file_path=rel_path,
        planned_runs=_coerce_planned_runs(result.get("planned_runs")),
        material=result.get("material"),
        thickness=result.get("thickness"),
        sheet_size=result.get("sheet_size"),
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

    operation_wipe_filter = WorkOrderOperation.operation_group == "LASER"
    if nest_backed_operation_ids:
        operation_wipe_filter = or_(operation_wipe_filter, WorkOrderOperation.id.in_(nest_backed_operation_ids))
    existing_operations = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id == child_work_order.id,
            operation_wipe_filter,
        )
        .all()
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
                material=nest.material,
                thickness=nest.thickness,
                sheet_size=nest.sheet_size,
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
    material = payload.get("material")
    thickness = payload.get("thickness")
    sheet_size = payload.get("sheet_size")

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
        material=_infer_material(cleaned),
        thickness=_infer_thickness(cleaned),
        sheet_size=_infer_sheet_size(cleaned),
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
