import json
from datetime import datetime
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkspaceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["view", "draft"]
    name: str = Field(min_length=1, max_length=100)
    data: Dict[str, Any]
    version: int = Field(ge=0, description="0 creates; updates require the last observed version")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A name is required")
        return value

    @field_validator("data")
    @classmethod
    def bounded_json(cls, value: dict) -> dict:
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Draft must contain valid JSON") from exc
        if len(encoded.encode("utf-8")) > 200_000:
            raise ValueError("Draft exceeds 200 KB")
        return value


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    namespace: str
    kind: str
    name: str
    data: Dict[str, Any]
    version: int
    updated_at: datetime


class TeamTableData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str = Field(min_length=1, max_length=60, pattern=r"^[a-zA-Z0-9_-]+$")
    layout: Dict[str, Any]
    filters: Dict[str, str]


class TeamWorkspaceWrite(WorkspaceWrite):
    kind: Literal["view"] = "view"

    @field_validator("data")
    @classmethod
    def table_configuration_only(cls, value: dict) -> dict:
        return TeamTableData.model_validate(value).model_dump()


class TeamWorkspaceList(BaseModel):
    items: list[WorkspaceResponse]
    can_manage: bool
