"""The deploy gate must reject old, inactive and merely-started worker runtimes."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = [pytest.mark.unit]
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("worker_release", ROOT / ".github/scripts/verify_worker_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def evidence():
    now = datetime.now(timezone.utc)
    deployment = {"id": "new-deployment", "status": "SUCCESS", "createdAt": (now - timedelta(seconds=60)).isoformat()}
    identity = {
        "release": "a" * 40,
        "instance_id": "12345678-1234-1234-1234-123456789abc",
        "deployment_id": deployment["id"],
        "observed_at": (now - timedelta(seconds=2)).isoformat(),
    }
    return now, deployment, identity


def line(identity):
    return json.dumps({"message": json.dumps({"event": "worker_runtime_ready", "identity": identity})})


def structured_line(identity):
    # Railway promotes the application JSON fields and leaves message empty.
    return json.dumps({"event": "worker_runtime_ready", "identity": identity, "message": "", "level": "info"})


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
    now, deployment, identity = evidence
    assert release.active_worker(status(deployment), "werco-worker", "production") == deployment
    assert release.matching_identity(encode(identity), deployment, "a" * 40, now.timestamp()) == identity
    assert release.active_worker(status(deployment, False), "werco-worker", "production") is None
    assert release.active_worker(status(deployment), "werco-worker", "staging") is None
    assert release.active_worker(status(deployment), "werco-api", "production") is None


@pytest.mark.parametrize("state", ["BUILDING", "DEPLOYING", "FAILED", "CRASHED", "REMOVED"])
def test_latest_deployment_is_not_proof_of_a_running_worker(evidence, state):
    _, deployment, _ = evidence
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
    now, deployment, identity = evidence
    identity[field] = value
    assert release.matching_identity(encode(identity), deployment, "a" * 40, now.timestamp()) is None


@pytest.mark.parametrize("age", [91, -6, 1000])
@pytest.mark.parametrize("encode", [line, structured_line])
def test_stale_future_and_predeployment_heartbeats_fail(evidence, age, encode):
    now, deployment, identity = evidence
    identity["observed_at"] = (now - timedelta(seconds=age)).isoformat()
    assert release.matching_identity(encode(identity), deployment, "a" * 40, now.timestamp()) is None


def test_heartbeat_before_active_deployment_start_cannot_pass(evidence):
    now, deployment, identity = evidence
    identity["observed_at"] = (now - timedelta(seconds=65)).isoformat()
    assert release.matching_identity(line(identity), deployment, "a" * 40, now.timestamp()) is None


def test_ignores_unrelated_or_malformed_logs_and_requires_publication_event(evidence):
    now, deployment, identity = evidence
    noise = '\n'.join(['plain text', '{bad json', json.dumps({"event": "startup", "identity": identity})])
    assert release.matching_identity(noise, deployment, "a" * 40, now.timestamp()) is None
    assert release.matching_identity(noise + '\n' + line(identity), deployment, "a" * 40, now.timestamp())
    identity.pop("observed_at")
    assert release.matching_identity(line(identity), deployment, "a" * 40, now.timestamp()) is None


def test_structured_event_cannot_replace_bad_identity_with_valid_nested_message(evidence):
    now, deployment, identity = evidence
    forged = {
        "event": "worker_runtime_ready",
        "identity": {**identity, "release": "c" * 40},
        "message": json.dumps({"event": "worker_runtime_ready", "identity": identity}),
    }
    assert release.matching_identity(json.dumps(forged), deployment, "a" * 40, now.timestamp()) is None


@pytest.mark.parametrize("identity_value", [None, [], {}, {"unexpected": "field"}])
def test_malformed_structured_identity_does_not_pass(evidence, identity_value):
    now, deployment, _ = evidence
    assert release.matching_identity(structured_line(identity_value), deployment, "a" * 40, now.timestamp()) is None


def test_different_active_deployment_does_not_validate_latest(evidence):
    _, deployment, _ = evidence
    data = status(deployment)
    instance = data["environments"]["edges"][0]["node"]["serviceInstances"]["edges"][0]["node"]
    instance["activeDeployments"] = [{**deployment, "id": "older"}]
    assert release.active_worker(data, "werco-worker", "production") is None


@pytest.mark.parametrize("replaced", [False, True])
@pytest.mark.parametrize("encode", [line, structured_line])
def test_verifier_rechecks_platform_state_after_reading_heartbeat(
    evidence, tmp_path, monkeypatch, capsys, replaced, encode
):
    _, deployment, identity = evidence
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
    args = SimpleNamespace(expect="a" * 40, timeout=1, service="werco-worker", environment="production")
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
    smoke = next(step for step in build if "smoke_fabrication_quote_worker.py" in step.get("run", ""))
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


def test_native_quote_runtime_is_packaged_in_both_api_images():
    for filename in ('Dockerfile', 'Dockerfile.prod'):
        dockerfile = (ROOT / 'backend' / filename).read_text()
        assert 'WERCO_QUOTE_WORKER_PYTHON=/opt/werco-quote-worker/bin/python' in dockerfile
        assert '--require-hashes -r requirements-quoting.lock' in dockerfile
        assert 'libgl1' in dockerfile
        assert 'alembic upgrade head &&' in dockerfile
    package = json.loads((ROOT / 'frontend/package.json').read_text())
    assert package['scripts']['build'] == 'vite build'
    assert 'nesting-worker' not in package['scripts']['type-check']
