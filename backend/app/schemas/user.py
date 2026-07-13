import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.user import UserRole
from app.schemas.base import UTCModel

# Common weak substrings rejected by the password-strength policy.
_COMMON_PASSWORD_PATTERNS = ("password", "123456", "qwerty", "admin", "letmein", "welcome")


def validate_password_strength(value: str) -> str:
    """Canonical AS9100D/CMMC password-strength policy.

    Single source of truth reused by public registration, the admin-driven user
    create/reset paths, and CSV import so a weak password cannot enter through any
    path. Rules: at least 12 characters, at least one uppercase, lowercase, digit,
    and special character, and no common weak substring. Raises ``ValueError`` with
    every failure joined (Pydantic surfaces it as HTTP 422); returns the value
    unchanged on success.
    """
    errors = []
    if len(value) < 12:
        errors.append("Password must be at least 12 characters")
    if not re.search(r'[A-Z]', value):
        errors.append("Password must contain at least one uppercase letter")
    if not re.search(r'[a-z]', value):
        errors.append("Password must contain at least one lowercase letter")
    if not re.search(r'[0-9]', value):
        errors.append("Password must contain at least one number")
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]', value):
        errors.append("Password must contain at least one special character (!@#$%^&*()_+-=[]{};\':\"\\|,.<>/?)")
    # Check for common patterns
    if any(pattern in value.lower() for pattern in _COMMON_PASSWORD_PATTERNS):
        errors.append("Password contains a common pattern that is not allowed")
    if errors:
        raise ValueError("; ".join(errors))
    return value


class UserBase(UTCModel):
    email: EmailStr = Field(..., max_length=255, description="Email address")
    employee_id: str = Field(..., min_length=1, max_length=50, pattern=r'^[A-Za-z0-9\-_]+$', description="Employee ID")
    first_name: str = Field(
        ..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$', description="First name (letters only)"
    )
    last_name: str = Field(
        ..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$', description="Last name (letters only)"
    )
    role: UserRole = Field(default=UserRole.OPERATOR)
    department: Optional[str] = Field(None, max_length=100)


class UserCreate(UserBase):
    password: str = Field(..., min_length=12, max_length=128, description="Password")

    @field_validator('password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength - AS9100D compliant"""
        return validate_password_strength(v)

    @field_validator('first_name', 'last_name', mode='before')
    @classmethod
    def capitalize_name(cls, v: str) -> str:
        """Capitalize first letter of names"""
        return v.strip().title() if isinstance(v, str) else v


class PublicRegister(BaseModel):
    email: EmailStr = Field(..., max_length=255, description="Email address")
    first_name: str = Field(..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$', description="First name")
    last_name: str = Field(..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$', description="Last name")
    employee_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=50,
        pattern=r'^[A-Za-z0-9\-_]+$',
        description="Employee ID (auto-generated if not provided)",
    )
    password: str = Field(..., min_length=12, max_length=128, description="Password")

    @field_validator('password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength - AS9100D compliant"""
        return validate_password_strength(v)

    @field_validator('first_name', 'last_name', mode='before')
    @classmethod
    def capitalize_name(cls, v: str) -> str:
        """Capitalize first letter of names"""
        return v.strip().title() if isinstance(v, str) else v


class UserUpdate(BaseModel):
    version: int  # Required for optimistic locking
    email: Optional[EmailStr] = Field(None, max_length=255)
    first_name: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$')
    last_name: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\'\.,]+$')
    role: Optional[UserRole] = None
    department: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None

    @field_validator('first_name', 'last_name', mode='before')
    @classmethod
    def capitalize_name(cls, v: Optional[str]) -> Optional[str]:
        """Capitalize first letter of names"""
        return v.strip().title() if v else v


class UserResponse(UserBase):
    id: int
    version: Optional[int] = 0  # For optimistic locking (optional for backwards compatibility)
    is_active: bool
    is_superuser: bool
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., min_length=1, description="Password")


class EmployeeLoginRequest(BaseModel):
    employee_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        pattern=r'^[A-Za-z0-9\-_]+$',
        description="Employee ID or 4-digit badge ID",
    )


class Token(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int = 900  # 15 minutes in seconds
    user: UserResponse


class TokenRefresh(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class RefreshTokenRequest(BaseModel):
    refresh_token: str
