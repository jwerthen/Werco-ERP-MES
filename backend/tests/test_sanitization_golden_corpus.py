"""Golden-corpus characterization of ``app/core/sanitization.py``.

**What this file is for.** It freezes the *observable output* of ``sanitize_string``
and ``sanitize_dict`` as they behave today (bleach 6.4.0) so that replacing the
sanitizer becomes a measurable diff instead of a leap of faith. bleach was archived
2026-06-10 and 6.4.0 is the final release there will ever be, so a replacement is a
question of when, not if. Every expectation below is a **hard-coded literal**,
generated once from the installed bleach and pasted in — deliberately *not* computed
by re-running the library under test, because an expectation derived from the
implementation proves nothing about a swap.

**A failure here does not mean "fix the test."** It means the sanitizer's observable
behavior changed. Decide *deliberately* whether that change is intended, then update
the literal and say so in the commit message. Nothing in this file is an assertion
about what the output *should* be — only about what it *is*.

**Why this matters more than a typical characterization test.** The ``sanitize_input``
middleware in ``app/main.py`` rewrites ``request._body`` with the sanitizer's output,
so whatever comes out of here is what gets **persisted**. In an AS9100D / ISO 9001
system, silently storing an NCR narrative as ``"Runout"`` is a records-integrity
defect, not a cosmetic one.

**The nh3 regression bait.** ``nh3`` is the leading bleach replacement and it was
evaluated and **rejected**: measured over a 113-input corpus it silently destroys
ordinary ERP text. Two families, both in html5ever's tree builder with no option to
disable them:

1. *Foreign-content truncation* — an element name that html5ever parses as MathML or
   SVG drops the element **and the entire rest of the string**. Roughly 98 ordinary
   English words are such names (``min max mean set text list line path filter degree
   use view stop switch and or not true false sum times limit matrix vector circle
   template none cos sin tan log`` …), so this fires on plain shop-floor prose.
2. *Unterminated ``<``* — ``<`` followed by a **letter** with no closing ``>``
   discards to end of input. (``<`` followed by a digit, space, ``.``, ``=``, ``-`` or
   ``_`` is safe on both engines; the hazard is specifically ``<`` + letter.)

Measured examples, bleach 6.4.0 vs nh3::

    "Runout<TIR spec on OP30 - see Bob"    bleach: "Runout&lt;TIR spec on OP30 - see Bob"
                                              nh3: "Runout"
    "Check OD<ID before press fit, ..."    bleach: escapes the "<", keeps everything
                                              nh3: "Check OD"
    "Tolerance <MIN> per print, ..."       bleach: "Tolerance  per print, inspect 100%"
                                              nh3: "Tolerance "
    "Qty <set> at OP20 - verify ..."       bleach: "Qty  at OP20 - verify with gage"
                                              nh3: "Qty "

Those exact inputs are in the corpus below, alongside a spread of the trigger words in
realistic ERP sentences, so a future migration attempt fails **loudly and specifically**
rather than shipping silent truncation. Cases carrying a known divergence annotate it in
the failure message. ``nh3`` is deliberately **not** a dependency of this test — the
divergences are encoded as recorded measurements, not re-run.

The corpus also carries a control group (``safe_lt_*``) that is byte-identical on both
engines. Those exist to prove the suite is characterizing the sanitizer rather than
over-fitting to bleach's quirks: a replacement that passes only those and fails the rest
is destroying data, and a replacement that fails those too is broken outright.

**Today's sanitizer is not innocent either** (``print_callout_*``). bleach deletes *any*
bracketed token, so ``"Dim is 2.500 <REF> per print"`` is stored as
``"Dim is 2.500  per print"`` — ``<REF>``, ``<TYP>``, ``<BASIC>`` and ``<MMC>`` are
standard drawing callouts, and an HTML comment is removed outright
(``malformed_comment``). It is much milder than nh3, because the rest of the sentence
survives, but it is the same class of records-integrity defect that disqualified nh3,
and it is live in production. Those rows are pinned as characterization, not endorsed.

**Known gap pinned as-is, not fixed here:** ``sanitize_dict`` recurses into nested
dicts, but for a **list** it only maps ``sanitize_string`` over the *string* elements —
so a dict nested inside a list is returned **unsanitized**. That is a real hole (JSON
request bodies routinely carry lists of objects, e.g. BOM lines and PO lines) and it is
characterized below by ``test_sanitize_dict_does_not_recurse_into_dicts_inside_lists``.
Pinning it here documents it; closing it is a behavior change and belongs in its own
change with its own review.
"""

