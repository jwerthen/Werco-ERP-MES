"""Structural guards on the ARQ worker's deploy configuration.

Two traps make it easy to "deploy a worker" and still have no worker running. Both are
config-shaped, so tests that read the deploy files are the only place they can be caught.

TRAP 1 -- the inherited healthcheck. CI deploys the API with
``cd backend && railway up . --path-as-root``, which makes ``backend/`` the archive root, so
Railway reads ``backend/railway.toml`` and its ``healthcheckPath = "/health"``. An arq
worker serves no HTTP and can never satisfy that probe: the deployment is never promoted and
the container is restarted -- while still running arq during each healthcheck window, so it
can fire crons in bursts before being killed. A partially-executed cron repeated on every
retry is worse than no worker. The fix is that the worker is deployed from the REPO ROOT and
reads the repo-root ``railway.toml``, which declares no healthcheck at all. These tests keep
those two files distinct and keep the CI steps rooted where they are.

TRAP 2 -- the accidental second API. Build a worker from ``backend/Dockerfile`` and leave
its start command at the default, and the container runs
``alembic upgrade head && uvicorn app.main:app``: it answers /health, passes its healthcheck,
looks green, and runs zero jobs. ``backend/Dockerfile.worker`` exists so the image's own CMD
is ``arq``, making that outcome impossible rather than merely documented.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _read(path: Path) -> str:
    return path.read_text()


class TestWorkerRailwayConfigIsSeparateFromTheApi:
    def test_repo_root_railway_toml_exists(self):
        assert (REPO_ROOT / "railway.toml").is_file(), (
            "The worker's Railway config must live at the repo root. It cannot live in "
            "backend/ -- that is where the API's railway.toml is, and Railway resolves one "
            "config file per archive root."
        )

    def test_worker_config_declares_no_healthcheck(self):
        """The whole reason the file lives at the repo root."""
        text = _read(REPO_ROOT / "railway.toml")
        # Assert on the ACTIVE (uncommented) lines only; the header discusses the trap at
        # length and mentions healthcheckPath by name several times.
        active = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        assert not any(ln.startswith("healthcheckPath") for ln in active), (
            "An arq worker binds no port and serves no HTTP. A healthcheckPath here means the "
            "deployment can never be promoted, while the container still runs crons during "
            "each failing healthcheck window."
        )

    def test_worker_config_starts_arq_not_uvicorn(self):
        text = _read(REPO_ROOT / "railway.toml")
        assert 'startCommand = "arq app.worker.WorkerSettings"' in text
        assert "uvicorn" not in text

    def test_worker_config_points_at_the_worker_dockerfile(self):
        text = _read(REPO_ROOT / "railway.toml")
        assert (
            'dockerfilePath = "backend/Dockerfile.worker"' in text
        ), "Repo-root-relative, because the worker's archive root is the repo root."

    def test_worker_runs_exactly_one_replica(self):
        """The cron scheduler is per-process: two replicas fire every cron twice, which for
        MRP AUTO_DRAFT means two sets of draft POs and work orders per day."""
        text = _read(REPO_ROOT / "railway.toml")
        assert "numReplicas = 1" in text

    def test_the_api_config_is_untouched(self):
        """The API still needs its healthcheck; this change must not have removed it."""
        text = _read(BACKEND_ROOT / "railway.toml")
        assert 'healthcheckPath = "/health"' in text
        assert "arq" not in text


class TestWorkerImageCannotBecomeASecondApi:
    def test_worker_dockerfile_exists(self):
        assert (BACKEND_ROOT / "Dockerfile.worker").is_file()

    def test_worker_dockerfile_cmd_is_arq(self):
        text = _read(BACKEND_ROOT / "Dockerfile.worker")
        assert 'CMD ["arq", "app.worker.WorkerSettings"]' in text

    def test_worker_dockerfile_never_starts_uvicorn(self):
        """If it could, forgetting the start-command override would silently produce a second
        API replica that passes health checks and does no background work."""
        active = [ln for ln in _read(BACKEND_ROOT / "Dockerfile.worker").splitlines() if not ln.startswith("#")]
        assert not any("uvicorn" in ln for ln in active)

    def test_worker_dockerfile_does_not_run_migrations(self):
        """Migrations belong to the API alone. Two services racing `alembic upgrade head`
        against one database at deploy time can corrupt the version table."""
        active = [ln for ln in _read(BACKEND_ROOT / "Dockerfile.worker").splitlines() if not ln.startswith("#")]
        assert not any("alembic" in ln for ln in active)

    def test_api_dockerfile_still_starts_the_api(self):
        text = _read(BACKEND_ROOT / "Dockerfile")
        assert "uvicorn app.main:app" in text


class TestCiKeepsTheArchiveRootsDistinct:
    """The repo-root railway.toml is only safe as long as no OTHER service is deployed from
    the repo root -- a service reading it would start running arq instead of its own app."""

    @property
    def workflow(self) -> str:
        return _read(REPO_ROOT / ".github" / "workflows" / "ci-cd.yml")

    @pytest.mark.parametrize(
        "service,expected_dir",
        [
            ("werco-api", "backend"),
            ("werco-api-staging", "backend"),
            ("werco-frontend", "frontend"),
            ("werco-frontend-staging", "frontend"),
        ],
    )
    def test_non_worker_services_deploy_from_their_own_subdirectory(self, service: str, expected_dir: str):
        lines = self.workflow.splitlines()
        for i, line in enumerate(lines):
            if f"railway up --service {service} " not in line:
                continue
            preceding = "\n".join(lines[max(0, i - 3) : i])
            assert f"cd {expected_dir}" in preceding, (
                f"{service} must be deployed from {expected_dir}/ so its archive root is "
                f"{expected_dir}/ and it reads {expected_dir}/railway.toml -- NOT the "
                "repo-root railway.toml, which starts an arq worker."
            )

    def test_worker_deploy_steps_are_gated_off_by_default(self):
        """PREPARE ONLY: merging this must not deploy anything. The steps stay skipped until
        the owner sets the repo variables, after the services exist."""
        text = self.workflow
        assert "if: vars.DEPLOY_WORKER_PRODUCTION == 'true'" in text
        assert "if: vars.DEPLOY_WORKER_STAGING == 'true'" in text

    def test_worker_deploy_step_does_not_cd_into_backend(self):
        lines = self.workflow.splitlines()
        for i, line in enumerate(lines):
            if "railway up --service werco-worker" not in line:
                continue
            preceding = "\n".join(lines[max(0, i - 3) : i])
            assert "cd backend" not in preceding, (
                "Deploying the worker from backend/ makes backend/railway.toml its config "
                "file again, reinstating the healthcheckPath=/health trap."
            )

    def test_worker_production_deploy_runs_after_the_release_stamp(self):
        """So backend/RELEASE ships in the worker's upload and its Sentry events carry the
        same release tag as the API's."""
        text = self.workflow
        stamp = text.index("Stamp release SHA into the backend artifact")
        worker = text.index("railway up --service werco-worker .")
        assert stamp < worker
