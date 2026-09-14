"""Exercise the shipped Docker contexts and Redis memory policy with harmless fixtures.

The fast tests inspect parsed Compose behavior. Enable RUN_DOCKER_HYGIENE_TESTS=1
for the Docker checks: they use Docker's own ignore matcher and a real Redis under
memory pressure. No repository .env, database, upload or credential file is read or
sent to Docker. The build contexts contain synthetic marker bytes only.
"""

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
DOCKER_ENABLED = os.getenv("RUN_DOCKER_HYGIENE_TESTS") == "1"
MARKER = "deployment-hygiene-fixture-only\n"


def _compose(name):
    return yaml.safe_load((REPO_ROOT / name).read_text())


def _redis_command(name):
    command = _compose(name)["services"]["redis"]["command"]
    return shlex.split(command) if isinstance(command, str) else list(command)


def _option(command, option):
    return command[command.index(option) + 1]


def _builds():
    """Read each effective context/Dockerfile pair from the shipped Compose files."""
    builds = set()
    for name in COMPOSE_FILES:
        for service in _compose(name)["services"].values():
            build = service.get("build")
            if build:
                builds.add((build["context"], build.get("dockerfile", "Dockerfile")))
    return sorted(builds)


BUILDS = _builds()


@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_shared_queue_store_preserves_jobs_instead_of_evicting(compose_name):
    command = _redis_command(compose_name)
    assert command[0] == "redis-server"
    assert _option(command, "--maxmemory-policy") == "noeviction"
    assert _option(command, "--appendonly") == "yes"
    services = _compose(compose_name)["services"]
    assert services["backend"]["environment"]["REDIS_URL"] == services["worker"]["environment"]["REDIS_URL"]
    assert services["redis"]["volumes"], "Accepted queue writes need persistent Redis storage."


def test_production_redis_leaves_memory_for_persistence_and_process_overhead():
    service = _compose("docker-compose.prod.yml")["services"]["redis"]
    redis_limit = _option(_redis_command("docker-compose.prod.yml"), "--maxmemory")
    container_limit = service["deploy"]["resources"]["limits"]["memory"]
    # The checked-in config uses MiB for both values; compare capacity, not text.
    redis_mb = int(redis_limit.lower().removesuffix("mb").removesuffix("m"))
    container_mb = int(container_limit.lower().removesuffix("mb").removesuffix("m"))
    assert 0 < redis_mb <= container_mb / 2


