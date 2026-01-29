from typing import List, Optional
from pydantic import BaseModel, Field


class BOMImportAssembly(BaseModel):
    part_number: Optional[str] = None
    name: Optional[str] = None
    revision: Optional[str] = None
    description: Optional[str] = None
    drawing_number: Optional[str] = None
    part_type: Optional[str] = None


class BOMImportItem(BaseModel):
    line_number: Optional[int] = None
    part_number: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    item_type: Optional[str] = None
    line_type: Optional[str] = None
    reference_designator: Optional[str] = None
    find_number: Optional[str] = None
    notes: Optional[str] = None


class BOMImportPreviewResponse(BaseModel):
    document_type: str = Field(..., description="Detected document type: bom or part")
    assembly: BOMImportAssembly
    items: List[BOMImportItem] = Field(default_factory=list)
    extraction_confidence: str = "low"
    warnings: List[str] = Field(default_factory=list)


class BOMImportCommitRequest(BaseModel):
    document_type: Optional[str] = None
    assembly: BOMImportAssembly
    items: List[BOMImportItem] = Field(default_factory=list)
    create_missing_parts: bool = True


class BOMImportResponse(BaseModel):
    document_type: str = Field(..., description="Detected document type: bom or part")
    assembly_part_id: int
    assembly_part_number: str
    bom_id: Optional[int] = None
    created_parts: int = 0
    created_bom_items: int = 0
    extraction_confidence: str = "low"
    warnings: List[str] = Field(default_factory=list)
