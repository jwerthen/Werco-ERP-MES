"""Per-nest laser-nest endpoints: edit, soft-delete, and PDF attach/detach/preview.

Manual laser-nest CREATE lives on the work-orders router
(``POST /work-orders/{id}/laser-nests/manual``) because it is scoped under a
parent work order. The per-nest routes here are addressed by nest id and do not
fit the ``/work-orders`` prefix, so they live under their own ``/laser-nests``
mount (see ``app/api/router.py``).
"""

import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.document import Document
from app.models.laser_nest import LaserNest
from app.models.user import User, UserRole
from app.schemas.work_order import (
    LaserNestAttachDocument,
    LaserNestManualResponse,
    LaserNestPdfExtractionResponse,
    LaserNestUpdate,
)
from app.services.audit_service import AuditService
from app.services.laser_nest_extraction_service import extract_nest_fields_from_pdf
from app.services.laser_nest_service import (
    manual_nest_response_dict,
    soft_delete_laser_nest,
    sync_laser_nest_to_operation,
)
from app.services.storage_service import is_s3_ref, open_ref_stream, ref_exists

router = APIRouter()

# RBAC: mutating laser-nest actions are limited to the same trio that may create
# them. The inline PDF preview (GET .../document) is intentionally readable by
# any authenticated user -- operators need to view the shop drawing.
_NEST_WRITE_ROLES = require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR])


@router.post("/extract", response_model=LaserNestPdfExtractionResponse)
async def extract_laser_nest_from_pdf(
    file: UploadFile = File(...),
    current_user: User = Depends(_NEST_WRITE_ROLES),
    company_id: int = Depends(get_current_company_id),
):
    """Auto-extract nest fields (CNC #, material, size) from a single nest PDF.

    Stateless: no DB write, no audit. Feeds the manual-modal auto-fill -- the
    planner still verifies and saves. ``company_id`` is passed through for
    tenant-scoped AI-usage telemetry on the underlying ``run_llm_task`` call.

    Declared as a static literal ``/extract`` POST so it is matched ahead of the
    dynamic ``/{laser_nest_id}`` routes.
    """
    file_name = file.filename or "nest.pdf"
    is_pdf = file.content_type == "application/pdf" or file_name.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=400, detail="Only PDF files are supported for nest extraction")

    # extract_nest_fields_from_pdf is sync + blocking (native-PDF document block,
    # or flattened-text fallback, + LLM call); run it off the event loop. It NEVER
    # raises -- a bad PDF degrades to a filename-only result with a warning.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
            # Capture the path BEFORE the body read, so a failed read still hits
            # the finally cleanup and never leaks the temp file.
            temp_path = temp.name
            temp.write(await file.read())
        result = await run_in_threadpool(extract_nest_fields_from_pdf, temp_path, file_name, company_id)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    return LaserNestPdfExtractionResponse(
        cnc_number=result.get("cnc_number"),
        material=result.get("material"),
        thickness=result.get("thickness"),
        sheet_size=result.get("sheet_size"),
        planned_runs=result.get("planned_runs"),
        confidence=result.get("extraction_confidence"),
        source=result.get("source", "none"),
        warning=result.get("warning"),
        passes=result.get("passes"),
    )


