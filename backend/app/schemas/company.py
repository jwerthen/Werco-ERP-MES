from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.base import UTCModel
from app.schemas.user import validate_password_strength


class CompanyBase(UTCModel):
    name: str = Field(..., min_length=1, max_length=255, description="Company name")
    slug: Optional[str] = Field(
        None, max_length=100, description="URL-safe identifier (auto-generated if not provided)"
    )
    logo_url: Optional[str] = Field(None, max_length=500)
    timezone: str = Field(default="America/Chicago", max_length=50)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    website: Optional[str] = Field(None, max_length=255)


class CompanyRegister(BaseModel):
    """Self-registration: creates a new company + admin user"""

    company_name: str = Field(..., min_length=1, max_length=255)
    admin_email: EmailStr
    admin_first_name: str = Field(..., min_length=1, max_length=50)
    admin_last_name: str = Field(..., min_length=1, max_length=50)
    admin_password: str = Field(..., min_length=12, max_length=128)

    @field_validator('admin_password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        # Reuse the canonical AS9100D/CMMC strength policy (schemas.user) so the
        # UNAUTHENTICATED self-registration path can't accept a weaker first-admin
        # password than /auth/register. Previously omitted the common-substring
        # check, so "Password1234!" was accepted here.
        return validate_password_strength(v)


class CompanyCreate(CompanyBase):
    """Platform admin creates a company + initial admin"""

    admin_email: EmailStr
    admin_first_name: str = Field(..., min_length=1, max_length=50)
    admin_last_name: str = Field(..., min_length=1, max_length=50)
    admin_password: str = Field(..., min_length=12, max_length=128)
    parent_company_id: Optional[int] = None

    @field_validator('admin_password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        # Platform-admin company creation must meet the same first-admin strength
        # policy as self-registration; without this the POST /platform/companies
        # path accepted any 12-char password (no complexity check at all).
        return validate_password_strength(v)


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    logo_url: Optional[str] = Field(None, max_length=500)
    timezone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=50)
    website: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None


class CompanyResponse(CompanyBase):
    id: int
    is_active: bool
    allow_ai_egress: bool
    parent_company_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    user_count: Optional[int] = None

    class Config:
        from_attributes = True


class CompanyAIEgressUpdate(BaseModel):
    """Request body for the dedicated AI-egress kill-switch toggle."""

    allow_ai_egress: bool = Field(..., description="Allow outbound AI document-extraction egress to the Anthropic API")


class CompanyListResponse(BaseModel):
    id: int
    name: str
    slug: str
    logo_url: Optional[str] = None
    is_active: bool
    user_count: int = 0
    active_work_orders: int = 0

    class Config:
        from_attributes = True
