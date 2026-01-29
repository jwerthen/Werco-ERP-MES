"""
Document Processing Service for Purchase Order Extraction
Handles PDF (native + OCR) and Word document (.doc, .docx) text extraction.
"""
import os
import tempfile
import logging
from typing import Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = ['.pdf', '.doc', '.docx', '.xlsx', '.xls']


class DocumentExtractionResult:
    def __init__(self, text: str, is_ocr: bool = False, page_count: int = 0, confidence: str = "high", file_type: str = "pdf"):
        self.text = text
        self.is_ocr = is_ocr
        self.page_count = page_count
        self.confidence = confidence  # high, medium, low
        self.file_type = file_type  # pdf, docx, doc


# Alias for backward compatibility
PDFExtractionResult = DocumentExtractionResult


def extract_text_from_document(file_path: str) -> DocumentExtractionResult:
    """
    Extract text from PDF or Word document.
    Automatically detects file type and uses appropriate extraction method.
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    
    logger.info(f"[DOC_EXTRACT] Processing file: {file_path}")
    logger.info(f"[DOC_EXTRACT] Detected extension: '{ext}'")
    
    if ext == '.pdf':
        logger.info("[DOC_EXTRACT] Routing to PDF extractor")
        return extract_text_from_pdf(file_path)
    elif ext in ['.docx', '.doc']:
        logger.info(f"[DOC_EXTRACT] Routing to Word extractor for {ext}")
        return extract_text_from_word(file_path)
    elif ext in ['.xlsx', '.xls']:
        logger.info(f"[DOC_EXTRACT] Routing to Excel extractor for {ext}")
        return extract_text_from_excel(file_path)
    else:
        logger.warning(f"[DOC_EXTRACT] Unknown extension: {ext}")
        return DocumentExtractionResult(
            text="",
            confidence="low",
            file_type="unknown"
        )


def extract_text_from_word(doc_path: str) -> DocumentExtractionResult:
    """
    Extract text from Word document (.docx or .doc).
    """
    path = Path(doc_path)
    ext = path.suffix.lower()
    
    logger.info(f"[WORD] extract_text_from_word called with: {doc_path}")
    logger.info(f"[WORD] Extension detected: '{ext}'")
    
    if ext == '.docx':
        logger.info("[WORD] Calling _extract_docx_text")
        return _extract_docx_text(doc_path)
    elif ext == '.doc':
        logger.info("[WORD] Calling _extract_doc_text")
        return _extract_doc_text(doc_path)
    else:
        logger.warning(f"[WORD] Unsupported Word extension: {ext}")
        return DocumentExtractionResult(text="", confidence="low", file_type="unknown")


def extract_text_from_excel(excel_path: str) -> DocumentExtractionResult:
    """
    Extract text from Excel document (.xlsx or .xls).
    """
    path = Path(excel_path)
    ext = path.suffix.lower()

    logger.info(f"[EXCEL] extract_text_from_excel called with: {excel_path}")
    logger.info(f"[EXCEL] Extension detected: '{ext}'")

    if not os.path.exists(excel_path):
        logger.error(f"[EXCEL] File does not exist: {excel_path}")
        return DocumentExtractionResult(text="", confidence="low", file_type=ext.strip('.'))

    file_size = os.path.getsize(excel_path)
    logger.info(f"[EXCEL] File size: {file_size} bytes")
    if file_size == 0:
        logger.error("[EXCEL] File is empty (0 bytes)")
        return DocumentExtractionResult(text="", confidence="low", file_type=ext.strip('.'))

    try:
        import pandas as pd
    except ImportError as e:
        logger.error(f"[EXCEL] pandas not installed: {e}")
        return DocumentExtractionResult(text="", confidence="low", file_type=ext.strip('.'))

    try:
        sheets = pd.read_excel(excel_path, sheet_name=None, dtype=str)
        all_text = []
        for sheet_name, df in sheets.items():
            all_text.append(f"--- Sheet: {sheet_name} ---")
            df = df.fillna("")
            # Convert to pipe-delimited rows to preserve structure
            all_text.extend(
                df.astype(str).apply(lambda row: " | ".join([cell.strip() for cell in row.tolist() if cell.strip()]), axis=1).tolist()
            )
        text = "\n".join([line for line in all_text if line.strip()])
        return DocumentExtractionResult(text=text, confidence="medium", file_type=ext.strip('.'))
    except Exception as e:
        logger.error(f"[EXCEL] Extraction failed: {e}")
        return DocumentExtractionResult(text="", confidence="low", file_type=ext.strip('.'))


def _extract_docx_text(docx_path: str) -> DocumentExtractionResult:
    """Extract text from .docx file using python-docx."""
    logger.info(f"[DOCX] Starting extraction from: {docx_path}")
    
    # Verify file exists
    if not os.path.exists(docx_path):
        logger.error(f"[DOCX] File does not exist: {docx_path}")
        return DocumentExtractionResult(
            text="",
            confidence="low",
            file_type="docx"
        )
    
    file_size = os.path.getsize(docx_path)
    logger.info(f"[DOCX] File size: {file_size} bytes")
    
    if file_size == 0:
        logger.error("[DOCX] File is empty (0 bytes)")
        return DocumentExtractionResult(
            text="",
            confidence="low",
            file_type="docx"
        )
    
    try:
        from docx import Document
        logger.info("[DOCX] python-docx imported successfully")
    except ImportError as e:
        logger.error(f"[DOCX] python-docx not installed: {e}")
        return DocumentExtractionResult(
            text="",
            confidence="low",
            file_type="docx"
        )
    
    try:
        # Open the document - use absolute path to avoid issues
        abs_path = os.path.abspath(docx_path)
        logger.info(f"[DOCX] Opening document at absolute path: {abs_path}")
        
        doc = Document(abs_path)
        logger.info(f"[DOCX] Document opened successfully")
        
        all_text = []
        
        # Extract paragraphs
        para_count = len(doc.paragraphs)
        logger.info(f"[DOCX] Found {para_count} paragraphs")
        
        for para in doc.paragraphs:
            if para.text.strip():
                all_text.append(para.text.strip())
        
        logger.info(f"[DOCX] Extracted {len(all_text)} non-empty paragraphs")
        
        # Extract tables (critical for PO line items)
        table_count = len(doc.tables)
        logger.info(f"[DOCX] Found {table_count} tables")
        
        for table_idx, table in enumerate(doc.tables):
            for row_idx, row in enumerate(table.rows):
                row_text = []
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text:
                        row_text.append(cell_text)
                if row_text:
                    all_text.append("\t".join(row_text))
            logger.info(f"[DOCX] Table {table_idx + 1}: extracted {len(table.rows)} rows")
        
        text = "\n".join(all_text)
        
        logger.info(f"[DOCX] Extraction complete: {len(text)} chars total")
        
        if not text.strip():
            logger.warning("[DOCX] Document appears to be empty (no text content)")
            return DocumentExtractionResult(
                text="",
                confidence="low",
                file_type="docx"
            )
        
        return DocumentExtractionResult(
            text=text,
            is_ocr=False,
            page_count=1,
            confidence="high",
            file_type="docx"
        )
        
    except Exception as e:
        logger.error(f"[DOCX] Extraction failed with exception: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"[DOCX] Traceback: {traceback.format_exc()}")
        return DocumentExtractionResult(
            text="",
            confidence="low",
            file_type="docx"
        )


def _extract_doc_text(doc_path: str) -> DocumentExtractionResult:
    """
    Extract text from legacy .doc file.
    Tries multiple methods: antiword, catdoc, or python-docx fallback.
    """
    import subprocess
    
    logger.info(f"[DOC] Starting legacy .doc extraction from: {doc_path}")
    
    # Verify file exists and get absolute path
    abs_path = os.path.abspath(doc_path)
    if not os.path.exists(abs_path):
        logger.error(f"[DOC] File does not exist: {abs_path}")
        return DocumentExtractionResult(text="", confidence="low", file_type="doc")
    
    file_size = os.path.getsize(abs_path)
    logger.info(f"[DOC] File size: {file_size} bytes, absolute path: {abs_path}")
    
    # Method 1: Try antiword (Linux/Mac)
    logger.info("[DOC] Trying antiword...")
    try:
        result = subprocess.run(
            ['antiword', abs_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        logger.info(f"[DOC] antiword returncode: {result.returncode}, stdout length: {len(result.stdout)}, stderr: {result.stderr[:200] if result.stderr else 'none'}")
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"[DOC] antiword extraction successful: {len(result.stdout)} chars")
            return DocumentExtractionResult(
                text=result.stdout,
                is_ocr=False,
                page_count=1,
                confidence="high",
                file_type="doc"
            )
        else:
            logger.warning(f"[DOC] antiword returned empty or failed: rc={result.returncode}")
    except FileNotFoundError:
        logger.warning("[DOC] antiword not installed")
    except subprocess.TimeoutExpired:
        logger.warning("[DOC] antiword timed out")
    except Exception as e:
        logger.warning(f"[DOC] antiword exception: {type(e).__name__}: {e}")
    
    # Method 2: Try catdoc (alternative for .doc)
    logger.info("[DOC] Trying catdoc...")
    try:
        result = subprocess.run(
            ['catdoc', abs_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        logger.info(f"[DOC] catdoc returncode: {result.returncode}, stdout length: {len(result.stdout)}")
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"[DOC] catdoc extraction successful: {len(result.stdout)} chars")
            return DocumentExtractionResult(
                text=result.stdout,
                is_ocr=False,
                page_count=1,
                confidence="medium",
                file_type="doc"
            )
    except FileNotFoundError:
        logger.warning("[DOC] catdoc not installed")
    except subprocess.TimeoutExpired:
        logger.warning("[DOC] catdoc timed out")
    except Exception as e:
        logger.warning(f"[DOC] catdoc exception: {type(e).__name__}: {e}")
    
    # Method 3: Try python-docx (rarely works with legacy .doc but worth trying)
    logger.info("[DOC] Trying python-docx fallback...")
    try:
        from docx import Document
        doc = Document(abs_path)
        all_text = [para.text for para in doc.paragraphs if para.text.strip()]
        if all_text:
            text = "\n".join(all_text)
            logger.info(f"[DOC] python-docx extraction successful: {len(text)} chars")
            return DocumentExtractionResult(
                text=text,
                is_ocr=False,
                page_count=1,
                confidence="medium",
                file_type="doc"
            )
        else:
            logger.warning("[DOC] python-docx found no text")
    except Exception as e:
        logger.warning(f"[DOC] python-docx exception: {type(e).__name__}: {e}")
    
    logger.error("All DOC extraction methods failed")
    return DocumentExtractionResult(
        text="",
        confidence="low",
        file_type="doc"
    )


def extract_text_from_pdf(pdf_path: str) -> DocumentExtractionResult:
    """
    Extract text from PDF file.
    First attempts native extraction, falls back to OCR if needed.
    """
    # Try native PDF extraction first
    native_text, page_count = _extract_native_text(pdf_path)
    
    # If we got meaningful text, return it
    if native_text and len(native_text.strip()) > 100:
        logger.info(f"Native PDF extraction successful: {len(native_text)} chars from {page_count} pages")
        return DocumentExtractionResult(
            text=native_text,
            is_ocr=False,
            page_count=page_count,
            confidence="high",
            file_type="pdf"
        )
    
    # Fall back to OCR
    logger.info("Native extraction yielded insufficient text, attempting OCR...")
    ocr_text, page_count = _extract_ocr_text(pdf_path)
    
    if ocr_text and len(ocr_text.strip()) > 50:
        logger.info(f"OCR extraction successful: {len(ocr_text)} chars from {page_count} pages")
        return DocumentExtractionResult(
            text=ocr_text,
            is_ocr=True,
            page_count=page_count,
            confidence="medium",
            file_type="pdf"
        )
    
    # Both methods failed
    logger.warning("Both native and OCR extraction failed or yielded minimal text")
    return DocumentExtractionResult(
        text=native_text or ocr_text or "",
        is_ocr=bool(ocr_text),
        page_count=page_count,
        confidence="low",
        file_type="pdf"
    )


def _extract_native_text(pdf_path: str) -> Tuple[str, int]:
    """Extract text using pypdf (native PDF text)."""
    try:
        from pypdf import PdfReader
        
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        
        all_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                all_text.append(f"--- Page {i+1} ---\n{text}")
        
        return "\n\n".join(all_text), page_count
    except Exception as e:
        logger.error(f"Native PDF extraction failed: {e}")
        return "", 0


def _extract_ocr_text(pdf_path: str) -> Tuple[str, int]:
    """Extract text using OCR (for scanned PDFs)."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
        
        # Convert PDF pages to images
        images = convert_from_path(pdf_path, dpi=300)
        page_count = len(images)
        
        all_text = []
        for i, image in enumerate(images):
            text = pytesseract.image_to_string(image)
            if text:
                all_text.append(f"--- Page {i+1} (OCR) ---\n{text}")
        
        return "\n\n".join(all_text), page_count
    except ImportError as e:
        logger.warning(f"OCR dependencies not available: {e}")
        return "", 0
    except Exception as e:
        logger.error(f"OCR extraction failed: {e}")
        return "", 0


