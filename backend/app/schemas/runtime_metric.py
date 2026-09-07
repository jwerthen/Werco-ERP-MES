import json
from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROUTES = frozenset(json.loads((Path(__file__).parent.parent / "data/runtime_metric_routes.json").read_text()))


class RuntimeMetricWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    metric_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")
    name: Literal["LCP", "INP", "CLS"]
    route: str = Field(max_length=100)
    device: Literal["mobile", "tablet", "desktop"]
    navigation: Literal["document", "soft"]
    release: str = Field(pattern=r"^(?:[a-f0-9]{40}|development|unknown)$")
    value: float = Field(ge=0, le=300_000)
    sequence: int = Field(ge=1, le=10_000)

    @field_validator("route")
    @classmethod
    def known_route(cls, value):
        if value not in ROUTES:
            raise ValueError("Use an approved route template, without identifiers or query parameters")
        return value

    @model_validator(mode="after")
    def bounded_layout_shift(self):
        if self.name == "CLS" and self.value > 100:
            raise ValueError("Invalid layout shift value")
        return self


class RuntimeMetricBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    samples: List[RuntimeMetricWrite] = Field(min_length=1, max_length=10)


class RuntimeMetricSettingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
