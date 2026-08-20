#!/usr/bin/env bash
#
# Railway deploy failure diagnostics.
#
# WHY THIS EXISTS. On 2026-08-20 at 16:35 UTC both production deploys for
# 08312d3 failed identically and the workflows had thrown away everything that
# could tell us WHY. `railway up` printed:
#
#     Indexing...
#     Uploading...
#     Deploys have been paused due to an upstream issue      <-- on STDERR
#
# The deploy steps pipe only STDOUT into `tee`, so the teed log -- which is what
# the "Confirm Railway accepted the upload" step dumps under "--- deploy log ---"
# -- held the first two lines and nothing else. Reading that dump, the failure
# was indistinguishable from a revoked token, an exhausted plan quota, or a CLI
# release that changed its output. (Two fixes shipped together: the deploy steps
# now redirect 2>&1 into the tee, and this script runs on failure.)
#
# CONTRACT.
#   * Runs ONLY from an `if: failure()` step. It adds nothing to a green deploy.
#   * NEVER changes the job's outcome: every probe's exit status is captured and
#     reported, never propagated, and the script always exits 0. Callers pair it
#     with `continue-on-error: true` as a second belt.
#   * NEVER prints a secret. It reads $RAILWAY_TOKEN only by handing it to the
#     Railway CLI through the environment, and never echoes it or
#     $RAILWAY_PROJECT_ID. GitHub masks both as *** anyway; the redaction below
#     is an ADDITIONAL layer, applied before the text reaches the log, so it
#     cannot defeat the masking.
#
# ON REDACTION. jwerthen/Werco-ERP-MES is a PUBLIC repository, so Actions logs
# are world-readable. CLI output can carry the account's login email (`railway
# whoami`) and project/service/environment UUIDs (`railway status`). Neither is
# a credential, but neither needs publishing either, so both are rewritten out
# of every probe's output. Error text -- the part with the diagnostic value --
# survives untouched.
#
# USAGE:  bash .github/scripts/railway_diagnostics.sh [deploy-log ...]
#         Any log paths given are dumped (redacted) if they exist. Missing paths
#         are reported, not fatal -- a job whose deploy step never ran has none.
#
# Guarded by backend/tests/test_ci_workflow_gates.py.

set -u

REDACT_EMAIL='s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<redacted-email>/g'
REDACT_UUID='s/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<redacted-uuid>/g'

capture="${RUNNER_TEMP:-/tmp}/railway-diagnostic-probe.out"

redact() {
  sed -E -e "$REDACT_EMAIL" -e "$REDACT_UUID" "$1"
}

# Run one probe, report its exit status and its (redacted) combined output.
# The command is EXPECTED to fail in several of these cases -- a project-scoped
# token cannot `whoami`, and an unlinked checkout cannot `status` -- so the exit
# status is data, not an error.
probe() {
  label="$1"
  shift
  echo "::group::probe: ${label}"
  status=0
  if "$@" >"$capture" 2>&1; then
    status=0
  else
    status=$?
  fi
  echo "exit status: ${status}"
  if [ -s "$capture" ]; then
    redact "$capture"
  else
    echo "(no output)"
  fi
  echo "::endgroup::"
}

echo "=============================================================="
echo " Railway deploy diagnostics -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================================================="
echo
echo "This step runs only because a deploy step failed. It changes nothing and"
echo "cannot make the job pass. See the interpretation guide at the bottom."
echo

# ---------------------------------------------------------------- toolchain --
# Which CLI actually ran. `npm install -g @railway/cli` prints only "added 16
# packages", so before the pin landed NOTHING in the log recorded the version --
# which is why "did a CLI release change the output?" could only be answered by
# cross-referencing npm publish dates after the fact.
probe "railway --version" railway --version
probe "npm ls -g @railway/cli" npm ls -g --depth=0 @railway/cli
probe "node --version" node --version

# --------------------------------------------------------------------- auth --
# Distinguishes "the token is dead" from "the token is fine, Railway said no".
# A project-scoped token (what these workflows use) FAILS whoami by design, so
# a non-zero exit here is not by itself evidence of anything -- the error TEXT
# is: "Unauthorized" / "Invalid token" reads very differently from the
# not-supported-for-project-tokens response.
probe "railway whoami" railway whoami

# ------------------------------------------------------------------ project --
# `railway status` takes no --project flag, but a project-scoped RAILWAY_TOKEN
# implies its project, so this can still resolve. Its output names the project,
# environment and services the token can see -- i.e. whether the token still has
# access at all, and whether the services this workflow deploys still exist.
probe "railway status" railway status
probe "railway list" railway list

# ----------------------------------------------------------- reachability ----
# Separates "Railway is having an incident" from "this account/token is the
# problem". A healthy backboard answers a bare GET with HTTP 400 and a JSON
# error body; a timeout or 5xx is an outage signal. Unauthenticated: no token is
# sent, so this probe cannot leak one.
probe "railway API reachability" \
  curl -sS --max-time 20 -o /dev/null \
  -w 'http_status=%{http_code} dns=%{time_namelookup}s connect=%{time_connect}s total=%{time_total}s\n' \
  https://backboard.railway.com/graphql/v2

# ------------------------------------------------------------- deploy logs ----
# The teed `railway up` output, which now includes stderr. On 2026-08-20 this is
# the line that was missing and that named the cause outright.
if [ "$#" -gt 0 ]; then
  for log in "$@"; do
    echo "::group::deploy log: ${log}"
    if [ -f "$log" ]; then
      if [ -s "$log" ]; then
        redact "$log"
      else
        echo "(file exists but is empty -- the CLI produced no output)"
      fi
    else
      echo "(no such file -- this deploy step did not run, or was skipped)"
    fi
    echo "::endgroup::"
  done
fi

# ------------------------------------------------------------ how to read ----
cat <<'GUIDE'

--------------------------------------------------------------
 Interpreting the above
--------------------------------------------------------------
 "Deploys have been paused due to an upstream issue"
     Railway-side incident. Not the token, not the quota, not the
     CLI. Nothing in this repo can fix it. Check
     https://status.railway.com and re-run the deploy after.

 "Unauthorized" / "Invalid token" / "Not Authorized" on whoami
 AND on status
     The token is dead -- rotated, revoked, or the project was
     deleted. Mint a new project token in the Railway dashboard
     and update the RAILWAY_TOKEN repo secret.

 whoami fails but status resolves the project
     Normal. Project-scoped tokens do not support whoami.

 status/list report the project but the service is absent
     The service was renamed or deleted. Reconcile the
     --service names in the workflow with the dashboard.

 Anything about plan limits, trial expiry, payment, resource
 or usage limits
     Billing/quota. Railway blocks new deployments; the account
     owner has to resolve it in the dashboard.

 Reachability probe times out or returns 5xx
     Railway (or the runner's egress) is down. Retry later.

 railway --version differs from the version pinned in the
 workflow
     The pin was bypassed or the install step failed. Confirm the
     "Build Logs:" success contract still matches the CLI output
     before bumping the pin.
--------------------------------------------------------------
GUIDE

# Always succeed. A diagnostic that can fail is a diagnostic that can change a
# verdict, and this one is only ever reached when the verdict is already "fail".
exit 0
