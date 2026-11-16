"""Every bounded request field on the CAR / FAI / quote-line-revision writes fits its column.

The bug class this pins: ``FAICreate.reason`` was bounded at 500 while ``fais.reason`` is
``String(50)`` (and a characteristic's ``specification`` at 500 against ``String(255)``). On
the SQLite test engine an over-long string is stored silently, so no API test ever saw it; on
Postgres it is a ``DataError`` at flush -- a 500, not a 422. The P2.1 / P2.5 work added ~20
string fields to these schemas, so the bound is checked structurally here rather than one
API probe at a time:

* a ``str`` field that writes a ``String(n)`` column must declare ``max_length <= n``;
* an enum field must have no value longer than its ``String(n)`` column (the CAR's
  ``initiation_reason`` is a plain ``String(40)`` + CHECK, not a native enum);
* a ``Text`` column needs no bound here (bounded elsewhere or unbounded on purpose).

Request fields with no column of the same name (``version``, ``expected_updated_at``, ...)
are not writes and are skipped.
"""

import enum
import typing

import pytest

from app.api.endpoints.quotes import QuoteLineRevisionEdit
from app.models.quality import CorrectiveActionRequest, FAICharacteristic, FirstArticleInspection
from app.models.quote import QuoteLine
from app.schemas.quality import (
    CARCreate,
    CARUpdate,
    FAICharacteristicCreate,
    FAICharacteristicUpdate,
    FAICreate,
    FAIUpdate,
)
from app.services.quote_send_gate import QUOTE_LINE_REVISION_MAX_LENGTH, normalize_line_revision

pytestmark = pytest.mark.unit

REQUEST_SCHEMAS = [
    (CARCreate, CorrectiveActionRequest),
    (CARUpdate, CorrectiveActionRequest),
    (FAICreate, FirstArticleInspection),
    (FAIUpdate, FirstArticleInspection),
    (FAICharacteristicCreate, FAICharacteristic),
    (FAICharacteristicUpdate, FAICharacteristic),
]


def _enum_type(annotation):
    """The Enum class inside ``Optional[SomeEnum]`` / ``SomeEnum``, else ``None``."""
    candidates = typing.get_args(annotation) or (annotation,)
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, enum.Enum):
            return candidate
    return None


def _max_length(field):
    return next((meta.max_length for meta in field.metadata if hasattr(meta, "max_length")), None)


def _bounded_writes(schema, model):
    table = model.__table__
    for name, field in schema.model_fields.items():
        if name not in table.c:
            continue
        width = getattr(table.c[name].type, "length", None)
        if width is None:  # Text, Integer, Date, Numeric ...
            continue
        yield name, field, width


@pytest.mark.parametrize("schema,model", REQUEST_SCHEMAS, ids=lambda value: getattr(value, "__name__", str(value)))
def test_every_string_request_field_fits_its_column(schema, model):
    checked = []
    for name, field, width in _bounded_writes(schema, model):
        enum_type = _enum_type(field.annotation)
        if enum_type is not None:
            longest = max(len(str(member.value)) for member in enum_type)
            assert longest <= width, f"{schema.__name__}.{name}: enum value of {longest} chars > column {width}"
        else:
            bound = _max_length(field)
            assert bound is not None, f"{schema.__name__}.{name} writes String({width}) with no max_length"
            assert bound <= width, f"{schema.__name__}.{name}: max_length {bound} > column String({width})"
        checked.append(name)
    assert checked, f"{schema.__name__} bounds nothing -- the guard is not looking at it"


def test_the_car_form_fields_are_among_those_checked():
    """Guard the guard: the P2.1 string columns are reached by the loop above."""
    names = {name for name, _field, _width in _bounded_writes(CARCreate, CorrectiveActionRequest)}
    assert {
        "initiation_reason",
        "initiation_reason_other",
        "rma_number",
        "vendor_po_number",
        "vendor_approval_name",
        "vendor_approval_title",
        "customer_approval_name",
    } <= names
    # Server-owned: never a request field, so never a bound to check.
    assert not names & {"customer_name", "quality_approved_by", "quality_approved_at"}


def test_quote_line_revision_entry_fits_the_column():
    width = QuoteLine.__table__.c.part_revision.type.length
    assert QUOTE_LINE_REVISION_MAX_LENGTH == width
    assert QuoteLineRevisionEdit(line_id=1, part_revision="x" * width).part_revision == "X" * width
    with pytest.raises(ValueError):
        normalize_line_revision("X" * (width + 1))
