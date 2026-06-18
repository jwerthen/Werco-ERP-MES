"""Unit tests for the 4x6 receiving-label PDF renderer.

Pure rendering, NO DB / network. These pin down:
* Valid PDF bytes are produced (starts with %PDF) for full + minimal field sets.
* The ``fmt`` arg gates non-PDF formats (only "pdf" is implemented).
* ``_fit`` truncates over-long text (drawString does not wrap) and passes short
  text through unchanged.
* Whole-number quantities render without a trailing ".0".
"""

import pytest

from app.services.label_service import _fit, _format_quantity, build_receiving_label_pdf

pytestmark = pytest.mark.unit


def _full_label(**overrides) -> bytes:
    kwargs = dict(
        part_number="WERX-10293",
        revision="C",
        part_description="Precision machined titanium bracket",
        quantity=25.0,
        unit_of_measure="each",
        lot_number="LOT-2026-0617-AX991",
        serial_numbers="SN001, SN002",
        heat_number="HT-55821",
        po_number="PO-2026-4471",
        vendor_name="Acme Aerospace Supply Co.",
        receipt_number="RCV-20260618-007",
        received_date="2026-06-18",
        location_code="WH1-RECV-A-01",
        is_critical=True,
    )
    kwargs.update(overrides)
    return build_receiving_label_pdf(**kwargs)


def test_renders_valid_pdf_with_full_fields():
    pdf = _full_label()
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500


def test_renders_valid_pdf_with_minimal_fields():
    pdf = build_receiving_label_pdf(
        part_number="BOLT-001",
        revision="A",
        part_description=None,
        quantity=100,
        unit_of_measure="each",
        lot_number="L1",
        is_critical=False,
    )
    assert pdf[:4] == b"%PDF"


def test_critical_and_noncritical_both_render():
    assert _full_label(is_critical=True)[:4] == b"%PDF"
    assert _full_label(is_critical=False)[:4] == b"%PDF"


def test_long_description_does_not_raise():
    # drawString does not wrap; the renderer must truncate, not overflow/raise.
    pdf = _full_label(part_description="X" * 400, vendor_name="Y" * 400, part_number="Z" * 200)
    assert pdf[:4] == b"%PDF"


def test_unsupported_format_rejected():
    with pytest.raises(ValueError):
        build_receiving_label_pdf(
            part_number="P",
            revision="A",
            part_description=None,
            quantity=1,
            unit_of_measure="each",
            lot_number="L",
            fmt="zpl",
        )


def test_fit_truncates_overlong_text():
    out = _fit("x" * 200, "Helvetica", 9, 100)
    assert out.endswith("…")
    assert len(out) < 200


def test_fit_passes_short_text_through():
    assert _fit("SHORT", "Helvetica", 9, 500) == "SHORT"


def test_fit_handles_empty():
    assert _fit(None, "Helvetica", 9, 100) == ""
    assert _fit("", "Helvetica", 9, 100) == ""


@pytest.mark.parametrize(
    "value,expected",
    [(25.0, "25"), (100, "100"), (2.5, "2.5"), (None, "0")],
)
def test_format_quantity(value, expected):
    assert _format_quantity(value) == expected
