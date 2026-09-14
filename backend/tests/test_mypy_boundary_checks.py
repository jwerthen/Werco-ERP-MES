"""Exercise the real mypy boundary policy with intentionally invalid shadow files."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module",
    ["app/schemas/stock_piece.py", "app/db/tenant_filter.py", "app/services/pdf_text.py"],
)
def test_boundary_modules_reject_incorrect_contracts(module, tmp_path):
    # Shadow real module paths so mypy applies the production module-specific
    # configuration. Nothing is written to app/ or imported into the test app.
    probe = tmp_path / "probe.py"
    probe.write_text(
        'def quantity(value: int) -> int:\n'
        '    return "incorrect"\n'
        '\n'
        'quantity("incorrect")\n'
        'quantity(1, 2)\n'
        'object().missing_attribute\n'
        '\n'
        'def optional_text(value: str | None) -> str:\n'
        '    return value.upper()\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file=mypy.ini",
            "--no-incremental",
            "--follow-imports=skip",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--shadow-file",
            module,
            str(probe),
            module,
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    for code in ("arg-type", "return-value", "call-arg", "attr-defined", "union-attr"):
        assert f"[{code}]" in result.stdout, result.stdout + result.stderr