def _docker(*args, input_text=None, timeout=60):
    return subprocess.run(
        ["docker", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def docker_daemon():
    if not DOCKER_ENABLED:
        pytest.skip("Set RUN_DOCKER_HYGIENE_TESTS=1 to run synthetic-context and Redis integration checks")
    assert shutil.which("docker"), "Docker checks were explicitly requested but docker is unavailable"
    # Requested integration checks must FAIL, not silently skip, if CI loses Docker.
    _docker("version", "--format", "{{.Server.Version}}")


def _required_paths(context):
    backend = [
        "app/main.py",
        "alembic.ini",
        "alembic/versions/001_add_performance_indexes.py",
        "requirements.txt",
        "requirements.lock",
        "requirements-dev.lock",
        "RELEASE",
        ".env.example",
    ]
    frontend = [
        "package.json",
        "package-lock.json",
        "public/release.txt",
        "src/features/nesting/lib/contour-packing.ts",
        "tools/build-nesting-worker.mjs",
        "tools/nesting-server-worker.ts",
        "tools/verify-nesting-profile.mjs",
        "tsconfig.json",
        "tsconfig.nesting-worker.json",
        "vite.config.ts",
        "index.html",
        "tailwind.config.js",
        "postcss.config.js",
        "nginx.conf",
        ".env.example",
    ]
    if context.name == "backend":
        return backend
    if context.name == "frontend":
        return frontend
    assert context == REPO_ROOT, f"Add required build inputs for the new context {context}"
    return [f"backend/{path}" for path in backend] + [f"frontend/{path}" for path in frontend] + [".env.prod.example"]


def _forbidden_paths(context):
    paths = [
        ".env",
        ".env.local",
        ".env.production",
        ".env.example.local",
        ".env 2",
        ".env~",
        "credentials.env",
        ".venv311/lib/python3.11/site-packages/local.py",
        ".venv/lib/local.py",
        "venv/bin/python",
        "env/bin/python",
        ".aws/credentials",
        ".azure/accessTokens.json",
        ".ssh/id_ed25519",
        ".npmrc",
        ".pypirc",
        "pip.conf",
        "private.pem",
        "private.key",
        "credentials.p12",
        "credentials.pfx",
        "id_rsa",
        "local.db",
        "local.db-wal",
        "local.db-shm",
        "local.db-journal",
        "local.sqlite",
        "local.sqlite-wal",
        "local.sqlite3",
        "local.sqlite3-shm",
        "uploads/customer-drawing.dxf",
        "logs/server.log",
        "backups/customer-data.sql",
    ]
    prefixes = ("", "nested/") if context != REPO_ROOT else ("", "backend/", "frontend/", "nested/")
    return [prefix + path for prefix in prefixes for path in paths]


@pytest.mark.integration
@pytest.mark.parametrize("context_path,dockerfile_path", BUILDS)
def test_real_docker_context_excludes_private_files_and_keeps_build_inputs(
    docker_daemon, tmp_path, context_path, dockerfile_path
):
    context = (REPO_ROOT / context_path).resolve()
    dockerfile = context / dockerfile_path
    override = dockerfile.with_name(dockerfile.name + ".dockerignore")
    ignore = override if override.exists() else context / ".dockerignore"
    assert ignore.is_file(), f"Build context {context_path} has no ignore policy"
    scratch = tmp_path / "context"
    scratch.mkdir()
    (scratch / ".dockerignore").write_text(ignore.read_text())
    # FROM scratch needs no base-image download and COPY performs no shell expansion.
    (scratch / "Dockerfile").write_text("FROM scratch\nCOPY . /payload/\n")
    required = _required_paths(context)
    forbidden = _forbidden_paths(context)
    for relative in required + forbidden:
        marker = scratch / relative
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(MARKER)
    output = tmp_path / "result"
    _docker("build", "--network=none", "--output", f"type=local,dest={output}", str(scratch), timeout=120)
    for relative in forbidden:
        assert not (output / "payload" / relative).exists(), f"{context_path} admits private/local file {relative}"
    for relative in required:
        assert (output / "payload" / relative).read_text() == MARKER, f"{context_path} omits required file {relative}"


@pytest.mark.integration
@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_real_redis_rejects_cache_pressure_without_losing_a_queued_job(docker_daemon, compose_name):
    service = _compose(compose_name)["services"]["redis"]
    command = _redis_command(compose_name)
    # Use the actual service settings, but only synthetic credentials and a small
    # bound for deterministic pressure. No port is exposed and no volume is mounted.
    command[command.index("--requirepass") + 1] = "hygiene-test-only"
    if "--maxmemory" in command:
        command[command.index("--maxmemory") + 1] = "8mb"
    else:
        command.extend(["--maxmemory", "8mb"])
    name = "werco-redis-hygiene-" + uuid.uuid4().hex

    def redis_cli(*args, input_text=None):
        return _docker(
            "exec",
            "-i",
            "-e",
            "REDISCLI_AUTH=hygiene-test-only",
            name,
            "redis-cli",
            "--raw",
            *args,
            input_text=input_text,
        )

    try:
        _docker(
            "run", "--detach", "--name", name, "--network=none", "--memory=64m", service["image"], *command, timeout=120
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                if redis_cli("PING").strip() == "PONG":
                    break
            except subprocess.CalledProcessError:
                pass
            assert time.monotonic() < deadline, "Synthetic Redis did not become ready"
            time.sleep(0.1)
        assert redis_cli("SET", "arq:job:retained", "queued-payload", "EX", "3600").strip() == "OK"
        assert redis_cli("ZADD", "arq:queue", "1", "retained").strip() == "1"
        commands = "".join(f"SET cache:pressure:{i} {'x' * 65536}\n" for i in range(192))
        response = redis_cli(input_text=commands)
        assert "OOM" in response, "The fixture did not exhaust Redis; retention was not exercised"
        assert redis_cli("GET", "arq:job:retained").strip() == "queued-payload"
        assert redis_cli("ZSCORE", "arq:queue", "retained").strip() == "1"
        stats = redis_cli("INFO", "stats")
        assert "evicted_keys:0" in stats
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30, check=False)
