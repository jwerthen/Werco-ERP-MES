"""Pydantic contracts for shared-PIN crew-station kiosks + the badge-token mint.

Mirrors ``schemas/signin_station.py``: the shared PIN is accepted on create /
reset and on station-login, but is NEVER echoed back — ``KioskStationResponse``
carries no ``pin_hash`` and no PIN, and the minted station JWT is returned
exactly once from ``KioskStationLoginResponse``.

The badge-token contracts (``KioskBadgeTokenRequest`` / ``...Response``) shape
``POST /auth/kiosk-badge-token``: a station-token-gated exchange of a badge
scan for a 5-minute, kiosk-scoped OPERATOR access token. Deliberately NO
``refresh_token`` field — a shared terminal must never hold a long-lived
credential for an individual operator.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import UTCModel

# Shared PIN policy: 4–8 digits, numeric only (same policy as signin stations).
_PIN_PATTERN = r'^\d{4,8}$'


class KioskStationLoginRequest(BaseModel):
    station_id: int = Field(..., gt=0, description="KioskStation id from the tablet URL ?station=<id>")
    pin: str = Field(..., pattern=_PIN_PATTERN, description="Shared station PIN (4–8 digits)")


class KioskStationInfo(BaseModel):
    """The station identity the tablet needs to render its header + queue calls."""

    id: int
    label: str
    work_center_id: int
    work_center_code: Optional[str] = None
    work_center_name: Optional[str] = None


class KioskStationLoginResponse(BaseModel):
    access_token: str = Field(..., description="Scoped type='kiosk' JWT — held in tablet sessionStorage only")
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds")
    station: KioskStationInfo


class KioskStationCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100, description="Tablet label, e.g. 'Weld Bay Kiosk'")
    work_center_id: int = Field(..., gt=0, description="Work center this station is bound to")
    pin: str = Field(..., pattern=_PIN_PATTERN, description="Shared station PIN (4–8 digits)")


class KioskStationResetPinRequest(BaseModel):
    pin: str = Field(..., pattern=_PIN_PATTERN, description="New shared station PIN (4–8 digits)")


class KioskStationResponse(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    work_center_id: int
    work_center_code: Optional[str] = None
    work_center_name: Optional[str] = None
    revoked: bool
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[int] = None
    last_used_at: Optional[datetime] = None
    created_by: Optional[int] = None
    created_at: datetime


class KioskStationListResponse(BaseModel):
    stations: list[KioskStationResponse]


class KioskBadgeTokenRequest(BaseModel):
    employee_id: str = Field(..., min_length=1, max_length=50, description="Badge / employee id scanned at the kiosk")


class KioskBadgeUser(BaseModel):
    id: int
    full_name: str
    employee_id: Optional[str] = None


class KioskBadgeTokenResponse(BaseModel):
    """5-minute kiosk-scoped operator access token. NO refresh token, ever."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds (300)")
    user: KioskBadgeUser
