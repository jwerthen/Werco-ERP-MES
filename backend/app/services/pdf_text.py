"""Escaping for text interpolated into reportlab ``Paragraph`` markup.

**Why this module exists.** ``reportlab.platypus.Paragraph`` does not take plain
text — it parses a mini-HTML dialect (``<b>``, ``<i>``, ``<font>``, ``<br/>``,
``&nbsp;`` …) via ``paraparser``. Every ``Paragraph`` in this codebase is built
by f-string interpolation of record fields into that dialect, which makes each
one an injection sink. Unescaped, a perfectly ordinary manufacturing value is a
**hard failure**, not a cosmetic one::

    Paragraph("Check OD<ID before press", styles["Normal"])
    ValueError: paraparser: syntax error: parse ended with 1 unclosed tags

That is an HTTP 500 on a quote or Certificate-of-Conformance download, triggered
by a customer name or a part note. ASME Y14.5 drawing notation (``<REF>``,
``<TYP>``, ``<MMC>``, ``<BASIC>``, ``<MIN>``) and ampersands in company names
(``R&D Group``, ``Smith & Sons``) hit it routinely.

**Why escaping here and not at ingest.** This is the *only* sink in the backend
that interprets markup in user-controlled strings. Audited and found safe with
no escaping of their own: HTML email (``email_service`` renders through a Jinja2
``Environment`` with ``autoescape=select_autoescape(['html', 'xml'])``, and every
caller goes through a template rather than the raw ``body=`` path), thermal
labels (``label_service`` uses ``canvas.drawString``, which draws glyphs and
parses nothing), and reportlab ``Table`` cells (plain strings, not run through
``paraparser``). The frontend renders no raw HTML at all — see
``tests/test_frontend_no_raw_html_render_guard.py``, which is the standing
enforcement of that. Escaping at the one sink that interprets markup keeps the
stored record byte-exact, which an AS9100D/ISO 9001 records system requires.

**Use ``pdf_escape`` on every interpolated value, including numbers and dates.**
Uniformity is deliberate: the next person editing these templates should not
have to work out which f-string holes are attacker-reachable. Wrap the
*formatted* string for numerics — ``pdf_escape(f"{quantity:g}")``, not
``pdf_escape(quantity)`` — so the format spec still applies. Never wrap the
literal markup in the template: ``f"<b>Customer:</b> {pdf_escape(name)}"`` keeps
the bold tag working and neutralizes only the value.
"""

from typing import Any
from xml.sax.saxutils import escape

__all__ = ["pdf_escape"]


def pdf_escape(value: Any) -> str:
    """Return ``value`` safe to interpolate into a reportlab ``Paragraph``.

    Escapes ``&``, ``<`` and ``>`` into ``&amp;``, ``&lt;`` and ``&gt;``.
    Delegates to :func:`xml.sax.saxutils.escape`, which escapes ``&`` **first**
    — a hand-rolled ``.replace()`` chain in the wrong order double-escapes it
    (``&`` -> ``&amp;`` -> ``&amp;amp;``) and renders the entity text literally
    in the PDF.

    Quotes are intentionally left alone. Values are interpolated into element
    *content*, never into an attribute value, so ``"`` and ``'`` are not
    special; escaping them would put visible ``&quot;`` noise on a customer-
    facing document.

    ``None`` becomes ``""`` rather than the string ``"None"``: these builders
    take many ``Optional`` fields, and a stray ``"None"`` on a Certificate of
    Conformance is worse than a blank. Non-strings are coerced with ``str``.
    """
    if value is None:
        return ""
    return escape(value if isinstance(value, str) else str(value))
