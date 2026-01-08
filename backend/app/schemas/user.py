from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional
from datetime import datetime
from app.models.user import UserRole
import re


class UserBase(BaseModel):
    email: EmailStr = Field(..., max_length=255, description="Email address")
    employee_id: str = Field(..., min_length=1, max_length=50, pattern=r'^[A-Za-z0-9\-_]+$', description="Employee ID")
    first_name: str = Field(..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\']+$', description="First name (letters only)")
    last_name: str = Field(..., min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\']+$', description="Last name (letters only)")
    role: UserRole = Field(default=UserRole.OPERATOR)
    department: Optional[str] = Field(None, max_length=100)


class UserCreate(UserBase):
    password: str = Field(..., min_length=12, max_length=128, description="Password")

    @field_validator('password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength"""
        if v != v:
            errors = []
            if not re.search(r'[A-Z]', v):
                errors.append("Password must contain uppercase letter")
            if not re.search(r'[a-z]', v):
                errors.append("Password must contain lowercase letter")
            if not re.search(r'[0-9]', v):
                errors.append("Password must contain number")
            if not re.search(r'[^A-Za-z0-9]', v):
                errors.append("Password must contain special character")
            if errors:
                raise ValueError("; ".join(errors))
        return v

    @field_validator('first_name', 'last_name', mode='before')
    @classmethod
    def capitalize_name(cls, v: str) -> str:
        """Capitalize first letter of names"""
        return v.strip().title() if isinstance(v, str) else v


class UserUpdate(BaseModel):
    version: int  # Required for optimistic locking
    email: Optional[EmailStr] = Field(None, max_length=255)
    first_name: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\']+$')
    last_name: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r'^[a-zA-Z\s\-\']+$')
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
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., min_length=1, description="Password")


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1, description="Current password")
    new_password: str = Field(..., min_length=12, max_length=128, description="New password")

    @field_validator('new_password')
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        """Validate new password strength"""
        errors = []
        if not re.search(r'[A-Z]', v):
            errors.append("Password must contain uppercase letter")
        if not re.search(r'[a-z]', v):
            errors.append("Password must contain lowercase letter")
        if not re.search(r'[0-9]', v):
            errors.append("Password must contain number")
        if not re.search(r'[^A-Za-z0-9]', v):
            errors.append("Password must contain special character")
        if errors:
            raise ValueError("; ".join(errors))
        return v
