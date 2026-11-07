"""Credential predicates for judgment writes.

``get_current_user`` (``app/api/deps.py``) marks every authenticated ``User`` with the
credential that carried the request:

* ``_api_token_id`` -- the ``api_tokens`` row id when a long-lived per-user API token
  (``type="api"``, bots and MCP clients) authenticated the request, else ``None``.
  An interactive credential clears it explicitly, because one ORM instance can serve
  more than one request through a shared identity map.
* ``_token_scope`` -- ``"api"`` for an API token, ``"kiosk"`` for a badge-minted
  crew-station operator token, ``None`` for an ordinary interactive session.

Two predicates exist on purpose, because the owner rulings of 2026-11-05 draw two
different lines:

* :func:`is_api_token_credential` -- a standing machine credential. A crew-station badge
  token is a PERSON at the kiosk (the badge is theirs and they are standing there), so it
  is not refused by this predicate. Used where a person on the floor may act but a bot
  may not, e.g. closing an operation short (ruling 1).
* :func:`is_interactive_person` -- a person signed in to the app in their own session:
  not an API token and not a station/badge-scoped token. Used where the ruling asks for
  "a named person clicking Accept" (ruling 3: AI recommendations are applied only by a
  person in the app).

Both read attributes with ``getattr`` defaults so a ``User`` built outside a request
(a worker, a test fixture) reads as an interactive person, which is what it is to every
other gate. ``app/services/hank_task_service.py`` and its siblings carry older inline
copies of the stricter predicate; they can migrate here later.

This module is pure: it imports nothing from the app, so any layer can use it.
"""

from typing import Any

# Mirrors ``app.api.deps.API_TOKEN_SCOPE``; repeated here (not imported) so this module
# stays import-cycle free. ``tests/test_credentials.py`` pins the two together.
API_TOKEN_SCOPE = "api"
KIOSK_TOKEN_SCOPE = "kiosk"


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