from typing import NamedTuple

import pytest

from app.core.sanitization import sanitize_dict, sanitize_string

pytestmark = pytest.mark.unit


class Case(NamedTuple):
    """One frozen input/output pair.

    ``nh3`` records what the rejected replacement engine does with this input, when
    that differs from bleach. It is documentation carried into the failure message —
    nothing in this file executes nh3.
    """

    label: str
    raw: str
    expected: str
    nh3: str = ""


# Recorded nh3 divergence families. Only the four inputs quoted in the module
# docstring have a measured literal output; the rest name the family, because
# inventing a literal we did not measure would be worse than useless.
_NH3_FOREIGN = (
    "nh3 divergence (foreign-content truncation): html5ever parses this element name as "
    "MathML/SVG and drops the element AND the entire rest of the string"
)
_NH3_UNTERMINATED = (
    "nh3 divergence (unterminated '<'): '<' followed by a letter with no closing '>' "
    "discards everything from the '<' to the end of the input"
)
_NH3_ENTITY = "nh3 divergence: nh3 decodes HTML entities; bleach leaves them as literal text"


GOLDEN_CORPUS: list[Case] = [
    # ------------------------------------------------------------------
    # Family A - element names html5ever treats as MathML/SVG foreign content.
    # bleach drops the tag and keeps the surrounding prose (note the resulting
    # DOUBLE SPACE, which is itself part of the frozen behavior). nh3 drops the
    # tag and everything after it.
    # ------------------------------------------------------------------
    Case(
        "mathml_min_uppercase",
        "Tolerance <MIN> per print, inspect 100%",
        "Tolerance  per print, inspect 100%",
        _NH3_FOREIGN + '; measured nh3 output is "Tolerance "',
    ),
    Case(
        "mathml_set",
        "Qty <set> at OP20 - verify with gage",
        "Qty  at OP20 - verify with gage",
        _NH3_FOREIGN + '; measured nh3 output is "Qty "',
    ),
    Case(
        "mathml_max", "Run to <max> stock removal on the rougher", "Run to  stock removal on the rougher", _NH3_FOREIGN
    ),
    Case(
        "mathml_mean",
        "Report the <mean> of five parts per AS9102",
        "Report the  of five parts per AS9102",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_text", "Add <text> to the traveler before release", "Add  to the traveler before release", _NH3_FOREIGN
    ),
    Case(
        "mathml_list",
        "Pull the <list> of open NCRs for the audit",
        "Pull the  of open NCRs for the audit",
        _NH3_FOREIGN,
    ),
    Case("mathml_line", "Scribe the <line> per drawing note 4", "Scribe the  per drawing note 4", _NH3_FOREIGN),
    Case(
        "svg_path",
        "Follow the <path> on the nest, do not re-order",
        "Follow the  on the nest, do not re-order",
        _NH3_FOREIGN,
    ),
    Case(
        "svg_filter", "Change the coolant <filter> every 500 hours", "Change the coolant  every 500 hours", _NH3_FOREIGN
    ),
    Case("mathml_degree", "Chamfer to 30 <degree> included angle", "Chamfer to 30  included angle", _NH3_FOREIGN),
    Case(
        "svg_use", "Do not <use> the backup gage without recall", "Do not  the backup gage without recall", _NH3_FOREIGN
    ),
    Case(
        "svg_view",
        "Operator to <view> the process sheet before start",
        "Operator to  the process sheet before start",
        _NH3_FOREIGN,
    ),
    Case("svg_stop", "Hit <stop> if the spindle load spikes", "Hit  if the spindle load spikes", _NH3_FOREIGN),
    Case(
        "svg_switch", "The limit <switch> on OP40 needs replacing", "The limit  on OP40 needs replacing", _NH3_FOREIGN
    ),
    Case("mathml_and", "Deburr <and> break all sharp edges", "Deburr  break all sharp edges", _NH3_FOREIGN),
    Case("mathml_or", "Use the Kennametal <or> Sandvik insert", "Use the Kennametal  Sandvik insert", _NH3_FOREIGN),
    Case(
        "mathml_not", "Do <not> stamp the part on the datum face", "Do  stamp the part on the datum face", _NH3_FOREIGN
    ),
    Case(
        "mathml_true",
        "Set the flag <true> after first-article approval",
        "Set the flag  after first-article approval",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_false",
        "Left the interlock <false>, see maintenance log",
        "Left the interlock , see maintenance log",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_sum",
        "The <sum> of scrap at OP20 and OP30 is 4 pcs",
        "The  of scrap at OP20 and OP30 is 4 pcs",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_times",
        "Ran the cycle three <times> to prove repeatability",
        "Ran the cycle three  to prove repeatability",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_limit",
        "Upper control <limit> exceeded on Xbar chart",
        "Upper control  exceeded on Xbar chart",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_matrix",
        "Update the risk <matrix> for this process change",
        "Update the risk  for this process change",
        _NH3_FOREIGN,
    ),
    Case(
        "mathml_vector",
        "Tool <vector> approach is wrong on the sim",
        "Tool  approach is wrong on the sim",
        _NH3_FOREIGN,
    ),
    Case("svg_circle", "Bolt <circle> dia is 4.250 per rev C", "Bolt  dia is 4.250 per rev C", _NH3_FOREIGN),
    Case("html_template", "Copy the router <template> from PN 88213", "Copy the router  from PN 88213", _NH3_FOREIGN),
    Case("svg_none", "Surface treatment: <none> required", "Surface treatment:  required", _NH3_FOREIGN),
    Case(
        "mathml_cos",
        "Use <cos> of the taper angle for the offset",
        "Use  of the taper angle for the offset",
        _NH3_FOREIGN,
    ),
    Case("mathml_sin", "Feed correction is <sin> theta times DOC", "Feed correction is  theta times DOC", _NH3_FOREIGN),
    Case(
        "mathml_tan",
        "The <tan> of the lead angle drives the ramp",
        "The  of the lead angle drives the ramp",
        _NH3_FOREIGN,
    ),
    Case("mathml_log", "Record it in the maintenance <log> today", "Record it in the maintenance  today", _NH3_FOREIGN),
    Case(
        "mathml_multiple_triggers",
        "Check <min> and <max> on the CMM report",
        "Check  and  on the CMM report",
        _NH3_FOREIGN,
    ),
    # ------------------------------------------------------------------
    # Family B - "<" followed by a LETTER with no closing ">". bleach escapes the
    # "<" and keeps every character; nh3 discards to end of input.
    # ------------------------------------------------------------------
    Case(
        "unterminated_tir",
        "Runout<TIR spec on OP30 - see Bob",
        "Runout&lt;TIR spec on OP30 - see Bob",
        _NH3_UNTERMINATED + '; measured nh3 output is "Runout"',
    ),
    Case(
        "unterminated_od_id",
        "Check OD<ID before press fit, log in QMS",
        "Check OD&lt;ID before press fit, log in QMS",
        _NH3_UNTERMINATED + '; measured nh3 output is "Check OD"',
    ),
    Case(
        "unterminated_flatness",
        "Hold the part if flatness<Spec on the granite",
        "Hold the part if flatness&lt;Spec on the granite",
        _NH3_UNTERMINATED,
    ),
    Case(
        "unterminated_feed",
        "Feed<IPR recommended by the tooling vendor",
        "Feed&lt;IPR recommended by the tooling vendor",
        _NH3_UNTERMINATED,
    ),
    Case(
        "unterminated_bore",
        "Bore dia<Nominal after heat treat, shim as needed",
        "Bore dia&lt;Nominal after heat treat, shim as needed",
        _NH3_UNTERMINATED,
    ),
    Case("unterminated_lone_letter", "value<a", "value&lt;a", _NH3_UNTERMINATED),
    # ------------------------------------------------------------------
    # Control group - "<" in a NON-tag position. Identical on bleach and nh3.
    # If a replacement passes only this block and fails the two above, it is
    # destroying data; if it fails this block too, it is simply broken.
    # ------------------------------------------------------------------
    Case("safe_lt_dot", "wall<.120 nom", "wall&lt;.120 nom"),
    Case("safe_lt_equals", "qty<=10 reorder", "qty&lt;=10 reorder"),
    Case("safe_lt_digit", "x<1", "x&lt;1"),
    Case("safe_lt_space", "OD< ID error", "OD&lt; ID error"),
    Case("safe_lt_digit_tol", "tol <0.005 TIR per print", "tol &lt;0.005 TIR per print"),
    Case("safe_lt_minus", "gap<-0.002 after shrink", "gap&lt;-0.002 after shrink"),
    Case("safe_lt_underscore", "rev<_draft only, not released", "rev&lt;_draft only, not released"),
    Case("safe_gt_only", "OD > ID always", "OD &gt; ID always"),
    Case("safe_lt_gt_pair_numeric", "2.500 < OD < 2.505 acceptance band", "2.500 &lt; OD &lt; 2.505 acceptance band"),
    # ------------------------------------------------------------------
    # TODAY'S sanitizer loses data too, and this block is the evidence.
    #
    # bleach deletes ANY bracketed token, not just the ~98 MathML/SVG names —
    # <REF>, <TYP>, <BASIC> and <MMC> are standard drawing callouts, and they
    # vanish from the stored record. It is far milder than nh3 (the rest of the
    # sentence survives) but it is the same class of records-integrity defect the
    # nh3 evaluation rejected nh3 for, and it is live in production today.
    # nh3 does NOT diverge here: these are unknown element names rather than
    # foreign content, so both engines drop the token and keep the remainder.
    #
    # Pinned as characterization, NOT endorsed. If the sanitizer is ever changed
    # to preserve bracketed tokens, these are the rows to update.
    # ------------------------------------------------------------------
    Case("print_callout_ref", "Dim is 2.500 <REF> per print", "Dim is 2.500  per print"),
    Case("print_callout_typ", "Hole <TYP> 4 plcs", "Hole  4 plcs"),
    Case("print_callout_basic", "Angle 30 <BASIC>", "Angle 30 "),
    Case("print_callout_mmc", "Bore <MMC> callout", "Bore  callout"),
    # ------------------------------------------------------------------
    # Ordinary ERP field values - must survive byte-identical.
    # ------------------------------------------------------------------
    Case("plain_wo_number", "WO-2026-0417", "WO-2026-0417"),
    Case("plain_material", "AL 6061-T6511 bar stock, 2.500 OD", "AL 6061-T6511 bar stock, 2.500 OD"),
    Case("plain_operation", "Op 30 - CNC Mill - Haas VF-4SS", "Op 30 - CNC Mill - Haas VF-4SS"),
    Case("plain_customer_po", "Customer PO 4500123987 rev C", "Customer PO 4500123987 rev C"),
    Case("plain_lot_heat", "Lot L-240117-A / Heat 88213", "Lot L-240117-A / Heat 88213"),
    Case(
        "plain_ncr_narrative",
        "OP20 bore undersize 0.0015 in.; 3 pcs held, MRB dispositioned rework.",
        "OP20 bore undersize 0.0015 in.; 3 pcs held, MRB dispositioned rework.",
    ),
    Case(
        "plain_apostrophe",
        "Operator's note: gage R&R due 2026-08-01",
        "Operator's note: gage R&amp;R due 2026-08-01",
        _NH3_ENTITY,
    ),
    Case(
        "plain_quotes",
        'Print says "typ 4 plcs" but only 3 exist',
        'Print says "typ 4 plcs" but only 3 exist',
    ),
    # ------------------------------------------------------------------
    # Classic XSS payloads. Note the shape of the frozen behavior: with
    # strip=True bleach removes the TAG but keeps its text content, so the
    # script SOURCE survives as inert literal text. That is bleach's documented
    # behavior, it is safe once the value is HTML-escaped at render time, and it
    # is exactly the kind of thing a replacement engine changes silently.
    # ------------------------------------------------------------------
    Case("xss_script_tag", "<script>alert('xss')</script>", "alert('xss')"),
    Case("xss_img_onerror", "<img src=x onerror=alert(1)>", ""),
    Case("xss_anchor_javascript", '<a href="javascript:alert(1)">click</a>', "click"),
    Case("xss_svg_onload", "<svg/onload=alert(1)>", ""),
    Case("xss_iframe", '<iframe src="http://evil.example/"></iframe>', ""),
    Case("xss_body_onload", "<body onload=alert(1)>Inspection complete</body>", "Inspection complete"),
    Case("xss_style_url", '<div style="background:url(javascript:alert(1))">note</div>', "note"),
    Case(
        "xss_script_in_narrative",
        "Scrapped 2 pcs <script>fetch('/api/v1/users')</script> at OP40",
        "Scrapped 2 pcs fetch('/api/v1/users') at OP40",
    ),
    Case("xss_nested_script", "<scr<script>ipt>alert(1)</script>", "ipt&gt;alert(1)"),
    Case(
        "xss_encoded_script",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        _NH3_ENTITY,
    ),
    # ------------------------------------------------------------------
    # Malformed markup and paste artifacts.
    # ------------------------------------------------------------------
    Case("malformed_unclosed_bold", "<b>unclosed bold", "unclosed bold"),
    Case("malformed_stray_close", "</b> stray close tag", " stray close tag"),
    Case("malformed_angle_soup", "<<>>", "&lt;&lt;&gt;&gt;"),
    Case("malformed_empty_tag", "<>", "&lt;&gt;"),
    Case("malformed_spaced_tag", "< b >spaced tag< /b >", "&lt; b &gt;spaced tag&lt; /b &gt;"),
    Case("malformed_tag_in_tag", "<p<script>alert(1)</script>", "alert(1)"),
    Case("malformed_comparison_prose", "a < b and b > c", "a &lt; b and b &gt; c"),
    Case("malformed_comment", "<!-- internal note: do not ship --> ready", " ready"),
    Case("malformed_cdata", "<![CDATA[raw]]> after", " after"),
    Case("malformed_doctype", "<!DOCTYPE html> leftover paste", " leftover paste"),
    # ------------------------------------------------------------------
    # HTML entities. bleach passes a WELL-FORMED entity through untouched and
    # escapes a bare "&" (including the "&notanentity;" near-miss). nh3 decodes,
    # which makes every one of these discriminating.
    # ------------------------------------------------------------------
    Case("entity_amp", "Vendor &amp; Co.", "Vendor &amp; Co.", _NH3_ENTITY),
    Case("entity_quot", "&quot;critical&quot; characteristic", "&quot;critical&quot; characteristic", _NH3_ENTITY),
    Case("entity_copy", "&copy; 2026 Werco Manufacturing", "&copy; 2026 Werco Manufacturing", _NH3_ENTITY),
    Case("entity_not_an_entity", "&notanentity; stays put", "&amp;notanentity; stays put", _NH3_ENTITY),
    Case("entity_bare_ampersand", "R&D department sign-off", "R&amp;D department sign-off", _NH3_ENTITY),
    Case("entity_numeric", "Bore &#216;25.4 mm", "Bore &#216;25.4 mm", _NH3_ENTITY),
    Case(
        "entity_lt_gt",
        "&lt;MIN&gt; is the literal print callout",
        "&lt;MIN&gt; is the literal print callout",
        _NH3_ENTITY,
    ),
    # ------------------------------------------------------------------
    # Unicode / CJK / literal U+00A0. All must survive byte-identical - the
    # sanitizer must not normalize, transliterate, or NCR-encode non-ASCII.
    # ------------------------------------------------------------------
    Case("unicode_diameter", "Ø25.4 mm bore, ±0.05", "Ø25.4 mm bore, ±0.05"),
    Case("unicode_cjk", "検査完了 - 品質保証部", "検査完了 - 品質保証部"),
    Case("unicode_german", "Prüfbericht — Härte 45 HRC", "Prüfbericht — Härte 45 HRC"),
    Case("unicode_accents", "Émile's résumé note", "Émile's résumé note"),
    # Literal U+00A0 (a real no-break space character, not the "&nbsp;" entity).
    # Word/Excel paste puts these into ERP text constantly; the sanitizer must
    # neither drop them nor fold them to U+0020.
    Case("unicode_nbsp_literal", "\u00a0literal nbsp\u00a0here\u00a0", "\u00a0literal nbsp\u00a0here\u00a0"),
    Case("unicode_emoji", "Ship ✅ pending ⚠", "Ship ✅ pending ⚠"),
    Case("unicode_rtl", "ملاحظة الجودة", "ملاحظة الجودة"),
    # ------------------------------------------------------------------
    # Empty and whitespace-only. Pinned because "" vs None vs " " is exactly the
    # kind of thing a replacement gets subtly wrong, and because these values
    # reach required-field validation downstream.
    # ------------------------------------------------------------------
    Case("empty_string", "", ""),
    Case("whitespace_spaces", "   ", "   "),
    Case("whitespace_tab_newline", "\t\n ", "\t\n "),
    Case("whitespace_newline", "\n", "\n"),
]


