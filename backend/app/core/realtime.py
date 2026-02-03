import logging
from typing import Any, Callable
from anyio import from_thread

logger = logging.getLogger(__name__)


def safe_broadcast(coro: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run a realtime broadcast safely from sync contexts."""
    try:
        from_thread.run(coro, *args, **kwargs)
    except Exception as exc:
        logger.debug("Realtime broadcast failed: %s", exc)
