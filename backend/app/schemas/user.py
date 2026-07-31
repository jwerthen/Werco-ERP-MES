from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.user import UserRole
from app.schemas.base import UTCModel

# Weak substrings rejected by the password-strength policy.
#
# This list is load-bearing. When the four character-class rules were dropped on
# 2026-07-29 (see validate_password_strength), NIST SP 800-63B's trade is explicit:
# composition rules are removed *and replaced* by a blocklist check. Shipping only
# the removal would be a net weakening, so the list was expanded from six entries to
# the common-credential families below plus the shop's own name.
#
# Entries are matched as case-insensitive SUBSTRINGS, so keep them >=4 characters —
# a 3-character entry rejects far too much (e.g. "abc" would reject "Fabricator").
_COMMON_PASSWORD_PATTERNS = (
    # Original six.
    "password",
    "123456",
    "qwerty",
    "admin",
    "letmein",
    "welcome",
    # Keyboard walks.
    "qwertyuiop",
    "asdfgh",
    "zxcvbn",
    "1qaz",
    "1q2w3e",
    "qazwsx",
    # Perennial top-100 entries.
    "iloveyou",
    "abc123",
    "monkey",
    "dragon",
    "sunshine",
    "princess",
    "football",
    "baseball",
    "trustno1",
    "shadow",
    "master",
    "superman",
    "starwars",
    "whatever",
    "freedom",
    "passw0rd",
    "p@ssw0rd",
    "login",
    # Digit runs.
    "111111",
    "000000",
    "121212",
    "654321",
    "112233",
    # Local context — the highest-yield additions for a single-shop deployment.
    "werco",
    "wercomfg",
)


def validate_password_strength(value: str) -> str:
    """Canonical password-strength policy: length + blocklist, no composition rules.

    Single source of truth reused by public registration, the admin-driven user
    create/reset paths, company registration, and CSV import, so a weak password
    cannot enter through any path. Raises ``ValueError`` with every failure joined
    (Pydantic surfaces it as HTTP 422); returns the value unchanged on success.

    Rules: at least 12 characters, and no common weak substring.

    The four character-class requirements (upper/lower/digit/special) were removed on
    2026-07-29. This follows NIST SP 800-63B section 5.1.1.2, which recommends against
    composition rules and for length plus a blocklist check — the composition rules
    were actively inverting the security ordering here. Concretely, they rejected
    ``correct horse battery staple`` (28 characters, high entropy, and the special-
    character class did not even include the space) while accepting ``Aa1!aaaaaaaa``
    (12 characters, trivially guessable). The trade is only sound because the
    blocklist was expanded at the same time; do not shrink ``_COMMON_PASSWORD_PATTERNS``
    without restoring something in its place.

    The 12-character minimum is deliberately unchanged, and is additionally enforced
    as a Pydantic ``Field(min_length=12)`` on four schemas and as ``minLength={12}``
    on five frontend inputs. Changing it means changing all of those together.
    """
    errors = []
    if len(value) < 12:
        errors.append("Password must be at least 12 characters")
    if any(pattern in value.lower() for pattern in _COMMON_PASSWORD_PATTERNS):
        errors.append("Password contains a common word or pattern that is too easy to guess")
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
