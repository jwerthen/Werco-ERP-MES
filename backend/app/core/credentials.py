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
:data:`API_TOKEN_COMMENT_WRITE` is the refusal every comment / watcher write answers an API
token (any scope): a comment is a person's own words. :data:`PEOPLE_ONLY_HANK_TASK_KINDS` names the
Hank task kinds that are comment writes too (``add_note``), and
:data:`AGENT_PEOPLE_ONLY_TASK_DETAIL` is what the MCP package answers an agent that tries one.

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

# Badge sign-on credentials (AS9100D 8.5.2): setting, clearing or unlocking a user's floor PIN
# (``PUT`` / ``DELETE /users/{user_id}/floor-pin``, ``POST .../floor-pin/unlock``) and turning the company's "require a PIN
# for badge sign-in" policy on or off (``PUT /companies/me/floor-pin-policy``). Credential
# administration is a person's act, so each of those routes refuses ANY API token, whatever its
# role or scope, with this text before it reads or writes anything. None of them is an MCP tool
# (``app.mcp.catalog.FLOOR_PIN_OPERATIONS``): a person's own JWT would otherwise let an agent
# handle a PIN. A crew-station badge token never reaches them (its path fence keeps it on the
# shop floor).
API_TOKEN_FLOOR_PIN_ADMIN = (
    "A floor PIN, and the company's badge-sign-in PIN policy, are set by a person signed in to the app, "
    "not an API token: credential administration is a person's act."
)

# Self-service password change (``POST /users/change-password``). A person changes their own
# password signed in to the app in their own session -- never through a standing API token, and
# never from a crew-station badge token or station token. The route refuses every credential that
# is not ``is_interactive_person`` 403 with this text before it reads the body's passwords. (A
# badge token is already turned away by its shop-floor path fence in ``get_current_user``; the
# explicit check keeps the rule in one place rather than resting on a fence elsewhere.) Not an MCP
# tool (``app.mcp.catalog.PASSWORD_OPERATIONS``): a person's own JWT would otherwise let an agent
# change that person's password.
PASSWORD_CHANGE_PEOPLE_ONLY = (
    "Your password is changed by you, signed in to the app: an API token or a shop-floor badge or station "
    "token cannot change a password."
)

# Restricted document viewing (AS9100D 7.5.3.1 / 7.5.3.2, migration 131). Deciding who may open a
# document (``POST /documents/{document_id}/access-level``) and who may open export-controlled ones
# (``PUT /users/{user_id}/export-authorization``) are a person's judgment, so both routes refuse ANY
# API token, whatever its role or scope, with these texts before they read or write anything.
# Neither is an MCP tool (``app.mcp.catalog.DOCUMENT_ACCESS_OPERATIONS``): a person's own JWT would
# otherwise let an agent grant export access or lower an export-controlled drawing's level.
API_TOKEN_DOCUMENT_ACCESS_LEVEL = (
    "A document's access level is changed by an Admin or Quality user signed in to the app, not an API token: "
    "deciding who may open a controlled document is a person's judgment."
)
API_TOKEN_EXPORT_AUTHORIZATION = (
    "Export authorization is granted or revoked by an Admin signed in to the app, not an API token: deciding "
    "who may open export-controlled documents is a person's judgment."
)

# In-process inspection sign-off (``POST /shop-floor/operations/{operation_id}/inspection``,
# AS9100D 8.6). Signing that an inspection was performed is quality evidence someone has to have
# done, the same judgment as an FAI final disposition, so the route refuses ANY API token,
# whatever its role or scope, with this text before it reads or writes anything. It is not an MCP
# tool (``app.mcp.catalog.INSPECTION_SIGN_OFF_OPERATIONS``). A crew-station badge token is a
# person, not an API token, but the sign-off is a desk verb: the kiosk path fence
# (``deps._is_kiosk_scope_allowed_path``) refuses this path to every badge token, as it does labor
# approval, so no crew-station badge of any role can sign off. Pinned by
# ``tests/api/test_inspection_sign_off.py``.
API_TOKEN_INSPECTION_SIGN_OFF = (
    "An inspection is signed off by a person signed in to the app, not an API token: recording that an "
    "inspection was performed is a person's judgment."
)

# Comments and watchers (docs/NOTIFICATIONS_PLAN.md section 3.6; owner decision for work-order
# comments, 2026-11-16). A comment is a person's own words and a watch is a person's own
# preference, so every comment / watcher WRITE (``POST``/``PATCH``/``DELETE /comments``,
# ``POST``/``DELETE /watchers``) refuses ANY API token, whatever its role or scope, with this
# text before it reads or writes anything. The reads (the thread, the watch state, mention
# search) stay open to a token the read gates admit. None of the writes is an MCP tool
# (``app.mcp.catalog.COMMENT_WRITE_OPERATIONS``). A crew-station badge token never reaches
# them (its path fence keeps it on the shop floor). Hank's ``add_note`` task, which posts a
# work-order comment, answers an API token the same text at prepare and at execute
# (``hank_task_service.PEOPLE_ONLY_KINDS``).
API_TOKEN_COMMENT_WRITE = (
    "A comment is a person's own words: an API token cannot post, edit or delete comments or change who is "
    "watching a record. Sign in to the app."
)

# The Hank task kinds only a PERSON may run, because each posts a person's own words (owner decision
# for work-order comments, 2026-11-16): ``add_note`` posts a work-order comment. Two gates read this
# one set, at the two doors an automated caller could use:
#
# * ``hank_task_service`` refuses ANY API token 403 (``API_TOKEN_COMMENT_WRITE``) at prepare and
#   execute, and leaves these kinds out of a token's capabilities;
# * the MCP package (``app.mcp.people_only``) refuses an AGENT on ANY credential -- a person's own
#   access JWT included -- 403 (``AGENT_PEOPLE_ONLY_TASK_DETAIL``) before ``prepare_task`` or
#   ``execute_task`` is dispatched for one, for the reason the comment write routes are not MCP tools
#   at all (``app.mcp.catalog.COMMENT_WRITE_OPERATIONS``): a person's JWT would otherwise let an agent
#   post words in that person's name.
#
# A person in the app prepares and executes them as usual. Kept here, in this import-free module,
# so the MCP package can read it without importing a service.
PEOPLE_ONLY_HANK_TASK_KINDS = frozenset({"add_note"})
AGENT_PEOPLE_ONLY_TASK_DETAIL = "A note is a person's own words: post it from the app, not through an agent."


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