# The four inputs quoted verbatim in the nh3 evaluation. A future migration must
# not be able to make this suite pass by quietly deleting the inconvenient rows,
# so their presence is asserted separately below.
NH3_DESTRUCTION_WITNESSES: tuple[str, ...] = (
    "Runout<TIR spec on OP30 - see Bob",
    "Check OD<ID before press fit, log in QMS",
    "Tolerance <MIN> per print, inspect 100%",
    "Qty <set> at OP20 - verify with gage",
)

# The bleach linkify-ReDoS advisory's payload shape (~30 KB of near-email text).
# Kept here as a size/shape characterization; tests/test_bleach_linkify_guard.py
# owns the security argument and the wall-clock bound.
REDOS_PAYLOAD_SHAPE = "a." * 15000

_CHANGED_ON_PURPOSE = (
    "The sanitizer's observable behavior CHANGED. This test is a characterization "
    "snapshot, not a specification: do not 'fix' it by editing the expectation until "
    "you have decided the new behavior is what you want. Whatever comes out of "
    "sanitize_string is what gets PERSISTED (app/main.py sanitize_input rewrites "
    "request._body), so a change here changes stored records."
)


@pytest.mark.parametrize("case", GOLDEN_CORPUS, ids=[c.label for c in GOLDEN_CORPUS])
def test_sanitize_string_matches_frozen_output(case: Case):
    """Every corpus input still produces its frozen bleach-6.4.0 output."""
    actual = sanitize_string(case.raw)
    detail = f"\n{case.nh3}" if case.nh3 else ""
    assert actual == case.expected, (
        f"[{case.label}] sanitize_string({case.raw!r})\n"
        f"  expected (frozen): {case.expected!r}\n"
        f"  actual:            {actual!r}\n"
        f"{_CHANGED_ON_PURPOSE}{detail}"
    )


