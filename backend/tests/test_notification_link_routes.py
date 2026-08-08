"""STRUCTURAL GUARD: every SPA route the backend can put in a notification must resolve.

A notification's ``link`` is a relative path handed to the React SPA -- in-app as a
``<Link to={...}>`` in the bell popover / inbox, and in email as ``FRONTEND_BASE_URL + link``
in the "Open in Werco" button. If the path matches no route declared in
``frontend/src/App.tsx``, the SPA falls through to its catch-all ``NotFound``, which is
mounted with NO ``Layout`` and NO ``PrivateRoute`` -- the sidebar, the top bar, and the bell
itself all disappear. To the user that reads as "the app broke", which is exactly how the
2026-08-07 ``receipt.created`` bug got escalated.

Four broken shapes shipped green because NOTHING checked this. These tests are the check.

HOW A PYTHON TEST KNOWS THE REACT ROUTE TABLE: it PARSES ``frontend/src/App.tsx``. That is
deliberate. App.tsx *is* the routing source of truth; a duplicated TS/Python constant would
be a second source that drifts from the first, which is precisely the failure mode being
guarded against. The "Backend Tests" CI job checks out the whole repo (``actions/checkout``
at repo root, ``working-directory: ./backend``), so the file is present -- and if it ever is
not, these tests FAIL LOUDLY rather than ``pytest.skip``, because a guard that silently
evaporates is not a guard.

Four assertions:

1. every template in ``notification_links.ALL_LINK_TEMPLATES`` resolves to a declared,
   non-catch-all route;
2. every shape in ``notification_links.LEGACY_LINK_SHAPES`` still has a redirect route --
   deleting one from App.tsx turns THIS backend test red, which is the whole point of the
   legacy-redirect compatibility guarantee (already-delivered emails carry those shapes as
   absolute URLs and can never be migrated);
3. every template is a relative single-slash path with no scheme (the anchor-sink fence:
   react-router renders a value with a scheme or a leading ``//`` as a plain external
   ``<a href>``);
4. NO inline link literal survives anywhere under ``backend/app`` -- that is what makes (1)
   TOTAL rather than a sample of whatever someone remembered to register.
"""

import re
import string
from pathlib import Path

import pytest

from app.services import notification_links as links

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_TSX = REPO_ROOT / "frontend" / "src" / "App.tsx"
BACKEND_APP = Path(__file__).resolve().parents[1] / "app"

# A regex that stopped matching would make every assertion below pass VACUOUSLY, so the
# parse is floored. App.tsx declared 87 non-catch-all routes when this guard was written.
_MIN_EXPECTED_ROUTES = 60


def _declared_route_regexes() -> list[re.Pattern]:
    """Compile every ``path="..."`` declared in App.tsx into a matcher.

    The catch-all ``path="*"`` is EXCLUDED on purpose -- it is what we are guarding
    against, not a destination.
    """
    assert APP_TSX.is_file(), (
        f"Cannot find {APP_TSX}. This guard parses the React route table directly and must "
        f"never be skipped; run the backend suite from a full repo checkout."
    )
    text = APP_TSX.read_text()
    raw = re.findall(r'path="([^"]+)"', text)
    paths = [p for p in raw if p != "*"]
    assert len(paths) >= _MIN_EXPECTED_ROUTES, (
        f"App.tsx route parse looks broken: found only {len(paths)} routes "
        f"(expected >= {_MIN_EXPECTED_ROUTES}). Fix the parser -- do NOT lower the floor."
    )
    compiled = []
    for path in paths:
        segments = [r"[^/]+" if seg.startswith(":") else re.escape(seg) for seg in path.split("/")]
        compiled.append(re.compile("^" + "/".join(segments) + "$"))
    return compiled


def _concrete(template: str) -> str:
    """Fill every ``{field}`` placeholder with a plausible integer PK."""
    fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    return template.format(**{name: "12345" for name in fields})


def _path_only(url: str) -> str:
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# 1. Forward: everything the backend can emit today resolves.
# ---------------------------------------------------------------------------


def test_every_emittable_template_resolves_to_a_real_route():
    regexes = _declared_route_regexes()
    for template in links.ALL_LINK_TEMPLATES:
        path = _path_only(_concrete(template))
        assert any(rx.match(path) for rx in regexes), (
            f"notification link template {template!r} resolves to {path!r}, which matches NO "
            f"route in frontend/src/App.tsx -- it would render the catch-all NotFound. "
            f"Add the route to App.tsx, or emit None instead of this link."
        )


