# The ARQ background worker — reference

> **Executing the cutover? Use [`WORKER_DEPLOYMENT_RUNBOOK.md`](WORKER_DEPLOYMENT_RUNBOOK.md),
> not this file.** That is the step-by-step procedure: pre-flight checks with explicit stop
> conditions, the quantified per-job blast radius, the staged cron rollout, rollback, and
> which steps are irreversible. **This** file is the reference behind it — why the worker
> exists, what the code does, what changed. If the two disagree, the runbook is the one being
> executed and this one is the one to correct.

**Status: PREPARED, NOT DEPLOYED.** Everything in the repo is ready. No Railway service has
been created, no variable has been set, nothing has been deployed, and both CI deploy steps
are gated off.

Read §1 before §5. The most important finding is not "there is no worker" — it is that the
enqueue side was pointed at the wrong Redis, so **creating a worker without the code fix
would have produced a worker that connects to a different, empty Redis than the API and
sits there consuming nothing.** That failure is silent.

---

## 1. What was actually wrong

The original report said "there is no worker, so every cron has never fired, and anything
enqueued from a request is sitting in Redis unconsumed." The first half is consistent with
the repo. **The second half is false**, and the difference changes the plan.

`backend/app/core/queue.py` resolved ARQ's Redis from `REDIS_HOST` / `REDIS_PORT` /
`REDIS_DB` and **never read `REDIS_URL`** — while every other Redis consumer (the response
cache, the slowapi limiter storage, the login throttle, the `/health/ready` probe) reads
`REDIS_URL`, and both `docs/ENVIRONMENT_VARIABLES.md` and `backend/.env.example` told
operators `REDIS_URL` "takes precedence over individual settings". It also had **no
password field at all**, so it could not authenticate to a managed Redis even with the host
set correctly.

So on a deployment provisioned from those docs — set `REDIS_URL`, which is the single
string Railway hands you, and nothing else — the queue resolved to `localhost:6379`. Every
enqueue failed with `ConnectionRefused`. **Nothing was queued.**
And `/health/ready` reported `redis: healthy` throughout, because it pings `REDIS_URL`.

