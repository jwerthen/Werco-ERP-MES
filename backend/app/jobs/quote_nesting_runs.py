"""Bounded subprocess runner and durable ID-only queue relay for nesting drafts."""

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.core.nesting_geometry_profile import geometry_profile_identity, is_current_geometry_profile
from app.core.queue import enqueue_job, get_redis_pool
from app.core.time_utils import to_utc_iso
from app.db.database import atomic_transaction
from app.db.session import SessionLocal
from app.models.quote_nesting_run import QuoteNestingRun
from app.schemas.quote_nesting_runs import (
    HEARTBEAT_SECONDS,
    MAX_ESTIMATE_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_OPTIONS,
    MAX_SECONDS,
    SOLVER_VERSION,
)
from app.services import quote_nesting_runs as service
from app.services.audit_service import AuditWriteError
from app.services.nesting_run_protocol import (
    RunProtocolError,
    exact,
    expected_options,
    parse_message,
    require,
    validate_hello,
    validate_summary,
)
from app.services.nesting_runtime import RUNTIME_REFRESH_SECONDS, RUNTIME_TTL, current_release, runtime_key
from app.services.quote_nesting_drafts import canonical_json

logger = logging.getLogger(__name__)
NODE_PATH = Path("/usr/local/bin/node")
BUNDLE_PATH = Path("/app/nesting-runtime/solver.cjs")
MANIFEST_PATH = Path("/app/nesting-runtime/manifest.json")
CHILD_ENV = {"LANG": "C.UTF-8", "TZ": "UTC"}
_SEMAPHORE = asyncio.Semaphore(1)


async def _kill(process) -> None:
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            logger.debug("Nesting subprocess had already exited")
        await process.wait()


async def verify_runtime() -> dict:
    """Verify fixed installed artifacts and the actual runtime without inherited env."""
    process = None
    try:
        require(NODE_PATH.is_file() and BUNDLE_PATH.is_file() and MANIFEST_PATH.is_file())
        require(MANIFEST_PATH.stat().st_size <= 4096 and BUNDLE_PATH.stat().st_size <= 20 * 1024 * 1024)
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        exact(
            manifest,
            {
                "protocol",
                "solver_version",
                "bundle_sha256",
                "node_major",
                "max_option_evaluations",
                "entrypoint",
                "geometry_profile",
            },
        )
        require(is_current_geometry_profile(manifest['geometry_profile']))
        require(manifest["protocol"] == 1 and type(manifest["protocol"]) is int)
        require(manifest["solver_version"] == SOLVER_VERSION and manifest["node_major"] == 22)
        require(manifest["max_option_evaluations"] == MAX_OPTIONS and manifest["entrypoint"] == "solver.cjs")
        require(hashlib.sha256(BUNDLE_PATH.read_bytes()).hexdigest() == manifest["bundle_sha256"])
        process = await asyncio.create_subprocess_exec(
            str(NODE_PATH),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=CHILD_ENV,
            limit=1024,
        )
        output = await asyncio.wait_for(process.stdout.read(128), timeout=5)
        await asyncio.wait_for(process.wait(), timeout=5)
        node_version = output.decode("ascii").strip()
        import re

        require(process.returncode == 0 and re.fullmatch(r"v22\.\d+\.\d+", node_version))
        return {
            "release": current_release(),
            "protocol": 1,
            "solver_version": SOLVER_VERSION,
            "bundle_sha256": manifest["bundle_sha256"],
            "node_version": node_version,
        }
    except (OSError, ValueError, UnicodeError, asyncio.TimeoutError) as exc:
        raise ValueError("runtime_unavailable") from exc
    finally:
        await _kill(process)


def _log_runtime_identity(identity: dict) -> None:
    # Deployment readiness must remain observable when application logs are
    # WARNING-only. This logger emits only the fixed, non-sensitive identity
    # envelope; it never widens logging for geometry or other worker activity.
    identity_logger = logging.getLogger("werco.nesting_runtime_identity")
    identity_logger.setLevel(logging.INFO)
    identity_logger.propagate = False
    if not identity_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        identity_logger.addHandler(handler)
    identity_logger.info("%s", canonical_json({"event": "nesting_runtime_ready", "identity": identity}))


