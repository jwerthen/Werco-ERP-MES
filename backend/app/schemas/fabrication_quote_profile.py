"""Reusable process inputs; null parameters remain unknown, not zero."""

from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.fabrication_quote.schemas import Currency, OperationLine, Positive, StrictModel


class SaveFabricationQuoteProfile(StrictModel):
    key: UUID | None = None
    expected_revision: int | None = Field(default=None, strict=True, ge=1)
    name: str = Field(min_length=1, max_length=200)
    process: str = Field(min_length=1, max_length=100)
    machine: str | None = Field(default=None, max_length=200)
    material: str | None = Field(default=None, max_length=200)
    thickness_mm: Positive | None = None
    currency: Currency = "USD"
    template: OperationLine
    evidence_note: str = Field(min_length=1, max_length=4000)

    @field_validator("name", "process", "evidence_note")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Provide a nonblank value")
        return value

    @field_validator("machine", "material")
    @classmethod
    def optional_text(cls, value):
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def revision_and_process(self):
        if (self.key is None) != (self.expected_revision is None):
            raise ValueError("Provide key and expected_revision together to append a revision")
        if self.template.process != self.process:
            raise ValueError("Profile process must match its operation template process")
        return self