@pytest.mark.parametrize("case", GOLDEN_CORPUS, ids=[c.label for c in GOLDEN_CORPUS])
def test_sanitize_string_is_idempotent(case: Case):
    """``f(f(x)) == f(x)`` for every corpus input.

    This is load-bearing rather than academic. ``sanitize_input`` re-sanitizes on
    every POST/PUT/PATCH, so an already-stored value goes back through the sanitizer
    each time a user opens a record and saves it. If sanitizing were not idempotent,
    ``R&D`` would drift to ``R&amp;D`` then ``R&amp;amp;D`` and so on, one edit at a
    time — cumulative, silent corruption of stored records. bleach is idempotent here
    because it recognizes a well-formed entity and leaves it alone; a replacement that
    decodes entities on input would break this immediately.
    """
    once = sanitize_string(case.raw)
    twice = sanitize_string(once)
    assert twice == once, (
        f"[{case.label}] sanitizing twice is not the same as sanitizing once — stored values will "
        f"drift on every edit.\n  raw:   {case.raw!r}\n  once:  {once!r}\n  twice: {twice!r}"
    )


def test_corpus_labels_are_unique():
    """Guard against a copy-paste that silently shadows a case in the ids list."""
    labels = [c.label for c in GOLDEN_CORPUS]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    assert duplicates == [], f"duplicate corpus labels: {duplicates}"


