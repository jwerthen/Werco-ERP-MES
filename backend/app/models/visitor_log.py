"""Visitor sign-in / sign-out log for the entrance tablet.

Each ``VisitorLog`` row is one visitor's presence record: who they are, who
they came to see, and when they signed in / out. Rows are written either by a
PIN-unlocked sign-in station (the tablet) or by staff from the admin page.

Compliance posture:
- Tenant-scoped via ``TenantMixin`` (non-null ``company_id``, appended LAST in
  column order so the migration mirrors the mixin shape).
- Soft-delete only (``SoftDeleteMixin``); never physically deleted, so the
  attendance record survives for audit.
- ``visitor_name`` / ``host_name`` are CUI/PII and must never cross an external
  boundary; host email notification is internal SMTP to the company's own
  employee only.

Enum storage mirrors ``app.models.downtime`` — native ``SQLEnum`` columns over
``str``-backed ``enum.Enum`` classes co-located here.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class VisitorStatus(str, enum.Enum):
    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"


class VisitorPurpose(str, enum.Enum):
    MEETING = "meeting"
    DELIVERY = "delivery"
    CONTRACTOR = "contractor"
    INTERVIEW = "interview"
    AUDIT = "audit"
    OTHER = "other"


class VisitorLog(Base, SoftDeleteMixin, TenantMixin):
    __tablename__ = "visitor_logs"

    id = Column(Integer, primary_key=True, index=True)

    # Visitor identity (CUI PII — never egress externally).
    visitor_name = Column(String(120), nullable=False)
    visitor_company = Column(String(120), nullable=True)
    visitor_phone = Column(String(40), nullable=True)

    # Host — free-text plus an optional matched internal user (matched by name
    # within the company only; never cross-tenant).
    host_name = Column(String(120), nullable=True)
    host_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Purpose of visit (native SQLEnum, mirroring downtime).
    purpose = Column(SQLEnum(VisitorPurpose), nullable=False)
    purpose_note = Column(String(255), nullable=True)  # required when purpose=OTHER

    # Safety / NDA acknowledgment checkbox.
    safety_acknowledged = Column(Boolean, nullable=False, default=False, server_default='false')

    # Presence lifecycle.
    status = Column(SQLEnum(VisitorStatus), nullable=False, default=VisitorStatus.SIGNED_IN)
    signed_in_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    signed_out_at = Column(DateTime, nullable=True)  # NULL = still on-site

    # Which station recorded it (NULL if a staff member created the row).
    signin_station_id = Column(Integer, ForeignKey("signin_stations.id"), nullable=True)
    station_label = Column(String(100), nullable=True)  # denormalized actor label at sign-in

    # Staff back-entry attribution — NULL for live station/tablet captures; set to
    # the ADMIN/MANAGER who recorded an offline (paper-logged) visit after a
    # lobby-tablet outage, with its ACTUAL times. Its presence is the positive
    # "staff back-entry" flag: such a row never masquerades as a live lobby
    # capture. Unlike a bare ``signin_station_id IS NULL`` (which also holds for a
    # live staff sign-in via the tablet endpoint), this column distinguishes a
    # back-dated entry from a live capture.
    entered_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    host = relationship("User", foreign_keys=[host_user_id])
    station = relationship("SigninStation", foreign_keys=[signin_station_id])
    entered_by = relationship("User", foreign_keys=[entered_by_user_id])
