"""Regression guards for the Sentry sampling / release-tagging wiring.

WHY THIS FILE EXISTS. Sentry was activated in production with
``traces_sample_rate=1.0`` hardcoded and no ``release``. Both are quiet failures:

* At 1.0 every request becomes a performance transaction, including Railway's
  ``/health`` probe and the shop-floor wallboard poll -- two clients that hit the API
  every 30 seconds forever and produce no useful trace data. Transaction volume burns
  the same Sentry quota that real errors are billed against, so the observable symptom
  of over-sampling is *losing error reports*, which looks identical to "no errors".
* With no ``release``, an error cannot be tied to a deploy, and ``/health/detailed``
  reported only a hardcoded ``version: "1.0.0"`` -- so answering "is commit X actually
  running?" meant cross-referencing GitHub Actions against the Railway API by hand.

These tests only read config, source files and workflow YAML, so they need no DB, no
network and no Sentry account.

NOTE ON WHAT ``traces_sample_rate`` DOES: it governs performance transactions ONLY.
Error capture is governed by a different Sentry setting (``sample_rate``, left at its
1.0 default). Lowering the trace rate does not lose a single exception -- the tests
below pin that separation so nobody "restores" 1.0 out of a misplaced fear of it.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"

# Minimum env for a Settings() that passes the security validators, so these tests
# exercise the observability fields and nothing else. Mirrors tests/test_config.py.
BASE_ENV = {
    "SECRET_KEY": "a" * 64,
    "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
}


def _settings(**overrides: str) -> Any:
    """Build a Settings() from a clean environment plus ``overrides``."""
    from app.core.config import Settings

    with mock.patch.dict(os.environ, {**BASE_ENV, **overrides}, clear=True):
        return Settings()


@pytest.fixture
def no_release_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point RELEASE_FILE at a path that does not exist.

    A developer who ran the CI stamp locally would otherwise have a real
    ``backend/RELEASE`` on disk and see different results than CI.
    """
    import app.core.config as config_module

    monkeypatch.setattr(config_module, "RELEASE_FILE", tmp_path / "absent" / "RELEASE")


class TestTracesSampleRate:
    """The rate must come from settings and default low."""

    def test_defaults_to_ten_percent(self) -> None:
        assert _settings().SENTRY_TRACES_SAMPLE_RATE == 0.1

    def test_reads_from_the_environment(self) -> None:
        assert _settings(SENTRY_TRACES_SAMPLE_RATE="1.0").SENTRY_TRACES_SAMPLE_RATE == 1.0
        assert _settings(SENTRY_TRACES_SAMPLE_RATE="0").SENTRY_TRACES_SAMPLE_RATE == 0.0

    @pytest.mark.parametrize("bad", ["10", "-0.1", "1.5"])
    def test_rejects_a_rate_outside_zero_to_one(self, bad: str) -> None:
        """``10`` meaning "10 percent" must fail loudly.

        Sentry reads any value >= 1.0 as "sample everything", so accepting it would
        silently produce the exact over-sampling this setting exists to prevent.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="SENTRY_TRACES_SAMPLE_RATE"):
            _settings(SENTRY_TRACES_SAMPLE_RATE=bad)


class TestAppRelease:
    """APP_RELEASE resolves from the env var, else from the shipped RELEASE file."""

    def test_defaults_to_none_when_nothing_is_set(self, no_release_file: None) -> None:
        assert _settings().APP_RELEASE is None

    def test_reads_from_the_environment(self, no_release_file: None) -> None:
        assert _settings(APP_RELEASE="deadbeef").APP_RELEASE == "deadbeef"

    def test_falls_back_to_the_release_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import app.core.config as config_module

        release = tmp_path / "RELEASE"
        release.write_text("a1b2c3d4e5f6\n")
        monkeypatch.setattr(config_module, "RELEASE_FILE", release)

        assert _settings().APP_RELEASE == "a1b2c3d4e5f6"

    def test_environment_variable_wins_over_the_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import app.core.config as config_module

        release = tmp_path / "RELEASE"
        release.write_text("from-file")
        monkeypatch.setattr(config_module, "RELEASE_FILE", release)

        assert _settings(APP_RELEASE="from-env").APP_RELEASE == "from-env"

    @pytest.mark.parametrize("contents", ["", "   \n", "x" * 201, "line-one\nline-two"])
    def test_unusable_file_contents_degrade_to_no_release(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str
    ) -> None:
        """A stray file must mean "no release", never a boot failure or a payload.

        ``/health/detailed`` is unauthenticated, so whatever this returns is public.
        """
        import app.core.config as config_module

        release = tmp_path / "RELEASE"
        release.write_text(contents)
        monkeypatch.setattr(config_module, "RELEASE_FILE", release)

        assert config_module.read_release_file() is None
        assert _settings().APP_RELEASE is None

    def test_a_utf16_stamp_degrades_to_no_release(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Windows PowerShell 5.1 redirects (`>`) as UTF-16, which is not a release.

        Decoded as UTF-8 it is NULs and replacement characters, none of which ``.strip()``
        removes -- without a charset check that mush would be reported to Sentry and
        echoed from the unauthenticated ``/health/detailed`` as the running commit.
        """
        import app.core.config as config_module

        release = tmp_path / "RELEASE"
        release.write_bytes("a1b2c3d4\r\n".encode("utf-16"))
        monkeypatch.setattr(config_module, "RELEASE_FILE", release)

        assert config_module.read_release_file() is None

    def test_missing_file_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import app.core.config as config_module

        monkeypatch.setattr(config_module, "RELEASE_FILE", tmp_path / "nope" / "RELEASE")
        assert config_module.read_release_file() is None