def test_nh3_destruction_witnesses_are_still_in_the_corpus():
    """The four measured nh3-destruction inputs must remain covered.

    Without this, a migration could turn the suite green by deleting exactly the rows
    that document why the migration is unsafe.
    """
    raws = {c.raw for c in GOLDEN_CORPUS}
    missing = [w for w in NH3_DESTRUCTION_WITNESSES if w not in raws]
    assert missing == [], (
        "the following measured nh3-destruction inputs were removed from the corpus; they are the "
        f"regression bait for a sanitizer swap and must stay: {missing}"
    )


def test_corpus_covers_both_nh3_divergence_families_and_a_control_group():
    """Structural check: the corpus keeps its three load-bearing blocks.

    Coverage counts, not just presence — a corpus trimmed to one token per family
    stops being evidence.
    """
    foreign = [c for c in GOLDEN_CORPUS if c.nh3.startswith(_NH3_FOREIGN)]
    unterminated = [c for c in GOLDEN_CORPUS if c.nh3.startswith(_NH3_UNTERMINATED)]
    controls = [c for c in GOLDEN_CORPUS if c.label.startswith("safe_lt") or c.label.startswith("safe_gt")]

    assert len(foreign) >= 25, f"foreign-content trigger words thinned out to {len(foreign)}"
    assert len(unterminated) >= 5, f"unterminated-'<' cases thinned out to {len(unterminated)}"
    assert len(controls) >= 8, f"the identical-on-both-engines control group thinned out to {len(controls)}"

    # The control group's defining property: bleach only escapes, never deletes.
    for case in controls:
        assert case.expected.replace("&lt;", "<").replace("&gt;", ">") == case.raw, (
            f"[{case.label}] is in the control group but is not a pure escape — it belongs in a "
            "destruction family, not the anti-overfit control block"
        )


