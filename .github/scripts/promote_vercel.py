#!/usr/bin/env python3
"""Promote only a staged production SPA whose API is already compatible.

Secrets come only from the environment. Responses/errors never print credentials.
Run inside the shared production-deploy concurrency group after the API release gate.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SHA = re.compile(r"^[0-9a-f]{40}$")
VERCEL_API = "https://api.vercel.com"


def request(url, token=None, method="GET", raw=False):
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read(2_000_000)
            return body.decode().strip() if raw else json.loads(body or b"{}")
    except urllib.error.HTTPError as error:
        # Provider response bodies may contain privileged metadata. Status is sufficient.
        raise RuntimeError(f"{urllib.parse.urlparse(url).hostname} returned HTTP {error.code}") from None
    except (OSError, ValueError):
        raise RuntimeError(f"Request to {urllib.parse.urlparse(url).hostname} failed") from None


def git_success(*args):
    return (
        subprocess.run(["git", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode
        == 0
    )


def compatible_backend(live_sha, target_sha):
    if not SHA.fullmatch(live_sha or "") or not SHA.fullmatch(target_sha or ""):
        return False
    if live_sha == target_sha:
        return True
    # Frontend-only release: the live API must be an ancestor with identical backend
    # contents. Comparing only this push's changed paths misses a previous failed deploy.
    return git_success("merge-base", "--is-ancestor", live_sha, target_sha) and git_success(
        "diff", "--quiet", "--no-renames", live_sha, target_sha, "--", "backend/"
    )


def require_compatible_api(api_url, target):
    health = request(api_url.rstrip("/") + "/health/detailed")
    release = health.get("checks", {}).get("application", {}).get("release")
    if health.get("status") != "healthy" or not compatible_backend(release, target):
        raise RuntimeError("The live API is not healthy and compatible with this frontend commit; promotion stopped")
    return release


def eligible_deployment(deployment, project, target):
    return (
        deployment.get("projectId") == project
        and deployment.get("target") == "production"
        and deployment.get("readyState", deployment.get("state")) == "READY"
        and deployment.get("meta", {}).get("githubCommitSha") == target
        and deployment.get("meta", {}).get("githubCommitRef") == "main"
    )


def current_main(repository, token):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Invalid GitHub repository")
    return request(f"https://api.github.com/repos/{repository}/git/ref/heads/main", token)["object"]["sha"]


def promote(args):
    token = os.environ.get("VERCEL_TOKEN")
    github_token = os.environ.get("GITHUB_TOKEN")
    if not token or not github_token or not SHA.fullmatch(args.sha):
        raise RuntimeError("VERCEL_TOKEN, GITHUB_TOKEN and an exact commit SHA are required")
    query = urllib.parse.urlencode({"teamId": args.team})

    def vercel(path, method="GET"):
        return request(VERCEL_API + path + ("&" if "?" in path else "?") + query, token, method)

    project = vercel(f"/v9/projects/{args.project}")
    if project.get("autoAssignCustomDomains") is not False:
        raise RuntimeError("Vercel auto-assignment is enabled; disable it before using coordinated releases")
    if project.get("id") != args.project or project.get("link", {}).get("productionBranch") != "main":
        raise RuntimeError("Unexpected Vercel project or production branch")
    if current_main(args.repository, github_token) != args.sha:
        print("Superseded release: a newer main commit owns the next frontend promotion.")
        return
    api_release = require_compatible_api(args.api_url, args.sha)
    current_id = project.get("targets", {}).get("production", {}).get("id")
    if re.fullmatch(r"dpl_[A-Za-z0-9]+", current_id or ""):
        current = vercel(f"/v13/deployments/{current_id}")
        if eligible_deployment(current, args.project, args.sha):
            if request(args.public_url.rstrip("/") + "/release.txt", raw=True) == args.sha:
                print(f"Public website is already verified at {args.sha}; API {api_release} is compatible.")
                return
    deadline = time.monotonic() + args.timeout
    deployment = None
    while time.monotonic() < deadline:
        params = urllib.parse.urlencode(
            {"projectId": args.project, "target": "production", "limit": 100, "meta-githubCommitSha": args.sha}
        )
        candidates = vercel("/v7/deployments?" + params).get("deployments", [])
        matching = [item for item in candidates if item.get("meta", {}).get("githubCommitSha") == args.sha]
        matching.sort(key=lambda item: item.get("created", item.get("createdAt", 0)), reverse=True)
        if matching:
            candidate = matching[0]
            deployment_id = candidate.get("uid", candidate.get("id"))
            if not re.fullmatch(r"dpl_[A-Za-z0-9]+", deployment_id or ""):
                raise RuntimeError("Invalid staged deployment identifier")
            detail = vercel(f"/v13/deployments/{deployment_id}")
            if eligible_deployment(detail, args.project, args.sha):
                deployment = detail
                break
            if detail.get("readyState") in {"ERROR", "CANCELED"}:
                raise RuntimeError("The staged Vercel production build failed; current website retained")
        time.sleep(10)
    if deployment is None:
        raise RuntimeError("Timed out waiting for this commit's staged production build; current website retained")
    # Re-check immediately before the only external mutation. The workflow lock also
    # serializes both Railway deploy paths so they cannot move the API underneath us.
    require_compatible_api(args.api_url, args.sha)
    if current_main(args.repository, github_token) != args.sha:
        print("Superseded while staging; current website retained.")
        return
    if not args.apply:
        print(f"Validated staged frontend {args.sha} against API {api_release}; no promotion requested.")
        return
    deployment_id = deployment["id"]
    # A duplicate workflow should simply verify the already-current artifact.
    if project.get("targets", {}).get("production", {}).get("id") != deployment_id:
        vercel(f"/v10/projects/{args.project}/promote/{deployment_id}", "POST")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            if request(args.public_url.rstrip("/") + "/release.txt", raw=True) == args.sha:
                require_compatible_api(args.api_url, args.sha)
                print(f"Public website verified at {args.sha}; API {api_release} is compatible.")
                return
        except RuntimeError:
            pass
        time.sleep(10)
    raise RuntimeError("Promotion was submitted, but the public release receipt did not verify; inspect Vercel aliases")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "jwerthen/Werco-ERP-MES"))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        promote(args)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
