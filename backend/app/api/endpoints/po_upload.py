"""
Purchase Order PDF Upload and Extraction API
"""
import os
import logging
from typing import Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.api.deps import get_current_user, require_role
from app.models.user import User, UserRole
from app.models.purchasing import Vendor, PurchaseOrder, PurchaseOrderLine, POStatus
from app.models.part import Part
from app.models.audit_log import AuditLog
from app.schemas.po_upload import (
    POExtractionResult, POCreateFromUpload, POUploadResponse,
    VendorExtracted, LineItemExtracted
)
from app.services.pdf_service import (
    extract_text_from_document, save_uploaded_document, move_pdf_to_po,
    SUPPORTED_EXTENSIONS
)
from app.services.llm_service import extract_po_data_with_llm, validate_extracted_data
from app.services.matching_service import match_vendor, match_po_line_items, check_po_number_exists

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = [
    'application/pdf',
    'application/msword',  # .doc
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',  # .docx
]


def log_audit(db: Session, user_id: int, action: str, resource_type: str, resource_id: int, details: str):
    """Create audit log entry."""
    audit = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        description=details,
        ip_address="system"
    )
    db.add(audit)


@router.post("/upload-po", response_model=POExtractionResult)
async def upload_and_extract_po(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Upload a purchase order PDF or Word document and extract data using AI.
    Supports: .pdf, .doc, .docx
    Returns extracted data for user review before committing.
    """
    # Validate file type by extension
    filename_lower = file.filename.lower()
    file_ext = '.' + filename_lower.rsplit('.', 1)[-1] if '.' in filename_lower else ''
    
    if file_ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported file type. Allowed: PDF, DOC, DOCX"
        )
    
    # Read file content
    content = await file.read()
    
    # Validate file size
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB")
    
    # Save document
    doc_path = save_uploaded_document(content, file.filename)
    
    try:
        # Extract text from document (PDF or Word)
        logger.info(f"[PO_UPLOAD] Calling extract_text_from_document for: {doc_path}")
        extraction_result = extract_text_from_document(doc_path)
        logger.info(f"[PO_UPLOAD] Extraction result - file_type: {extraction_result.file_type}, confidence: {extraction_result.confidence}, text_length: {len(extraction_result.text)}")
        
        if not extraction_result.text or len(extraction_result.text.strip()) < 50:
            # Provide specific error message based on file type
            if extraction_result.file_type in ['docx', 'doc']:
                error_msg = "Could not extract text from Word document. The document may be empty, password-protected, or corrupted."
            elif extraction_result.file_type == 'pdf':
                error_msg = "Could not extract text from PDF. The file may be empty, corrupted, or image-only without OCR support."
            else:
                error_msg = "Could not extract text from document. Unsupported file format."
            
            logger.warning(f"[PO_UPLOAD] Extraction yielded insufficient text: {len(extraction_result.text)} chars")
            
            return POExtractionResult(
                extraction_confidence="low",
                pdf_was_ocr=extraction_result.is_ocr,
                pdf_page_count=extraction_result.page_count,
                pdf_path=doc_path,
                validation_issues=[{
                    "field": "document_content",
                    "severity": "error",
                    "message": error_msg
                }]
            )
        
        # Extract structured data using LLM
        llm_result = extract_po_data_with_llm(
            extraction_result.text, 
            is_ocr=extraction_result.is_ocr
        )
        
        # Check for LLM errors
        if "_error" in llm_result:
            return POExtractionResult(
                extraction_confidence="low",
                pdf_was_ocr=extraction_result.is_ocr,
                pdf_page_count=extraction_result.page_count,
                pdf_path=doc_path,
                validation_issues=[{
                    "field": "extraction",
                    "severity": "error",
                    "message": llm_result["_error"]
                }]
            )
        
        # Match vendor
        vendor_name = llm_result.get("vendor", {}).get("name", "")
        vendor_match = match_vendor(vendor_name, db)
        
        # Match line items to parts
        line_items = llm_result.get("line_items", [])
        matched_items = match_po_line_items(line_items, db)
        
        # Check if PO number already exists
        po_number = llm_result.get("po_number", "")
        po_exists = check_po_number_exists(po_number, db) if po_number else False
        
        # Validate extracted data
        validation_issues = validate_extracted_data(llm_result)
        
        # Add PO exists warning
        if po_exists:
            validation_issues.insert(0, {
                "field": "po_number",
                "severity": "warning",
                "message": f"PO number '{po_number}' already exists in the system"
            })
        
        # Build response
        vendor_data = llm_result.get("vendor", {})
        
        result = POExtractionResult(
            po_number=po_number,
            vendor=VendorExtracted(
                name=vendor_data.get("name"),
                address=vendor_data.get("address")
            ),
            vendor_match=vendor_match.to_dict(),
            matched_vendor_id=vendor_match.match_id,
            order_date=llm_result.get("order_date"),
            expected_delivery_date=llm_result.get("expected_delivery_date"),
            required_date=llm_result.get("required_date"),
            payment_terms=llm_result.get("payment_terms"),
            shipping_method=llm_result.get("shipping_method"),
            ship_to=llm_result.get("ship_to"),
            line_items=[
                LineItemExtracted(
                    line_number=item.get("line_number", i+1),
                    part_number=item.get("part_number"),
                    description=item.get("description"),
                    qty_ordered=item.get("qty_ordered", 0),
                    unit_of_measure=item.get("unit_of_measure", "EA"),
                    unit_price=item.get("unit_price", 0),
                    line_total=item.get("line_total", 0),
                    confidence=item.get("confidence", "medium"),
                    part_match=item.get("part_match"),
                    matched_part_id=item.get("matched_part_id")
                )
                for i, item in enumerate(matched_items)
            ],
            subtotal=llm_result.get("subtotal"),
            tax=llm_result.get("tax"),
            shipping_cost=llm_result.get("shipping_cost"),
            total_amount=llm_result.get("total_amount"),
            notes=llm_result.get("notes"),
            extraction_confidence=llm_result.get("extraction_confidence", "medium"),
            pdf_was_ocr=extraction_result.is_ocr,
            pdf_page_count=extraction_result.page_count,
            pdf_path=doc_path,
            validation_issues=validation_issues,
            po_number_exists=po_exists
        )
        
        # Audit log
        log_audit(
            db, current_user.id, "PO_DOC_UPLOAD", "purchase_order", 0,
            f"Uploaded PO document: {file.filename}, extracted {len(matched_items)} line items"
        )
        db.commit()
        
        return result
        
    except Exception as e:
        logger.error(f"PO extraction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")


@router.post("/create-from-upload", response_model=POUploadResponse)
def create_po_from_upload(
    data: POCreateFromUpload,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    """
    Create a purchase order from extracted and reviewed data.
    """
    vendor_id = data.vendor_id
    vendor_created = False
    parts_created = 0
    
    # Check PO number doesn't exist
    if check_po_number_exists(data.po_number, db):
        raise HTTPException(status_code=400, detail=f"PO number '{data.po_number}' already exists")
    
    # Create vendor if needed
    if data.create_vendor and data.new_vendor_name:
        # Generate vendor code if not provided
        vendor_code = data.new_vendor_code
        if not vendor_code:
            vendor_code = "V-" + "".join(c for c in data.new_vendor_name[:10].upper() if c.isalnum())
            # Ensure unique
            base_code = vendor_code
            counter = 1
            while db.query(Vendor).filter(Vendor.code == vendor_code).first():
                vendor_code = f"{base_code}-{counter}"
                counter += 1
        
        new_vendor = Vendor(
            code=vendor_code,
            name=data.new_vendor_name,
            address_line1=data.new_vendor_address,
            is_active=True,
            is_approved=False
        )
        db.add(new_vendor)
        db.flush()
        vendor_id = new_vendor.id
        vendor_created = True
    
    # Verify vendor exists
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=400, detail="Vendor not found")
    
    # Create any new parts
    from app.models.part import PartType
    part_id_map = {}  # Maps part_number to part_id for new parts
    for part_data in data.create_parts:
        new_part = Part(
            part_number=part_data.get("part_number"),
            name=part_data.get("description", part_data.get("part_number")),
            description=part_data.get("description"),
            part_type=PartType.PURCHASED,  # Parts from PO are purchased
            is_active=True,
            status="active"
        )
        db.add(new_part)
        db.flush()
        part_id_map[part_data.get("part_number")] = new_part.id
        parts_created += 1
    
    # Validate all line items have valid part_ids
    for item in data.line_items:
        if not item.part_id:
            # Check if we created this part
            if item.part_number in part_id_map:
                item.part_id = part_id_map[item.part_number]
            else:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Part '{item.part_number}' not found and not in create list"
                )
    
    # Create PO
    po = PurchaseOrder(
        po_number=data.po_number,
        vendor_id=vendor_id,
        status=POStatus.DRAFT,
        order_date=data.order_date,
        required_date=data.required_date,
        expected_date=data.expected_date,
        shipping_method=data.shipping_method,
        ship_to=data.ship_to,
        notes=data.notes,
        source_document_path=data.pdf_path,
        created_by=current_user.id
    )
    db.add(po)
    db.flush()
    
    # Create line items
    subtotal = 0.0
    for i, item in enumerate(data.line_items):
        line_total = item.line_total or (item.quantity_ordered * item.unit_price)
        
        po_line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=i + 1,
            part_id=item.part_id,
            quantity_ordered=item.quantity_ordered,
            unit_price=item.unit_price,
            line_total=line_total,
            notes=item.notes
        )
        db.add(po_line)
        subtotal += line_total
    
    # Update PO totals
    po.subtotal = subtotal
    po.total = subtotal + (po.tax or 0) + (po.shipping or 0)
    
    # Move PDF to PO directory
    if data.pdf_path:
        new_path = move_pdf_to_po(data.pdf_path, po.id)
        po.source_document_path = new_path
    
    # Audit log
    log_audit(
        db, current_user.id, "PO_CREATE_FROM_UPLOAD", "purchase_order", po.id,
        f"Created PO {po.po_number} from uploaded PDF with {len(data.line_items)} lines"
    )
    
    db.commit()
    
    return POUploadResponse(
        success=True,
        po_id=po.id,
        po_number=po.po_number,
        message=f"PO {po.po_number} created successfully",
        lines_created=len(data.line_items),
        vendor_created=vendor_created,
        parts_created=parts_created
    )


@router.get("/pdf/{path:path}")
def get_uploaded_pdf(
    path: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Serve uploaded PDF file for preview."""
    import os
    
    # Security: ensure path is within uploads directory
    full_path = os.path.join("uploads", "purchase_orders", path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    
    # Verify path doesn't escape uploads directory
    real_path = os.path.realpath(full_path)
    uploads_dir = os.path.realpath("uploads")
    if not real_path.startswith(uploads_dir):
        raise HTTPException(status_code=403, detail="Access denied")
    
    return FileResponse(full_path, media_type="application/pdf")


@router.get("/search-parts")
def search_parts(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search parts for matching during PO review."""
    parts = db.query(Part).filter(
        Part.is_active == True,
        Part.part_number.ilike(f"%{q}%")
    ).limit(limit).all()
    
    return [
        {
            "id": p.id,
            "part_number": p.part_number,
            "name": p.name,
            "description": p.description
        }
        for p in parts
    ]


@router.get("/search-vendors")
def search_vendors(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search vendors for matching during PO review."""
    vendors = db.query(Vendor).filter(
        Vendor.is_active == True,
        Vendor.name.ilike(f"%{q}%")
    ).limit(limit).all()
    
    return [
        {
            "id": v.id,
            "code": v.code,
            "name": v.name
        }
        for v in vendors
    ]
