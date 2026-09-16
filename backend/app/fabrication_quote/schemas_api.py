"""Persistence contracts for the replacement estimator."""

from datetime import date
from typing import Literal

from pydantic import Field

from app.fabrication_quote.schemas import Amount, QuotePlan, StrictModel


class CreateQuote(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    customer_id: int | None = Field(default=None, gt=0)
    request_key: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    plan: QuotePlan = Field(default_factory=QuotePlan)


class UpdateQuote(StrictModel):
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    customer_id: int | None = Field(default=None, gt=0)
    plan: QuotePlan


class RevisionAction(StrictModel):
    expected_revision: int = Field(ge=1)
    review_note: str = Field(default="", max_length=4000)


class CalculateQuote(StrictModel):
    plan: QuotePlan
    quote_id: int | None = Field(default=None, gt=0)
    # Scenarios are exploratory. Approval always evaluates on the server's date.
    as_of: date | None = None


class ActualObservation(StrictModel):
    request_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    quote_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=120)
    observed_on: date
    good_quantity: Amount
    scrap_quantity: Amount
    setup_labor_seconds: Amount | None = None
    run_labor_seconds: Amount | None = None
    machine_seconds: Amount | None = None
    observed_cost: Amount | None = None
    source: str = Field(min_length=1, max_length=2000)
    note: str = Field(min_length=1, max_length=4000)
    completeness: Literal["partial", "complete"] = "partial"