def test_redos_payload_shape_passes_through_unchanged():
    """~30 KB of near-email text comes back byte-identical.

    Expectation stated as an identity rather than a 30 KB literal; it is still not
    computed from the library under test.
    """
    assert sanitize_string(REDOS_PAYLOAD_SHAPE) == REDOS_PAYLOAD_SHAPE


# ---------------------------------------------------------------------------
# sanitize_string: non-string inputs and the unused allow_tags seam.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ""),
        (0, "0"),
        (42, "42"),
        (False, "False"),
        (True, "True"),
        (3.5, "3.5"),
        (["a", "b"], "['a', 'b']"),
    ],
    ids=["none", "zero", "int", "false", "true", "float", "list"],
)
def test_sanitize_string_coerces_non_string_inputs(value, expected):
    """Non-``str`` input is coerced with ``str()`` (``None`` becomes ``""``).

    Worth pinning because the coercion is lossy in a surprising direction: a
    numeric ``0`` becomes the string ``"0"`` and ``None`` becomes ``""``, not
    ``"None"``. Callers that hand this a non-string get a string back.
    """
    assert sanitize_string(value) == expected


def test_sanitize_string_allow_tags_seam_is_honored_but_unused():
    """``allow_tags`` works, and no production caller passes it.

    ``app/core/sanitization.py`` defaults it to ``None`` -> ``tags=[]``. Pinned so
    that a replacement API which drops or reinterprets this parameter is noticed.
    """
    assert sanitize_string("<b>bold</b> text", allow_tags=["b"]) == "<b>bold</b> text"
    assert sanitize_string("<b>bold</b> text") == "bold text"


