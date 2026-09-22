"""Explicit opt-in work-order watches, separate from reviewed mutation proposals."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.document import DocumentType


class HankWatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    work_order_id: int = Field(gt=0)
    condition: Literal['blockers_cleared', 'pdf_attached']
    document_type: DocumentType | None = None

    @model_validator(mode='after')
    def document_filter_only_for_pdf(self) -> 'HankWatchInput':
        if self.condition != 'pdf_attached' and self.document_type is not None:
            raise ValueError('A document type applies only to a PDF attachment watch')
        return self


class HankWatchCreate(HankWatchInput):
    expected_company_id: int = Field(gt=0)
    request_key: str

    @field_validator('request_key')
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        return str(UUID(value))
