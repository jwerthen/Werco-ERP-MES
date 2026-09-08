from datetime import datetime
from typing import Dict, List, Optional

from pydantic import Field

from app.schemas.base import UTCModel


class ImportBatchRowResponse(UTCModel):
    row_key: str
    source_row: int
    status: str
    data: Dict[str, str]
    error: Optional[str] = None
    result: Optional[dict] = None


class ImportBatchResponse(UTCModel):
    id: int
    entity: str
    filename: str
    version: int
    created_at: datetime
    updated_at: datetime
    total_rows: int
    counts: Dict[str, int]
    created_records: int
    rows: List[ImportBatchRowResponse] = Field(default_factory=list)
    row_offset: int = 0
    has_more_rows: bool = False
    requires_credentials: bool = False


class ImportBatchHistory(UTCModel):
    batches: List[ImportBatchResponse]
    has_more: bool
