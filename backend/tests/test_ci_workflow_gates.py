"""Regression coverage for the delivery gates in .github/workflows/.

WHY THIS FILE EXISTS. Three of this repo's CI gates shipped in a state where
they could not fail, and nothing noticed for months -- a gate that fails open
looks exactly like a clean run, which is the same failure mode
``test_precommit_config.py`` was written for:

* ``pr-check.yml``'s frontend ESLint and TypeScript steps ran as
  ``<cmd> 2>/dev/null || echo "completed with warnings"``. The ``|| echo``
  forced exit 0, so a real type error rendered as a green step captioned
  "completed with warnings" -- with the diagnostics sitting in the log,
  unread and unenforced (both tools report on stdout, which was never
  redirected; ``2>/dev/null`` hid only the tools' own failure output).
* ``ci-cd.yml``'s MyPy step carried ``continue-on-error: true`` while mypy was
  in fact clean over all 326 source files -- the gate was decoration, inside a
  status check ("Backend Linting") that IS required by the main ruleset.
* the backend coverage floor sat at 50% against ~81% actual, and the frontend
  jest thresholds at 2% against ~56% actual, so ~30 points of coverage could
  evaporate without turning anything red.

These tests assert the shape of the fix, not the behavior of CI, so they run
anywhere with no runner and no network. They live in the REQUIRED "Backend
Tests" context on purpose: a gate that can silently un-gate itself needs a gate.

ON THE SWALLOWING PATTERNS. A step that is genuinely advisory must say so with
``continue-on-error: true`` and a comment explaining why -- that is this repo's
existing idiom (the Trivy SARIF upload and the two advisory audit copies in
ci-cd.yml all use it) and unlike ``|| true`` it is greppable, shows in the UI as
a distinct outcome, and still surfaces the command's output.
"""

import configparser
import json
import re
import subprocess
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PYTEST_INI = REPO_ROOT / "backend" / "pytest.ini"
JEST_CONFIG = REPO_ROOT / "frontend" / "jest.config.js"
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"


def _frontend_script(name: str) -> str:
    """Return frontend/package.json's script body, or "" when it is absent."""
    return json.loads(PACKAGE_JSON.read_text())["scripts"].get(name, "")


# The two workflows that carry the merge-relevant gates.
GATE_WORKFLOWS = ("pr-check.yml", "ci-cd.yml")

# Every job that exists to say "no" to a bad change. The four ci-cd.yml ones are
# REQUIRED status checks in the main ruleset; pr-check.yml's two are not, but
# they are the PR-facing signal and are kept at parity on purpose.
GATE_JOBS = (
    ("ci-cd.yml", "backend-lint"),
    ("ci-cd.yml", "backend-test"),
    ("ci-cd.yml", "frontend-lint"),
    ("ci-cd.yml", "frontend-test"),
    ("pr-check.yml", "backend-checks"),
    ("pr-check.yml", "frontend-checks"),
)

# Shell fragments that make a command's exit status meaningless, discard its
# diagnostics, or both. See the module docstring for the sanctioned alternative.
FAILURE_SWALLOWING_FRAGMENTS = ("2>/dev/null", "|| echo", "|| true")

# Coverage ratchets. Backend actual is ~81%; frontend actual, measured on this
# branch, is 56.40 statements / 46.71 branches / 41.31 functions / 56.34 lines.
# Each floor sits a few points under actual to absorb shard/ordering jitter.
EXPECTED_BACKEND_COVERAGE_FLOOR = 78
EXPECTED_JEST_THRESHOLDS = {"statements": 52, "branches": 43, "functions": 38, "lines": 52}


