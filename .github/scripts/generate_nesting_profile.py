#!/usr/bin/env python3
"""Verify both normative profiles; explicitly generate or check their frontend copies."""

import argparse
import hashlib
import json
import re
from pathlib import Path

PROFILES = (
    ("werco-compensated-v1", "geometry-profile.generated.json"),
    ("werco-remnant-domain-v1", "remnant-domain-profile.generated.json"),
)
MAX_PROFILE_BYTES = 16384
MAX_SAFE_INTEGER = 9007199254740991


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate normative profile field")
        value[key] = item
    return value


def checked_profile(source: Path, expected_id: str) -> dict:
    """Match the runtime's ASCII/string-constant contract without importing the app."""
    raw = source.read_bytes()
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Normative geometry profile exceeds its configuration budget")
    wrapper = json.loads(raw.decode("ascii"), object_pairs_hook=unique_object)
    if not isinstance(wrapper, dict) or set(wrapper) != {"identity", "profile"}:
        raise ValueError("Invalid normative geometry profile wrapper")
    identity, profile = wrapper["identity"], wrapper["profile"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {"id", "sha256"}
        or identity["id"] != expected_id
        or not isinstance(identity["sha256"], str)
        or re.fullmatch("[a-f0-9]{64}", identity["sha256"]) is None
        or not isinstance(profile, dict)
    ):
        raise ValueError("Invalid normative geometry profile identity")
    stack = [profile]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if any(not key.isascii() for key in item):
                raise ValueError("Normative profile keys require ASCII")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif type(item) is str:
            if not item.isascii():
                raise ValueError("Normative profile strings require ASCII")
        elif type(item) is not int or abs(item) > MAX_SAFE_INTEGER:
            raise ValueError(
                "Normative profile constants require decimal strings or safe integers"
            )
    canonical = json.dumps(
        {"id": identity["id"], "profile": profile},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if hashlib.sha256(canonical.encode("ascii")).hexdigest() != identity["sha256"]:
        raise ValueError(
            "Normative geometry profile SHA-256 does not match its canonical payload"
        )
    return wrapper


def generate(root: Path, *, write: bool = False) -> list[dict]:
    """Validate the complete linked pair before writing either generated file."""
    wrappers = [
        checked_profile(
            root / "backend/app/data/nesting_profiles" / f"{identity}.json", identity
        )
        for identity, _ in PROFILES
    ]
    if wrappers[1]["profile"].get("compensatedProfile") != wrappers[0]["identity"]:
        raise ValueError(
            "Remnant domain profile does not bind the exact compensated profile"
        )
    for (_, filename), wrapper in zip(PROFILES, wrappers):
        target = root / "frontend/src/features/nesting/lib" / filename
        generated = (
            json.dumps(wrapper, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
        )
        if write:
            target.write_text(generated, encoding="ascii")
        elif not target.exists() or target.read_text(encoding="ascii") != generated:
            raise ValueError(
                f"Generated profile {filename} is stale; run this script with --write and review the diff"
            )
    return wrappers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="Write the generated frontend adapters"
    )
    parser.add_argument(
        "--check", action="store_true", help="Check without modifying files (default)"
    )
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("Choose --write or --check")
    root = Path(__file__).resolve().parents[2]
    try:
        wrappers = generate(root, write=args.write)
    except (ValueError, OSError, RecursionError) as exc:
        raise SystemExit(str(exc)) from exc
    for wrapper in wrappers:
        print(
            f"Verified geometry profile {wrapper['identity']['id']}: {wrapper['identity']['sha256']}"
        )


if __name__ == "__main__":
    main()
