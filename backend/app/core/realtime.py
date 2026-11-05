import asyncio
import functools
import logging
from typing import Any, Callable, Coroutine, Set

from anyio import from_thread

logger = logging.getLogger(__name__)

# Strong references to scheduled broadcasts. The event loop holds only a weak
# reference to a task, so an unreferenced fire-and-forget task can be garbage
# collected before it runs. Each task removes itself when it finishes.
_in_flight: Set["asyncio.Task[Any]"] = set()


def _on_broadcast_done(task: "asyncio.Task[Any]") -> None:
    _in_flight.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Realtime broadcast %s failed: %s: %s", task.get_name(), type(exc).__name__, exc)


def _schedule_on_loop(bound: Callable[[], Coroutine[Any, Any, Any]], name: str) -> None:
    """Start ``bound`` as a task on the RUNNING loop. Must run on the event-loop thread."""
    task = asyncio.get_running_loop().create_task(bound(), name=name)
    _in_flight.add(task)
    task.add_done_callback(_on_broadcast_done)


def safe_broadcast(coro: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Schedule a realtime broadcast; never blocks or fails the calling request.

    KEYWORD ARGUMENTS ARE BOUND HERE, NOT FORWARDED. ``anyio.from_thread.run`` takes
    positional arguments only (its one keyword is ``token``), and this used to hand it
    ``company_id=...`` directly: ``TypeError`` before the coroutine was ever created,
    swallowed at DEBUG. Every tenant-scoped call site passes ``company_id`` by keyword,
    so every tenant-scoped broadcast was silently dropped, while the few call sites
    that left ``company_id`` out were the only ones that reached a socket -- and they
    reached EVERY tenant's. ``functools.partial`` carries the tenant scope into
    ``app.core.websocket``, whose helpers now require ``company_id`` and refuse ``None``.

    FIRE-AND-FORGET, not run-to-completion. Delivery revalidates every recipient
    socket's identity against the database (``ConnectionManager._may_deliver``), one
    socket at a time; waiting for that would hold the sync route's worker thread --
    and its threadpool token -- for sockets x broadcasts lookups, and the lookups
    themselves need threadpool tokens, so enough concurrent writes could starve the
    pool outright. The broadcast is therefore started as a task on the event loop and
    this returns at once: from a sync route (an AnyIO worker thread) via
    ``from_thread.run_sync``, and from an ``async`` route directly on the running loop
    (``from_thread`` refuses the loop thread, which silently dropped those too).

    Best-effort by design: call sites run after their commit, and a broadcast failure
    must never fail that request. Failures -- scheduling or delivery -- are logged at
    WARNING with the coroutine's name and the exception only, never the payload.
    """
    bound = functools.partial(coro, *args, **kwargs)
    name = f"realtime:{getattr(coro, '__name__', 'broadcast')}"
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # A worker thread (sync route): hop onto the loop just long enough to start the task.
            from_thread.run_sync(_schedule_on_loop, bound, name)
        else:
            _schedule_on_loop(bound, name)
    except Exception as exc:
        logger.warning("Realtime broadcast %s failed: %s: %s", name, type(exc).__name__, exc)