# ---------------------------------------------------------------------------
# sanitize_dict
# ---------------------------------------------------------------------------


def test_sanitize_dict_sanitizes_flat_string_values():
    payload = {
        "work_order_number": "WO-2026-0417",
        "notes": "Runout<TIR spec on OP30 - see Bob",
        "description": "<script>alert(1)</script>bore undersize",
    }
    assert sanitize_dict(payload) == {
        "work_order_number": "WO-2026-0417",
        "notes": "Runout&lt;TIR spec on OP30 - see Bob",
        "description": "alert(1)bore undersize",
    }


def test_sanitize_dict_recurses_into_nested_dicts():
    payload = {"outer": {"inner": {"notes": "<b>deep</b> note", "keep": "plain"}}}
    assert sanitize_dict(payload) == {"outer": {"inner": {"notes": "deep note", "keep": "plain"}}}


def test_sanitize_dict_maps_over_string_elements_of_a_list():
    payload = {"tags": ["<b>one</b>", "two", "Qty <set> at OP20 - verify with gage"]}
    assert sanitize_dict(payload) == {"tags": ["one", "two", "Qty  at OP20 - verify with gage"]}


def test_sanitize_dict_passes_non_string_scalars_through_untouched():
    """Numbers, bools, ``None`` and non-string list elements are returned as-is.

    Note the contrast with ``sanitize_string``, which coerces the same values to
    strings: inside a dict they keep their type, because ``sanitize_dict`` only calls
    ``sanitize_string`` on values that are already ``str``.
    """
    payload = {
        "quantity": 42,
        "unit_cost": 12.75,
        "is_active": True,
        "closed_at": None,
        "mixed_list": [1, 2.5, True, None],
    }
    assert sanitize_dict(payload) == payload
    assert sanitize_dict(payload)["quantity"] == 42
    assert isinstance(sanitize_dict(payload)["quantity"], int)