async def _publish_runtime(ctx: dict, identity: dict) -> None:
    while True:
        try:
            pool = ctx.get("redis") or await get_redis_pool()
            payload = {
                **identity,
                "instance_id": ctx["nesting_instance_id"],
                "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID"),
                "observed_at": to_utc_iso(datetime.utcnow()),
            }
            stored = await asyncio.wait_for(pool.set(runtime_key(), canonical_json(payload), ex=RUNTIME_TTL), timeout=5)
            if stored is not True:
                raise ValueError("Nesting runtime heartbeat was not stored")
            _log_runtime_identity(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Nesting runtime heartbeat unavailable")
        await asyncio.sleep(RUNTIME_REFRESH_SECONDS)


async def startup_nesting_runtime(ctx: dict) -> None:
    if ctx.get("nesting_relay_enabled") is not True:
        logger.warning("Nesting runtime disabled: the durable queue/lease relay is not enabled")
        return
    try:
        identity = await verify_runtime()
    except ValueError:
        logger.warning("Nesting runtime unavailable; server nesting calculations remain disabled")
        return
    ctx["nesting_runtime_identity"] = identity
    ctx["nesting_instance_id"] = str(uuid4())
    ctx["nesting_runtime_heartbeat"] = asyncio.create_task(_publish_runtime(ctx, identity))
    # Identity contains only build/runtime metadata, never geometry or secrets.
    logger.info("Nesting runtime ready %s", canonical_json(identity))


async def shutdown_nesting_runtime(ctx: dict) -> None:
    task = ctx.get("nesting_runtime_heartbeat")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # TTL bounds stale readiness. Do not delete another worker's same-release heartbeat.


def _transaction(function, *args, **kwargs):
    with SessionLocal() as db, atomic_transaction(db):
        return function(db, *args, **kwargs)


def _finish(company_id: int, run_id: int, lease: str | None, code: str | None, summary: dict | None = None) -> None:
    def finish(db):
        run = service.get_run(db, company_id, run_id, locked=True)
        if lease is None and (run.status != "QUEUED" or code is None):
            return
        service.finish_run(db, run, code, summary=summary, lease_token=lease)

    _transaction(finish)


async def _drain_stderr(stream) -> None:
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        total += len(chunk)
        if total > 32768:
            raise ValueError("output_limit")
        # Deliberately discard child diagnostics; only bounded generic error codes
        # reach records/logs. A parser traceback could contain private geometry.


async def run_quote_nesting_task(*, company_id: int, run_id: int) -> dict:
    """Accept only identifiers from Redis; never accept caller geometry or paths."""
    if type(company_id) is not int or type(run_id) is not int or company_id < 1 or run_id < 1:
        return {"status": "not_claimed"}
    async with _SEMAPHORE:
        process = read_task = stderr_task = None
        lease = None
        code = None
        summary = None
        try:
            runtime = await verify_runtime()
            claimed = _transaction(service.claim_run, company_id, run_id, runtime=runtime)
            if claimed is None:
                return {"status": "not_claimed"}
            payload, lease = claimed
            raw = canonical_json(payload).encode("utf-8") + b"\n"
            require(len(raw) <= MAX_ESTIMATE_BYTES + 1024)
            planned = expected_options(payload["estimate"])
            manifest = {
                "solver_version": runtime["solver_version"],
                "bundle_sha256": runtime["bundle_sha256"],
                "geometry_profile": geometry_profile_identity(),
            }
            loop = asyncio.get_running_loop()
            deadline = loop.time() + MAX_SECONDS
            next_heartbeat = loop.time() + HEARTBEAT_SECONDS
            process = await asyncio.create_subprocess_exec(
                str(NODE_PATH),
                "--max-old-space-size=512",
                str(BUNDLE_PATH),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=CHILD_ENV,
                cwd=str(BUNDLE_PATH.parent),
                limit=MAX_MESSAGE_BYTES + 1,
            )
            stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
            process.stdin.write(raw)
            await asyncio.wait_for(process.stdin.drain(), timeout=max(0.1, deadline - loop.time()))
            process.stdin.close()
            await process.stdin.wait_closed()
            hello_seen = False
            keys = []
            complete = 0
            read_task = asyncio.create_task(process.stdout.readline())
            while True:
                if loop.time() >= deadline:
                    raise ValueError("time_limit")
                if stderr_task.done():
                    stderr_task.result()
                done, _ = await asyncio.wait({read_task}, timeout=min(1, deadline - loop.time()))
                # Reads are short and release the DB session immediately; only
                # every 10-second heartbeat mutates/audits the lease.
                if loop.time() >= next_heartbeat:
                    _transaction(service.heartbeat, company_id, run_id, lease)
                    next_heartbeat = loop.time() + HEARTBEAT_SECONDS
                else:
                    with SessionLocal() as db:
                        service.live_run(db, company_id, run_id, lease)
                if not done:
                    continue
                try:
                    line = read_task.result()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise ValueError("output_limit") from exc
                if not line:
                    require(hello_seen and summary is not None)
                    break
                message = parse_message(line)
                if not hello_seen:
                    validate_hello(message, payload["input_sha256"], manifest)
                    require(message["node_version"] == runtime["node_version"])
                    _transaction(service.accept_hello, company_id, run_id, lease, message)
                    hello_seen = True
                elif summary is not None:
                    raise RunProtocolError("Unexpected output after summary")
                elif message.get("type") == "option":
                    require(len(keys) < min(len(planned), MAX_OPTIONS))
                    _transaction(service.append_checkpoint, company_id, run_id, lease, message)
                    keys.append({"group_id": message["group_id"], "option_id": message["option_id"]})
                    complete += int(message["result"]["complete"])
                elif message.get("type") == "summary":
                    validate_summary(message, payload["input_sha256"], keys, len(planned), complete)
                    summary = message
                elif message.get("type") == "error":
                    exact(message, {"type", "protocol", "input_sha256", "code"})
                    require(message["protocol"] == 1 and message["input_sha256"] == payload["input_sha256"])
                    require(message["code"] in ("invalid_geometry", "runtime_error", "output_limit", "input_limit"))
                    raise ValueError(message["code"])
                else:
                    raise RunProtocolError("Unexpected nesting message")
                read_task = asyncio.create_task(process.stdout.readline())
            await asyncio.wait_for(process.wait(), timeout=max(0.1, deadline - loop.time()))
            require(process.returncode == 0)
            await stderr_task
            if summary["stop_reason"] == "work_limit":
                code = "work_limit"
        except asyncio.CancelledError:
            code = "worker_shutdown"
            raise
        except RunProtocolError:
            code = "invalid_protocol"
        except asyncio.TimeoutError:
            code = "time_limit"
        except ValueError as exc:
            code = str(exc) if str(exc) in service.ERROR_MESSAGES else "runtime_error"
        except HTTPException:
            code = "worker_lost"
        except AuditWriteError:
            # Never record a lifecycle change without its audit. Leave the lease
            # for the audited relay to expire if the audit store remains down.
            code = "audit_unavailable"
        except Exception:
            code = "runtime_error"
        finally:
            await _kill(process)
            for task in (read_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (read_task, stderr_task) if task is not None), return_exceptions=True
            )
            if code != "audit_unavailable":
                try:
                    _finish(company_id, run_id, lease, code, summary)
                except Exception:
                    logger.warning("Nesting run finalization unavailable; lease relay will reconcile")
        return {"status": "finished", "code": code}


