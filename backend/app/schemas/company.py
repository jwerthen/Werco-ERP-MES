from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional
from datetime import datetime
import re


class CompanyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Company name")
    slug: Optional[str] = Field(None, max_length=100, description="URL-safe identifier (auto-generated if not provided)")
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
        errors = []
        if len(v) < 12:
            errors.append("Password must be at least 12 characters")
        if not re.search(r'[A-Z]', v):
            errors.append("Must contain uppercase letter")
        if not re.search(r'[a-z]', v):
            errors.append("Must contain lowercase letter")
        if not re.search(r'[0-9]', v):
            errors.append("Must contain number")
        if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]', v):
            errors.append("Must contain special character")
        if errors:
            raise ValueError("; ".join(errors))
        return v


class CompanyCreate(CompanyBase):
    """Platform admin creates a company + initial admin"""
    admin_email: EmailStr
    admin_first_name: str = Field(..., min_length=1, max_length=50)
    admin_last_name: str = Field(..., min_length=1, max_length=50)
    admin_password: str = Field(..., min_length=12, max_length=128)
    parent_company_id: Optional[int] = None


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
    parent_company_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    user_count: Optional[int] = None

    class Config:
        from_attributes = True


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
