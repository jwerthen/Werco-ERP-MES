from datetime import datetime
from typing import List, Literal, Optional

from app.schemas.base import UTCModel

TimelineCategory = Literal["job", "production", "labor", "material", "quality", "blocker", "audit"]


class WorkOrderTimelineEntry(UTCModel):
    id: str
    occurred_at: datetime
    category: TimelineCategory
    evidence: Literal["business_record", "audit", "telemetry"]
    title: str
    detail: Optional[str] = None
    actor_id: Optional[int] = None
    actor_name: Optional[str] = None
    source_label: str
    source_url: str
    # The operation this item belongs to (an operation's start / finish, its production
    # receipts, step evidence and labor, a blocker filed against it); null for the job's own.
    operation_id: Optional[int] = None


class WorkOrderTimelineResponse(UTCModel):
    items: List[WorkOrderTimelineEntry]
    next_cursor: Optional[str] = None
    coverage: str = (
        "Business records and scoped audit history; supplemental quality events are best effort. Current record timestamps do not reconstruct every past edit; unavailable actors remain unknown."
    )