class TestSentryInit:
    """``init_sentry()`` must pass the configured values through to the SDK."""

    def _captured_init(self, monkeypatch: pytest.MonkeyPatch, **setting_overrides: Any) -> Dict[str, Any]:
        """Call init_sentry() with sentry_sdk.init stubbed; return the kwargs it got."""
        import sentry_sdk

        from app.core.config import settings
        from app.main import init_sentry

        captured: Dict[str, Any] = {}

        def fake_init(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(sentry_sdk, "init", fake_init)
        monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        for name, value in setting_overrides.items():
            monkeypatch.setattr(settings, name, value)

        init_sentry()
        return captured

    def test_sample_rate_comes_from_settings_not_a_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        captured = self._captured_init(monkeypatch)
        assert captured["traces_sample_rate"] == settings.SENTRY_TRACES_SAMPLE_RATE

    def test_a_custom_sample_rate_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._captured_init(monkeypatch, SENTRY_TRACES_SAMPLE_RATE=0.25)
        assert captured["traces_sample_rate"] == 0.25

    def test_release_is_passed_through_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._captured_init(monkeypatch, APP_RELEASE="1234567890abcdef")
        assert captured["release"] == "1234567890abcdef"

    def test_init_still_works_when_release_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sentry accepts release=None; boot must not depend on the stamp existing."""
        captured = self._captured_init(monkeypatch, APP_RELEASE=None)
        assert captured["release"] is None
        assert captured["dsn"]  # init actually ran

    def test_error_capture_is_never_sampled_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sample_rate`` (errors) must stay at the SDK default of 1.0.

        The whole argument for lowering ``traces_sample_rate`` is that it costs no
        errors. Passing an error ``sample_rate`` would quietly break that promise.
        """
        captured = self._captured_init(monkeypatch, SENTRY_TRACES_SAMPLE_RATE=0.01)
        assert "sample_rate" not in captured

    def test_a_bad_dsn_cannot_stop_the_app_from_booting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sentry_sdk.init`` validates the DSN synchronously and raises ``BadDsn``.

        ``init_sentry()`` runs at import of ``app.main``, so an uncaught raise means
        uvicorn never binds: a typo or a stray space in the Railway ``SENTRY_DSN``
        variable would take the API down instead of leaving it merely un-instrumented.
        """
        import sentry_sdk
        from sentry_sdk.utils import BadDsn

        from app.core.config import settings
        from app.main import init_sentry

        def boom(**kwargs: Any) -> None:
            raise BadDsn("Unsupported scheme ''")

        monkeypatch.setattr(sentry_sdk, "init", boom)
        monkeypatch.setattr(settings, "SENTRY_DSN", "not-a-dsn")

        init_sentry()  # must not raise

    def test_no_dsn_means_no_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sentry_sdk

        from app.core.config import settings
        from app.main import init_sentry

        calls = []
        monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
        monkeypatch.setattr(settings, "SENTRY_DSN", None)

        init_sentry()
        assert calls == []


class TestReleaseStampShipsWithTheArtifact:
    """CI bakes the SHA into the uploaded source; guard the ways that silently breaks."""

    def test_release_file_is_not_gitignored(self) -> None:
        """``railway up`` honors .gitignore when choosing what to upload.

        Ignoring ``backend/RELEASE`` -- an easy, well-meaning cleanup, since it is a
        build artifact -- would stop it shipping without any other visible change.
        """
        try:
            result = subprocess.run(
                ["git", "check-ignore", "backend/RELEASE"],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - git absent
            pytest.skip("git unavailable")
        # exit 0 => the path IS ignored; 1 => not ignored (what we require); anything
        # else (128 = not a git repo, e.g. a source tarball) is git failing, not an
        # answer -- assert on 1 specifically so an error can never read as a pass.
        if result.returncode not in (0, 1):  # pragma: no cover - no git metadata
            pytest.skip(f"git check-ignore could not answer (exit {result.returncode})")
        assert result.returncode == 1, "backend/RELEASE is gitignored, so `railway up` will not upload it."

    def test_release_file_is_never_committed(self) -> None:
        """The stamp is a per-deploy artifact; a committed one would go stale and lie.

        It is deliberately not gitignored (see above), which leaves it untracked and
        catchable by a stray ``git add -A`` after a break-glass manual deploy. A
        committed SHA would keep being reported by every build that did not overwrite it
        -- staging and local dev, which have no stamping step -- and "reporting the wrong
        commit is worse than reporting none" is the premise the whole design rests on.
        """
        try:
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "backend/RELEASE"],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - git absent
            pytest.skip("git unavailable")
        assert result.returncode != 0, "backend/RELEASE is committed; it must stay a per-deploy artifact."

    def test_ci_stamps_the_sha_before_uploading_the_backend(self) -> None:
        with open(CI_WORKFLOW, encoding="utf-8") as fh:
            workflow = yaml.safe_load(fh)

        steps = workflow["jobs"]["deploy-production"]["steps"]
        names = [step.get("name", "") for step in steps]

        stamp_index = next(i for i, step in enumerate(steps) if "backend/RELEASE" in (step.get("run") or ""))
        deploy_index = names.index("Deploy Backend to Production")

        assert stamp_index < deploy_index, (
            "The RELEASE stamp must be written BEFORE `railway up` uploads backend/, "
            f"got stamp at {stamp_index} and deploy at {deploy_index}."
        )
        assert "GITHUB_SHA" in steps[stamp_index]["run"], "The stamp must carry the workflow's commit SHA."

    def test_dockerfile_copies_the_backend_root_into_the_image(self) -> None:
        """The stamp only reaches the container because the Dockerfile copies backend/ wholesale."""
        dockerfile = (BACKEND_ROOT / "Dockerfile").read_text()
        assert "COPY --chown=appuser:appgroup . ." in dockerfile

    def test_dockerignore_does_not_exclude_the_release_file(self) -> None:
        patterns = {
            line.strip()
            for line in (BACKEND_ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        assert "RELEASE" not in patterns