async def relay_quote_nesting_runs_task(limit: int = 100) -> dict:
    """Bounded cross-tenant ID discovery; every lifecycle write then scopes that tenant."""
    now = datetime.utcnow()
    with SessionLocal() as db:
        rows = (
            db.query(QuoteNestingRun.company_id, QuoteNestingRun.id, QuoteNestingRun.status)
            .filter(
                (QuoteNestingRun.status == "QUEUED")
                | ((QuoteNestingRun.status == "RUNNING") & (QuoteNestingRun.lease_expires_at <= now))
            )
            .order_by(QuoteNestingRun.id)
            .limit(min(max(limit, 1), 100))
            .all()
        )
    enqueued = expired = 0
    for company_id, run_id, status in rows:
        try:
            if status == "RUNNING":
                expired += int(_transaction(service.fail_expired, company_id, run_id))
            else:
                # Reuse the same ID only while a real queue job is pending. ARQ
                # results for a completed prior delivery must not block relay.
                await enqueue_job(
                    "run_quote_nesting_job",
                    company_id=company_id,
                    run_id=run_id,
                    _job_id=f"quote-nesting:{company_id}:{run_id}",
                )
                enqueued += 1
        except Exception:
            logger.warning("Nesting relay could not dispatch or expire one run")
    return {"scanned": len(rows), "enqueued": enqueued, "expired": expired}
