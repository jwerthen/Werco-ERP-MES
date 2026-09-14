"""Protect reproducible installs and non-destructive lock regeneration."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lock_dependencies", BACKEND / "scripts" / "lock_dependencies.py")
lock_dependencies = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock_dependencies)


def _locked_versions(name):
    return {
        (package, version)
        for package, version in re.findall(r"^([a-z0-9-]+)==([^ ;\\\n]+)", (BACKEND / name).read_text(), re.MULTILINE)
    }


def test_runtime_and_development_lock_the_same_application_versions():
    """A package passing CI must be the same version installed in API/worker images."""
    runtime = _locked_versions("requirements.lock")
    development = _locked_versions("requirements-dev.lock")
    assert runtime
    assert runtime <= development


def test_test_tools_are_absent_from_the_runtime_lock():
    runtime = {name for name, _ in _locked_versions("requirements.lock")}
    development = {name for name, _ in _locked_versions("requirements-dev.lock")}
    for tool in ("pytest", "pytest-asyncio", "pytest-cov", "pytest-xdist", "pip-audit", "uv"):
        assert tool not in runtime
        assert tool in development


@pytest.fixture
def lock_workspace(tmp_path, monkeypatch):
    for name in lock_dependencies.INPUTS:
        (tmp_path / name).write_text("example>=1\n")
    for name in lock_dependencies.LOCKS:
        (tmp_path / name).write_text("example==1\n")
    monkeypatch.setattr(lock_dependencies, "BACKEND_DIR", tmp_path)
    monkeypatch.setattr(lock_dependencies.shutil, "which", lambda *args, **kwargs: "/fake/uv")
    monkeypatch.setattr(
        lock_dependencies.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=f"uv {lock_dependencies.UV_VERSION}\n"),
    )
    return tmp_path


def test_failed_development_resolution_preserves_both_committed_locks(lock_workspace, monkeypatch):
    def fail_after_runtime(scratch, uv, upgrade_packages):
        (scratch / "requirements.lock").write_text("example==2\n")
        raise subprocess.CalledProcessError(1, [uv], stderr="incompatible development dependency\n")

    monkeypatch.setattr(lock_dependencies, "compile_locks", fail_after_runtime)
    monkeypatch.setattr(lock_dependencies.sys, "argv", ["lock_dependencies.py"])
    assert lock_dependencies.main() == 1
    for name in lock_dependencies.LOCKS:
        assert (lock_workspace / name).read_text() == "example==1\n"


def test_check_detects_a_stale_lock_without_writing_either_file(lock_workspace, monkeypatch):
    def compile_changed(scratch, uv, upgrade_packages):
        for name in lock_dependencies.LOCKS:
            (scratch / name).write_text("example==2\n")

    monkeypatch.setattr(lock_dependencies, "compile_locks", compile_changed)
    monkeypatch.setattr(lock_dependencies.sys, "argv", ["lock_dependencies.py", "--check"])
    assert lock_dependencies.main() == 1
    for name in lock_dependencies.LOCKS:
        assert (lock_workspace / name).read_text() == "example==1\n"


def test_successful_regeneration_publishes_both_results(lock_workspace, monkeypatch):
    def compile_changed(scratch, uv, upgrade_packages):
        for name in lock_dependencies.LOCKS:
            # A resolver must never work directly on committed output files.
            assert (lock_workspace / name).read_text() == "example==1\n"
            (scratch / name).write_text("example==2\n")

    monkeypatch.setattr(lock_dependencies, "compile_locks", compile_changed)
    monkeypatch.setattr(lock_dependencies.sys, "argv", ["lock_dependencies.py"])
    assert lock_dependencies.main() == 0
    for name in lock_dependencies.LOCKS:
        assert (lock_workspace / name).read_text() == "example==2\n"
