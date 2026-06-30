"""Pydantic contracts for shared-PIN visitor sign-in stations.

The shared PIN is accepted on create / reset and on station-login, but is NEVER
echoed back: ``SigninStationResponse`` carries no ``pin_hash`` and no PIN, and
the minted JWT is returned exactly once from ``StationLoginResponse``.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# Shared PIN policy: 4–8 digits, numeric only.
_PIN_PATTERN = r'^\d{4,8}$'


class StationLoginRequest(BaseModel):
    station_id: int = Field(..., gt=0, description="SigninStation id from the tablet URL ?station=<id>")
    pin: str = Field(..., pattern=_PIN_PATTERN, description="Shared station PIN (4–8 digits)")


class StationLoginResponse(BaseModel):
    token: str = Field(..., description="Scoped type='signin' JWT — held in tablet sessionStorage only")
    station_label: str
    expires_in: int = Field(..., description="Token lifetime in seconds")


class SigninStationCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100, description="Tablet label, e.g. 'Lobby Tablet'")
    pin: str = Field(..., pattern=_PIN_PATTERN, description="Shared station PIN (4–8 digits)")


class StationResetPinRequest(BaseModel):
    pin: str = Field(..., pattern=_PIN_PATTERN, description="New shared station PIN (4–8 digits)")


class SigninStationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    revoked: bool
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[int] = None
    last_used_at: Optional[datetime] = None
    created_by: Optional[int] = None
    created_at: datetime


class SigninStationListResponse(BaseModel):
    stations: list[SigninStationResponse]
