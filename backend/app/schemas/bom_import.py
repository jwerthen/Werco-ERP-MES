from typing import List, Optional
from pydantic import BaseModel, Field


class BOMImportResponse(BaseModel):
    document_type: str = Field(..., description="Detected document type: bom or part")
    assembly_part_id: int
    assembly_part_number: str
    bom_id: Optional[int] = None
    created_parts: int = 0
    created_bom_items: int = 0
    extraction_confidence: str = "low"
    warnings: List[str] = Field(default_factory=list)

