"""The deploy gate must reject old, inactive and merely-started solver runtimes."""

import importlib.util
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.schemas.quote_nesting_runs import SOLVER_VERSION

pytestmark = [pytest.mark.unit]
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("worker_release", ROOT / ".github/scripts/verify_worker_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def evidence():
    now = datetime.now(timezone.utc)
    deployment = {"id": "new-deployment", "status": "SUCCESS", "createdAt": (now - timedelta(seconds=60)).isoformat()}
    manifest = {
        "protocol": 1,
        "solver_version": SOLVER_VERSION,
        "bundle_sha256": "b" * 64,
        "node_version": "v22.23.2",
    }
    identity = {
        **manifest,
        "release": "a" * 40,
        "instance_id": "12345678-1234-1234-1234-123456789abc",
        "deployment_id": deployment["id"],
        "observed_at": (now - timedelta(seconds=2)).isoformat(),
    }
    return now, deployment, manifest, identity


def line(identity):
    return json.dumps({"message": json.dumps({"event": "nesting_runtime_ready", "identity": identity})})


def test_server_build_manifest_and_application_share_the_release_identity():
    build_script = (ROOT / 'frontend/tools/build-nesting-worker.mjs').read_text()
    declared = re.search(r"solver_version:\s*['\"]([^'\"]+)['\"]", build_script)
    assert declared is not None and declared.group(1) == SOLVER_VERSION
    source = (ROOT / 'frontend/src/features/nesting/lib/run-manifest.ts').read_text()
    shared = re.search(r"export const SOLVER_VERSION\s*=\s*['\"]([^'\"]+)['\"]", source)
    assert shared is not None and shared.group(1) == SOLVER_VERSION
    # Existing verify() tests use the application identity, rather than a second
    # copy of the verifier's literal, and therefore fail if its strict pin drifts.


@pytest.mark.parametrize(
    'solver', ['werco-contour-v4', 'werco-contour-v5', 'werco-contour-v7', 'werco-contour-v6-extra']
)
def test_old_future_or_prefix_solver_cannot_pass_the_strict_release_pin(evidence, tmp_path, monkeypatch, solver):
    _, _, manifest, _ = evidence
    manifest['solver_version'] = solver
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(release, 'command', lambda _: pytest.fail('Invalid manifest must fail before platform queries'))
    args = SimpleNamespace(manifest=path, expect='a' * 40, timeout=1, service='werco-worker', environment='production')
    with pytest.raises(ValueError, match='Invalid expected release'):
        release.verify(args)


def structured_line(identity):
    # Railway promotes the application JSON fields and leaves message empty.
    return json.dumps({"event": "nesting_runtime_ready", "identity": identity, "message": "", "level": "info"})


def status(deployment, active=True):
    return {
        "environments": {
            "edges": [
                {
                    "node": {
                        "name": "production",
                        "serviceInstances": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceName": "werco-worker",
                                        "latestDeployment": deployment,
                                        "activeDeployments": [deployment] if active else [],
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        }
    }


@pytest.mark.parametrize("encode", [line, structured_line])
def test_fresh_matching_identity_and_active_success_are_both_required(evidence, encode):
    now, deployment, manifest, identity = evidence
    assert release.active_worker(status(deployment), "werco-worker", "production") == deployment
    assert release.matching_identity(encode(identity), deployment, "a" * 40, manifest, now.timestamp()) == identity
    assert release.active_worker(status(deployment, False), "werco-worker", "production") is None
    assert release.active_worker(status(deployment), "werco-worker", "staging") is None
    assert release.active_worker(status(deployment), "werco-api", "production") is None


@pytest.mark.parametrize("state", ["BUILDING", "DEPLOYING", "FAILED", "CRASHED", "REMOVED"])
def test_latest_deployment_is_not_proof_of_a_running_worker(evidence, state):
    _, deployment, _, _ = evidence
    deployment["status"] = state
    assert release.active_worker(status(deployment), "werco-worker", "production") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("release", "c" * 40),
        ("deployment_id", "old-deployment"),
        ("bundle_sha256", "c" * 64),
        ("node_version", "v22.23.1"),
        ("solver_version", "old-solver"),
        ("protocol", True),
        ("instance_id", "bad"),
    ],
)
@pytest.mark.parametrize("encode", [line, structured_line])
def test_identity_mismatch_cannot_pass(evidence, field, value, encode):
    now, deployment, manifest, identity = evidence
    identity[field] = value
    assert release.matching_identity(encode(identity), deployment, "a" * 40, manifest, now.timestamp()) is None


@pytest.mark.parametrize("age", [91, -6, 1000])
@pytest.mark.parametrize("encode", [line, structured_line])
def test_stale_future_and_predeployment_heartbeats_fail(evidence, age, encode):
    now, deployment, manifest, identity = evidence
    identity["observed_at"] = (now - timedelta(seconds=age)).isoformat()
    assert release.matching_identity(encode(identity), deployment, "a" * 40, manifest, now.timestamp()) is None


def test_heartbeat_before_active_deployment_start_cannot_pass(evidence):
    now, deployment, manifest, identity = evidence
    identity["observed_at"] = (now - timedelta(seconds=65)).isoformat()
    assert release.matching_identity(line(identity), deployment, "a" * 40, manifest, now.timestamp()) is None


def test_ignores_unrelated_or_malformed_logs_and_requires_publication_event(evidence):
    now, deployment, manifest, identity = evidence
    noise = '\n'.join(['plain text', '{bad json', json.dumps({"event": "startup", "identity": identity})])
    assert release.matching_identity(noise, deployment, "a" * 40, manifest, now.timestamp()) is None
    assert release.matching_identity(noise + '\n' + line(identity), deployment, "a" * 40, manifest, now.timestamp())
    identity.pop("observed_at")
    assert release.matching_identity(line(identity), deployment, "a" * 40, manifest, now.timestamp()) is None


def test_structured_event_cannot_replace_bad_identity_with_valid_nested_message(evidence):
    now, deployment, manifest, identity = evidence
    forged = {
        "event": "nesting_runtime_ready",
        "identity": {**identity, "release": "c" * 40},
        "message": json.dumps({"event": "nesting_runtime_ready", "identity": identity}),
    }
    assert release.matching_identity(json.dumps(forged), deployment, "a" * 40, manifest, now.timestamp()) is None


@pytest.mark.parametrize("identity_value", [None, [], {}, {"unexpected": "field"}])
def test_malformed_structured_identity_does_not_pass(evidence, identity_value):
    now, deployment, manifest, _ = evidence
    assert (
        release.matching_identity(structured_line(identity_value), deployment, "a" * 40, manifest, now.timestamp())
        is None
    )


def test_different_active_deployment_does_not_validate_latest(evidence):
    _, deployment, _, _ = evidence
    data = status(deployment)
    instance = data["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][0]["node"]
    instance["activeDeployments"] = [{**deployment, "id": "older"}]
    assert release.active_worker(data, "werco-worker", "production") is None


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("encode", [line, structured_line])
def test_verifier_rechecks_platform_state_after_reading_heartbeat(
    evidence, tmp_path, monkeypatch, capsys, replaced, encode
):
    _, deployment, manifest, identity = evidence
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    rechecked = {**deployment, "id": "replacement"} if replaced else deployment
    responses = iter([json.dumps(status(deployment)), encode(identity), json.dumps(status(rechecked))])
    commands = []

    def probe(args):
        commands.append(args)
        return next(responses)

    monkeypatch.setattr(release, "command", probe)
    ticks = iter([0, 0, 2, 2])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    args = SimpleNamespace(
        manifest=manifest_path, expect="a" * 40, timeout=1, service="werco-worker", environment="production"
    )
    if replaced:
        with pytest.raises(SystemExit, match="no active deployment"):
            release.verify(args)
        assert "Active worker verified" not in capsys.readouterr().out
    else:
        release.verify(args)
        assert "Active worker verified" in capsys.readouterr().out
    assert commands[1] == [
        "railway",
        "logs",
        deployment["id"],
        "--service",
        "werco-worker",
        "--environment",
        "production",
        "--json",
        "--lines",
        "200",
        "--since",
        "90s",
    ]


def test_image_and_postdeploy_runtime_gates_are_enforced_before_promotion():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-cd.yml").read_text())
    build = workflow["jobs"]["build"]["steps"]
    smoke = next(step for step in build if "smoke_nesting_worker.py" in step.get("run", ""))
    assert not smoke.get("continue-on-error")
    assert not smoke.get("if")
    steps = workflow["jobs"]["deploy-production"]["steps"]
    verify = next(i for i, step in enumerate(steps) if "verify_worker_release.py" in step.get("run", ""))
    upload = next(i for i, step in enumerate(steps) if "railway up --service werco-worker " in step.get("run", ""))
    promote = next(i for i, step in enumerate(steps) if "promote_vercel.py" in step.get("run", ""))
    assert upload < verify < promote
    assert not steps[verify].get("continue-on-error")
    assert "steps.worker_scope.outputs.changed == 'true'" in steps[verify]["if"]


def test_compose_worker_preserves_packaged_runtime_and_api_stays_python_only():
    for filename in ("docker-compose.yml", "docker-compose.prod.yml"):
        worker = yaml.safe_load((ROOT / filename).read_text())["services"]["worker"]
        assert worker["build"] == {"context": ".", "dockerfile": "backend/Dockerfile.worker"}
        assert all(not volume.endswith(":/app") for volume in worker.get("volumes", []))
    for filename in ("Dockerfile", "Dockerfile.prod"):
        active = '\n'.join(
            line for line in (ROOT / "backend" / filename).read_text().splitlines() if not line.startswith('#')
        )
        assert "nesting-runtime" not in active and "node:" not in active
    assert "nesting-runtime/" in (ROOT / "backend/.dockerignore").read_text()


def test_isolated_frontend_production_build_contains_its_required_profile_verifier():
    package = json.loads((ROOT / 'frontend/package.json').read_text())
    assert 'node tools/verify-nesting-profile.mjs' in package['scripts']['build']
    production = (ROOT / 'frontend/Dockerfile.prod').read_text()
    copy = 'COPY tools/verify-nesting-profile.mjs ./tools/'
    assert copy in production and production.index(copy) < production.index('RUN npm run build')
    assert (ROOT / 'frontend/tools/verify-nesting-profile.mjs').is_file()
    assert 'COPY src ./src' in production
    generated = ROOT / 'frontend/src/features/nesting/lib/geometry-profile.generated.json'
    assert generated.is_file()
    # Railway uploads frontend/ alone, so the verifier must not require ../backend.
    checker = (ROOT / 'frontend/tools/verify-nesting-profile.mjs').read_text()
    assert '../src/features/nesting/lib/geometry-profile.generated.json' in checker
    assert '../backend' not in checker


def test_image_smoke_timeout_removes_only_its_own_container(monkeypatch):
    spec = importlib.util.spec_from_file_location("worker_smoke", ROOT / ".github/scripts/smoke_nesting_worker.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    commands = []

    def run(args, **_kwargs):
        commands.append(args)
        if len(commands) == 1:
            raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(smoke.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        smoke.container("synthetic-image", ["solver.cjs"])
    name = next(arg.split("=", 1)[1] for arg in commands[0] if arg.startswith("--name="))
    assert name.startswith("werco-nesting-smoke-")
    assert commands[1] == ["docker", "rm", "--force", name]
