from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.core.time_utils import to_utc_iso


class UTCModel(BaseModel):
    """Base for API RESPONSE schemas: datetime fields serialize as UTC ISO-8601 with a
    trailing 'Z' (via to_utc_iso); `date` fields are unaffected (stay YYYY-MM-DD)."""

    model_config = ConfigDict(from_attributes=True, json_encoders={datetime: to_utc_iso})
