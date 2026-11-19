import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, false
from sqlalchemy.orm import relationship

from app.db.database import Base


class UserRole(str, enum.Enum):
    PLATFORM_ADMIN = "platform_admin"  # Werco oversight role — cross-company read access
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint('company_id', 'email', name='uq_users_company_email'),
        UniqueConstraint('company_id', 'employee_id', name='uq_users_company_employee_id'),
        # Lock-step with migration 079_restore_stamped_over_idx (originally
        # migration 026's tenancy composite; skipped by the create_all+stamp
        # bootstrap): tenant-scoped active-user lists.
        Index("ix_users_company_active", "company_id", "is_active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    employee_id = Column(String(50), index=True, nullable=False)
    email = Column(String(255), index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.OPERATOR, nullable=False)
    department = Column(String(100))
    # E.164 phone for SMS notifications (PR 4). Nullable; PII-minimized — serialized only
    # in the self-profile and admin user-management responses, never in general lists.
    phone = Column(String(32), nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)

    # CMMC Level 2 - Track last password change
    password_changed_at = Column(DateTime, default=datetime.utcnow)
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)

    # Floor PIN (migration 130, AS9100D 8.5.2 / 7.5.3): the second factor on badge /
    # employee-ID sign-on (``POST /auth/employee-login``, ``POST /auth/kiosk-badge-token``).
    # ``floor_pin_hash`` is a bcrypt hash made by ``app.core.floor_pin.hash_floor_pin`` and is
    # never serialized, logged or audited -- the admin schema exposes only ``has_floor_pin``.
    # The failed-attempt counter and lock are SEPARATE from ``failed_login_attempts`` /
    # ``locked_until`` above, so a PIN lockout never locks the person's password login.
    floor_pin_hash = Column(String(255), nullable=True)
    floor_pin_set_at = Column(DateTime, nullable=True)
    # Wrong PINs since the last correct PIN or admin unlock / set / clear. CUMULATIVE: a lock
    # does not reset it, so the lock tiers escalate (5 -> 15 minutes, 10 -> 2 hours, 15 ->
    # ``floor_pin_admin_locked``; ``app.core.floor_pin``).
    floor_pin_failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    floor_pin_locked_until = Column(DateTime, nullable=True)
    # Set at 15 cumulative wrong PINs: only an Admin / Manager unlock, set or clear lifts it.
    floor_pin_admin_locked = Column(Boolean, nullable=False, default=False, server_default=false())
    # Set when a promotion into Admin / Manager / Quality cleared a PIN: badge sign-on is refused
    # (403 ``floor_pin_reset_required``) until an Admin sets a new PIN or clears the PIN.
    floor_pin_reset_required = Column(Boolean, nullable=False, default=False, server_default=false())

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Lineage FK mirrors migration 080 (originally 003; skipped by the
    # create_all+stamp bootstrap).
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL", name="fk_users_created_by"), nullable=True)

    # Relationships
    company = relationship("Company", back_populates="users")
    # foreign_keys is pinned: time_entries carries TWO FK paths to users
    # (user_id and, since the 080 FK restore, approved_by), so the join must
    # name which one it follows.
    time_entries = relationship("TimeEntry", back_populates="user", foreign_keys="TimeEntry.user_id")
    notification_preference = relationship("NotificationPreference", back_populates="user", uselist=False)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def company_name(self) -> Optional[str]:
        return self.company.name if self.company else None

    @property
    def has_floor_pin(self) -> bool:
        """True when a floor PIN is set (the admin schema's view of ``floor_pin_hash``)."""
        return bool(self.floor_pin_hash)
