#!/usr/bin/env python3
"""Verify the normative profile; explicitly generate or check its frontend copy."""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Write the generated frontend adapter")
    parser.add_argument("--check", action="store_true", help="Check without modifying files (default)")
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("Choose --write or --check")
    root = Path(__file__).resolve().parents[2]
    source = root / "backend/app/data/nesting_profiles/werco-compensated-v1.json"
    target = root / "frontend/src/features/nesting/lib/geometry-profile.generated.json"
    wrapper = json.loads(source.read_text(encoding="ascii"))
    if set(wrapper) != {"identity", "profile"} or set(wrapper["identity"]) != {"id", "sha256"}:
        raise SystemExit("Invalid normative geometry profile wrapper")
    canonical = json.dumps(
        {"id": wrapper["identity"]["id"], "profile": wrapper["profile"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    if digest != wrapper["identity"]["sha256"]:
        raise SystemExit("Normative geometry profile SHA-256 does not match its canonical payload")
    generated = json.dumps(wrapper, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    if args.write:
        target.write_text(generated, encoding="ascii")
    elif not target.exists() or target.read_text(encoding="ascii") != generated:
        raise SystemExit("Generated geometry profile is stale; run this script with --write and review the diff")
    print(f"Verified geometry profile {wrapper['identity']['id']}: {digest}")


if __name__ == "__main__":
    main()
