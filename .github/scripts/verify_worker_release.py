"""Fail closed until Railway's active worker publishes the tested runtime identity.

No API credentials, Redis credentials or private geometry are read or printed. The
CLI uses the existing project token. Logs alone cannot pass: the identity must be
fresh, belong to the currently active SUCCESS deployment, and match the exact CI
image. The worker emits this event only after its Redis heartbeat write succeeds.
"""

import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A UTC offset is required")
    return parsed.timestamp()


def active_worker(status, service, environment):
    environments = status.get("environments", {}).get("edges", [])
    for edge in environments:
        env = edge.get("node", {})
        if env.get("name") != environment:
            continue
        for item in env.get("serviceInstances", {}).get("edges", []):
            instance = item.get("node", {})
            if instance.get("serviceName") != service:
                continue
            latest = instance.get("latestDeployment") or {}
            active = instance.get("activeDeployments") or []
            if latest.get("status") == "SUCCESS" and any(
                deployment.get("id") == latest.get("id") and deployment.get("status") == "SUCCESS"
                for deployment in active
            ):
                return latest
    return None


def matching_identity(lines, deployment, release, manifest, now):
    for line in reversed(lines.splitlines()):
        try:
            event = json.loads(line)
            # Railway's --json wraps the application's log event as a message.
            if isinstance(event, dict) and isinstance(event.get("message"), str):
                message = event["message"]
                _, marker, payload = message.partition("{")
                event = json.loads(marker + payload)
            if not isinstance(event, dict) or event.get("event") != "nesting_runtime_ready":
                continue
            identity = event["identity"]
            if not isinstance(identity, dict):
                continue
            if set(identity) != {
                "release",
                "protocol",
                "solver_version",
                "bundle_sha256",
                "node_version",
                "instance_id",
                "deployment_id",
                "observed_at",
            }:
                continue
            if (
                identity["release"] != release
                or identity["deployment_id"] != deployment["id"]
                or type(identity["protocol"]) is not int
                or identity["protocol"] != manifest["protocol"]
                or identity["solver_version"] != manifest["solver_version"]
                or identity["bundle_sha256"] != manifest["bundle_sha256"]
                or identity["node_version"] != manifest["node_version"]
                or not re.fullmatch(r"[0-9a-fA-F-]{36}", identity["instance_id"])
            ):
                continue
            observed = timestamp(identity["observed_at"])
            if -5 <= now - observed <= 90 and observed >= timestamp(deployment["createdAt"]):
                return identity
        except (KeyError, TypeError, ValueError):
            continue
    return None


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
    # Never echo raw CLI diagnostics: they can include account or application data.
    if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("Railway read-only probe unavailable")
    return result.stdout


def verify(args):
    manifest = json.loads(args.manifest.read_text())
    if (
        not re.fullmatch(r"[0-9a-f]{40}", args.expect)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("bundle_sha256", ""))
        or manifest.get("node_version") != "v22.23.2"
        or manifest.get("protocol") != 1
        or manifest.get("solver_version") != "werco-contour-v4"
    ):
        raise ValueError("Invalid expected release or CI runtime manifest")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            status = json.loads(command(["railway", "status", "--json"]))
            deployment = active_worker(status, args.service, args.environment)
            if deployment:
                lines = command(
                    [
                        "railway",
                        "logs",
                        deployment["id"],
                        "--service",
                        args.service,
                        "--environment",
                        args.environment,
                        "--json",
                        "--lines",
                        "200",
                        "--filter",
                        "nesting_runtime_ready",
                        "--since",
                        "90s",
                    ]
                )
                identity = matching_identity(
                    lines, deployment, args.expect, manifest, datetime.now(timezone.utc).timestamp()
                )
                if identity:
                    # A deployment replaced while logs were fetched is not proof of the
                    # active release. Also recheck freshness after the platform request.
                    rechecked = active_worker(
                        json.loads(command(["railway", "status", "--json"])), args.service, args.environment
                    )
                    if (
                        rechecked
                        and rechecked["id"] == deployment["id"]
                        and matching_identity(
                            lines, rechecked, args.expect, manifest, datetime.now(timezone.utc).timestamp()
                        )
                    ):
                        print(
                            "Active worker verified: release " + args.expect + "; bundle " + manifest["bundle_sha256"]
                        )
                        return
        except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            pass
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    raise SystemExit(
        "Worker release verification timed out: no active deployment with a fresh matching runtime heartbeat."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--service", default="werco-worker")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--timeout", type=int, default=600)
    verify(parser.parse_args())
