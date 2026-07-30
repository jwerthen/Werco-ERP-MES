"""Guard for **GHSA-g75f-g53v-794x** (`bleach` linkify ReDoS), the one advisory in
``docs/SECURITY_ADVISORY_SUPPRESSIONS.md`` that no scanner will ever flag again.

**Read this before deciding the file is redundant.** ``bleach`` is pinned at
6.4.0, and after that bump ``pip-audit`` reports **nothing** for bleach. That is
a *database artifact, not a fix*:

* The defect is a catastrophic-backtracking regex (CWE-1333, CVSS 4.3,
  availability-only — the advisory explicitly rules out XSS, authn bypass,
  disclosure and RCE) in ``bleach/linkifier.py`` →
  ``LinkifyFilter.handle_email_addresses()``, which runs
  ``self.email_re.finditer(text)`` over the pattern from ``build_email_re()``.
  It triggers on ``bleach.linkify(text, parse_email=True)`` with ~20-30 KB of
  near-email text such as ``"a." * 15000`` (no ``@`` required) — measured
  upstream at 1.4 ms → 8.7 s, roughly 180x.
* 6.4.0 did **not** fix it. Diffed against 6.3.0, ``handle_email_addresses`` is
  byte-identical and the only change in ``build_email_re`` is cosmetic (a
  ``.format()`` call reflowed onto one line). The regex is unchanged.
* The OSV record lists an explicit version list of exactly ``["6.3.0"]`` — not a
  range. It was published 2026-06-16, after bleach was archived (2026-06-10) and
  after 6.4.0 shipped as the final release there will ever be. So the scanner
  went quiet without anything being repaired.

**Consequence: there is no allowlist entry and no ``--ignore-vuln`` flag for this
advisory, because the scanner is already silent. If someone later introduces
``linkify()``, nothing will warn them. These tests are the only remaining
protection.**

The app is safe today because it never reaches the vulnerable code path, and
that is structural rather than incidental:

1. The only bleach import in the repo is ``app/core/sanitization.py:1`` →
   ``from bleach import clean``.
2. The single call is ``clean(value, tags=allow_tags or [], strip=True)``;
   ``allow_tags`` defaults to ``None`` and no caller passes it.
3. ``parse_email`` is not a parameter of ``clean()`` at all — it exists only on
   ``linkify()`` / ``Linker`` / ``LinkifyFilter``.
4. **The load-bearing link:** module-level ``clean()`` constructs
   ``Cleaner(...)`` passing **no ``filters``**, so ``Cleaner.filters == []`` and
   ``LinkifyFilter`` is never constructed.

**The input is attacker-controlled, and the argument does not rest on that.**
``sanitize_string`` is reached from the ``sanitize_input`` HTTP middleware
(``app/main.py:681,691`` → ``sanitize_dict`` → ``sanitize_string`` → ``clean``),
which runs on every JSON-bodied POST/PUT/PATCH, *before* route-level auth
dependencies, with no request-body size cap. An unauthenticated caller can hand
this code the advisory's exact payload. The safety argument rests **entirely** on
the linkify code path not existing here — not on the input being trustworthy.

The realistic way this gets armed is bleach's own documented clean+linkify
recipe, ``Cleaner(filters=[partial(LinkifyFilter, parse_email=True)])``.

**These tests were mutation-verified against exactly that change**, applied to
the real ``sanitize_string``. Four of the six fail on it: the surface scan, the
poisoned-constructor check, the anchor-emission check, and the wall-clock bound
(which went 0.0015 s → 3.5 s, a ~2300x blowup reproducing the advisory locally).
The two that survive it do so for different reasons, and both are worth keeping:
``test_clean_cannot_reach_the_linkify_filter`` watches ``bleach``'s own API
surface (which that mutation bypasses by constructing ``Cleaner`` directly), so
it is the one that would catch a future bleach release putting
``filters``/``parse_email`` on ``clean()`` itself; and
``test_redos_payload_passes_through_the_sanitizer_unchanged`` is a correctness
baseline, not an arming detector — the ReDoS payload has no ``@``, so linkify
burns 3.5 s on it and still returns it unchanged. That last point is why the
wall-clock test exists at all: on this payload, output equality alone cannot
distinguish armed from safe.
"""

import inspect
import time
from pathlib import Path

import bleach
import bleach.linkifier
import pytest
from bleach.sanitizer import Cleaner

from app.core.sanitization import sanitize_string

pytestmark = pytest.mark.unit

_APP_DIR = Path(__file__).resolve().parents[1] / "app"

# The advisory's payload shape: ~30 KB of near-email text, no "@" needed.
_REDOS_PAYLOAD = "a." * 15000

_ADVISORY = (
    "GHSA-g75f-g53v-794x (bleach linkify ReDoS) has NO upstream fix and is invisible to pip-audit — "
    "see docs/SECURITY_ADVISORY_SUPPRESSIONS.md and this module's docstring"
)


