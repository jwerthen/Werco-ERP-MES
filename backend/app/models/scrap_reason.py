import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint

from app.db.database import Base
from app.db.mixins import TenantMixin


class ScrapCategory(str, enum.Enum):
    """Vocabulary for ``ScrapReasonCode.category``.

    Stored as a plain ``String(50)`` (NOT a native SQLEnum) so adding a category
    never needs an ``ALTER TYPE`` -- same rationale as ``TimeEntrySource`` /
    ``WorkOrderType``; this repo has repeatedly paid to convert native enums to
    varchar (migrations 013/018/019/021/029). Values are lower-case tokens.
    """

    MATERIAL = "material"
    MACHINE = "machine"
    TOOLING = "tooling"
    OPERATOR = "operator"
    SETUP = "setup"
    PROGRAMMING = "programming"
    ENGINEERING = "engineering"
    SUPPLIER = "supplier"
    HANDLING = "handling"
    OTHER = "other"


class ScrapReasonCode(Base, TenantMixin):
    """Predefined reason codes for categorizing scrap (Lean Phase 1).

    Modeled on ``DowntimeReasonCode`` (app/models/downtime.py) with one deliberate
    difference: ``code`` is unique PER TENANT via ``UniqueConstraint(company_id,
    code)`` -- NOT globally ``unique=True`` (DowntimeReasonCode's global unique is
    a known cross-tenant defect; do not copy it). Referenced by the nullable
    ``scrap_reason_code_id`` FK on ``time_entries`` / ``work_order_operations`` /
    ``work_orders``; the free-text ``scrap_reason`` columns on those tables stay
    as narrative detail.
    """

    __tablename__ = "scrap_reason_codes"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_scrap_reason_codes_company_code"),)

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    category = Column(String(50), nullable=False, default=ScrapCategory.OTHER.value)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