def test_the_guard_itself_rejects_unresolvable_paths():
    """Self-test. Without this, a matcher that started matching EVERYTHING (or a route
    parse that silently over-generalized) would make every other assertion here pass while
    guarding nothing. Each path below is a shape that must NOT resolve -- ``/dashboard`` in
    particular is the dead href the daily-digest email template used to carry."""
    regexes = _declared_route_regexes()
    for known_bad in ("/dashboard", "/does-not-exist", "/purchasing/12345/extra", "/quality/ncr/1/2"):
        assert not any(rx.match(known_bad) for rx in regexes), (
            f"{known_bad!r} unexpectedly resolves -- the App.tsx route matcher has become "
            f"over-permissive and every other assertion in this file is now vacuous."
        )


def test_every_module_constant_is_registered_in_all_link_templates():
    """A new constant that never lands in ALL_LINK_TEMPLATES would escape test 1."""
    declared = {
        value
        for name, value in vars(links).items()
        if name.isupper() and isinstance(value, str) and value.startswith("/")
    }
    missing = declared - set(links.ALL_LINK_TEMPLATES)
    assert not missing, f"link constants missing from ALL_LINK_TEMPLATES (so unguarded): {sorted(missing)}"


# ---------------------------------------------------------------------------
# 2. Legacy: the redirect routes are a permanent compatibility guarantee.
# ---------------------------------------------------------------------------


def test_every_legacy_shape_still_has_a_redirect_route():
    """Rows already in ``notifications.link`` -- and absolute URLs already DELIVERED by
    email, which no migration can reach -- carry these shapes. ``vercel.json`` rewrites
    every path to index.html, so even a cold click from a mail client is resolved by React
    Router. The redirect routes must exist FOREVER; removing one breaks this test."""
    regexes = _declared_route_regexes()
    for shape in links.LEGACY_LINK_SHAPES:
        path = _path_only(_concrete(shape))
        assert any(rx.match(path) for rx in regexes), (
            f"legacy notification link shape {shape!r} ({path!r}) no longer resolves against "
            f"frontend/src/App.tsx. A redirect route for it was removed -- restore it. These "
            f"URLs are in delivered email and cannot be migrated."
        )


# ---------------------------------------------------------------------------
# 3. The anchor-sink fence: relative, single-slash, no scheme.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", links.ALL_LINK_TEMPLATES + links.LEGACY_LINK_SHAPES)
def test_templates_are_relative_single_slash_paths(template: str):
    assert template.startswith("/"), f"{template!r} is not root-relative"
    assert not template.startswith("//"), f"{template!r} is protocol-relative -- react-router treats it as EXTERNAL"
    scheme_head = template.split("/", 1)[0]
    assert ":" not in scheme_head, f"{template!r} looks like it carries a URL scheme"


# ---------------------------------------------------------------------------
# 4. No inline link literals anywhere else in the backend.
# ---------------------------------------------------------------------------


_INLINE_LINK_KWARG = re.compile(r'\blink(?:_path)?\s*=\s*f?"/')
_INLINE_LINK_CONTEXT = re.compile(r'"link_path"\s*:\s*f?"/')


def test_no_inline_link_literals_outside_the_registry():
    """Force every future author through ``notification_links``.

    ``notification_links.py`` itself is skipped -- and that skip is LOAD-BEARING, not
    optional: its module docstring quotes both patterns while stating the rule.

    Known, deliberate exclusion: ``services/auto_evidence_service.py`` emits ~12 QMS
    evidence deep links via a differently-named kwarg (``module_link=``), most of which are
    also dead routes. Those are tracked as separate follow-up work and do NOT match these
    patterns; if that service is ever routed through this registry, widen the regex too.
    """
    offenders: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        if path.name == "notification_links.py":
            continue
        source = path.read_text()
        for pattern in (_INLINE_LINK_KWARG, _INLINE_LINK_CONTEXT):
            for match in pattern.finditer(source):
                line_no = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(BACKEND_APP.parent)}:{line_no}")
    assert not offenders, (
        "Inline notification link literals found -- every link value must come from "
        f"app/services/notification_links.py so the route guard can cover it: {offenders}"
    )
