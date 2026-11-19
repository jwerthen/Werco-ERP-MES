"""Request / response contracts for purchase-order quality flow-down (AS9100D 8.4.3).

See ``app.services.po_flowdown_service`` for the block's shape and semantics.
"""

from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.po_flowdown import DEFAULT_FLOWDOWN_HEADING
from app.schemas.base import UTCModel

# A clause code as a request carries it: surrounding whitespace stripped, 1-20 characters.
QualityClauseCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]

# The longest list of clause codes one purchase order may select.
MAX_PO_QUALITY_CLAUSES = 200
# Bounds a clause's or the profile's free text (it prints on every purchase order).
MAX_FLOWDOWN_TEXT = 20_000


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


# ── Profile ─────────────────────────────────────────────────────────────────


class POFlowdownProfileResponse(UTCModel):
    """The company's flow-down settings. ``id`` null and ``version`` 0 = never saved."""

    id: Optional[int] = None
    enabled: bool = False
    heading: str = DEFAULT_FLOWDOWN_HEADING
    document_reference: Optional[str] = None
    body: Optional[str] = None
    version: int = 0
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None


class POFlowdownProfileUpdate(BaseModel):
    """A full replacement of the flow-down settings, guarded by ``version``."""

    enabled: bool = Field(..., description="Print the quality flow-down block on purchase orders")
    heading: str = Field(DEFAULT_FLOWDOWN_HEADING, min_length=1, max_length=255, description="Block heading")
    document_reference: Optional[str] = Field(
        None, max_length=100, description="The supplier quality requirements document cited, e.g. 'SQR-100 Rev C'"
    )
    body: Optional[str] = Field(
        None, max_length=MAX_FLOWDOWN_TEXT, description="The standard flow-down paragraph printed under the heading"
    )
    version: int = Field(..., ge=0, description="The version you loaded (0 when the settings were never saved)")

    @field_validator("heading", "document_reference", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return _strip(value)

    @field_validator("document_reference", "body", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return _blank_to_none(value)


# ── Clause catalog ──────────────────────────────────────────────────────────


class POQualityClauseResponse(UTCModel):
    id: int
    code: str
    title: str
    body: Optional[str] = None
    default_selected: bool
    is_active: bool
    sort_order: int
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class POQualityClauseCreate(BaseModel):
    code: QualityClauseCode = Field(
        ..., description="Short code printed on the PO, e.g. 'Q1'. Unique in the company (ignoring case); immutable."
    )
    title: str = Field(..., min_length=1, max_length=255)
    body: Optional[str] = Field(None, max_length=MAX_FLOWDOWN_TEXT)
    default_selected: bool = Field(False, description="Applies to every PO that has not chosen its own clauses")
    is_active: bool = True
    sort_order: int = Field(0, ge=0, le=1_000_000, description="Print order (then code)")

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value: object) -> object:
        return _strip(value)

    @field_validator("body", mode="before")
    @classmethod
    def blank_body(cls, value: object) -> object:
        return _blank_to_none(value)


class POQualityClauseUpdate(BaseModel):
    """Only the fields sent change. ``code`` may be echoed back unchanged; it cannot be changed."""

    code: Optional[str] = Field(None, max_length=20, description="Immutable: must equal the stored code if sent")
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    body: Optional[str] = Field(None, max_length=MAX_FLOWDOWN_TEXT)
    default_selected: Optional[bool] = None
    is_active: Optional[bool] = Field(None, description="false retires the clause (it is never deleted)")
    sort_order: Optional[int] = Field(None, ge=0, le=1_000_000)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value: object) -> object:
        return _strip(value)

    @field_validator("body", mode="before")
    @classmethod
    def blank_body(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("title", "default_selected", "is_active", "sort_order")
    @classmethod
    def not_null(cls, value: object) -> object:
        """An EXPLICIT null on a NOT NULL column is a 422 (omitting the field leaves it unchanged)."""
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


# ── Print ───────────────────────────────────────────────────────────────────


class POFlowdownClausePrint(BaseModel):
    code: str
    title: str
    body: Optional[str] = None


class POFlowdownPrint(BaseModel):
    """The block a purchase order prints (live on a draft, frozen once issued). Present only when
    it prints, so ``enabled`` is always true here; a disabled block is ``quality_flowdown: null``."""

    enabled: bool = True
    heading: str
    document_reference: Optional[str] = None
    body: Optional[str] = None
    clauses: List[POFlowdownClausePrint] = Field(default_factory=list)
