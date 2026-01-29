from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import or_
import os
import uuid
from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.document import Document, DocumentType
from pydantic import BaseModel

router = APIRouter()

# Create uploads directory
UPLOAD_DIR = "/app/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


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


def generate_document_number(db: Session, doc_type: str) -> str:
    prefix = doc_type[:3].upper()
    today = datetime.now().strftime("%Y%m")
    
    last_doc = db.query(Document).filter(
        Document.document_number.like(f"{prefix}-{today}-%")
    ).order_by(Document.document_number.desc()).first()
    
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Document)
    
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
                Document.description.ilike(search_filter)
            )
        )
    
    return query.order_by(Document.created_at.desc()).limit(100).all()


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
    current_user: User = Depends(get_current_user)
):
    """Upload a new document"""
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
        document_type=DocumentType(document_type),
        description=description,
        part_id=part_id if part_id and part_id > 0 else None,
        work_order_id=work_order_id if work_order_id and work_order_id > 0 else None,
        vendor_id=vendor_id if vendor_id and vendor_id > 0 else None,
        file_name=file.filename,
        file_path=file_path,
        file_size=len(content),
        mime_type=file.content_type,
        status="released",
        created_by=current_user.id
    )
    
    db.add(document)
    db.commit()
    db.refresh(document)
    
    return document


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/{document_id}/download")
def download_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from fastapi.responses import FileResponse
    
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    if not document.file_path or not os.path.exists(document.file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(
        document.file_path,
        filename=document.file_name,
        media_type=document.mime_type
    )


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER]))
):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    
    # Delete file if exists
    if document.file_path and os.path.exists(document.file_path):
        os.remove(document.file_path)
    
    db.delete(document)
    db.commit()
    
    return {"message": "Document deleted"}


@router.get("/types/list")
def list_document_types(current_user: User = Depends(get_current_user)):
    return [{"value": t.value, "label": t.value.replace("_", " ").title()} for t in DocumentType]