def _load_workflow(name: str) -> dict:
    with open(WORKFLOWS_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _iter_steps(workflow: dict) -> Iterator[Tuple[str, dict]]:
    """Yield (job_key, step) for every step in the workflow."""
    for job_key, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_key, step


class TestNoGateSwallowsItsOwnFailure:
    """Every `run:` in the gate workflows must let a failure be a failure."""

    @pytest.mark.parametrize("workflow_name", GATE_WORKFLOWS)
    def test_no_run_step_discards_its_exit_status_or_output(self, workflow_name: str) -> None:
        workflow = _load_workflow(workflow_name)
        offenders = []
        for job_key, step in _iter_steps(workflow):
            script = step.get("run")
            if not script:
                continue
            for fragment in FAILURE_SWALLOWING_FRAGMENTS:
                if fragment in script:
                    offenders.append(f"{workflow_name} job={job_key} step={step.get('name', '?')!r} has {fragment!r}")

        assert not offenders, (
            "These steps cannot fail and/or hide their output:\n  "
            + "\n  ".join(offenders)
            + "\nIf a step is genuinely advisory use `continue-on-error: true` with a comment "
            "explaining why, so it stays visible and greppable."
        )


class TestGateStepsAreBlocking:
    """`continue-on-error` is the other way to un-gate a gate, and it is silent.

    Unswallowing `|| echo` only closes the shell-level hole; a single
    `continue-on-error: true` on the same step reopens it, reports the job green,
    and is invisible in the checks list. Advisory steps are legitimate -- the
    Trivy SARIF upload and the two audit copies in ci-cd.yml are -- but not
    inside the jobs whose entire purpose is to fail.
    """

    @pytest.mark.parametrize("workflow_name,job_key", GATE_JOBS)
    def test_no_step_in_a_gate_job_is_advisory(self, workflow_name: str, job_key: str) -> None:
        workflow = _load_workflow(workflow_name)
        job = (workflow.get("jobs") or {}).get(job_key)
        assert job is not None, f"{workflow_name} lost its {job_key!r} job -- was it renamed?"

        advisory = [
            step.get("name", step.get("uses", "?")) for step in job.get("steps") or [] if step.get("continue-on-error")
        ]
        assert not advisory, (
            f"{workflow_name} job {job_key} has continue-on-error on: {advisory}. "
            "These jobs exist to block a bad merge; a step that cannot fail reports green. "
            "If the check is genuinely advisory, move it out of this job."
        )


class TestMyPyIsBlocking:
    """ci-cd.yml's MyPy step sits inside the REQUIRED 'Backend Linting' context."""

    def _mypy_step(self) -> dict:
        workflow = _load_workflow("ci-cd.yml")
        for job_key, step in _iter_steps(workflow):
            if job_key == "backend-lint" and "mypy" in (step.get("run") or ""):
                return step
        pytest.fail("No MyPy step found in ci-cd.yml's backend-lint job -- did the job or step get renamed?")

    def test_mypy_step_exists_and_is_not_continue_on_error(self) -> None:
        step = self._mypy_step()
        assert "continue-on-error" not in step, (
            "ci-cd.yml's MyPy step has continue-on-error again. mypy is clean across the app "
            "package; if it has genuinely regressed, fix the annotations rather than re-opening "
            "the gate."
        )

    def test_mypy_uses_the_repo_config(self) -> None:
        # Without --config-file mypy runs with defaults and reports a different
        # (much noisier) set of errors than the documented gate command.
        assert "--config-file=mypy.ini" in self._mypy_step()["run"]


class TestFrontendLintGateMatchesCiCd:
    """pr-check.yml must not be laxer than the required ci-cd.yml job."""

    @pytest.mark.parametrize(
        "workflow_name,job_key", [("pr-check.yml", "frontend-checks"), ("ci-cd.yml", "frontend-lint")]
    )
    def test_eslint_caps_warnings(self, workflow_name: str, job_key: str) -> None:
        workflow = _load_workflow(workflow_name)
        eslint_runs = [
            step["run"] for key, step in _iter_steps(workflow) if key == job_key and "lint" in (step.get("run") or "")
        ]
        assert eslint_runs, f"No ESLint step found in {workflow_name} job {job_key}"
        # frontend/package.json's "lint" is a bare `eslint src --ext .ts,.tsx` with
        # no cap of its own, so the passthrough is what makes warnings fatal.
        assert any("--max-warnings=0" in run for run in eslint_runs), (
            f"{workflow_name}'s ESLint step must pass --max-warnings=0; the package.json script "
            "has no warning cap, so without it a11y regressions pass as warnings."
        )

    @pytest.mark.parametrize(
        "workflow_name,job_key", [("pr-check.yml", "frontend-checks"), ("ci-cd.yml", "frontend-lint")]
    )
    def test_typescript_is_checked(self, workflow_name: str, job_key: str) -> None:
        workflow = _load_workflow(workflow_name)
        runs = [step.get("run") or "" for key, step in _iter_steps(workflow) if key == job_key]

        inline = [run for run in runs if "tsc --noEmit" in run]
        delegated = [run for run in runs if "npm run type-check" in run]
        assert inline or delegated, f"{workflow_name} job {job_key} lost its TypeScript check"

        if delegated and not inline:
            # A workflow may call the npm script instead of tsc directly -- that is
            # how the two programs (source + tests) stay in one place. But then the
            # SCRIPT is the gate, so follow the indirection: pointing the step at a
            # script that does not run tsc would satisfy the assertion above while
            # checking nothing, which is precisely the fail-open shape this file exists
            # to catch.
            script = _frontend_script("type-check")
            assert "tsc --noEmit" in script, (
                f"{workflow_name} job {job_key} delegates to `npm run type-check`, but that "
                f"script does not run tsc: {script!r}"
            )

    def test_the_type_check_script_covers_the_test_files_too(self) -> None:
        """The test files must stay inside a tsc program.

        They were outside every one until 2026-08-04: ``tsconfig.json`` excludes
        ``**/*.test.ts(x)``, and CI ran a bare ``tsc --noEmit`` that inherited the
        exclusion, while ts-jest ran transpile-only. 229 test files were checked by
        nothing, which is how ``SPC.tsx`` stayed green for months while being
        non-functional in production -- its test mocked the API client as resolving
        the shape the page wrongly expected, and no gate could see the contradiction.

        Asserting both ``-p`` targets is what stops the test program being quietly
        dropped from the script, which would restore that hole without touching a
        workflow file.
        """
        script = _frontend_script("type-check")
        assert "-p tsconfig.json" in script, "the type-check script must still check the source program"
        assert "-p tsconfig.test.json" in script, (
            "the type-check script must check the TEST program too; without it the test "
            "files are type-checked by nothing (see this test's docstring)"
        )

        test_tsconfig = REPO_ROOT / "frontend" / "tsconfig.test.json"
        assert test_tsconfig.is_file(), "frontend/tsconfig.test.json is missing"
        # Strip // comments -- the file is JSONC, like the tsconfig it extends.
        raw = re.sub(r"^\s*//.*$", "", test_tsconfig.read_text(), flags=re.MULTILINE)
        config = json.loads(raw)
        excluded = " ".join(config.get("exclude", []))
        assert (
            "test" not in excluded
        ), f"tsconfig.test.json must not exclude the test files it exists to cover; exclude={config.get('exclude')!r}"


class TestCoverageFloors:
    def _pytest_ini_floor(self) -> int:
        # Raw parser: pytest.ini's filterwarnings values carry regex punctuation
        # that ConfigParser's %-interpolation would choke on.
        parser = configparser.RawConfigParser()
        parser.read(PYTEST_INI, encoding="utf-8")
        addopts = parser["pytest"]["addopts"]
        match = re.search(r"--cov-fail-under=(\d+)", addopts)
        assert match, "backend/pytest.ini addopts no longer sets --cov-fail-under at all"
        return int(match.group(1))

    def test_backend_floor_is_ratcheted(self) -> None:
        assert self._pytest_ini_floor() >= EXPECTED_BACKEND_COVERAGE_FLOOR, (
            "backend/pytest.ini's --cov-fail-under was lowered. It is the source of truth for the "
            "REQUIRED 'Backend Tests' job, which passes no threshold of its own."
        )

    @pytest.mark.parametrize("workflow_name", GATE_WORKFLOWS)
    def test_workflow_floor_does_not_diverge_from_pytest_ini(self, workflow_name: str) -> None:
        # A CLI --cov-fail-under overrides pytest.ini's addopts, so an explicit
        # copy in a workflow silently pins that job to whatever number it was
        # left at. No override at all is fine (ci-cd.yml's required backend-test
        # job has none and inherits addopts) -- a DIFFERENT one is not.
        workflow = _load_workflow(workflow_name)
        explicit = [
            int(m)
            for _, step in _iter_steps(workflow)
            for m in re.findall(r"--cov-fail-under=(\d+)", step.get("run") or "")
        ]
        expected = self._pytest_ini_floor()
        assert all(value == expected for value in explicit), (
            f"{workflow_name} pins --cov-fail-under={explicit} but backend/pytest.ini says {expected}. "
            "Keep them equal, or drop the CLI flag and let pytest.ini be authoritative."
        )

    def test_frontend_thresholds_are_ratcheted(self) -> None:
        source = JEST_CONFIG.read_text(encoding="utf-8")
        block = re.search(r"coverageThreshold:\s*\{\s*global:\s*\{(.*?)\}", source, re.DOTALL)
        assert block, "frontend/jest.config.js no longer declares coverageThreshold.global"
        found = {key: int(value) for key, value in re.findall(r"(\w+):\s*(\d+)", block.group(1))}
        lowered = {
            key: (found.get(key), floor) for key, floor in EXPECTED_JEST_THRESHOLDS.items() if found.get(key, 0) < floor
        }
        assert not lowered, (
            f"frontend jest coverage thresholds were lowered (found vs floor): {lowered}. "
            "Raising them is fine and needs no change here; lowering them needs a reason, since "
            "they are the only thing holding frontend coverage in place."
        )


# The deploy workflows and the artifact each one stamps with the commit SHA. The
# frontend marker rides in public/ because Vite copies that directory into the build
# output verbatim; the backend one is read by app/core/config.py into APP_RELEASE.
DEPLOY_WORKFLOWS = ("ci-cd.yml", "deploy-frontend-production.yml")
RELEASE_VERIFIER = REPO_ROOT / ".github" / "scripts" / "verify_release.py"
FRONTEND_RELEASE_MARKER = "frontend/public/release.txt"

# The jobs that deploy to PRODUCTION, and the only ones the release-SHA contract binds.
# ci-cd.yml's deploy-staging is deliberately excluded: it fires on `develop`, a branch
# this repo does not use (every PR merges to main), it stamps nothing, and its services
# have no release endpoint to poll -- so it keeps the older, weaker "did anything
# answer /health" check. It carries the same latent false-failure as production did; if
# `develop` is ever revived, give it this treatment and add it here.
PRODUCTION_DEPLOY_JOBS = (
    ("ci-cd.yml", "deploy-production"),
    ("deploy-frontend-production.yml", "deploy-frontend"),
)


def _git(*args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, timeout=15)


class TestDeployFailureSignalIsHonest:
    """The 2026-08-04 false-failure, and the gate that replaced the signal it broke.

    `railway up` exited 1 on two consecutive production deploys (#198, #203) with
    "Failed to stream build logs" roughly 66s after an upload Railway had accepted,
    built and shipped. Its exit status reports whether the CLI could tail a log, not
    whether the deploy landed -- but the job was gated on it, so a healthy deploy went
    red and, because a failed step skips the rest of the job, ALSO skipped the frontend
    deploy, the worker deploy, `verify_launch` and the release tag. Production ran two
    commits the release history denied.

    The fix demotes that exit status to advisory and gates on evidence instead: an
    upload receipt, then a poll of the running service for the SHA this run stamped.
    That trade is only safe while BOTH halves stay in place -- an advisory deploy step
    with no gate behind it is a deploy that cannot fail, which is strictly worse than
    what it replaced. These tests hold the trade open.
    """

    @pytest.mark.parametrize("workflow_name", DEPLOY_WORKFLOWS)
    def test_every_advisory_deploy_step_is_followed_by_a_blocking_gate(self, workflow_name: str) -> None:
        workflow = _load_workflow(workflow_name)
        for job_key, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for index, step in enumerate(steps):
                if "railway up" not in (step.get("run") or ""):
                    continue
                if not step.get("continue-on-error"):
                    continue  # still gated on its own exit status; nothing to check.
                later = steps[index + 1 :]
                blocking = [s for s in later if s.get("run") and not s.get("continue-on-error")]
                assert blocking, (
                    f"{workflow_name} job {job_key} step {step.get('name')!r} is advisory "
                    "(continue-on-error) but nothing blocking runs after it, so a failed "
                    "deploy would report green. Keep the upload receipt and the release check."
                )

    @pytest.mark.parametrize("workflow_name", DEPLOY_WORKFLOWS)
    def test_advisory_deploy_steps_are_receipted(self, workflow_name: str) -> None:
        """A tolerated exit status is only safe because a rejected upload still fails.

        The CLI prints a "Build Logs:" URL once, when the upload has been accepted and a
        build queued. Grepping the teed log for it is what separates "the flake we mean
        to tolerate" from "the token is wrong and nothing was ever deployed" -- without
        it, a misconfigured token would be discovered only by the release poll timing out.
        """
        workflow = _load_workflow(workflow_name)
        for job_key, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for index, step in enumerate(steps):
                script = step.get("run") or ""
                if "railway up" not in script or not step.get("continue-on-error"):
                    continue
                assert "tee " in script, (
                    f"{workflow_name} job {job_key} step {step.get('name')!r} tolerates its exit "
                    "status but does not tee its output, so the receipt check has nothing to read."
                )
                receipts = [s for s in steps[index + 1 :] if "Build Logs:" in (s.get("run") or "")]
                assert receipts, (
                    f"{workflow_name} job {job_key} step {step.get('name')!r} is advisory but no "
                    "later step checks the log for a 'Build Logs:' upload receipt."
                )

    def test_production_deploy_verifies_the_release_sha(self) -> None:
        """The replacement gate must compare against THIS run's commit.

        The check it replaced curl'd /health, which the previous container answers just
        as happily -- it proved the site was up, never that the deploy had landed.
        """
        workflow = _load_workflow("ci-cd.yml")
        scripts = [
            step.get("run") or "" for _, step in _iter_steps(workflow) if "verify_release.py" in (step.get("run") or "")
        ]
        assert scripts, "ci-cd.yml no longer verifies the deployed release SHA."
        joined = "\n".join(scripts)
        assert "GITHUB_SHA" in joined, "The release check must compare against the SHA this run deployed."
        for label in ("--label backend", "--label frontend"):
            assert label in joined, f"ci-cd.yml stopped verifying {label.split()[-1]} deploy freshness."

    def test_release_verifier_exists_and_compiles(self) -> None:
        assert RELEASE_VERIFIER.is_file(), f"{RELEASE_VERIFIER} is referenced by the deploy workflows but missing."
        compile(RELEASE_VERIFIER.read_text(encoding="utf-8"), str(RELEASE_VERIFIER), "exec")


class TestFrontendReleaseMarkerShipsWithTheArtifact:
    """Same two rules as backend/RELEASE, for the marker that reports the deployed SPA.

    Mirrors ``test_sentry_observability_config.py::TestReleaseStampShipsWithTheArtifact``
    -- the failure modes are identical and both are one well-meaning cleanup away.
    """

    def test_marker_is_not_gitignored(self) -> None:
        """``railway up`` honors .gitignore, so ignoring it silently stops it shipping."""
        result = _git("git", "check-ignore", FRONTEND_RELEASE_MARKER)
        if result.returncode not in (0, 1):  # pragma: no cover - no git metadata
            pytest.skip(f"git check-ignore could not answer (exit {result.returncode})")
        assert result.returncode == 1, (
            f"{FRONTEND_RELEASE_MARKER} is gitignored, so `railway up` will not upload it "
            "and the frontend release check can never pass."
        )

    def test_marker_is_never_committed(self) -> None:
        """A committed SHA would go stale and keep reporting a commit that is not running."""
        result = _git("git", "ls-files", "--error-unmatch", FRONTEND_RELEASE_MARKER)
        assert result.returncode != 0, (
            f"{FRONTEND_RELEASE_MARKER} is committed; it must stay a per-deploy artifact, "
            "or every build without a stamping step will advertise a stale commit."
        )

    @pytest.mark.parametrize("workflow_name,job_key", PRODUCTION_DEPLOY_JOBS)
    def test_stamp_is_written_before_the_frontend_upload(self, workflow_name: str, job_key: str) -> None:
        workflow = _load_workflow(workflow_name)
        job = (workflow.get("jobs") or {}).get(job_key)
        assert job is not None, f"{workflow_name} lost its {job_key!r} job -- was it renamed?"
        steps = job.get("steps") or []

        uploads = [
            i
            for i, s in enumerate(steps)
            if "railway up" in (s.get("run") or "") and "frontend" in (s.get("run") or "")
        ]
        assert uploads, f"{workflow_name} job {job_key} no longer uploads the frontend."
        stamps = [i for i, s in enumerate(steps) if FRONTEND_RELEASE_MARKER in (s.get("run") or "")]
        assert stamps, f"{workflow_name} job {job_key} uploads the frontend without stamping {FRONTEND_RELEASE_MARKER}."
        assert min(stamps) < min(uploads), (
            f"{workflow_name} job {job_key} stamps {FRONTEND_RELEASE_MARKER} at step {min(stamps)} "
            f"but uploads the frontend at step {min(uploads)} -- the stamp must precede the upload."
        )
