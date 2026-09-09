"""Exercise the actual two-profile generation and isolated frontend build guards."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "generate_nesting_profile", ROOT / ".github/scripts/generate_nesting_profile.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def encoded(wrapper):
    return (
        json.dumps(wrapper, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("ascii")


def rehash(wrapper):
    canonical = json.dumps(
        {"id": wrapper["identity"]["id"], "profile": wrapper["profile"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    wrapper["identity"]["sha256"] = hashlib.sha256(canonical).hexdigest()


@pytest.fixture
def tree(tmp_path):
    for identity, filename in GENERATOR.PROFILES:
        for relative in (
            f"backend/app/data/nesting_profiles/{identity}.json",
            f"frontend/src/features/nesting/lib/{filename}",
        ):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
    target = tmp_path / "frontend/tools/verify-nesting-profile.mjs"
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "frontend/tools/verify-nesting-profile.mjs", target)
    return tmp_path


def frontend_check(root):
    node = shutil.which("node")
    if node is None:
        pytest.skip("The frontend build verifier requires Node")
    return subprocess.run(
        [node, "tools/verify-nesting-profile.mjs"],
        cwd=root / "frontend",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def generated_files(root):
    return [
        root / "frontend/src/features/nesting/lib" / name
        for _, name in GENERATOR.PROFILES
    ]


def test_both_generated_profiles_check_without_changes_and_frontend_build_needs_no_backend(
    tree,
):
    before = [path.read_bytes() for path in generated_files(tree)]
    wrappers = GENERATOR.generate(tree)
    GENERATOR.generate(tree, write=True)
    assert before == [path.read_bytes() for path in generated_files(tree)]
    assert (
        wrappers[0]["identity"]["sha256"]
        == "21e8689fb2ce80c72befbc5866f658cd74fe8ed336d1b5c070e182f3081aa55a"
    )
    assert wrappers[1]["profile"]["compensatedProfile"] == wrappers[0]["identity"]
    shutil.rmtree(tree / "backend")
    result = frontend_check(tree)
    assert result.returncode == 0, result.stderr
    assert all(identity in result.stdout for identity, _ in GENERATOR.PROFILES)


@pytest.mark.parametrize("profile", [0, 1])
@pytest.mark.parametrize("change", ["missing", "checksum", "duplicate"])
def test_either_generated_copy_must_be_present_and_exact(tree, profile, change):
    target = generated_files(tree)[profile]
    if change == "missing":
        target.unlink()
    elif change == "checksum":
        wrapper = json.loads(target.read_bytes())
        wrapper["profile"]["numerics"]["integerGridMm"] = "0.01"
        target.write_bytes(encoded(wrapper))
    else:
        target.write_bytes(
            target.read_bytes().replace(
                b'"identity":', b'"identity": {}, "identity":', 1
            )
        )
    with pytest.raises(ValueError, match="stale"):
        GENERATOR.generate(tree)
    result = frontend_check(tree)
    assert result.returncode != 0
    assert "Verified generated" not in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        "wrong-base",
        "extra",
        "id",
        "float",
        "bool",
        "null",
        "unsafe",
        "unicode",
        "duplicate",
        "large",
    ],
)
def test_invalid_normative_profile_is_refused_before_either_generated_file_is_written(
    tree, change
):
    source = tree / "backend/app/data/nesting_profiles/werco-remnant-domain-v1.json"
    wrapper = json.loads(source.read_bytes())
    if change == "wrong-base":
        wrapper["profile"]["compensatedProfile"]["sha256"] = "a" * 64
    elif change == "extra":
        wrapper["approved"] = True
    elif change == "id":
        wrapper["identity"]["id"] = "other-domain"
    elif change in ("float", "bool", "null", "unsafe", "unicode"):
        wrapper["profile"]["budgets"]["maxDomainVertices"] = {
            "float": 1.5,
            "bool": True,
            "null": None,
            "unsafe": 9007199254740992,
            "unicode": "\u03b1",
        }[change]
    rehash(wrapper)
    raw = encoded(wrapper)
    if change == "duplicate":
        raw = raw.replace(b'"identity":', b'"identity": {}, "identity":', 1)
    elif change == "large":
        raw += b" " * 16384
    source.write_bytes(raw)
    before = [path.read_bytes() for path in generated_files(tree)]
    with pytest.raises(ValueError):
        GENERATOR.generate(tree, write=True)
    assert before == [path.read_bytes() for path in generated_files(tree)]
    # An isolated deployment must reject the same corruption, even if its own
    # self-reported hash was recomputed and no normative source is available.
    generated_files(tree)[1].write_bytes(raw)
    assert frontend_check(tree).returncode != 0


def test_explicit_generation_repairs_stale_copy_from_verified_source_only(tree):
    target = generated_files(tree)[1]
    expected = target.read_bytes()
    target.write_text("{}\n")
    with pytest.raises(ValueError, match="stale"):
        GENERATOR.generate(tree)
    GENERATOR.generate(tree, write=True)
    assert target.read_bytes() == expected
    assert frontend_check(tree).returncode == 0