def test_sanitize_dict_does_not_recurse_into_dicts_inside_lists():
    """KNOWN GAP, pinned as-is: a dict nested inside a list is NOT sanitized.

    ``sanitize_dict`` recurses for a dict value and maps ``sanitize_string`` over the
    *string* elements of a list value — a dict element inside that list falls through
    the ``isinstance(item, str)`` check and is returned untouched.

    This is a real hole, not a quirk: JSON request bodies routinely carry lists of
    objects (BOM lines, PO lines, routing operations), and every string field on those
    objects reaches the database unsanitized. It is characterized here rather than
    fixed because closing it changes what gets persisted on those paths and deserves
    its own change and its own review. If someone fixes it, THIS test is the one that
    should fail and be rewritten.
    """
    payload = {"lines": [{"notes": "<script>alert(1)</script>", "part": "<b>PN-1</b>"}]}
    result = sanitize_dict(payload)
    assert result == {"lines": [{"notes": "<script>alert(1)</script>", "part": "<b>PN-1</b>"}]}
    assert result["lines"][0]["notes"] == "<script>alert(1)</script>"


def test_sanitize_dict_keys_filter_limits_what_is_sanitized():
    """With ``keys`` supplied, unlisted keys pass through verbatim.

    The filter is applied at every level of the recursion (the same ``keys`` list is
    passed down into nested dicts), so a nested key must be named to be sanitized.
    No production caller uses this parameter today — ``sanitize_input`` calls
    ``sanitize_dict(data)`` with one argument.
    """
    payload = {"notes": "<b>x</b>", "raw": "<b>y</b>", "nested": {"notes": "<b>z</b>", "raw": "<b>w</b>"}}
    assert sanitize_dict(payload, keys=["notes", "nested"]) == {
        "notes": "x",
        "raw": "<b>y</b>",
        "nested": {"notes": "z", "raw": "<b>w</b>"},
    }


def test_sanitize_dict_returns_a_new_dict_and_does_not_mutate_the_input():
    payload = {"notes": "<b>x</b>", "nested": {"notes": "<b>y</b>"}}
    result = sanitize_dict(payload)
    assert payload == {"notes": "<b>x</b>", "nested": {"notes": "<b>y</b>"}}
    assert result is not payload
    assert result["nested"] is not payload["nested"]


def test_sanitize_dict_handles_an_empty_dict():
    assert sanitize_dict({}) == {}
