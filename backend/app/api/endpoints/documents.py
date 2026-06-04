import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user, require_role
from app.db.database import get_db
from app.models.document import Document, DocumentType
from app.models.part import Part
from app.models.purchasing import Vendor
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder

router = APIRouter()


def _resolve_upload_dir() -> str:
    preferred_dir = os.getenv("UPLOAD_DIR", "/app/uploads")
    try:
        os.makedirs(preferred_dir, exist_ok=True)
        return preferred_dir
    except OSError:
        # Fall back to a local writable directory for tests/dev environments.
        fallback_dir = os.path.abspath(os.getenv("UPLOAD_DIR_FALLBACK", "./uploads"))
        os.makedirs(fallback_dir, exist_ok=True)
        return fallback_dir


UPLOAD_DIR = _resolve_upload_dir()


class DocumentResponse(BaseModel):
    id: int
    document_number: str
    revision: str
    title: str
    document_type: str
    description: Optional[str] = None
    part_id: Optional[int] = None
    work_order_id: Optional[int] = None
    vendor_id: Optional[int] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    status: str
    created_at: datetime

    class Config:
        from_attributes = True
        use_enum_values = True


class WorkOrderDocumentAttachRequest(BaseModel):
    work_order_id: int


def generate_document_number(db: Session, doc_type: str) -> str:
    prefix = doc_type[:3].upper()
    today = datetime.now().strftime("%Y%m")

    last_doc = (
        db.query(Document)
        .filter(Document.document_number.like(f"{prefix}-{today}-%"))
        .order_by(Document.document_number.desc())
        .first()
    )

    if last_doc:
        last_num = int(last_doc.document_number.split("-")[-1])
        new_num = last_num + 1
    else:
        new_num = 1

    return f"{prefix}-{today}-{new_num:04d}"


@router.get("/", response_model=List[DocumentResponse])
def list_documents(
    part_id: Optional[int] = None,
    work_order_id: Optional[int] = None,
    vendor_id: Optional[int] = None,
    document_type: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(Document).filter(Document.company_id == company_id)

    if part_id:
        query = query.filter(Document.part_id == part_id)
    if work_order_id:
        query = query.filter(Document.work_order_id == work_order_id)
    if vendor_id:
        query = query.filter(Document.vendor_id == vendor_id)
    if document_type:
        query = query.filter(Document.document_type == document_type)
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Document.document_number.ilike(search_filter),
                Document.title.ilike(search_filter),
                Document.description.ilike(search_filter),
            )
        )

    return query.order_by(Document.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    document_type: str = Form(...),
    description: str = Form(None),
    part_id: int = Form(None),
    work_order_id: int = Form(None),
    vendor_id: int = Form(None),
    revision: str = Form("A"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Upload a new document"""
    try:
        parsed_document_type = DocumentType(document_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document type") from exc

    normalized_part_id = part_id if part_id and part_id > 0 else None
    normalized_work_order_id = work_order_id if work_order_id and work_order_id > 0 else None
    normalized_vendor_id = vendor_id if vendor_id and vendor_id > 0 else None

    if normalized_part_id:
        part = (
            db.query(Part)
            .filter(Part.id == normalized_part_id, Part.company_id == company_id)
            .first()
        )
        if not part:
            raise HTTPException(status_code=404, detail="Part not found")

    if normalized_work_order_id:
        work_order = (
            db.query(WorkOrder)
            .filter(WorkOrder.id == normalized_work_order_id, WorkOrder.company_id == company_id)
            .first()
        )
        if not work_order:
            raise HTTPException(status_code=404, detail="Work order not found")

    if normalized_vendor_id:
        vendor = (
            db.query(Vendor)
            .filter(Vendor.id == normalized_vendor_id, Vendor.company_id == company_id)
            .first()
        )
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")

    # Generate unique filename
    file_ext = os.path.splitext(file.filename)[1] if file.filename else ""
    unique_name = f"{uuid.uuid4()}{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    # Save file
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    # Create document record
    doc_number = generate_document_number(db, document_type)

    document = Document(
        document_number=doc_number,
        revision=revision,
        title=title,
        document_type=parsed_document_type,
        description=description,
        part_id=normalized_part_id,
        work_order_id=normalized_work_order_id,
        vendor_id=normalized_vendor_id,
        file_name=file.filename,
        file_path=file_path,
        file_size=len(content),
        mime_type=file.content_type,
        status="released",
        created_by=current_user.id,
        company_id=company_id,
    )

    db.add(document)
    db.commit()
    db.refresh(document)

    return document


@router.get("/types/list")
def list_document_types(current_user: User = Depends(get_current_user)):
    return [{"value": t.value, "label": t.value.replace("_", " ").title()} for t in DocumentType]


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    document = db.query(Document).filter(Document.id == document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/{document_id}/download")
def download_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    from fastapi.responses import FileResponse

    document = db.query(Document).filter(Document.id == document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if not document.file_path or not os.path.exists(document.file_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(document.file_path, filename=document.file_name, media_type=document.mime_type)


@router.post("/{document_id}/attach-work-order", response_model=DocumentResponse)
def attach_document_to_work_order(
    document_id: int,
    payload: WorkOrderDocumentAttachRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    document = db.query(Document).filter(Document.id == document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    work_order = (
        db.query(WorkOrder)
        .filter(WorkOrder.id == payload.work_order_id, WorkOrder.company_id == company_id)
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found")

    file_name = document.file_name or ""
    is_pdf = document.mime_type == "application/pdf" or file_name.lower().endswith(".pdf")
    if not is_pdf:
        raise HTTPException(status_code=400, detail="Only PDF documents can be attached as work order drawings")

    document.work_order_id = payload.work_order_id
    db.commit()
    db.refresh(document)
    return document


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    document = db.query(Document).filter(Document.id == document_id, Document.company_id == company_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete file if exists
    if document.file_path and os.path.exists(document.file_path):
        os.remove(document.file_path)

    db.delete(document)
    db.commit()

    return {"message": "Document deleted"}
