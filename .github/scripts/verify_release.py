#!/usr/bin/env python3
"""Poll a deployed service until it reports the commit SHA this workflow deployed.

WHY THIS FILE EXISTS. On 2026-08-04 two consecutive production deploys (PRs #198 and
#203) were reported RED by CI while the code shipped perfectly well. `railway up`
uploaded the source, Railway created and ran the build, and the new image went live --
then the CLI failed to tail the build log ("Failed to stream build logs: Failed to
retrieve build log") and exited 1 about 66 seconds after the upload. GitHub marked the
step failed, which SKIPPED every step after it: the frontend deploy, the worker deploy,
`verify_launch`, and the GitHub Release. Production was two commits ahead of what the
release history claimed, and nobody could tell from the Actions tab.

The lesson is that `railway up`'s exit status answers "could the CLI read the log
stream?", not "is the new code serving traffic?". Only the running service can answer
the second question, so that is what this script asks -- it polls until the deployed
artifact reports back the exact SHA the workflow uploaded.

    backend   GET /health/detailed -> checks.application.release   (from backend/RELEASE)
    frontend  GET /release.txt     -> the raw body                 (from frontend/public/)

BODY, NOT STATUS CODE. The frontend is an SPA behind nginx `try_files $uri $uri/
/index.html`, so a MISSING /release.txt returns **200 with index.html**, not 404
(verified against production 2026-08-04). Anything that checks only the status code
passes when the marker was never deployed. This compares the body to the expected SHA
and accepts nothing else.

No third-party imports on purpose: this runs on a bare runner before any pip install.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# A body this long is a page, not a SHA -- almost always nginx's SPA fallback
# index.html. Truncate it in diagnostics rather than dumping a whole document.
_MAX_ECHO = 200


def _extract(payload: str, json_path: str | None) -> str:
    """Pull the release marker out of a response body.

    ``json_path`` is a dotted path into a JSON document (``checks.application.release``).
    When it is None the body itself is the marker and is returned stripped.
    """
    if not json_path:
        return payload.strip()

    node = json.loads(payload)
    for key in json_path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"{json_path!r} is not present in the response")
        node = node[key]
    return str(node).strip()


def _probe(url: str, json_path: str | None, timeout: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    return _extract(body, json_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Service name, for log lines.")
    parser.add_argument("--url", required=True, help="URL that reports the deployed release.")
    parser.add_argument("--expect", required=True, help="The commit SHA this run deployed.")
    parser.add_argument(
        "--json-path",
        default=None,
        help="Dotted path into a JSON body. Omit when the body IS the marker.",
    )
    parser.add_argument("--timeout", type=int, default=900, help="Total seconds to wait (default 900).")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between polls (default 10).")
    args = parser.parse_args()

    expected = args.expect.strip()
    if not expected:
        print("--expect was empty; refusing to 'verify' against nothing.", file=sys.stderr)
        return 1

    url = args.url.strip()
    if not url:
        print("--url was empty; nothing to poll.", file=sys.stderr)
        return 1

    # FAIL IN THE FIRST SECOND, not after the full timeout. The workflow passes a
    # pre-concatenated "${PRODUCTION_URL}/release.txt", so an unset or renamed repo
    # variable yields the NON-empty string "/release.txt". urlopen would raise
    # ValueError("unknown url type"), which the retry arm below would happily swallow
    # every 10s until the deadline -- and then blame a perfectly healthy Railway build.
    # A missing config value is not a condition that improves by waiting.
    if not url.startswith(("http://", "https://")):
        print(
            f"--url must be absolute http(s), got {url!r}.\n"
            "This is a workflow configuration error, not a deploy failure: check that the "
            "repo variable feeding it (PRODUCTION_API_URL / PRODUCTION_URL) is set.",
            file=sys.stderr,
        )
        return 1

    deadline = time.monotonic() + args.timeout
    attempt = 0
    last = "<no successful response>"
    # Which failure we are looking at decides what the operator should go read, so track
    # whether the service EVER answered rather than guessing in the summary.
    ever_responded = False

    print(f"Waiting for {args.label} to report release {expected} at {url}", flush=True)

    while time.monotonic() < deadline:
        attempt += 1
        try:
            seen = _probe(url, args.json_path, timeout=20)
            ever_responded = True
            if seen == expected:
                print(f"Attempt {attempt}: {args.label} is serving {expected}.", flush=True)
                return 0
            last = seen[:_MAX_ECHO] if seen else "<empty body>"
            # A rolling deploy legitimately serves the OLD release for a while, so a
            # mismatch is normal early and only meaningful once the deadline passes.
            print(f"Attempt {attempt}: {args.label} still reports {last!r}", flush=True)
        except (urllib.error.URLError, OSError) as exc:
            # Expected while the container restarts behind the load balancer.
            last = f"<unreachable: {exc}>"
            print(f"Attempt {attempt}: {args.label} {last}", flush=True)
        except (ValueError, KeyError) as exc:
            # Malformed/unexpected body: the SPA fallback page, or a health schema change.
            ever_responded = True
            last = f"<unparseable: {exc}>"
            print(f"Attempt {attempt}: {args.label} {last}", flush=True)

        time.sleep(args.interval)

    if ever_responded:
        diagnosis = (
            "The service answered but never reported this commit. The upload reached Railway "
            "(the step before this one proved a build was created), so the build or the "
            "container start most likely failed -- open the Build Logs URL printed by the "
            "deploy step above. If another deploy of the same service overlapped this one, "
            "the other run's commit may be live instead; check the production-deploy "
            "concurrency group."
        )
    else:
        diagnosis = (
            f"The service never answered at all, so nothing here shows the deploy failed. "
            f"Check that {url} is reachable and that the endpoint still exists before "
            "reading anything into the Railway build."
        )

    # stdout is block-buffered into the Actions log pipe; flush it so the attempt history
    # above cannot land AFTER this summary and read backwards.
    sys.stdout.flush()
    print(
        f"\n{args.label} never reported release {expected} within {args.timeout}s.\n"
        f"Last seen: {last}\n" + diagnosis,
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
