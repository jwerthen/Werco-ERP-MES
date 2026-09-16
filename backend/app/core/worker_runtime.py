"""Fresh, credential-free release evidence for the shared ARQ worker."""

import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import settings

logger = logging.getLogger(__name__)


async def publish_identity(redis, instance_id: str) -> dict:
    identity = {
        "release": settings.APP_RELEASE or "unset",
        "instance_id": instance_id,
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID", "local"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    # A log is proof only after the running worker successfully writes to its Redis.
    await redis.set(f"werco:worker:runtime:{instance_id}", json.dumps(identity), ex=90)
    logger.info(json.dumps({"event": "worker_runtime_ready", "identity": identity}))
    return identity


async def _refresh(redis, instance_id: str) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await publish_identity(redis, instance_id)
        except Exception:
            logger.warning("Worker runtime heartbeat unavailable")


async def start_runtime_heartbeat(ctx) -> None:
    redis = ctx.get("redis")
    if redis is None:
        return  # Unit-test lifecycle contexts do not construct an ARQ Redis pool.
    instance_id = str(uuid4())
    await publish_identity(redis, instance_id)
    ctx["runtime_heartbeat_task"] = asyncio.create_task(_refresh(redis, instance_id))


async def stop_runtime_heartbeat(ctx) -> None:
    task = ctx.pop("runtime_heartbeat_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
