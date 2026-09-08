"""Live worker identity only; no geometry or credentials are stored in this heartbeat."""

import asyncio
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.core.queue import get_redis_pool
from app.schemas.quote_nesting_runs import SOLVER_VERSION

RUNTIME_TTL = 90
RUNTIME_REFRESH_SECONDS = 30


def current_release() -> str:
    # Settings already resolves the deployed RELEASE file. Development is an
    # explicitly unversioned release; the immutable bundle SHA still binds it.
    return settings.APP_RELEASE or "development"


def runtime_key() -> str:
    return "quote-nesting:runtime:v1:" + current_release()


async def runtime_status() -> dict:
    unavailable = {"schema_version": 1, "available": False, "identity": None}
    try:
        pool = await asyncio.wait_for(get_redis_pool(), timeout=2)
        raw = await asyncio.wait_for(pool.get(runtime_key()), timeout=2)
        if raw is None:
            return {**unavailable, "reason": "missing"}
        if len(raw) > 4096:
            return {**unavailable, "reason": "invalid_identity"}
        identity = json.loads(raw)
        required = {
            "release",
            "protocol",
            "solver_version",
            "bundle_sha256",
            "node_version",
            "instance_id",
            "observed_at",
            "deployment_id",
        }
        if not isinstance(identity, dict) or set(identity) != required:
            return {**unavailable, "reason": "invalid_identity"}
        if identity["deployment_id"] is not None and (
            not isinstance(identity["deployment_id"], str) or len(identity["deployment_id"]) > 200
        ):
            return {**unavailable, "reason": "invalid_identity"}
        if identity["release"] != current_release():
            return {**unavailable, "reason": "release_mismatch"}
        age = (
            datetime.now(timezone.utc).timestamp()
            - datetime.fromisoformat(identity["observed_at"].replace("Z", "+00:00")).timestamp()
        )
        if age < -5 or age > RUNTIME_TTL:
            return {**unavailable, "reason": "stale"}
        from re import fullmatch

        if (
            type(identity["protocol"]) is not int
            or identity["protocol"] != 1
            or identity["solver_version"] != SOLVER_VERSION
            or not isinstance(identity["bundle_sha256"], str)
            or not fullmatch(r"[0-9a-f]{64}", identity["bundle_sha256"])
            or not isinstance(identity["node_version"], str)
            or not fullmatch(r"v22\.\d+\.\d+", identity["node_version"])
            or not isinstance(identity["instance_id"], str)
            or len(identity["instance_id"]) != 36
        ):
            return {**unavailable, "reason": "invalid_identity"}
        return {"schema_version": 1, "available": True, "reason": "ready", "identity": identity}
    except (Exception, asyncio.TimeoutError):
        return {**unavailable, "reason": "queue_unavailable"}