def test_clean_cannot_reach_the_linkify_filter():
    """The structural claim: ``clean()`` has no seam that reaches ``LinkifyFilter``.

    Two halves. ``clean()`` exposes neither ``parse_email`` (which would arm the
    vulnerable ``handle_email_addresses`` directly) nor ``filters`` (which would
    let a caller inject ``LinkifyFilter`` into the pipeline). And the ``Cleaner``
    that ``clean()`` builds with the app's exact arguments carries an empty
    filter list, so no filter — vulnerable or otherwise — runs at all.
    """
    clean_params = set(inspect.signature(bleach.clean).parameters)
    assert "parse_email" not in clean_params, (
        f"bleach.clean now accepts parse_email (params: {sorted(clean_params)}); the vulnerable email regex "
        f"is reachable from the app's sanitizer. {_ADVISORY}"
    )
    assert "filters" not in clean_params, (
        f"bleach.clean now accepts filters (params: {sorted(clean_params)}); a caller could inject "
        f"LinkifyFilter into the sanitizer pipeline. {_ADVISORY}"
    )

    # `filters` IS a Cleaner parameter — the seam exists, it is simply never used.
    # Asserting that keeps the next assertion from passing vacuously if bleach
    # ever dropped the kwarg outright.
    assert "filters" in inspect.signature(Cleaner.__init__).parameters

    # These are exactly the arguments app/core/sanitization.py passes through
    # `clean()`: tags=[] (allow_tags defaults to None; no caller overrides it)
    # and strip=True.
    assert Cleaner(tags=[], strip=True).filters == [], (
        "the Cleaner built by the app's clean() call now carries filters; if one of them is LinkifyFilter "
        f"the ReDoS is live on the sanitizer path. {_ADVISORY}"
    )


def test_no_linkify_surface_anywhere_under_app():
    """Catch a new linkify import site anywhere in ``app/``, not just in
    ``sanitization.py``.

    A grep rather than an import check on purpose: this fails on the *commit*
    that introduces the call, without needing that code to be exercised.
    """
    forbidden = ("linkify", "parse_email", "Linker", "LinkifyFilter")
    hits: list[str] = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in forbidden:
                if token in line:
                    hits.append(f"{path.relative_to(_APP_DIR.parent)}:{lineno}: {line.strip()}")

    assert hits == [], (
        "linkify surface appeared under app/ — the sanitizer's safety argument is that this code path does "
        "not exist here. Occurrences:\n  " + "\n  ".join(hits) + f"\n{_ADVISORY}"
    )


def test_sanitize_string_never_constructs_a_linkify_filter(monkeypatch):
    """Prove the absence by construction rather than by grep.

    Poison ``LinkifyFilter.__init__`` so that merely instantiating it explodes,
    then run the app's real sanitizer over HTML-bearing input. If ``clean()``
    ever grows a linkify filter, this raises instead of returning.

    ``sanitize_string`` is called directly, not through the middleware — the
    middleware wraps sanitization in a broad ``except Exception`` that logs a
    warning and continues, which would swallow the proof.
    """

    def _explode(*args, **kwargs):
        raise AssertionError(
            f"LinkifyFilter was constructed while sanitizing input — the ReDoS path is live. {_ADVISORY}"
        )

    monkeypatch.setattr(bleach.linkifier.LinkifyFilter, "__init__", _explode)

    result = sanitize_string('<script>alert(1)</script>Contact <b>a.b.c@example.com</b> <a href="#">here</a>')

    # Sanity check that the call did real work rather than short-circuiting.
    assert "<script>" not in result
    assert "<b>" not in result
    assert "a.b.c@example.com" in result


def test_sanitizer_does_not_emit_anchors_for_plain_text():
    """The behavioral tell, and the cheapest deterministic one.

    Verified by mutation: arming the recipe makes ``sanitize_string`` return
    ``mail <a href="mailto:a.b@example.com">a.b@example.com</a> now`` for
    plain-text input. The anchor survives ``tags=[]`` + ``strip=True`` because
    ``LinkifyFilter`` runs *after* sanitization — so the sanitizer would not just
    become slow, it would start **emitting** HTML it was called to remove.
    """
    assert "<a" not in sanitize_string(
        "mail a.b@example.com now"
    ), f"the sanitizer linkified an email address — LinkifyFilter is in the pipeline. {_ADVISORY}"
    assert "<a" not in sanitize_string(
        "see http://example.com/x now"
    ), f"the sanitizer linkified a URL — LinkifyFilter is in the pipeline. {_ADVISORY}"


def test_redos_payload_passes_through_the_sanitizer_unchanged():
    """The advisory's exact payload shape is inert on the app's path.

    ``clean()`` with ``tags=[]`` and no filters has no HTML to strip here, so the
    30 KB of near-email text comes back byte-identical. Deterministic — no
    timing involved.
    """
    assert sanitize_string(_REDOS_PAYLOAD) == _REDOS_PAYLOAD


def test_redos_payload_does_not_blow_up_wall_clock():
    """Order-of-magnitude backstop, and the only guard here that measures the
    ReDoS itself rather than its preconditions.

    Measured on this payload, 200 runs while the full suite ran concurrently
    under ``-n auto``: median 1.3 ms, p99 5.6 ms, worst 56 ms. Armed with the
    linkify recipe it takes **3.5 s**. The 1.0 s bound sits in that gap with 18x
    headroom over the worst healthy sample and 3.5x under the armed cost.

    **If this ever flakes, delete it rather than loosening it toward 3.5 s** —
    the three deterministic guards above (surface scan, poisoned constructor,
    anchor emission) all catch the same mutation without a clock, so nothing is
    lost but the direct measurement.
    """
    started = time.perf_counter()
    sanitize_string(_REDOS_PAYLOAD)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, (
        f"sanitizing {len(_REDOS_PAYLOAD)} chars of near-email text took {elapsed:.2f}s (expected ~0.002s). "
        f"That is the signature of the vulnerable email regex running. {_ADVISORY}"
    )
