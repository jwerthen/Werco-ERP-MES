"""Crash/timeout boundary for native CAD and document parsers; no ERP imports."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def analyze_in_worker(content: bytes, filename: str, units_override: str | None = None) -> dict:
    backend = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="werco-quote-analysis-") as directory:
        source = Path(directory) / "source"
        target = Path(directory) / "result.json"
        source.write_bytes(content)
        interpreter = os.environ.get("WERCO_QUOTE_WORKER_PYTHON", sys.executable)
        command = [interpreter, "-m", "app.fabrication_quote.worker", str(source), str(target), "--name", filename]
        if units_override:
            command += ["--units", units_override]
        # Parsing requires no database credentials, supplier tokens or egress.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV"}
        }
        env["PYTHONPATH"] = str(backend)
        try:
            result = subprocess.run(command, cwd=backend, env=env, capture_output=True, timeout=60, check=False)
            if result.returncode != 0 or not target.exists():
                raise ValueError(
                    "The parser could not finish this file. Review the original and enter the required geometry manually."
                )
            if target.stat().st_size > 8 * 1024 * 1024:
                raise ValueError(
                    "The parser output exceeds the supported evidence budget. Split the source package into smaller files."
                )
            return json.loads(target.read_text())
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                "File analysis exceeded 60 seconds. Review the original or split the source into smaller files."
            ) from exc
        except OSError as exc:
            raise ValueError("The configured quotation worker could not start. Check its installed runtime.") from exc


def nest_in_worker(payload: dict) -> dict:
    return analyze_in_worker(json.dumps(payload, allow_nan=False).encode(), "__nest_input__.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--name", required=True)
    parser.add_argument("--units")
    args = parser.parse_args()
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass
    if args.name == "__nest_input__.json":
        from app.fabrication_quote.nesting import nest_parts

        try:
            analysis = nest_parts(json.loads(Path(args.source).read_bytes()))
        except (ValueError, TypeError, KeyError) as exc:
            analysis = {"status": "error", "message": str(exc)[:300]}
    else:
        from app.fabrication_quote.ingestion import analyze_file

        analysis = analyze_file(Path(args.source).read_bytes(), args.name, args.units)
    Path(args.target).write_text(json.dumps(analysis, allow_nan=False))


if __name__ == "__main__":
    main()
