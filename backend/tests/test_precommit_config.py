"""Regression coverage for .pre-commit-config.yaml.

WHY THIS FILE EXISTS. From its introduction until 2026-07-28 the repo's
pre-commit config was invalid YAML: line 13 read ``exclude:^.env.example$``
with no space after the colon. ``pre-commit`` therefore could not load the
config at all, so EVERY hook in it was silently dead -- including
``detect-private-key``. Nothing caught it:

* the ``check-yaml`` hook could not, because check-yaml is configured *by* the
  file it would have to parse first;
* CI never ran pre-commit at all;
* a dead hook fails open and looks exactly like a clean run.

Reviving the config surfaced a second class of latent bug in the hooks that had
never had a chance to run -- args split on YAML flow-sequence commas
(``--extend-ignore=E203,W503`` passed ``W503`` to flake8 as a filename), and a
``bandit`` hook pointing at ``backend/pyproject.toml``, a file that has never
existed in this repo. The tests below cover both classes.

On secret scanning specifically: ``detect-private-key`` is the repo's only
*enforcing* secret-detection control (it also runs in ci-cd.yml's Secret
Scanning job). Trivy's filesystem scan in the security-scan job does enable its
secret scanner by default, but it is non-blocking, its backend SARIF upload is
``continue-on-error``, its frontend SARIF is never uploaded, and it covers only
``backend/`` and ``frontend/`` -- not the repo root or ``.github/``. And neither
tool finds API tokens or passwords; this is a floor, not full secret scanning.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _all_hooks():
    for repo in _load_config()["repos"]:
        for hook in repo.get("hooks", []):
            yield repo.get("repo", "<local>"), hook


@pytest.mark.unit
class TestPreCommitConfig:
    def test_config_file_exists(self):
        assert CONFIG_PATH.is_file(), f"missing {CONFIG_PATH}"

    def test_config_is_valid_yaml(self):
        # THE regression. A ScannerError here means pre-commit loads nothing and
        # every local hook -- including secret detection -- is silently disabled.
        try:
            config = _load_config()
        except yaml.YAMLError as exc:  # pragma: no cover - the failure path is the point
            pytest.fail(
                f".pre-commit-config.yaml is not valid YAML, so pre-commit cannot load ANY "
                f"hook (this exact bug shipped once and disabled secret detection): {exc}"
            )
        assert isinstance(config, dict), "top-level config must be a mapping"
        assert config.get("repos"), "config must declare at least one repo"

    def test_every_hook_has_an_id(self):
        for repo_url, hook in _all_hooks():
            assert hook.get("id"), f"hook without an id in {repo_url!r}"

    def test_hook_args_are_all_flags(self):
        # Catches the YAML flow-sequence comma bug. Written as
        #     args: [--max-line-length=120, --extend-ignore=E203,W503]
        # YAML splits on BOTH commas, so the hook received a bare "W503" and
        # passed it to flake8 as a filename -- failing unconditionally, even on
        # a clean tree. Every arg we pass to these hooks is a flag, so a bare
        # value is always this bug. Quote the arg to fix it.
        stray = [
            (hook["id"], arg)
            for _, hook in _all_hooks()
            for arg in hook.get("args", [])
            if not str(arg).startswith("-")
        ]
        assert not stray, (
            "pre-commit hook args contain a non-flag value, which is almost always a YAML "
            f"flow-sequence comma splitting one arg into two: {stray}. Quote the argument, "
            "e.g. args: ['--extend-ignore=E203,W503']."
        )

    def test_referenced_config_files_exist(self):
        # Catches the `bandit -c backend/pyproject.toml` class of bug: a hook
        # pointing at a config file that is not in the repo. Such a hook fails
        # (or silently misbehaves) at run time rather than at review time.
        missing = []
        for _, hook in _all_hooks():
            for arg in hook.get("args", []):
                if not isinstance(arg, str) or "=" not in arg:
                    continue
                _, _, value = arg.partition("=")
                if not value or value.startswith("-") or "/" not in value:
                    continue
                if not (REPO_ROOT / value).exists():
                    missing.append(f"{hook['id']}: {arg}")
        assert not missing, "pre-commit hooks reference config files that do not exist: " + "; ".join(missing)

    def test_detect_private_key_hook_is_present(self):
        # The repo's only enforcing secret-detection control. ci-cd.yml's
        # Secret Scanning job runs this exact hook by id, so removing it here
        # silently turns that CI job into a no-op. Removing it should be a
        # deliberate act that also updates the CI job -- not a quiet edit.
        hook_ids = {hook["id"] for _, hook in _all_hooks()}
        assert "detect-private-key" in hook_ids, (
            "detect-private-key was removed from .pre-commit-config.yaml. It is the repo's only "
            "enforcing secret-detection control and the CI secret-scan job runs this exact hook."
        )
