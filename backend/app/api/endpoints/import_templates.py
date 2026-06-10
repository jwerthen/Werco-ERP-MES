"""Server-generated XLSX import templates (A0.2 Excel migration kit).

Replaces the CSV templates the frontend used to hardcode client-side: each
entity gets a downloadable .xlsx with a styled header row, a ``#``-marked
guidance row (skipped on import), and example rows on a separate sheet.
Templates contain no tenant data — they are static workbooks — so any
authenticated user may download them.
"""

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.deps import get_current_user
from app.models.user import User
from app.services.import_service import (
    IMPORT_TEMPLATES,
    XLSX_MEDIA_TYPE,
    build_import_template_workbook,
    list_import_templates,
)

router = APIRouter()


@router.get("/templates", summary="List available import templates")
def get_import_templates(current_user: User = Depends(get_current_user)):
    """List every entity that has a downloadable XLSX import template."""
    return {"templates": list_import_templates()}


@router.get("/templates/{entity}", summary="Download the XLSX import template for an entity")
def download_import_template(entity: str, current_user: User = Depends(get_current_user)):
    """Download the styled .xlsx template (header + guidance row + examples sheet)."""
    if entity not in IMPORT_TEMPLATES:
        valid = ", ".join(sorted(IMPORT_TEMPLATES))
        raise HTTPException(status_code=404, detail=f"Unknown import template '{entity}'. Valid entities: {valid}")

    content = build_import_template_workbook(entity)
    filename = f"werco-import-template-{entity}.xlsx"
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