def save_uploaded_document(file_content: bytes, filename: str, po_id: Optional[int] = None) -> str:
    """
    Save uploaded document (PDF or Word) to storage location.
    Returns the file path.
    """
    # Create upload directory
    base_dir = Path("uploads/purchase_orders")
    if po_id:
        upload_dir = base_dir / str(po_id)
    else:
        upload_dir = base_dir / "pending"
    
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Get original extension
    original_ext = Path(filename).suffix.lower()
    if original_ext not in SUPPORTED_EXTENSIONS:
        original_ext = '.pdf'  # Default
    
    # Sanitize filename
    stem = Path(filename).stem
    safe_stem = "".join(c for c in stem if c.isalnum() or c in "._- ")
    safe_filename = f"{safe_stem}{original_ext}"
    
    file_path = upload_dir / safe_filename
    
    # Handle duplicate filenames
    counter = 1
    original_path = file_path
    while file_path.exists():
        file_path = upload_dir / f"{safe_stem}_{counter}{original_ext}"
        counter += 1
    
    # Write file
    with open(file_path, 'wb') as f:
        f.write(file_content)
    
    logger.info(f"Saved document to {file_path}")
    return str(file_path)


# Alias for backward compatibility
def save_uploaded_pdf(file_content: bytes, filename: str, po_id: Optional[int] = None) -> str:
    return save_uploaded_document(file_content, filename, po_id)


def move_pdf_to_po(temp_path: str, po_id: int) -> str:
    """Move PDF from pending to PO-specific directory after PO creation."""
    import shutil
    
    source = Path(temp_path)
    if not source.exists():
        return temp_path
    
    dest_dir = Path(f"uploads/purchase_orders/{po_id}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    dest_path = dest_dir / source.name
    shutil.move(str(source), str(dest_path))
    
    logger.info(f"Moved PDF from {source} to {dest_path}")
    return str(dest_path)
