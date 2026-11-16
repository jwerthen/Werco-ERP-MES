"""Credential predicates for judgment writes.

``get_current_user`` (``app/api/deps.py``) marks every authenticated ``User`` with the
credential that carried the request:

* ``_api_token_id`` -- the ``api_tokens`` row id when a long-lived per-user API token
  (``type="api"``, bots and MCP clients) authenticated the request, else ``None``.
  An interactive credential clears it explicitly, because one ORM instance can serve
  more than one request through a shared identity map.
* ``_token_scope`` -- ``"api"`` for an API token, ``"kiosk"`` for a badge-minted
  crew-station operator token, ``None`` for an ordinary interactive session.

Two predicates exist on purpose, because the credential rules draw two different lines
(owner ruling 3 of 2026-11-05 for the AI writes; the owner's decision of 2026-11-07, and
this PR's extensions of it, for scrap, short closes and count judgments):

* :func:`is_api_token_credential` -- a standing machine credential. A crew-station badge
  token is a PERSON at the kiosk (the badge is theirs and they are standing there), so it
  is not refused by this predicate. Used where a person on the floor may act but a bot
  may not, e.g. declaring scrap or closing an operation short (the 2026-11-07 decision).
* :func:`is_interactive_person` -- a person signed in to the app in their own session:
  not an API token and not a station/badge-scoped token. Used where the ruling asks for
  "a named person clicking Accept" (ruling 3: AI recommendations are applied only by a
  person in the app).

Both read attributes with ``getattr`` defaults so a ``User`` built outside a request
(a worker, a test fixture) reads as an interactive person, which is what it is to every
other gate. ``app/services/hank_task_service.py`` and its siblings carry older inline
copies of the stricter predicate; they can migrate here later.

Two more predicates narrow an API token further (migration ``119_api_token_scope``,
the Claude-plugin brief P0.1 / P0.3). ``get_current_user`` copies the ``api_tokens``
row's ``scope`` / ``ai_external`` onto the user as ``_api_token_scope`` /
``_api_token_ai_external`` (an interactive credential clears them):

* :func:`is_read_only_api_token` -- an API token minted ``scope = "read_only"``: GET
  routes only, never a tag in ``WERCO_MCP_READ_ONLY_DENY_TAGS``.
* :func:`is_ai_external_credential` -- an API token whose answers go to an outside AI
  service (always read-only too).

Both FAIL CLOSED for an API token: a scope that is not exactly ``"full"`` (missing
included) reads as read-only, and an ``ai_external`` that is not exactly ``False`` reads
as AI-external. For a non-token credential both are False.

:func:`is_narrowed_api_token` is the union of the two -- the one predicate for "this
credential is a read-only connector" (an AI-external row is read-only whatever its scope
says). Reconcile-on-read uses it: a narrowed token's GET serves the stored state and
writes nothing (lead decision, Claude-plugin brief build 2026-11-15).

:data:`API_TOKEN_EGRESS_SWITCH` is the refusal every company egress kill-switch route
answers an API token (any scope): opening or closing data egress is a person's judgment.

This module is pure: it imports nothing from the app, so any layer can use it.
"""

from typing import Any

# Mirrors ``app.api.deps.API_TOKEN_SCOPE``; repeated here (not imported) so this module
# stays import-cycle free. ``tests/test_credentials.py`` pins the two together.
API_TOKEN_SCOPE = "api"
KIOSK_TOKEN_SCOPE = "kiosk"

# The two values of ``api_tokens.scope`` (``app.models.api_token.ApiTokenScope``), repeated
# here for the same import-cycle reason; ``tests/test_credentials.py`` pins them together.
API_TOKEN_SCOPE_FULL = "full"
API_TOKEN_SCOPE_READ_ONLY = "read_only"

# Refusal texts. The API layer's (``app.api.deps.get_current_user``) and the MCP door's
# (``app.mcp.server``) are worded for their callers; each is quoted verbatim by docs/MCP.md
# and docs/API.md, so change them only together with those.
READ_ONLY_ROUTE_WRITES_DETAIL = "Read-only API token cannot modify data."
READ_ONLY_ROUTE_DENIED_DETAIL = "Read-only API token cannot access this resource."
READ_ONLY_TOOL_WRITES_DETAIL = "Read-only API token: this tool writes."
READ_ONLY_TOOL_DENIED_DETAIL = "Read-only API token: this tool is not available to read-only tokens."

# The company egress kill switches -- ``allow_ai_egress`` (PUT /companies/me/ai-egress),
# ``allow_sms_egress`` (PUT /companies/me/sms-egress), ``allow_carrier_egress`` (PUT
# /admin/settings/shipping-profile) and ``allow_print_egress`` (PUT /receiving/print-profile)
# -- decide what leaves the CUI boundary, and the AI one also opens or closes every
# AI-external connector. Flipping one is a person's judgment (lead decision, Claude-plugin
# brief build 2026-11-15), so each of those routes refuses ANY API token, whatever its role
# or scope, with this text before it reads or writes anything; the two profile routes are
# refused whole, since they are the only writer of their switch. None of them is an MCP tool
# (``app.mcp.catalog.EXCLUDED_OPERATIONS``), so an agent on a person's own JWT cannot reach
# them either. docs/RBAC_PERMISSIONS.md -> Credential policy for judgment writes quotes it.
API_TOKEN_EGRESS_SWITCH = (
    "A company egress kill switch (AI, SMS, carrier or label-print egress) is turned on or off by an Admin "
    "signed in to the app, not an API token: opening or closing data egress is a person's judgment."
)


def is_api_token_credential(user: Any) -> bool:
    """True when the request was authenticated by a long-lived per-user API token."""
    return getattr(user, "_api_token_id", None) is not None


def is_interactive_person(user: Any) -> bool:
    """True when a person is signed in to the app in their own session.

    False for an API token and for a station- or badge-scoped token.
    """
    if is_api_token_credential(user):
        return False
    return getattr(user, "_token_scope", None) not in (API_TOKEN_SCOPE, KIOSK_TOKEN_SCOPE)


def is_read_only_api_token(user: Any) -> bool:
    """True when an API token minted ``scope = "read_only"`` authenticated the request.

    Fails closed: any API token whose scope is not exactly ``"full"`` -- a missing marker
    included -- is read-only.
    """
    return is_api_token_credential(user) and getattr(user, "_api_token_scope", None) != API_TOKEN_SCOPE_FULL


def is_ai_external_credential(user: Any) -> bool:
    """True when an AI-external API token authenticated the request.

    Fails closed: any API token whose ``ai_external`` marker is not exactly ``False`` -- a
    missing marker included -- is AI-external.
    """
    return is_api_token_credential(user) and getattr(user, "_api_token_ai_external", None) is not False


def is_narrowed_api_token(user: Any) -> bool:
    """True when a read-only OR an AI-external API token authenticated the request.

    The union of :func:`is_read_only_api_token` and :func:`is_ai_external_credential` (both
    fail closed), because an AI-external row is read-only whatever its scope says. False for
    a full API token, an interactive session, a kiosk badge and a display / station token.
    """
    return is_read_only_api_token(user) or is_ai_external_credential(user)
