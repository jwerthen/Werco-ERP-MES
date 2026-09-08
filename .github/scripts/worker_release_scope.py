"""One conservative scope for shared-worker releases and frontend deferral."""

import argparse
import re
import subprocess

WORKER_PATHS = (
    "backend/",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/tsconfig.nesting-worker.json",
    "frontend/tools/build-nesting-worker.mjs",
    "frontend/tools/nesting-server-worker.ts",
    "frontend/src/features/nesting/lib/",
    "railway.toml",
    ".railwayignore",
    ".dockerignore",
    ".github/scripts/worker_release_scope.py",
    ".github/scripts/verify_worker_release.py",
    ".github/scripts/smoke_nesting_worker.py",
    ".github/workflows/ci-cd.yml",
)


def worker_changed(before, head):
    if any(not re.fullmatch(r"[0-9a-f]{40}", sha or "") or sha == "0" * 40 for sha in (before, head)):
        return True
    # Unknown commits and failed diffs fail closed into the full pipeline.
    result = subprocess.run(
        ["git", "diff", "--quiet", "--no-renames", before, head, "--", *WORKER_PATHS],
        capture_output=True,
        check=False,
        timeout=20,
    )
    return result.returncode != 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default="")
    parser.add_argument("--head", default="")
    args = parser.parse_args()
    print("true" if worker_changed(args.before, args.head) else "false")