> **"There is no Redis backlog" was true only until the fix deployed.** That sentence
> described the state *while the enqueue side was broken*. The fix shipped to production on
> 2026-08-04 (PR #201, `50d8d1c`), and from that moment the API enqueued successfully into
> `arq:queue` with no consumer, so a backlog did begin to accumulate — self-bounding at 24 h,
> because arq gives job payloads a 24-hour TTL (`expires_extra_ms = 86_400_000`) and pops
> anything older as "job expired". Measured directly before the worker's first boot on
> 2026-08-05: **4 jobs**, all enqueued within the preceding hour. Not three weeks of work.

What that cost while it was broken:

| Path | Symptom |
|---|---|
| `POST /scheduling/run-background` | **500 after ~11 s.** The only user-visible hard break — `enqueue_job` is awaited with no try/except. |
| PO receiving, WO completion, visitor check-in | Silent ~11 s latency tax per action, then swallowed. |
| Notification outbox (`after_commit`) | Silent ~1 s tax (fast-fail path), then swallowed. |
| Everything else | No background work of any kind. |

> The **~11 s** figures were documented as "~5 s" until 2026-08-05. `arq/connections.py:271-299`
> retries `conn_retries` times *after* the first attempt, so the default budget is
> 6 attempts × 1 s + 5 sleeps × 1 s = 11 s, not 5 × 1 s. Any earlier decision citing ~5 s was
> made against a number 2.2× low.

### Fixed in the code

`REDIS_URL` is now the source of truth for **both** sides of the queue, with the
host/port/db trio as fallback (so docker-compose, which sets only the trio, is unchanged)
and `REDIS_PASSWORD` filling in a credential neither source supplied. Both the enqueue side
and `WorkerSettings` call the same `get_redis_settings()`, and
`backend/tests/test_worker_redis_parity.py` fails the build if they can ever diverge again.

A worker whose Redis resolves to the localhost default in `production`/`staging` now
**refuses to start** (`assert_redis_configured`, raised at import of `app.worker`, before
arq builds anything). "Started successfully and consumed nothing" is no longer reachable.

### Redis transport profiles — same target, different patience

Added 2026-08-05 after the worker's **first production boot crashed 22 seconds in**:

```
redis.exceptions.TimeoutError: Timeout connecting to server
  redis/asyncio/connection.py:296   connect()
  redis/asyncio/client.py:1567      pipeline execute -> NEW pooled connection
  arq/worker.py:488                 run_job's FIRST pipeline
  arq/worker.py:404                 t.result()   <- bare, no filter -> process exit
```

That pipeline is arq's own job bookkeeping and sits **outside** `run_job`'s broad `except`
(which does not begin until `arq/worker.py:518`), and `_poll_iteration` re-raises
unconditionally — so **any** Redis blip during bookkeeping kills the process by arq's
design. The worker cannot rely on arq tolerating a blip; it has to not see one.

`get_redis_settings(profile=...)` now returns three transport profiles. **All three resolve
the same target** — the parity property is unchanged and is now stated over `TARGET_FIELDS`
(which Redis) rather than whole-object equality, with `TRANSPORT_FIELDS` (how long we wait)
allowed to differ and pinned to a declared list. The two lists together must cover every
field arq has, which is itself a test.

| Profile | Used by | `conn_timeout` | `conn_retries` | `retry_on_timeout` |
|---|---|---|---|---|
| `REQUEST` (default) | API enqueues | 1 s | 5 | `False` |
| `COMMIT_PATH` (= `fast_fail=True`) | notification outbox, in a request's commit path | 1 s | 0 | `False` |
| `WORKER` | `WorkerSettings` only | 5 s | 1 | **`True`** |

**`retry_on_timeout=True` is load-bearing and not optional.** Passing `retry=` alone is a
measured no-op: redis-py raises its own `redis.exceptions.TimeoutError` at
`connection.py:296`, **outside** `call_with_retry`, and that class is *not* a subclass of the
builtin `TimeoutError` the retry policy knows about. Measured against a blackholed IP on a
connection built exactly as arq builds it:

| Configuration | Attempts | Elapsed |
|---|---|---|
| arq defaults (`conn_timeout=1`) | 1 | **1.00 s → dead** |
| `retry=Retry(ExponentialBackoff, 3)` alone | 1 | 5.00 s — **no-op, never fires** |
| `retry_on_timeout=True` alone | 2 | 10.00 s |
| both (the `WORKER` profile) | 4 | **27.01 s** |

This converts "dies at 1 s" into "dies at 27 s". It is deliberately **not** a guarantee — an
outage longer than ~27 s still kills the process, and `restartPolicyMaxRetries` stays at
**3** so a genuinely broken Redis still surfaces as a loud Railway *Crashed* deployment
rather than being absorbed silently.

**What this does not fix:** `arq.connections.create_pool` sets only `socket_connect_timeout`;
`socket_timeout` stays `None` on every connection, on the worker and on every request-path
enqueue. A Redis that accepts TCP and then goes silent blocks **forever** at any
`conn_timeout`. `RedisSettings` exposes no `socket_timeout` field, and reaching it means
bypassing `create_pool` via `Worker(redis_pool=...)`, which sets `self.redis_settings = None`
and destroys the parity guard outright. Named here, deliberately not fixed.

---

## 2. The blast radius — read before arming any cron

**Every cron in `WorkerSettings.cron_jobs` fires the moment a worker process runs.** There
is no separate enable step. Twelve are declared. Times are the container's local zone —
arq defaults to `datetime.now().astimezone().tzinfo`, which on Railway is **UTC unless `TZ`
is set**, so "6 AM" means 06:00 UTC = **01:00 Central**.

| Cron | When | What it does on its FIRST run |
|---|---|---|
| `run_mrp_auto_draft_job` | 06:00 daily | **Creates draft purchase orders and work orders** for every active company, and emails `mrp.completed` to every active `MANAGER` (only when the run produced actions). Not idempotent — a new `MRPRun` and new drafts every run. **Highest risk.** |
| `check_late_work_orders_job` | 08:00 daily | **One email per late WO to every supervisor + manager.** No age cap. `wo.late` is `recurring`, but suppression keys off an unread in-app row already existing — on a cold start there are none, so the first run fires in full. |
| `check_calibrations_job` | 07:00 daily | One `calibration.due` per equipment due within 7 days, to every user whose `department = 'Quality'`. Equipment due within **1 day is dispatched twice** (it is in both the 7-day and the 1-day set). Channel is in-app + **digest**, not direct email. |
| `check_quote_expiring_job` | 09:00 daily | One per quote expiring within 7 days, to `department = 'Sales'` users. **Digest-only** — no direct email. Window-bounded. |
| `check_low_stock_job` | 07:30 daily | **One aggregated** message per tenant to `department` `Purchasing` + `Inventory`. **Digest-only.** Low blast radius. |
| `send_daily_digest_job` | 08:00 daily | One digest email per opted-in user. |
| `aggregate_ai_learning_job` | 05:30 daily | Writes `AIRecommendation` rows and emits `work_order_blocker_escalated` events, which re-enter the outbox and generate more notifications. |
| `run_oee_auto_calc_job` | 02:30 daily | Writes `OEERecord` (`calculation_source='auto'`) for **yesterday only**; never overwrites `manual`. No backfill. Quiet. |
| `cleanup_old_logs_job` | Sun 02:00 | **Physical DELETEs**: completed jobs, notification logs, read notifications >90 d. Audit logs explicitly excluded. |
| `archive_aged_audit_logs_job` | 1st of month 03:00 | Exports aged audit rows to NDJSON in `AUDIT_ARCHIVE_DIR`. **Never deletes.** Needs a durable volume — see §8. |
| `poll_tracking_job` | every 30 min | Outbound carrier traffic, gated on `allow_carrier_egress` (default off). |
| `relay_pending_notifications_job` | every 5 min | Re-enqueues events with `notified_at IS NULL`. **Safely bounded** — see below. |

### The notification sweeper is NOT the storm risk

`backend/app/jobs/notification_jobs.py` caps it three independent ways:
`_RELAY_MAX_AGE_HOURS = 24` (anything older is **never** dispatched), `_RELAY_GRACE_MINUTES
= 2`, and `_RELAY_BATCH_LIMIT = 500` per 5-minute pass. Migration `072` also backfilled
`notified_at = created_at` on all pre-existing rows. So the first pass picks at most 500
events drawn only from the trailing 24 hours. **It cannot emit months of history.**

The **daily crons** are the storm risk, because they enumerate *current* state with no age
cap. Run the counts in §4 before arming them.

### The cron selector

`WORKER_CRON_JOBS` narrows the schedule. It has **two shapes**: name the crons you want (an
allowlist), or start from everything and subtract the ones you don't (a denylist, `-` prefix).

- unset or `all` → every cron (**the default — this changes nothing about what was declared**)
- `none` → no crons at all. The worker still drains enqueue-driven jobs (notifications,
  webhooks, labels, completion signals), which correspond to something a user actually did.
  **This is the correct value for the first boot.**
- comma-separated job names → arms exactly those, one at a time, in the order listed.
- `-<name>` → **excludes** that cron from the full set. `all,-run_mrp_auto_draft_job` and the
  bare `-run_mrp_auto_draft_job` are the same thing: a spec made only of exclusions implies
  `all` as its base. `-cron:run_mrp_auto_draft_job` works too — arq names crons
  `cron:<coroutine name>` and the startup log prints **that** form, so both spellings are
  accepted for exclusions exactly as they already are for inclusions.

**Why the exclusion form exists.** Switching one cron off with an allowlist means listing the
other eleven, which **freezes the set**: a cron added to `ALL_CRON_JOBS` in a later release
silently never registers on that worker. That is "I enabled the cron and nothing happened" —
precisely the failure this module exists to eliminate — arriving one deploy late instead of
immediately. A denylist subtracts from whatever the release declares, so new crons arrive
armed and the operator's variable does not rot.

An unknown name is a hard error at startup, not a silent skip — **and that includes a negated
one**: excluding a cron that does not exist means the job you meant to silence is still armed.
Two more shapes are refused rather than guessed: **mixing** inclusions with exclusions
(`poll_tracking_job,-run_mrp_auto_draft_job` reads either as "only the tracking poll" or as
"everything except MRP", and those differ by most of the schedule), and `none` combined with an
exclusion. `all` is the one positive token allowed alongside exclusions, as the explicit base.
Excluding *every* cron is legal and simply means `none`. Full syntax, error text and
case/whitespace rules: `docs/ENVIRONMENT_VARIABLES.md` → Background worker (ARQ), and the
`select_cron_jobs` docstring.

> **The `SUPPRESSED` log line reads correctly for either shape.** It diffs the registered
> crons against `ALL_CRON_JOBS` by object identity, and the denylist path filters that same
> list rather than rebuilding it, so what it prints is exact:
> `ARQ worker cron: 1 of 12 cron jobs SUPPRESSED by WORKER_CRON_JOBS='all,-run_mrp_auto_draft_job': cron:run_mrp_auto_draft_job`

---

## 3. What could not be established from the repo

I have no authorization to query the production Railway project and did not. Every item
here is a step for you.

1. **Does a Redis service exist in the Railway project at all?** `RAILWAY_DEPLOYMENT.md`
   never provisions one and lists no Redis variable, so "no" is plausible — in which case
   provisioning Redis is step zero.
2. **What Redis variables are set on `werco-api` in production?** The single most decisive
   check.
   ```
   railway variables --service werco-api --environment production --project "<PROJECT_ID>"
   ```
   (`railway variables` without `--set` is read-only.) `REDIS_HOST` absent ⇒ **proven: nothing
   ever queued, no backlog exists.** `REDIS_HOST` present but the Redis requires a password
   ⇒ also nothing queued, because the old code sent none.
3. **Does a worker service already exist, and what is its start command and healthcheck
   path?** Dashboard → service list → Settings → Deploy.
4. **What is `werco-api`'s actual start command?** `backend/Dockerfile`'s own comment records
   that Railway can override it; if it is overridden, the Dockerfile `CMD` is not what runs.
5. **What does Railway do to a service whose healthcheck fails, on this plan?** Confirm
   before creating anything. This determines whether a mis-configured worker sits dead or
   fires crons in bursts during restart windows.
6. **Is anything in Redis right now?** Only meaningful if #2 shows a reachable Redis. The
   queue is a **sorted set**, so `ZCARD`, not `LLEN`. Note that `railway run` executes
   **locally** with the service's variables injected, so a `*.railway.internal` host will not
   resolve from your laptop — and `redis-cli` is **not installed** in the image, but Python
   and the `redis` package are. Run it *inside* the container:
   ```
   railway ssh --service werco-api --environment production --project "<ID>" \
     python -c "import os,redis; r=redis.from_url(os.environ['REDIS_URL']); print(r.zcard('arq:queue'))"
   ```
   See `WORKER_DEPLOYMENT_RUNBOOK.md` §2.3 for the fuller version and how to read the result.
7. **Is SMTP configured?** If `SMTP_USER`/`SMTP_PASSWORD` are unset, `email_service.send_email`
   soft-skips and **no email leaves at all**, which changes the storm calculus completely.
8. **Are the egress kill switches off?** `allow_sms_egress`, `allow_carrier_egress`,
   `allow_print_egress` — all default off, but confirm.
9. **Has migration `072` applied in prod?** If not, the `notified_at` backfill that excludes
   pre-072 history has not happened.
10. **Is `TZ` set on any service?** If not, every "6 AM" cron is 01:00 Central.
11. **Has `backend/Dockerfile.worker` ever been built?** No — see §5.1b. Build it once
    locally before creating the service.

---

## 4. Sizing the first cron run (Supabase SQL editor)

```sql
-- Notification backlog. Only within_sweeper_window can ever be dispatched.
SELECT count(*) AS total_null,
       count(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS within_sweeper_window,
       min(created_at), max(created_at)
FROM operational_events WHERE notified_at IS NULL;

-- One email per row, per supervisor+manager in that tenant.
SELECT company_id, count(*) FROM work_orders
 WHERE is_deleted = false AND due_date < current_date
   AND status IN ('RELEASED','IN_PROGRESS') GROUP BY 1;

SELECT company_id, count(*) FROM users
 WHERE is_active AND role IN ('SUPERVISOR','MANAGER') GROUP BY 1;   -- role stores the enum NAME

SELECT company_id, count(*) FROM equipment
 WHERE status = 'ACTIVE' AND next_calibration_date > current_date
   AND next_calibration_date <= current_date + 7 GROUP BY 1;

-- valid_until is a DATE column, so compare against current_date.
SELECT company_id, count(*) FROM quotes
 WHERE status = 'SENT' AND valid_until > current_date
   AND valid_until <= current_date + 7 GROUP BY 1;
```

late-WO count × recipient count = the emails `check_late_work_orders_job` sends on day one.

---

## 5. Creating the service

Do this **after** §3 and §4, and only when you can watch the logs.

### 5.1 Merge the code fix first, and verify it alone

Deploy the `REDIS_URL` fix to the API **before** creating any worker. Then:

```
curl -s https://<api>/health/ready | jq .checks.job_queue_redis
```

Expected once `REDIS_URL` is set on `werco-api`:

```json
{ "status": "configured", "source": "REDIS_URL", "tls": false,
  "authenticated": true, "config_warnings": 0 }
```

`"status": "unconfigured"` means the API still has no real queue target — **stop and fix
that first.** `config_warnings > 0` means either that `REDIS_URL` and the host/port trio name
different instances, or that `REDIS_URL` points at loopback; the API startup log has the
detail. Note that `status` reports *which setting won*, not reachability — a
`REDIS_URL=redis://localhost:6379/0` (the value `backend/.env.example` ships) reads
`"configured"` and is not refused, because a single-box self-host on loopback is legitimate.
That case is what `config_warnings` is for. This endpoint deliberately reports no hostname and
no credential.

With the fix deployed and Redis reachable, enqueues start succeeding immediately — jobs
will begin accumulating in Redis for the first time. That is expected and is why the worker
comes next rather than later.

### 5.1b Smoke-test the worker image locally first (recommended)

**Not run here — the Docker daemon was not available in the session that prepared this.**
The image has never been built, so build it once before trusting it in Railway. It is a
near-copy of `backend/Dockerfile` with repo-root-relative `COPY` paths, so the likely
failure is a path, and you want that failure on your laptop.

```bash
# From the REPO ROOT -- the context is the repo root, not backend/.
docker build -f backend/Dockerfile.worker -t werco-worker:local .

# It must refuse to start with no Redis in production...
docker run --rm -e ENVIRONMENT=production \
  -e SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(64))') \
  -e REFRESH_TOKEN_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(64))') \
  -e DATABASE_URL=postgresql://u:p@h:5432/d -e CORS_ORIGINS=https://example.com \
  werco-worker:local
# expect: RedisConfigurationError ... Refusing to start rather than consume an empty queue

# ...and against the real compose stack it should come up and print its Redis target.
docker compose up -d redis && docker compose up worker
```

`docker compose up worker` also exercises the compose path, whose `REDIS_URL` /
`REDIS_PASSWORD` were corrected in this change — that worker previously could not
authenticate against `redis-server --requirepass` at all.

### 5.2 Create `werco-worker`

Railway dashboard → project → **New service** → **Empty service** → name it `werco-worker`.
Leave its **Root Directory empty**.

### 5.3 Set its variables

Copy every variable from `werco-api`, then verify these specifically. The compose worker
block was the starting point; the ones marked ★ are additional and were missing from it.

| Variable | Why |
|---|---|
| `REDIS_URL` | **The one that matters.** Byte-identical to `werco-api`'s. The worker refuses to start without a real target. |
| `DATABASE_URL` + the `SUPABASE_*` / `POSTGRES_*` set | Every job opens its own session. |
| `SECRET_KEY`, `REFRESH_TOKEN_SECRET_KEY` | Config validation requires them. |
| `ENVIRONMENT=production` | Arms the fail-fast Redis guard. |
| ★ `WORKER_CRON_JOBS=none` | **Set this for the first boot.** See §5.5. |
| ★ `SENTRY_DSN` | Otherwise a crashing cron is a log line nobody reads. Events are tagged `component=worker`. |
| ★ `FRONTEND_BASE_URL` | Every notification email builds its deep link from it. |
| `SMTP_*` | Email delivery. |
| `WEBHOOK_ENCRYPTION_KEY` | Outbound webhook secrets. |
| ★ `INTEGRATION_ENCRYPTION_KEY` | Carrier secrets; config **fails loudly** without it in production. |
| ★ `STORAGE_BACKEND` + `S3_*`/`AWS_*` | Label and document jobs write Documents; Railway has no persistent volume by default. |
| `ANTHROPIC_*` | `aggregate_ai_learning_job`. |
| ★ `AUDIT_ARCHIVE_DIR` | Only if you arm the monthly archive cron — see §8. |
| `TWILIO_*`, `SMS_DEFAULT_REGION` | Only if SMS is in use. |
| ★ `TZ` | Decide deliberately. Unset ⇒ UTC ⇒ "6 AM" is 01:00 Central. |

### 5.4 Confirm the two traps are closed

Both are handled in the repo, but confirm on the service:

- **Healthcheck.** The worker deploys from the **repo root**, so Railway reads the repo-root
  `railway.toml`, which declares **no** `healthcheckPath`. It does *not* read
  `backend/railway.toml` (`healthcheckPath = "/health"`), which an arq worker can never
  satisfy — a worker that inherited it would never be promoted, would be restarted, and
  **would still be running arq during each failing healthcheck window**, firing crons in
  bursts before being killed. Verify **Settings → Deploy → Healthcheck Path is empty**.
- **Second-API-replica.** The image is built from `backend/Dockerfile.worker`, whose `CMD`
  is `arq app.worker.WorkerSettings` and which contains no uvicorn and no `alembic`. Even
  with every override forgotten, the container is a worker. Verify **Settings → Deploy →
  Custom Start Command** is empty or `arq app.worker.WorkerSettings`.

`backend/tests/test_worker_deploy_config.py` asserts all of this and fails the build if it
regresses.

### 5.5 First boot: crons off

With `WORKER_CRON_JOBS=none`, enable the deploy and watch the log. You should see:

```
ARQ worker starting up (environment=production, release=<sha>)
ARQ worker Redis: redis://<host>:6379/0 [source=REDIS_URL, auth=password] | queue=arq:queue
ARQ worker cron: 12 of 12 cron jobs SUPPRESSED by WORKER_CRON_JOBS='none': ...
ARQ worker cron: none armed; draining enqueue-driven jobs only
ARQ worker ready (23 job functions registered)
```

The Redis line is the whole point: it must name the **same host** the API reports. The
password is never logged. Let it run long enough to drain the enqueue-driven backlog
(notifications, webhooks, labels), then check that in-app notifications are appearing.

### 5.6 Arm crons one at a time

Suggested order, safest first. Change `WORKER_CRON_JOBS`, redeploy, watch one cycle before
adding the next:

1. `relay_pending_notifications_job` — bounded by design (24 h floor, 500/pass).
2. `run_oee_auto_calc_job` — writes yesterday's OEE only, no email.
3. `check_low_stock_job` — one aggregated message per tenant.
4. `poll_tracking_job` — no-op while `allow_carrier_egress` is off.
5. `check_quote_expiring_job`, `check_calibrations_job` — window-bounded.
6. `send_daily_digest_job`.
7. `cleanup_old_logs_job` — physical deletes; confirm the 90-day windows are what you want.
8. `check_late_work_orders_job` — **run the §4 counts first.**
9. `aggregate_ai_learning_job`.
10. `archive_aged_audit_logs_job` — **only after §8.**
11. `run_mrp_auto_draft_job` — **last, and only deliberately.** It creates draft POs and
    work orders in production.

Then, once all are wanted: `WORKER_CRON_JOBS=all` (or unset it).

**Switching one back off later — do not go back to an allowlist.** Use the exclusion form, so
the other eleven stay whatever the current release declares:

```
WORKER_CRON_JOBS=all,-run_mrp_auto_draft_job    # everything except the MRP auto-draft pass
```

Listing the other eleven by name would work today and rot at the next release that adds a
cron. Procedure and blast radius: `WORKER_DEPLOYMENT_RUNBOOK.md` §6.4.

### 5.7 Turn on the CI deploy

Only after the service exists and a manual deploy has worked. Repo → Settings → Secrets and
variables → Actions → Variables:

- `DEPLOY_WORKER_PRODUCTION = true`
- `DEPLOY_WORKER_STAGING = true` (if you create `werco-worker-staging`)

Until these are set, both workflow steps are skipped and merging changes nothing.

---

## 6. Verifying it is actually working

There is no HTTP endpoint to curl — the worker serves none, by design. Use:

- **The startup log** (§5.5). The Redis host must match the API's.
- **Sentry**, filtered on `component:worker`.
- **The database**: `SELECT count(*) FROM operational_events WHERE notified_at IS NULL AND
  created_at > now() - interval '1 hour';` should trend to ~0.
- **`/health/ready` → `job_queue_redis`** for the enqueue half.

---

## 7. Rolling back

Delete or pause the `werco-worker` service, or set `WORKER_CRON_JOBS=none` and redeploy to
stop scheduled work while still draining request-driven jobs. To stop **one** cron and keep
the rest, set `WORKER_CRON_JOBS=all,-<job>` and redeploy — no allowlist, so the remaining
crons stay whatever the release declares. Nothing about the worker is required for the API to
serve — the API logs its queue target and carries on without one.

**Not reversible:** draft POs and work orders created by `run_mrp_auto_draft_job`, and
emails already sent. Both are why the order in §5.6 puts MRP last.

---

## 8. Open items for the owner

- **`AUDIT_ARCHIVE_DIR` needs durable storage.** Railway services have no persistent volume
  by default. `archive_aged_audit_logs_job` writes NDJSON exports there monthly; on
  ephemeral disk they vanish with the container. Attach a volume or point it at object
  storage **before** arming that cron. It only exports, never deletes, so a lost export is
  a lost archive, not lost audit data — but it is still a compliance artifact.
- **`TZ`.** Decide whether the crons should run on Central or UTC.
- **Two replicas would double every cron.** `numReplicas = 1` is in the config; do not raise
  it. Two processes each run the full schedule — MRP AUTO_DRAFT would create two sets of
  drafts per day.
- **The repo-root `railway.toml` hazard.** Because the worker's config file sits at the repo
  root, any service that has an empty Root Directory *and* is ever deployed from the repo
  root would read it and start running arq. CI deploys `werco-api` from `backend/` and
  `werco-frontend` from `frontend/`, and `test_worker_deploy_config.py` asserts it keeps
  doing so. If you ever enable Railway's GitHub integration on `werco-api`, set its Root
  Directory to `backend` first.
- **`--forwarded-allow-ips=*`** on the API remains an open finding from an earlier audit,
  unrelated to this work.
