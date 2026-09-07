import importlib.util
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("promote_vercel", ROOT / ".github/scripts/promote_vercel.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40
OLD = "b" * 40


def args(**changes):
    return Namespace(
        sha=SHA,
        project="prj_test",
        team="team_test",
        api_url="https://api.example.test",
        public_url="https://app.example.test",
        repository="owner/repo",
        timeout=1,
        apply=True,
        **changes,
    )


def deployment(**changes):
    return {
        "id": "dpl_test",
        "projectId": "prj_test",
        "target": "production",
        "readyState": "READY",
        "meta": {"githubCommitSha": SHA, "githubCommitRef": "main"},
        **changes,
    }


def test_frontend_only_compatibility_requires_ancestor_and_identical_backend(monkeypatch):
    git = Mock(side_effect=[True, True])
    monkeypatch.setattr(release, "git_success", git)
    assert release.compatible_backend(OLD, SHA)
    assert git.call_args_list[0].args == ("merge-base", "--is-ancestor", OLD, SHA)
    assert git.call_args_list[1].args == ("diff", "--quiet", "--no-renames", OLD, SHA, "--", "backend/")
    git.side_effect = [True, False]
    assert not release.compatible_backend(OLD, SHA)
    git.side_effect = [False]
    assert not release.compatible_backend(OLD, SHA)
    assert not release.compatible_backend("unknown", SHA)
    assert release.compatible_backend(SHA, SHA)


@pytest.mark.parametrize(
    "change",
    [
        {"projectId": "prj_other"},
        {"target": "preview"},
        {"readyState": "BUILDING"},
        {"meta": {"githubCommitSha": OLD, "githubCommitRef": "main"}},
        {"meta": {"githubCommitSha": SHA, "githubCommitRef": "feature"}},
    ],
)
def test_wrong_project_preview_branch_or_sha_cannot_be_promoted(change):
    assert not release.eligible_deployment(deployment(**change), "prj_test", SHA)


def harness(monkeypatch, *, api_sha=SHA, auto=False, main_sha=SHA, ready="READY"):
    monkeypatch.setenv("VERCEL_TOKEN", "private-provider-token")
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    monkeypatch.setattr(release, "git_success", lambda *args: False)
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    requests = []

    def request(url, token=None, method="GET", raw=False):
        requests.append((url, method))
        if "/git/ref/" in url:
            return {"object": {"sha": main_sha}}
        if "/health/detailed" in url:
            return {"status": "healthy", "checks": {"application": {"release": api_sha}}}
        if "/v9/projects/" in url:
            return {"id": "prj_test", "autoAssignCustomDomains": auto, "link": {"productionBranch": "main"}}
        if "/v7/deployments" in url:
            return {"deployments": [{"uid": "dpl_test", "meta": {"githubCommitSha": SHA}}]}
        if "/v13/deployments/" in url:
            return deployment(readyState=ready)
        if "/promote/" in url:
            return {}
        if "/release.txt" in url:
            return SHA
        raise AssertionError(url)

    monkeypatch.setattr(release, "request", request)
    return requests


def test_healthy_matching_api_is_checked_before_and_after_promotion(monkeypatch):
    requests = harness(monkeypatch)
    release.promote(args())
    writes = [index for index, (_, method) in enumerate(requests) if method == "POST"]
    api_checks = [index for index, (url, _) in enumerate(requests) if "/health/detailed" in url]
    assert len(writes) == 1
    assert api_checks[1] < writes[0] < api_checks[2]


@pytest.mark.parametrize("options", [{"api_sha": OLD}, {"auto": True}, {"ready": "ERROR"}])
def test_unsafe_or_failed_release_keeps_current_public_website(monkeypatch, options):
    requests = harness(monkeypatch, **options)
    with pytest.raises(RuntimeError):
        release.promote(args())
    assert not any(method == "POST" for _, method in requests)


def test_superseded_commit_is_skipped_without_promotion(monkeypatch):
    requests = harness(monkeypatch, main_sha=OLD)
    release.promote(args())
    assert not any(method == "POST" for _, method in requests)


def test_check_mode_has_no_external_mutations(monkeypatch):
    requests = harness(monkeypatch)
    options = args()
    options.apply = False
    release.promote(options)
    assert not any(method == "POST" for _, method in requests)


def test_already_current_release_does_not_wait_on_a_later_failed_rebuild(monkeypatch):
    requests = harness(monkeypatch, ready="ERROR")
    original = release.request

    def request(url, token=None, method="GET", raw=False):
        response = original(url, token, method, raw)
        if "/v9/projects/" in url:
            response["targets"] = {"production": {"id": "dpl_current"}}
        if "/v13/deployments/dpl_current" in url:
            return deployment(id="dpl_current")
        return response

    monkeypatch.setattr(release, "request", request)
    release.promote(args())
    assert not any(method == "POST" for _, method in requests)
    assert not any("/v7/deployments" in url for url, _ in requests)


def test_both_production_paths_use_same_promotion_gate_and_complete_git_history():
    import re

    for filename, job in [
        ("ci-cd.yml", "  deploy-production:"),
        ("deploy-frontend-production.yml", "  deploy-frontend:"),
    ]:
        workflow = re.split(r"(?m)^  [a-z][a-z-]*:", (ROOT / ".github/workflows" / filename).read_text().split(job)[1])[
            0
        ]
        assert "fetch-depth: 0" in workflow
        assert "python3 .github/scripts/promote_vercel.py" in workflow
        assert '--public-url "$PUBLIC_APP_URL" --apply' in workflow
        assert workflow.index("python3 .github/scripts/verify_release.py") < workflow.index("Promote the public")
        assert workflow.index("id: release_head") < workflow.index("railway up")
        # The API and worker must not roll back before a superseded UI is skipped.
        steps = re.split(r"(?m)^      - (?:name:|uses:)", workflow)[1:]
        guard = next(index for index, step in enumerate(steps) if "id: release_head" in step)
        assert all("steps.release_head.outputs.current == 'true'" in step for step in steps[guard + 1 :])
