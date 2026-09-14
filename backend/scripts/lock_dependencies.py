"""Compile or check the runtime and development dependency locks.

Install the compiler with ``python -m pip install uv==0.11.15``. Then run from
backend (or any directory):

    python scripts/lock_dependencies.py
    python scripts/lock_dependencies.py --check
    python scripts/lock_dependencies.py --upgrade-package <transitive-package>

Edit requirements.txt / requirements-dev.txt for direct dependency changes.
Existing locked versions are preferred, so ordinary regeneration does not upgrade
unrelated packages. Both locks resolve across platforms for Python >=3.11;
production and CI remain on the repository's supported Python 3.11 runtime.
Development resolution is constrained by the runtime lock. Install a lock with
``python -m pip install --require-hashes -r requirements[-dev].lock``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UV_VERSION = "0.11.15"
BACKEND_DIR = Path(__file__).resolve().parents[1]
INPUTS = ("requirements.txt", "requirements-dev.txt")
LOCKS = ("requirements.lock", "requirements-dev.lock")
COMPILE_COMMAND = "python scripts/lock_dependencies.py"


def compile_locks(directory: Path, uv: str, upgrade_packages: list[str]) -> None:
    """Generate both outputs before publishing either, sharing runtime versions."""
    for input_name, lock_name in zip(INPUTS, LOCKS):
        command = [
            uv,
            "pip",
            "compile",
            input_name,
            "--universal",
            "--python-version",
            "3.11",
            "--generate-hashes",
            "--no-annotate",
            "--custom-compile-command",
            COMPILE_COMMAND,
            "--output-file",
            lock_name,
            "--quiet",
        ]
        if lock_name == "requirements-dev.lock":
            command.extend(["--constraint", "requirements.lock"])
        for package in upgrade_packages:
            command.extend(["--upgrade-package", package])
        subprocess.run(command, cwd=directory, capture_output=True, text=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail on stale locks without changing files")
    parser.add_argument(
        "--upgrade-package", action="append", default=[], metavar="PACKAGE", help="allow only this package to upgrade"
    )
    args = parser.parse_args()
    if args.check and args.upgrade_package:
        parser.error("--check cannot be combined with --upgrade-package")

    # Prefer the tool installed beside this interpreter over an older global uv.
    uv = shutil.which("uv", path=str(Path(sys.executable).parent)) or shutil.which("uv")
    if uv is None:
        parser.error(f"install the compiler with: python -m pip install uv=={UV_VERSION}")
    version = subprocess.run([uv, "--version"], capture_output=True, text=True, check=True).stdout.split()
    if version[:2] != ["uv", UV_VERSION]:
        parser.error(f"this lock workflow requires uv=={UV_VERSION}; found {' '.join(version)}")

    with tempfile.TemporaryDirectory(prefix="werco-dependency-locks-") as temp_dir:
        scratch = Path(temp_dir)
        for name in INPUTS + LOCKS:
            source = BACKEND_DIR / name
            if source.exists():
                shutil.copyfile(source, scratch / name)
        try:
            compile_locks(scratch, uv, args.upgrade_package)
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, file=sys.stderr, end="")
            print(
                f"Dependency resolution failed; committed locks were not changed (exit {exc.returncode}).",
                file=sys.stderr,
            )
            return 1

        changed = [
            name
            for name in LOCKS
            if not (BACKEND_DIR / name).exists() or (scratch / name).read_bytes() != (BACKEND_DIR / name).read_bytes()
        ]
        if args.check:
            if changed:
                print(f"Stale dependency locks: {', '.join(changed)}. Run {COMPILE_COMMAND}.", file=sys.stderr)
                return 1
            print("Runtime and development dependency locks are current.")
            return 0

        for name in changed:
            shutil.copyfile(scratch / name, BACKEND_DIR / name)
        print(f"Updated: {', '.join(changed)}" if changed else "Dependency locks are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
