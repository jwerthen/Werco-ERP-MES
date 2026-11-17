"""Hank task kinds an agent may never run over MCP -- on ANY credential, a person's own JWT included.

Owner decision for work-order comments (2026-11-16): a comment is a person's own words. The comment
and watcher write routes are not MCP tools at all (``catalog.COMMENT_WRITE_OPERATIONS``) because a
person's own access JWT -- the stdio bridge's email / password, or the HTTP door -- would otherwise
let an agent post words in that person's name. Hank's ``add_note`` task is a comment write that
travels through two GENERATED tools that must stay tools for every other kind: ``prepare_task``
(``POST /hank/tasks``) and ``execute_task`` (``POST /hank/tasks/{task_id}/execute``). This module is
the one place that closes that door, and it is ONE code path, :func:`people_only_refusal`, called by
``server.on_call_tool`` after argument validation and before the call is dispatched:

* ``prepare_task`` whose ``kind`` is in ``credentials.PEOPLE_ONLY_HANK_TASK_KINDS`` -> 403
  (``credentials.AGENT_PEOPLE_ONLY_TASK_DETAIL``). The kind is read from the JSON body the call
  would send, so it is judged on exactly what the route would receive.
* ``execute_task`` -> the task is first READ through the same router, as the same caller
  (``GET /hank/tasks/{task_id}``); a people-only kind -> the same 403, and the execute is never
  dispatched. When the read is REFUSED for good (401 / 403 / 404 / 422: absent, foreign, another
  credential's task, a role that may not see it), the execute is NOT dispatched either: the read's
  own status and verbatim ``detail`` are the tool's error result. That is the answer the execute
  route would have given -- it resolves the task through the same ``HankTaskService.get`` -- so it
  tells the caller nothing ``get_task`` does not. Dispatching it instead "so the route's own error
  wins" would be a check-then-act gap: a people-only task that became visible to the caller between
  the read and the execute would be run unread. Any other outcome (a 5xx, a 429, a transport
  failure, a body without a ``kind``) means the kind is unknown, and the execute is NOT dispatched:
  503 with a neutral text. That fails closed without telling the caller anything, where proceeding
  would let a transient read failure post a note. So the execute is dispatched ONLY after a 200 read
  named a kind outside the people-only set.

The routes themselves are unchanged: a person in the app prepares and executes a note as usual, and
an API token is refused by ``hank_task_service`` on every door. Matching is by ROUTE (method + path
template), never by tool name, because generated names can shift when a router adds a twin.
``change_due_date`` is NOT here: it stays an agent action (owner follow-up), in parity with
``update_work_order``, which already sets ``due_date``.

Like the rest of the package this imports no service: the kind set and the refusal text live in the
import-free ``app.core.credentials``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from app.core.config import settings
from app.core.credentials import AGENT_PEOPLE_ONLY_TASK_DETAIL, PEOPLE_ONLY_HANK_TASK_KINDS
from app.mcp.results import extract_detail

# A read refused with one of these is refused for good: the execute route resolves the task through
# the same ``HankTaskService.get`` and would answer the same way, so the read's own answer is returned
# and the execute is never dispatched (dispatching it would run a task that appeared after the read).
_FINAL_REFUSALS = frozenset({401, 403, 404, 422})
AGENT_TASK_UNVERIFIED_DETAIL = (
    "Could not confirm what kind of Hank task this is before executing it, so nothing was executed. Try again."
)

logger = logging.getLogger(__name__)

_HANK_TASKS = f"{settings.API_V1_PREFIX.rstrip('/')}/hank/tasks"
_EXECUTE_SUFFIX = "/execute"
# The two routes a people-only kind travels through, by (method, path template).
HANK_PREPARE_ROUTE = ("POST", _HANK_TASKS)
HANK_EXECUTE_ROUTE = ("POST", _HANK_TASKS + "/{task_id}" + _EXECUTE_SUFFIX)
PEOPLE_ONLY_ROUTES = frozenset({HANK_PREPARE_ROUTE, HANK_EXECUTE_ROUTE})


def _kind_of(body: Any) -> Optional[str]:
    kind = body.get("kind") if isinstance(body, dict) else None
    return kind if isinstance(kind, str) else None


async def people_only_refusal(
    *, method: str, route: str, path: str, json_body: Any, executor: Any, auth: Any
) -> Optional[Tuple[int, Any]]:
    """``(status, detail)`` when this call must not be dispatched, else None.

    403 when it would run a people-only Hank task kind; the read's own status and ``detail`` when an
    execute's task read is refused for good (401 / 403 / 404 / 422); 503 when it could not be read
    to find out. ``route`` is the tool's path TEMPLATE and ``path`` the concrete path the call would
    hit. Only the execute case makes a request of its own (one GET, as the caller); every other tool
    returns None without touching the executor.
    """
    if (method, route) == HANK_PREPARE_ROUTE:
        if _kind_of(json_body) in PEOPLE_ONLY_HANK_TASK_KINDS:
            return 403, AGENT_PEOPLE_ONLY_TASK_DETAIL
        return None
    if (method, route) != HANK_EXECUTE_ROUTE or not path.endswith(_EXECUTE_SUFFIX):
        return None
    try:
        outcome = await executor.request(
            method="GET",
            path=path[: -len(_EXECUTE_SUFFIX)],
            query={},
            json=None,
            files=None,
            form=None,
            auth=auth,
        )
        if outcome.status in _FINAL_REFUSALS:
            # Refused to this caller: answer what the execute route would, and never dispatch it.
            return outcome.status, extract_detail(outcome)
        kind = _kind_of(outcome.json()) if outcome.status == 200 and outcome.is_json else None
    except Exception:  # noqa: BLE001 - an unreadable task is refused below, never executed blind
        logger.warning("people-only pre-read of %s failed; the execute is not dispatched", path, exc_info=True)
        kind = None
    if kind is None:
        return 503, AGENT_TASK_UNVERIFIED_DETAIL
    if kind in PEOPLE_ONLY_HANK_TASK_KINDS:
        return 403, AGENT_PEOPLE_ONLY_TASK_DETAIL
    return None
