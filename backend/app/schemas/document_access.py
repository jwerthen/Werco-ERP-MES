"""Request / response contracts for restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2, migration 131).

The rule itself is ``app.services.document_access``; the writes are
``app.services.document_access_service`` and ``PUT /users/{id}/export-authorization``.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.document import DocumentAccessLevel
from app.schemas.base import UTCModel

ACCESS_LEVEL_DESCRIPTION = (
    "Who may open the document. standard: anyone who can read documents. restricted: Admin, Manager, "
    "Quality and Supervisor (plus superusers and Platform Admins). export_controlled: ONLY users marked "
    "export-authorized; no role bypasses it."
)
REASON_MAX_LENGTH = 2000


def _reason_not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("A reason is required")
    return stripped


class DocumentAccessLevelChange(BaseModel):
    """``POST /documents/{document_id}/access-level``: the new level and why."""

    access_level: DocumentAccessLevel = Field(..., description=ACCESS_LEVEL_DESCRIPTION)
    reason: str = Field(
        ...,
        min_length=1,
        max_length=REASON_MAX_LENGTH,
        description="Why the access level is changing (required; recorded on the audit trail).",
    )

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _reason_not_blank(value)


class DocumentAccessReviewRow(UTCModel):
    """A document whose part's customer is export-controlled but whose level is not ``export_controlled``."""

    document_id: int
    document_number: str
    revision: Optional[str] = None
    title: str
    document_type: str
    access_level: str
    part_id: int
    part_number: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    classification: str = Field(..., description="The customer's control: itar_export_controlled or cui.")


class DocumentAccessReviewResponse(UTCModel):
    """``GET /documents/access-review``."""

    documents: List[DocumentAccessReviewRow]
    document_count: int
    unlinked_document_count: int = Field(
        ...,
        description=(
            "Documents below export_controlled with no part link: the server cannot tell whose they are, "
            "so a person has to judge them."
        ),
    )


class ExportAuthorizationUpdate(BaseModel):
    """``PUT /users/{user_id}/export-authorization``: grant or revoke, and why."""

    export_authorized: bool = Field(
        ...,
        description="True grants: the user may open export_controlled documents. False revokes.",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=REASON_MAX_LENGTH,
        description="Why authorization is granted or revoked (required; recorded on the audit trail).",
    )

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _reason_not_blank(value)
