from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Text, JSON
from sqlalchemy.orm import relationship, deferred
from datetime import datetime
import enum
from app.db.database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"


class User(Base):
    """
    User model with CMMC Level 2 compliance features.
    
    MFA (AC-3.1.1): Multi-factor authentication via TOTP
    Password Policy (IA-3.5.7/8/9): Tracking for policy enforcement
    """
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.OPERATOR, nullable=False)
    department = Column(String(100))
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    
    # CMMC Level 2 AC-3.1.1 - Multi-Factor Authentication (MFA)
    # Using deferred loading to prevent query failures if migration hasn't run
    mfa_enabled = deferred(Column(Boolean, default=False, nullable=True))
    mfa_secret = deferred(Column(String(32), nullable=True))  # Base32 encoded TOTP secret
    mfa_backup_codes = deferred(Column(JSON, nullable=True))  # List of one-time backup codes
    mfa_setup_at = deferred(Column(DateTime, nullable=True))  # When MFA was enabled
    
    # CMMC Level 2 - Track last password change
    password_changed_at = Column(DateTime, default=datetime.utcnow)
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    
    # Optimistic locking
    version = Column(Integer, default=1, nullable=False)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    
    # Relationships
    time_entries = relationship("TimeEntry", back_populates="user")
    notification_preference = relationship("NotificationPreference", back_populates="user", uselist=False)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
    
    @property
    def mfa_required(self) -> bool:
        """Check if MFA is required for this user based on role."""
        # All roles require MFA for CMMC compliance
        return True
    
    @property 
    def mfa_pending_setup(self) -> bool:
        """Check if user needs to set up MFA."""
        return self.mfa_required and not self.mfa_enabled