def _load_nest(db: Session, laser_nest_id: int, company_id: int) -> LaserNest:
    """Load a non-deleted, tenant-scoped nest eager on its operation + document.

    Returns 404 for a missing nest, a cross-tenant nest, OR a soft-deleted nest
    (a soft-deleted nest is treated as gone for all per-nest operations).
    """
    nest = (
        db.query(LaserNest)
        .options(joinedload(LaserNest.operation), joinedload(LaserNest.document))
        .filter(
            LaserNest.id == laser_nest_id,
            LaserNest.company_id == company_id,
            LaserNest.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not nest:
        raise HTTPException(status_code=404, detail="Laser nest not found")
    return nest


def _nest_old_values(nest: LaserNest) -> dict:
    return {
        "nest_name": nest.nest_name,
        "cnc_number": nest.cnc_number,
        "planned_runs": nest.planned_runs,
        "material": nest.material,
        "thickness": nest.thickness,
        "sheet_size": nest.sheet_size,
        "document_id": nest.document_id,
    }


@router.patch("/{laser_nest_id}", response_model=LaserNestManualResponse)
def update_laser_nest(
    laser_nest_id: int,
    payload: LaserNestUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(_NEST_WRITE_ROLES),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Edit a manual laser nest. Lowering planned_runs below completed_runs is
    allowed (over-run is acceptable); only the schema's ``ge=1`` floor applies."""
    nest = _load_nest(db, laser_nest_id, company_id)
    old_values = _nest_old_values(nest)

    fields = payload.model_dump(exclude_unset=True)
    planned_runs_changed = "planned_runs" in fields and fields["planned_runs"] != nest.planned_runs

    if "cnc_number" in fields:
        cnc = (fields["cnc_number"] or "").strip()
        nest.cnc_number = cnc or None
    if "nest_name" in fields:
        nest.nest_name = (fields["nest_name"] or "").strip() or nest.nest_name
    if "planned_runs" in fields and fields["planned_runs"] is not None:
        nest.planned_runs = fields["planned_runs"]
    if "material" in fields:
        nest.material = fields["material"]
    if "thickness" in fields:
        nest.thickness = fields["thickness"]
    if "sheet_size" in fields:
        nest.sheet_size = fields["sheet_size"]

    if planned_runs_changed:
        # Reverse-sync the operation's component_quantity + child WO rollup.
        sync_laser_nest_to_operation(db, nest)

    audit.log_update(
        resource_type="laser_nest",
        resource_id=nest.id,
        resource_identifier=nest.cnc_number or nest.nest_name,
        old_values=old_values,
        new_values=_nest_old_values(nest),
    )
    db.commit()
    db.refresh(nest)
    return LaserNestManualResponse(**manual_nest_response_dict(nest))


@router.post("/{laser_nest_id}/attach-document", response_model=LaserNestManualResponse)
def attach_laser_nest_document(
    laser_nest_id: int,
    payload: LaserNestAttachDocument,
    db: Session = Depends(get_db),
    current_user: User = Depends(_NEST_WRITE_ROLES),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Attach an already-uploaded PDF Document (from /documents/upload) to a nest.

    PDF-only: matches the validation used by the work-order-drawing attach path.
    """
    nest = _load_nest(db, laser_nest_id, company_id)
    document = db.query(Document).filter(Document.id == payload.document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    file_name = document.file_name or ""
    is_pdf = document.mime_type == "application/pdf" or file_name.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=400, detail="Only PDF documents can be attached to a laser nest")

    old_document_id = nest.document_id
    nest.document_id = document.id

    audit.log_update(
        resource_type="laser_nest",
        resource_id=nest.id,
        resource_identifier=nest.cnc_number or nest.nest_name,
        old_values={"document_id": old_document_id},
        new_values={"document_id": nest.document_id},
        description=f"Attached drawing to laser nest: {nest.cnc_number or nest.nest_name}",
    )
    db.commit()
    db.refresh(nest)
    return LaserNestManualResponse(**manual_nest_response_dict(nest))


@router.delete("/{laser_nest_id}/document", response_model=LaserNestManualResponse)
def detach_laser_nest_document(
    laser_nest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_NEST_WRITE_ROLES),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Detach the attached PDF from a nest. Only clears the FK -- the Document row
    (and its stored bytes) is left intact."""
    nest = _load_nest(db, laser_nest_id, company_id)
    old_document_id = nest.document_id
    nest.document_id = None

    audit.log_update(
        resource_type="laser_nest",
        resource_id=nest.id,
        resource_identifier=nest.cnc_number or nest.nest_name,
        old_values={"document_id": old_document_id},
        new_values={"document_id": None},
        description=f"Detached drawing from laser nest: {nest.cnc_number or nest.nest_name}",
    )
    db.commit()
    db.refresh(nest)
    return LaserNestManualResponse(**manual_nest_response_dict(nest))


@router.get("/{laser_nest_id}/document")
def get_laser_nest_document(
    laser_nest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Serve the attached PDF INLINE for operator preview.

    Read-only and intentionally available to ANY authenticated user (operators
    must preview the shop drawing), but still strictly tenant-scoped: the nest
    AND the document are both filtered by company_id, so no cross-tenant bytes
    can be served.
    """
    nest = _load_nest(db, laser_nest_id, company_id)
    if nest.document_id is None:
        raise HTTPException(status_code=404, detail="No document attached")

    document = db.query(Document).filter(Document.id == nest.document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    filename = document.file_name or f"laser-nest-{nest.id}.pdf"
    inline_disposition = f'inline; filename="{filename}"'

    # Per-row storage dispatch mirrors documents.py download, but forces an inline
    # PDF so the operator preview can embed it rather than download it.
    if is_s3_ref(document.file_path):
        if not ref_exists(document.file_path):
            raise HTTPException(status_code=404, detail="File not found")
        return StreamingResponse(
            open_ref_stream(document.file_path),
            media_type="application/pdf",
            headers={"Content-Disposition": inline_disposition},
        )

    if not document.file_path or not os.path.exists(document.file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        document.file_path,
        media_type="application/pdf",
        headers={"Content-Disposition": inline_disposition},
    )


@router.delete("/{laser_nest_id}")
def delete_laser_nest(
    laser_nest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(_NEST_WRITE_ROLES),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Soft-delete a manual laser nest and put its operation ON_HOLD."""
    nest = _load_nest(db, laser_nest_id, company_id)
    old_values = _nest_old_values(nest)
    identifier = nest.cnc_number or nest.nest_name

    soft_delete_laser_nest(db, nest, current_user.id)

    audit.log_delete(
        resource_type="laser_nest",
        resource_id=nest.id,
        resource_identifier=identifier,
        old_values=old_values,
        soft_delete=True,
    )
    db.commit()
    return {"message": "Laser nest deleted", "id": laser_nest_id}
