"""Independent process-boundary checks; no native CAD installation is needed."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.fabrication_quote import worker


def test_worker_passes_bytes_and_arguments_without_credentials_then_removes_files(
    monkeypatch,
):
    monkeypatch.setenv("WERCO_QUOTE_WORKER_PYTHON", "/worker runtime/bin/python")
    for key in (
        "DATABASE_URL",
        "SECRET_KEY",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HTTP_PROXY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "synthetic-do-not-inherit")
    monkeypatch.setenv("LANG", "C.UTF-8")
    captured = {}

    def run(command, **options):
        captured.update(command=command, options=options)
        assert command[0] == "/worker runtime/bin/python"
        assert command[1:3] == ["-m", "app.fabrication_quote.worker"]
        assert command[5:] == ["--name", "part ; echo ignored.dxf", "--units", "in"]
        assert Path(command[3]).read_bytes() == b"synthetic source bytes"
        assert options["timeout"] == 60 and options["check"] is False
        assert options["capture_output"] is True
        assert not options.get("shell", False)
        env = options["env"]
        assert env["LANG"] == "C.UTF-8"
        assert env["PYTHONPATH"] == str(options["cwd"])
        assert (
            not {
                "DATABASE_URL",
                "SECRET_KEY",
                "OPENAI_API_KEY",
                "AWS_SECRET_ACCESS_KEY",
                "HTTP_PROXY",
            }
            & env.keys()
        )
        Path(command[4]).write_text(json.dumps({"status": "needs_review", "geometry": None}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    assert worker.analyze_in_worker(b"synthetic source bytes", "part ; echo ignored.dxf", "in") == {
        "status": "needs_review",
        "geometry": None,
    }
    assert not Path(captured["command"][3]).exists()
    assert not Path(captured["command"][4]).exists()


def test_timeout_is_bounded_and_temporary_files_are_cleaned(monkeypatch):
    source = None

    def run(command, **options):
        nonlocal source
        source = Path(command[3])
        raise subprocess.TimeoutExpired(command, options["timeout"], output=b"synthetic private native output")

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(ValueError, match="exceeded 60 seconds") as error:
        worker.analyze_in_worker(b"source", "slow.step")
    assert "private native output" not in str(error.value)
    assert source is not None and not source.exists()


@pytest.mark.parametrize("returncode,write_target", [(1, False), (-11, True), (0, False)])
def test_crash_nonzero_and_missing_results_never_become_success(monkeypatch, returncode, write_target):
    def run(command, **options):
        if write_target:
            Path(command[4]).write_text('{"status":"complete"}')
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(ValueError, match="could not finish"):
        worker.analyze_in_worker(b"source", "invalid.step")


def test_startup_failure_has_actionable_error(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("synthetic missing interpreter")

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(ValueError, match="worker could not start"):
        worker.analyze_in_worker(b"source", "part.dxf")


@pytest.mark.parametrize("extra", [0, 1])
def test_output_evidence_budget_exact_boundary(monkeypatch, extra):
    maximum = 8 * 1024 * 1024

    def run(command, **options):
        with Path(command[4]).open("wb") as target:
            target.write(b"{}")
            # Valid JSON padded with whitespace tests the byte boundary independently of schema.
            target.write(b" " * (maximum + extra - 2))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    if extra:
        with pytest.raises(ValueError, match="evidence budget"):
            worker.analyze_in_worker(b"source", "large.step")
    else:
        assert worker.analyze_in_worker(b"source", "large.step") == {}


def test_invalid_json_output_is_an_error(monkeypatch):
    def run(command, **options):
        Path(command[4]).write_text("not JSON")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(ValueError):
        worker.analyze_in_worker(b"source", "invalid.step")


def test_nesting_uses_same_process_boundary_and_forbids_nonfinite_payload(monkeypatch):
    calls = []

    def analyze(content, filename):
        calls.append((json.loads(content), filename))
        return {"status": "complete", "placements": []}

    monkeypatch.setattr(worker, "analyze_in_worker", analyze)
    assert worker.nest_in_worker({"parts": [], "stocks": []})["status"] == "complete"
    assert calls == [({"parts": [], "stocks": []}, "__nest_input__.json")]
    with pytest.raises(ValueError):
        worker.nest_in_worker({"spacing_mm": float("nan")})
    assert len(calls) == 1


def test_real_main_venv_subprocess_uses_csv_parser(monkeypatch):
    monkeypatch.delenv("WERCO_QUOTE_WORKER_PYTHON", raising=False)
    result = worker.analyze_in_worker(b"MPN,Qty\n000123,4\n", "synthetic.csv")
    assert result["kind"] == "csv"
    assert result["table"]["rows"][1]["cells"] == ["000123", "4"]
    assert result["automatic_costing_ready"] is False


def test_child_resource_limits_are_set_before_parser_execution(monkeypatch, tmp_path):
    resource = pytest.importorskip("resource")
    from app.fabrication_quote import ingestion

    source, target = tmp_path / "source", tmp_path / "result.json"
    source.write_bytes(b"synthetic source")
    limits = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, values: limits.append((kind, values)))
    monkeypatch.setattr(
        worker.sys,
        "argv",
        ["worker", str(source), str(target), "--name", "sample.dxf", "--units", "in"],
    )

    def analyze(content, filename, units):
        assert limits == [
            (resource.RLIMIT_CPU, (45, 45)),
            (resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)),
        ]
        assert (content, filename, units) == (b"synthetic source", "sample.dxf", "in")
        return {"status": "needs_review"}

    monkeypatch.setattr(ingestion, "analyze_file", analyze)
    worker.main()
    assert json.loads(target.read_text()) == {"status": "needs_review"}


def test_child_nest_validation_error_is_structured(monkeypatch, tmp_path):
    resource = pytest.importorskip("resource")
    monkeypatch.setattr(resource, "setrlimit", lambda *_args: None)
    source, target = tmp_path / "source", tmp_path / "result.json"
    source.write_text('{"parts": "invalid", "stocks": []}')
    monkeypatch.setattr(
        worker.sys,
        "argv",
        ["worker", str(source), str(target), "--name", "__nest_input__.json"],
    )
    worker.main()
    result = json.loads(target.read_text())
    assert result["status"] == "error"
    assert "bounded lists" in result["message"]
