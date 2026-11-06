import asyncio
import functools
import logging
import weakref
from typing import Any, Callable, Coroutine, Set

from anyio import from_thread

from app.core.config import settings

logger = logging.getLogger(__name__)

# Per-process ceiling on broadcasts waiting for their turn (see ``_deliver_in_turn``).
# Deliveries run one at a time, so a sustained burst -- or recipient checks slowed by
# a struggling database -- queues them; past this many, new broadcasts are dropped
# with a WARNING instead of growing the backlog without limit. A drop costs one
# realtime nudge: every screen that listens also polls, and the broadcasts already
# queued for the same company trigger the same refetch.
MAX_PENDING_BROADCASTS = 1000

# Strong references to scheduled broadcasts. The event loop holds only a weak
# reference to a task, so an unreferenced fire-and-forget task can be garbage
# collected before it runs. Each task removes itself when it finishes.
_in_flight: Set["asyncio.Task[Any]"] = set()

# One delivery lock per event loop. An asyncio primitive binds to the first loop that
# contends for it, and the tests run several loops (pytest-asyncio's, each TestClient
# portal's), so a single module-level lock would raise on the second one. Keyed weakly
# so a closed test loop does not pin its lock.
_delivery_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _delivery_lock() -> asyncio.Lock:
    """The running loop's delivery lock, created on first use. Must run on the loop thread."""
    loop = asyncio.get_running_loop()
    lock = _delivery_locks.get(loop)
    if lock is None:
        lock = _delivery_locks[loop] = asyncio.Lock()
    return lock


async def _deliver_in_turn(bound: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """Run one broadcast while holding the loop's delivery lock.

    WHY ONE AT A TIME. Delivery revalidates each recipient socket against the database
    (``ConnectionManager._may_deliver`` -> ``run_in_threadpool`` -> a fresh
    ``SessionLocal``). Those calls draw on the SAME AnyIO worker-thread limiter every
    sync route needs and the SAME SQLAlchemy pool every request needs. One write can
    schedule dozens of broadcasts (an applied schedule sends one per work order plus
    one per work center); run concurrently, each would hold a thread token and a
    connection -- enough to make unrelated requests (a kiosk clock-in, an operation
    completion) wait for a token or time out on the pool, and a pool timeout inside
    the recheck closes a legitimate socket with 1008. Serialized, realtime holds at
    most ONE worker thread and ONE pooled connection per process.

    It also keeps per-socket ORDER: ``asyncio.Lock`` wakes waiters first-come,
    first-served and tasks first run in creation order, so a socket receives events in
    the order they were broadcast, and two deliveries never race to send to (or close)
    the same socket.

    The coroutine is created only once the lock is held, so a broadcast cancelled
    while queued (process shutdown) never leaves an un-awaited coroutine behind.
    """
    async with _delivery_lock():
        await bound()


def _on_broadcast_done(task: "asyncio.Task[Any]") -> None:
    _in_flight.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Realtime broadcast %s failed: %s: %s", task.get_name(), type(exc).__name__, exc)


def _schedule_on_loop(bound: Callable[[], Coroutine[Any, Any, Any]], name: str) -> None:
    """Queue ``bound`` as a task on the RUNNING loop. Must run on the event-loop thread."""
    loop = asyncio.get_running_loop()
    # ``list()`` snapshots the set: the tests run loops on more than one thread.
    pending = sum(1 for task in list(_in_flight) if task.get_loop() is loop)
    if pending >= MAX_PENDING_BROADCASTS:
        logger.warning("Realtime broadcast %s dropped: %d broadcasts already waiting in this process", name, pending)
        return
    task = loop.create_task(_deliver_in_turn(bound), name=name)
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
    pool outright. The broadcast is therefore queued as a task on the event loop and
    this returns at once: from a sync route (an AnyIO worker thread) via
    ``from_thread.run_sync``, and from an ``async`` route directly on the running loop
    (``from_thread`` refuses the loop thread, which silently dropped those too).

    SERIALIZED, not concurrent: queued broadcasts are delivered one at a time per
    process, in call order (``_deliver_in_turn``), and at most
    ``MAX_PENDING_BROADCASTS`` may wait. ``REALTIME_BROADCAST_ENABLED=false`` turns
    every push off without a code change; the listening screens keep polling.

    Best-effort by design: call sites run after their commit, and a broadcast failure
    must never fail that request. Failures -- scheduling or delivery -- are logged at
    WARNING with the coroutine's name and the exception only, never the payload.
    """
    if not settings.REALTIME_BROADCAST_ENABLED:
        return
    bound = functools.partial(coro, *args, **kwargs)
    name = f"realtime:{getattr(coro, '__name__', 'broadcast')}"
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # A worker thread (sync route): hop onto the loop just long enough to queue the task.
            from_thread.run_sync(_schedule_on_loop, bound, name)
        else:
            _schedule_on_loop(bound, name)
    except Exception as exc:
        logger.warning("Realtime broadcast %s failed: %s: %s", name, type(exc).__name__, exc)
