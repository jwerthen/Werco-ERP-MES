"""Regression guard for the platform-wide UTC datetime-serialization contract.

Timezone-consistency fix (branch ``fix/timezone-consistency``):
``app.schemas.base.UTCModel`` is the base for every API RESPONSE schema. Its
``model_config`` carries ``json_encoders={datetime: to_utc_iso}`` so that any
``datetime`` response field serializes as UTC ISO-8601 with a trailing ``Z``
(e.g. ``2026-07-01T19:17:00Z``), while ``date``-only fields stay bare
``YYYY-MM-DD``. The frontend's ``centralTime`` parser depends on that ``Z`` to
disambiguate the wire value as UTC before rendering it in America/Chicago; a
naive-UTC datetime serialized *without* the ``Z`` was the original bug (a
sign-out at 19:17 UTC rendered as viewer-local "7:17 PM" instead of Central
"2:17 PM").

The unit block asserts the contract directly on ``UTCModel`` (no DB). The risky
part is inheritance: a child schema that re-declares its own ``class Config``
(pydantic-v1 style) OR its own ``model_config`` (v2 style) must still inherit
``json_encoders`` from ``UTCModel`` — several real response schemas do exactly
that (e.g. ``VisitorLogResponse`` re-declares ``model_config``). Both shapes are
covered here so a future refactor that drops the inherited encoder fails loudly.

The endpoint-level assertion for the actual trigger (visitor sign-in/out
timestamps ending in ``Z``) lives in ``tests/test_visitor_logs.py`` alongside
the rest of the visitor-log coverage.
"""

from datetime import date, datetime, timezone
from typing import Optional

import pytest
from pydantic import ConfigDict

from app.core.time_utils import to_utc_iso
from app.schemas.base import UTCModel

pytestmark = pytest.mark.unit


class _PlainChild(UTCModel):
    """A vanilla UTCModel subclass with a datetime + a date field."""

    when: Optional[datetime] = None
    day: Optional[date] = None


class _V2ConfigChild(UTCModel):
    """A subclass that re-declares its own pydantic-v2 ``model_config``.

    ``from_attributes=True`` here (mirroring real schemas like
    ``VisitorLogResponse``) must NOT clobber the inherited ``json_encoders``.
    """

    model_config = ConfigDict(from_attributes=True)

    when: Optional[datetime] = None


class _V1ConfigChild(UTCModel):
    """A subclass that re-declares a pydantic-v1 style ``class Config``.

    Pydantic 2 still reads a nested ``class Config``; the inherited
    ``json_encoders`` must survive that override too.
    """

    class Config:
        from_attributes = True

    when: Optional[datetime] = None


def test_datetime_field_serializes_with_trailing_z():
    """A naive-UTC datetime field serializes as ISO-8601 with a trailing 'Z'."""
    model = _PlainChild(when=datetime(2026, 7, 1, 19, 17, 0))
    assert model.model_dump(mode="json")["when"] == "2026-07-01T19:17:00Z"


def test_date_only_field_stays_bare_yyyy_mm_dd():
    """A ``date``-only field is unaffected — no time, no 'Z'."""
    model = _PlainChild(day=date(2026, 7, 1))
    dumped = model.model_dump(mode="json")
    assert dumped["day"] == "2026-07-01"
    assert "Z" not in dumped["day"]


def test_none_datetime_serializes_as_null():
    """A ``None`` datetime serializes to JSON null, not a string."""
    model = _PlainChild(when=None)
    assert model.model_dump(mode="json")["when"] is None


def test_naive_and_aware_datetimes_normalize_to_same_z_string():
    """A naive datetime (assumed UTC) and the equivalent tz-aware UTC datetime
    both serialize to the identical trailing-'Z' string."""
    naive = _PlainChild(when=datetime(2026, 7, 1, 19, 17, 0))
    aware = _PlainChild(when=datetime(2026, 7, 1, 19, 17, 0, tzinfo=timezone.utc))
    naive_str = naive.model_dump(mode="json")["when"]
    aware_str = aware.model_dump(mode="json")["when"]
    assert naive_str == aware_str == "2026-07-01T19:17:00Z"


def test_tz_aware_non_utc_datetime_is_converted_to_utc_z():
    """A tz-aware datetime in a non-UTC zone is converted to UTC, then 'Z'.
    19:17 at +02:00 is 17:17 UTC."""
    from datetime import timedelta

    plus_two = timezone(timedelta(hours=2))
    model = _PlainChild(when=datetime(2026, 7, 1, 19, 17, 0, tzinfo=plus_two))
    assert model.model_dump(mode="json")["when"] == "2026-07-01T17:17:00Z"


def test_v2_model_config_child_still_inherits_json_encoders():
    """A child re-declaring pydantic-v2 ``model_config`` still emits the 'Z'."""
    model = _V2ConfigChild(when=datetime(2026, 7, 1, 19, 17, 0))
    assert model.model_dump(mode="json")["when"] == "2026-07-01T19:17:00Z"


def test_v1_class_config_child_still_inherits_json_encoders():
    """A child re-declaring a pydantic-v1 ``class Config`` still emits the 'Z'."""
    model = _V1ConfigChild(when=datetime(2026, 7, 1, 19, 17, 0))
    assert model.model_dump(mode="json")["when"] == "2026-07-01T19:17:00Z"


def test_encoder_matches_to_utc_iso_helper():
    """The serialized value is exactly what the shared ``to_utc_iso`` helper
    produces — the encoder is that helper, not a divergent re-implementation."""
    when = datetime(2026, 7, 1, 19, 17, 0)
    model = _PlainChild(when=when)
    assert model.model_dump(mode="json")["when"] == to_utc_iso(when)


def test_time_entry_response_emits_z_via_own_config():
    """``TimeEntryResponse`` predates ``UTCModel`` and does NOT inherit it — it
    carries its OWN ``class Config: json_encoders={datetime: to_utc_iso}``. It
    must keep emitting the trailing 'Z' on its clock_in/clock_out datetimes.
    Guards against a refactor that drops that Config assuming ``UTCModel`` covers
    it (it does not — ``TimeEntryBase`` extends ``BaseModel``)."""
    from app.schemas.time_entry import TimeEntryResponse

    entry = TimeEntryResponse.model_construct(
        id=1,
        clock_in=datetime(2026, 7, 1, 19, 17, 0),
        clock_out=datetime(2026, 7, 1, 21, 17, 0),
        created_at=datetime(2026, 7, 1, 19, 17, 0),
        updated_at=datetime(2026, 7, 1, 19, 17, 0),
    )
    js = entry.model_dump_json()
    assert '"clock_in":"2026-07-01T19:17:00Z"' in js
    assert '"clock_out":"2026-07-01T21:17:00Z"' in js
