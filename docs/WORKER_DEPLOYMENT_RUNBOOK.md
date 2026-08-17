# Background worker — cutover runbook

**Audience:** the person turning on the ARQ background worker in production, alone, before
the shop opens. **Read §1 and §3 before you touch anything.** Every command below is
copy-pasteable. Every step has a stop condition. Where a fact can only come from the live
Railway project, this document says so and tells you how to get it — it never guesses.

> **Where this document sits.** `docs/WORKER_SERVICE.md` is the *reference* (why the worker
> exists, what the code does, what changed). **This** is the *procedure*. They are
> deliberately separate; if they ever disagree, this file is the one you are executing and
> `WORKER_SERVICE.md` is the one to correct. `docs/DEPLOYMENT_RUNBOOK.md` covers the routine
> `werco-api` / `werco-frontend` deploy and mentions no worker at all; this is a one-time
> cutover of a subsystem that has no deploy history in this repo, with steps that cannot be
> undone, so it is not folded in there. `docs/NOTIFICATIONS.md` documents the notification pipeline's design, not
> its deployment.

**Nothing in this repo has been deployed.** No Railway service was created, no variable was
set, no Railway command was run against the project. Both CI worker-deploy steps are gated
off by default. Merging the branch changes nothing in production until you do §5.

---

## 0. The 60-second version

1. Nothing in this repo has ever deployed a worker (whether one exists in your Railway
   project is a live check — §4.2). **Separately**, the queue was pointed at the wrong Redis,
   so most likely *nothing was ever enqueued either* — there is probably no backlog at all.
   §2 tells you which of the two situations you are in. **Do not skip it.**
2. Deploy the API fix first, alone. Verify `job_queue_redis` on `/health/ready` (§5.1).
3. Create the worker with `WORKER_CRON_JOBS=none`. It drains only request-driven jobs.
4. Arm crons one at a time, in the order in §5.6, cheapest first.
5. `run_mrp_auto_draft_job` is **last**. It creates draft purchase orders and work orders in
   production. `check_late_work_orders_job` is second-to-last: it emails one message per
   late work order to every supervisor and manager, with no age cap.
6. Emails sent and drafts created are **not reversible**. Everything else is.

---

## 1. What is actually broken today

The report that started this said: *"there is no worker, so every cron has never fired, and
anything enqueued from a request is sitting in Redis unconsumed."*

**The first half is consistent with the repo. The second half is probably false**, and the
difference decides how dangerous starting a worker is.

### The two situations, and why they are different

| | **A — jobs queued and unconsumed** | **B — jobs never queued at all** |
|---|---|---|
| What happened | Enqueues succeeded. Redis holds a pile of jobs nobody consumed. | Every enqueue failed with `ConnectionRefused`. Nothing was ever written to Redis. |
| Cause | A worker was never started. | The queue resolved to `localhost:6379` inside the API container. |
| What a worker does on first boot | Immediately drains the pile — possibly thousands of jobs, including old emails. | Nothing to drain. Starts clean. |
| Risk of starting a worker | **Real.** Sized in §3. | **Near zero** for request-driven jobs. The crons are still the risk. |
| How you tell | `ZCARD arq:queue` > 0 | `ZCARD arq:queue` = 0 or the key does not exist |

### Why B is the likely one

`backend/app/core/queue.py` resolved ARQ's Redis from `REDIS_HOST` / `REDIS_PORT` /
`REDIS_DB` and **never read `REDIS_URL`**. It also had **no password field at all**. Every
other Redis consumer in the backend — the response cache, the rate-limiter storage, the
login throttle, and the `/health/ready` probe — reads `REDIS_URL`. And both
`docs/ENVIRONMENT_VARIABLES.md` and `backend/.env.example` told operators that `REDIS_URL`
"takes precedence over individual settings."

Railway hands you a managed Redis as a **single URL** (`redis://default:PASS@host:6379`).
So a deployment provisioned from those docs sets `REDIS_URL` and nothing else — and the
queue silently fell back to `localhost:6379`, where nothing is listening inside a container.
Meanwhile `/health/ready` reported `"redis": {"status": "healthy"}` the whole time, because it pings
`REDIS_URL`. That is why this was invisible.

Even if `REDIS_HOST` *was* set, a Railway managed Redis requires a password the old code
never sent — which lands you in situation B as well.

**What that cost while it was broken:**

| Path | Symptom |
|---|---|
| `POST /scheduling/run-background` | **HTTP 500 after ~5 s.** The only user-visible hard break. |
| PO receiving, WO completion, visitor check-in | Silent ~5 s delay per action, then swallowed. Nothing scheduled. |
| Notification outbox | Silent ~1 s delay per committed request, then swallowed. |
| Everything else | No background work of any kind — no emails, no digests, no OEE, no MRP, no tracking polls. |

### There is also a natural 24-hour ceiling on any Redis backlog

Even in situation A, the pile is age-limited. ARQ stores each job body under `arq:job:<id>`
with a **1-day TTL** (`expires_extra_ms`, arq 0.28.0 default). A worker that picks up a job
whose body has expired logs `job <id> expired` and discards it. So anything queued more than
~24 hours ago is already dead weight — it will be dropped, not executed. `ZCARD` can
therefore be much larger than the number of jobs that will actually run; §2.3 shows how to
tell them apart.

### What was fixed in the code (not yet deployed)

- `REDIS_URL` is now the source of truth for **both** the enqueue side and the worker, with
  the host/port/db trio as fallback and `REDIS_PASSWORD` filling a gap neither supplied.
- A worker whose Redis resolves to the localhost default in `production`/`staging`
  **refuses to start**. "Started successfully and consumed nothing" is no longer reachable.
- `/health/ready` now reports `job_queue_redis` — you can check the enqueue side with one
  `curl`, without shell access. (Note the endpoint: it is `/health/ready`, alongside the
  existing `redis` check, **not** `/health/detailed`.)
- The worker now initializes Sentry (tagged `component=worker`). It previously had none, so
  every cron traceback died in the container log.

---

## 2. Establishing which situation you are in

Do this first. It takes five minutes and it decides everything downstream.

Set these once in your shell:

```bash
export RAILWAY_PROJECT_ID="<your project id>"   # Railway dashboard -> project -> Settings
```

### 2.1 Does a Redis service even exist in the project?

Railway dashboard → your project → the service list. Look for a Redis service.

`RAILWAY_DEPLOYMENT.md` never provisions one and lists no Redis variable, so **"no Redis
service" is a plausible answer.**

- **No Redis service** → you are definitively in situation B, there is no backlog, and
  provisioning Redis is step zero. Add it (Railway → New → Database → Redis), then continue.
- **A Redis service exists** → continue to 2.2.

### 2.2 What Redis variables are actually set on `werco-api`? — the decisive check

```bash
railway variables --service werco-api --environment production --project "$RAILWAY_PROJECT_ID"
```

`railway variables` **without** `--set` is read-only. It prints values, so do not screen-share
this and do not paste the output anywhere.

Read the output for four names:

| What you see | What it means |
|---|---|
| `REDIS_HOST` **absent** (only `REDIS_URL` set) | **Situation B, proven.** Nothing was ever enqueued. No backlog. This is the expected result. |
| `REDIS_HOST` set to `localhost` / `127.0.0.1` | **Situation B, proven.** Same as above. |
| `REDIS_HOST` set to a real host, **and** `REDIS_URL` contains `default:<password>@` | **Situation B.** The old code sent no password, so every enqueue was rejected. |
| `REDIS_HOST` set to a real host **and** that Redis has no password | **Possibly situation A.** Go to 2.3 and count. |
| No `REDIS_URL` and no `REDIS_HOST` | Situation B, and the cache/limiter were also unconfigured. |

**Also note down** whether `REDIS_URL` and `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB` name *different*
instances. After the fix, `REDIS_URL` wins and the trio becomes dead config that will mislead
the next person. The API will log a warning about this at startup; unset the loser.

### 2.3 Count what is actually in Redis

**You cannot reach Railway's Redis from your laptop with `REDIS_URL`.** `railway run` executes
the command **locally** with the service's variables injected, and `REDIS_URL` points at
`*.railway.internal`, which resolves only inside Railway's private network. A local
`redis-cli -u "$REDIS_URL"` will fail with a DNS error — that failure means "you are outside
the network", **not** "Redis is down".

Two ways in. Use the first.

**(a) From inside the API container (works always).** `redis-cli` is **not installed** in the
image, but Python and the `redis` package are:

```bash
railway ssh --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" \
  python -c "
import os, redis
r = redis.from_url(os.environ['REDIS_URL'])
print('PING              :', r.ping())
print('queued (ZCARD)    :', r.zcard('arq:queue'))
print('live job bodies   :', sum(1 for _ in r.scan_iter('arq:job:*', count=500)))
print('results kept      :', sum(1 for _ in r.scan_iter('arq:result:*', count=500)))
print('used_memory_human :', r.info('memory')['used_memory_human'])
oldest = r.zrange('arq:queue', 0, 0, withscores=True)
print('oldest queued at  :', oldest)
"
```

**(b) From your laptop, if the Redis service exposes a public proxy URL.** Railway's managed
Redis usually also publishes `REDIS_PUBLIC_URL` (check the Redis service's Variables tab). If
it exists:

```bash
redis-cli -u "$REDIS_PUBLIC_URL" PING
redis-cli -u "$REDIS_PUBLIC_URL" ZCARD arq:queue
redis-cli -u "$REDIS_PUBLIC_URL" INFO memory | grep -E 'used_memory_human|maxmemory_human'
redis-cli -u "$REDIS_PUBLIC_URL" --scan --pattern 'arq:job:*' | wc -l
redis-cli -u "$REDIS_PUBLIC_URL" ZRANGE arq:queue 0 0 WITHSCORES
```

The queue is a **sorted set** — `ZCARD`, not `LLEN`. The queue name is `arq:queue`. Scores are
epoch **milliseconds** of when the job becomes eligible; divide by 1000 to read as a date.

**How to read the result:**

| Result | Verdict |
|---|---|
| `ZCARD` = 0, or `(error) ... no such key` | **Situation B confirmed. No backlog. Proceed — the request-driven half of the cutover is free.** |
| `ZCARD` > 0 but `live job bodies` = 0 | Everything queued has expired. A worker will log `job … expired` for each and discard them. Harmless, but the queue is stale — see §2.4. |
| `ZCARD` > 0 **and** `live job bodies` > 0 | **Situation A. STOP.** Real jobs will execute on first boot. Go to §2.4 before proceeding. |
| `PING` fails from inside the container | Redis is genuinely unreachable from the API. **STOP** and fix that before anything else. |

### 2.4 STOP CONDITION — if there is a live backlog

Do **not** start a worker against an unexamined backlog. Find out what is in it first:

```bash
railway ssh --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" \
  python -c "
import os, redis, collections, pickle
r = redis.from_url(os.environ['REDIS_URL'])
kinds = collections.Counter()
for k in r.scan_iter('arq:job:*', count=500):
    try:
        kinds[pickle.loads(r.get(k))['f']] += 1   # 'f' is the job function name
    except Exception:
        kinds['<unreadable>'] += 1
for name, n in kinds.most_common():
    print(f'{n:6d}  {name}')
"
```

Then decide, per job type, using the table in §3. If the pile contains hundreds of
`send_email_job` / `dispatch_notification_job` entries, starting a worker sends those emails.
Your options are: let them go (accept it), or purge the queue before starting the worker —
which is destructive and loses the work permanently:

```bash
# DESTRUCTIVE. Deletes every queued job. Only after you have decided.
railway ssh --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" \
  python -c "
import os, redis
r = redis.from_url(os.environ['REDIS_URL'])
n = r.zcard('arq:queue'); r.delete('arq:queue')
for k in r.scan_iter('arq:job:*', count=500): r.delete(k)
print('purged', n, 'queued jobs')
"
```

Purging the Redis queue does **not** lose notifications: the relay sweeper re-enqueues any
`operational_events` row with `notified_at IS NULL` from the trailing 24 hours (§3). It does
lose one-off jobs (a scheduling run, a label print) — all of which a user can retrigger.

---

## 3. The backlog question: what fires, how often, and who gets contacted

**This is the section the decision rests on.** Read it in full.

### 3.1 First, the two facts that shrink the problem

**Fact 1 — the notification sweeper is bounded and safe.** `relay_pending_notifications_job`
cannot emit months of history. Three independent limits in
`backend/app/jobs/notification_jobs.py`:

- `_RELAY_MAX_AGE_HOURS = 24` — anything older than 24 hours is **never** dispatched. Ever.
- `_RELAY_GRACE_MINUTES = 2` — the freshest 2 minutes are skipped.
- `_RELAY_BATCH_LIMIT = 500` per pass, every 5 minutes → at most 6,000/hour.

Migration `072` also backfilled `notified_at = created_at` on all rows that existed when it
ran, so all pre-`072` history is permanently excluded. **On first boot the sweeper picks at
most 500 events, drawn only from the trailing 24 hours.**

**Fact 2 — the daily crons are the storm risk.** They enumerate *current* state with no age
cap. `wo.late` is marked `recurring`, but the suppression that stops it re-notifying keys off
*an unread in-app row already existing*. On a cold start none exist, **so the first run fires
in full.**

### 3.2 Measure the blast before you arm anything

Run these in the Supabase SQL editor. Write the numbers down; §3.3 uses them.

```sql
-- L : late work orders per tenant (drives check_late_work_orders_job)
SELECT company_id, count(*) AS late_wos FROM work_orders
 WHERE is_deleted = false AND due_date < current_date
   AND status IN ('RELEASED','IN_PROGRESS') GROUP BY 1;

-- R : email recipients for late-WO alerts, per tenant (role stores the enum NAME)
SELECT company_id, count(*) AS supervisors_and_managers FROM users
 WHERE is_active AND role IN ('SUPERVISOR','MANAGER') GROUP BY 1;

-- M : managers, per tenant (drives the mrp.completed email)
SELECT company_id, count(*) AS managers FROM users
 WHERE is_active AND role = 'MANAGER' GROUP BY 1;

-- D : users who will receive a daily digest email
SELECT count(*) AS digest_users FROM notification_preferences p
  JOIN users u ON u.id = p.user_id
 WHERE p.digest_enabled AND p.digest_frequency = 'DAILY' AND u.is_active;

-- E : equipment due for calibration within 7 days (digest-only, see below).
-- Anything ALSO within 1 day is dispatched twice -- the cron runs a 7-day pass and a
-- separate 1-day "URGENT" pass over the overlapping set. Count that overlap separately:
SELECT company_id,
       count(*) AS due_7d,
       count(*) FILTER (WHERE next_calibration_date <= current_date + 1) AS also_due_1d
  FROM equipment
 WHERE status = 'ACTIVE' AND next_calibration_date > current_date
   AND next_calibration_date <= current_date + 7 GROUP BY 1;

-- Q : quotes expiring within 7 days (digest-only). valid_until is a DATE column.
SELECT company_id, count(*) FROM quotes
 WHERE status = 'SENT' AND valid_until > current_date
   AND valid_until <= current_date + 7 GROUP BY 1;

-- Notification backlog. Only within_sweeper_window can EVER be dispatched.
SELECT count(*) AS total_pending,
       count(*) FILTER (WHERE created_at >= now() - interval '24 hours') AS within_sweeper_window,
       min(created_at) AS oldest, max(created_at) AS newest
FROM operational_events WHERE notified_at IS NULL;
```

**The number that matters most: `L × R` is how many emails `check_late_work_orders_job` sends
on day one.** If you have 60 late work orders and 5 supervisors/managers, that is **300
emails in one burst**, all to your own staff.

### 3.3 Per-job blast radius

Times are the container's local zone. ARQ defaults to the container's timezone, which on
Railway is **UTC unless you set `TZ`** — so "6 AM" means 06:00 UTC = **01:00 Central**. Decide
this deliberately (§8).

#### Group 1 — SAFE. Idempotent, no message to any human.

| Job | Schedule | What it does on first run | Contacts anyone? |
|---|---|---|---|
| `relay_pending_notifications_job` | every 5 min | Re-enqueues ≤500 events from the trailing 24 h; older rows are permanently skipped. | Indirectly, bounded — see `within_sweeper_window` above. |
| `run_oee_auto_calc_job` | 02:30 daily | Writes `OEERecord` (`source='auto'`) for **yesterday only**, per active work center. Never overwrites a `manual` record. **Does not backfill history.** | **No.** |
| `poll_tracking_job` | every 30 min | Polls carriers for in-flight shipments — but only for tenants whose `allow_carrier_egress` is ON (default **off**). | **No,** while the switch is off. Outbound carrier traffic when on. |

#### Group 2 — WRITES DATA, no direct email. Review before arming.

| Job | Schedule | What it does on first run | Contacts anyone? |
|---|---|---|---|
| `cleanup_old_logs_job` | Sun 02:00 | **Physical DELETEs**: completed `Job` rows and `NotificationLog` rows older than 90 days; read `Notification` rows older than 90 days; unread notifications of deactivated users. **Audit logs are explicitly excluded** and are never touched. | No. |
| `archive_aged_audit_logs_job` | 1st of month, 03:00 | Exports audit rows past their retention window to NDJSON in `AUDIT_ARCHIVE_DIR`. **Exports only — never deletes.** Needs durable storage or the export vanishes with the container (§8). | No. |
| `aggregate_ai_learning_job` | 05:30 daily | Writes `AIRecommendation` rows and emits `work_order_blocker_escalated` events — **which re-enter the notification outbox and generate further notifications.** Guarded against duplicating an existing pending recommendation. | Indirectly, via the events it emits. |

#### Group 3 — EMAILS REAL PEOPLE. These are the ones to stage carefully.

Channel routing comes from `backend/app/services/notification_catalog.py`. Several of these
route to **digest**, not to direct email — meaning they land in the daily digest queue and are
only emailed by `send_daily_digest_job`, and only to users with a digest preference row.

| Job | Schedule | Channel | Volume on day one | Who |
|---|---|---|---|---|
| `check_late_work_orders_job` | 08:00 daily | in-app **+ direct email** | **L × R messages.** One per late WO, per recipient. No age cap. Cold-start suppression does not apply. | Every active user with role `SUPERVISOR` or `MANAGER` in that tenant. |
| `run_mrp_auto_draft_job` | 06:00 daily | direct email | **M messages per tenant** — one per manager. Only when the run produced actions. Plus the writes below. | Every active `MANAGER`. |
| `send_daily_digest_job` | 08:00 daily | direct email | **D emails** — one per user with `digest_enabled` + `DAILY` and at least one queued item in the last 24 h. | Those users. |
| `check_calibrations_job` | 07:00 daily | in-app + **digest** | E in-app rows per Quality user, **plus a second "URGENT" one for anything due within 1 day** (the 7-day and 1-day passes overlap). Email only via the digest job. | Users whose `department = 'Quality'` (exact string). Note the cron passes `department`, so the catalog's `roles=(QUALITY,)` is *not* used here. |
| `check_quote_expiring_job` | 09:00 daily | **digest only** | Q digest items. No direct email. | Sales-department users. |
| `check_low_stock_job` | 07:30 daily | **digest only** | **One aggregated message per tenant**, not one per item. Lowest blast radius of the group. | Purchasing + Inventory department users. |

**SMS:** no cron sends SMS unless a user has explicitly opted a channel in, the catalog entry
is `sms_eligible`, the user has a phone on file, **and** `Company.allow_sms_egress` is ON
(default **off**, enforced fail-closed). Confirm it is off in §4.6 and SMS is a non-issue.

#### Group 4 — THE ONE THAT WRITES BUSINESS RECORDS

`run_mrp_auto_draft_job` (06:00 daily) runs MRP in `AUTO_DRAFT` mode for **every active
company** and calls `MRPAutoService.process_actions`, which **creates draft purchase orders
and draft work orders**. It is **not idempotent** — each run creates a new `MRPRun` and a new
set of drafts. Arm it last, deliberately, and only when someone is available to review what
it produced the same morning.

#### Request-enqueued jobs (not crons) — these run as soon as the worker starts

These are what `WORKER_CRON_JOBS=none` still allows, and they are the *good* half: each one
corresponds to something a user actually did.

| Job | Triggered by | Effect |
|---|---|---|
| `dispatch_notification_job` | the outbox tee + the sweeper | Creates in-app notifications, sends email/SMS per catalog + preference |
| `send_email_job` | the dispatcher | SMTP send. **Soft-skips if `SMTP_USER`/`SMTP_PASSWORD` are unset** (§4.5) |
| `send_webhook_job`, `dispatch_work_order_completion_signals_job` | WO completion | Outbound HTTP to configured webhooks |
| `print_receiving_label_job` | PO receiving | Only prints if `auto_print_on_receipt` + `allow_print_egress` are both on (default off) |
| `process_tracking_webhook_job` | carrier webhook | Applies tracking events to a shipment |
| `run_scheduling_job` | `POST /scheduling/run-background` | Writes schedule rows. **This endpoint currently 500s** and starts working again after §5.1 |
| `dispatch_notification_direct_job` | visitor check-in | Host notification + email |

---

## 4. Pre-flight checks

Run all of these before §5. Each has an explicit stop condition.

### 4.1 Redis reachable and counted
§2. **Proceed only when** you know your `ZCARD`, and — if it is non-zero with live job
bodies — you have decided to accept or purge the backlog.

### 4.2 Does a worker service already exist?

Railway dashboard → project → service list. If something named `worker` already exists:

- **Settings → Deploy → Custom Start Command.** If it is **not** `arq app.worker.WorkerSettings`,
  that service is **a second API replica**, not a worker — it answers `/health`, looks green,
  and does zero background work.
- **Settings → Deploy → Healthcheck Path.** If it is `/health`, the service can never pass its
  healthcheck. See 4.3.

**STOP** if a worker service exists and you did not know about it. Understand what it has been
doing before adding another.

### 4.3 The healthcheck trap

`backend/railway.toml` sets `healthcheckPath = "/health"`. CI deploys the API with
`cd backend && railway up . --path-as-root`, which makes `backend/` the archive root, so that
file is the config for **any** service deployed that way.

**An ARQ worker serves no HTTP.** It has no ASGI app and binds no port. It can never satisfy
`/health`. A worker that inherits that path would fail its healthcheck forever: never
promoted, restarted up to `restartPolicyMaxRetries` (3), **and still running arq during each
of those windows** — so it could fire crons in bursts before being killed. A partially
executed cron, repeated per retry, is worse than no worker.

This is closed structurally: the worker deploys from the **repo root**, so Railway reads the
repo-root `railway.toml`, which declares **no** `healthcheckPath`. The Railway CLI has no flag
to select a config file, so a separate archive root is the only mechanism available.

**Verify after creating the service (§5.4): Settings → Deploy → Healthcheck Path is empty.**

**Live check you must do yourself:** confirm in Railway's current documentation what your plan
does to a service whose healthcheck fails, and confirm that `healthcheckPath` can be cleared
per-service. I had no authorization to test this against your project.

### 4.4 `werco-api`'s actual start command

`backend/Dockerfile`'s CMD is
`alembic upgrade head && uvicorn app.main:app --workers ${WEB_CONCURRENCY:-2} …`, **but
Railway can override a container's start command from the dashboard.** Check
`werco-api` → Settings → Deploy → Custom Start Command so you know what is really running
before you change anything. Note it down.

### 4.5 Is SMTP configured?

```bash
railway variables --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" \
  | grep -i smtp
```

If `SMTP_USER` or `SMTP_PASSWORD` is unset, `email_service.send_email` logs
`SMTP credentials not configured, skipping email send` and returns `False`. **No email leaves
at all** — which makes the §3.3 Group 3 volumes moot and the whole cutover much safer. If they
*are* set, take §3.2's `L × R` seriously.

### 4.6 Are the egress kill switches off?

```sql
SELECT id, name, allow_sms_egress, allow_ai_egress FROM companies;
SELECT company_id, allow_carrier_egress FROM company_shipping_profiles;
SELECT company_id, auto_print_on_receipt, allow_print_egress FROM company_print_profiles;
```

All default off. **Proceed when** they are off, or when you know why one is on.

### 4.7 Has migration `072` applied in production?

```sql
SELECT column_name FROM information_schema.columns
 WHERE table_name = 'operational_events' AND column_name = 'notified_at';
SELECT version_num FROM alembic_version;
```

**STOP** if `notified_at` does not exist: the backfill that excludes pre-`072` history has not
run, and the sweeper's safety floor is not in place.

### 4.8 Timezone

```bash
railway variables --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" | grep -i '^TZ'
```

Unset ⇒ UTC ⇒ every "6 AM" cron runs at **01:00 Central**. Decide now (§8); it is easier to set
`TZ` before the first cron than to explain a 1 a.m. MRP run.

### 4.9 Build the worker image locally once

**The image has never been built** — Docker was unavailable in the session that prepared it.
It is a near-copy of `backend/Dockerfile` with repo-root-relative `COPY` paths, so the likely
failure is a path. Get that failure on your laptop, not in Railway.

```bash
cd /path/to/Werco-ERP-MES          # REPO ROOT — the build context is the root, not backend/
docker build -f backend/Dockerfile.worker -t werco-worker:local .
```

Then confirm it refuses to start without Redis in production:

```bash
docker run --rm -e ENVIRONMENT=production \
  -e SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(64))') \
  -e REFRESH_TOKEN_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(64))') \
  -e DATABASE_URL=postgresql://u:p@h:5432/d -e CORS_ORIGINS=https://example.com \
  werco-worker:local
```

**Expected:** it exits with `RedisConfigurationError: … Refusing to start rather than consume
an empty queue in silence.` **STOP** if it instead starts up quietly, or if it starts uvicorn.

Optionally, against the local stack: `docker compose up -d redis && docker compose up worker`.

---

## 5. Staged cutover

Do this when you can watch the logs, not on a Friday, and not while anyone is mid-shift.

### 5.1 Stage 0 — deploy the API fix ALONE, and verify it

Merge and deploy the branch to `werco-api` **without** creating any worker. Then:

```bash
curl -s https://<your-api-host>/health/ready | jq '.checks.job_queue_redis'
```

**Expected once `REDIS_URL` is set on `werco-api`:**

```json
{
  "status": "configured",
  "source": "REDIS_URL",
  "tls": false,
  "authenticated": true,
  "config_warnings": 0
}
```

| Field | Meaning | Action |
|---|---|---|
| `"status": "configured"` **and** `"config_warnings": 0` | The API has a real queue target. | Proceed. |
| `"status": "unconfigured"` | Still resolving to localhost. **Enqueues still fail.** | **STOP.** Set `REDIS_URL` on `werco-api`. |
| `"status": "misconfigured"` | `REDIS_URL` is not a parseable DSN. | **STOP.** Fix the value. |
| `"status": "configured"` but `"config_warnings"` > 0 | Two possible causes; the API startup log names which. **(a)** `REDIS_URL` points at **loopback** — the value `backend/.env.example` ships. `status` reports *which setting won*, not reachability, and this is deliberately **not** refused (a single-box self-host on loopback is legitimate), so this warning is the only signal. Inside a Railway container nothing listens there. **(b)** `REDIS_URL` and the host/port/db trio name **different instances**; the URL wins and the trio is dead config. | **STOP.** (a) Point `REDIS_URL` at the managed Redis. (b) Unset the trio. |
| `"authenticated": false` on a managed Redis | No password in play. | Check the URL carries `default:<password>@`. |

No hostname and no credential is exposed here — the endpoint is unauthenticated by design.

Also confirm from the API's startup log:

```
API job queue Redis target: redis://<host>:6379/0 [source=REDIS_URL, auth=password]
```

**What changes the moment this deploys:** enqueues start *succeeding* for the first time.
Jobs begin accumulating in Redis with a ~24 h TTL each. That is expected and is exactly why
the worker comes next rather than next week. **Do not leave a long gap between Stage 0 and
Stage 1.** Same morning.

Quick functional proof: `POST /scheduling/run-background` should now return promptly instead
of 500-ing after five seconds.

### 5.2 Stage 1 — create the worker service

Railway dashboard → project → **New service** → **Empty service** → name it `werco-worker`.
Leave its **Root Directory empty** (the worker is the one service deployed from the repo root).

### 5.3 Stage 2 — set its variables, with crons OFF

Copy every variable from `werco-api`, then verify these specifically. ★ marks ones that are
easy to miss.

| Variable | Why |
|---|---|
| `REDIS_URL` | **The one that matters. Byte-identical to `werco-api`'s.** The worker refuses to start without a real target. |
| `DATABASE_URL` + the `SUPABASE_*` / `POSTGRES_*` set | Every job opens its own DB session. |
| `SECRET_KEY`, `REFRESH_TOKEN_SECRET_KEY` | Config validation requires them. |
| `ENVIRONMENT=production` | Arms the fail-fast Redis guard. |
| ★ **`WORKER_CRON_JOBS=none`** | **Set this now.** Without it, all 12 crons are live the moment the container boots. |
| ★ `SENTRY_DSN` | Otherwise a crashing cron is a log line nobody reads. Events are tagged `component=worker`. |
| ★ `FRONTEND_BASE_URL` | Every notification email builds its deep link from it. |
| `SMTP_*` | Email delivery. |
| `WEBHOOK_ENCRYPTION_KEY` | Outbound webhook secrets. |
| ★ `INTEGRATION_ENCRYPTION_KEY` | Carrier secrets; config **fails loudly** without it in production. |
| ★ `STORAGE_BACKEND` + `S3_*` / `AWS_*` | Label and document jobs write Documents; Railway has no persistent volume by default. |
| `ANTHROPIC_API_KEY` | `aggregate_ai_learning_job`. |
| ★ `AUDIT_ARCHIVE_DIR` | Only when you arm the monthly archive cron — and only pointing at durable storage (§8). |
| `TWILIO_*`, `SMS_DEFAULT_REGION` | Only if SMS is in use. |
| ★ `TZ` | Unset ⇒ UTC ⇒ "6 AM" is 01:00 Central. |

### 5.4 Stage 3 — confirm the two traps are closed, then deploy

Before the first deploy completes, check on the service:

- **Settings → Deploy → Healthcheck Path is EMPTY.** (§4.3.)
- **Settings → Deploy → Custom Start Command** is empty or exactly
  `arq app.worker.WorkerSettings`. The image's CMD is `arq` and contains no uvicorn and no
  `alembic`, so even a forgotten override yields a worker — but confirm.

Deploy it (either `railway up` from the repo root, or enable the CI gate in §7).

### 5.5 Stage 4 — verify the first boot

Watch the deploy log. **You are looking for exactly this shape:**

```
ARQ worker starting up (environment=production, release=<sha>)
ARQ worker Redis: redis://<host>:6379/0 [source=REDIS_URL, auth=password] | queue=arq:queue
ARQ worker cron: 12 of 12 cron jobs SUPPRESSED by WORKER_CRON_JOBS='none': cron:aggregate_ai_learning_job, …
ARQ worker cron: none armed; draining enqueue-driven jobs only
ARQ worker ready (23 job functions registered)
```

| What you see | Verdict |
|---|---|
| The Redis host **matches** what the API reports | **Correct.** This is the whole point of the line. |
| The Redis host **differs** from the API's | **STOP.** Two different Redis instances — the exact silent failure this cutover exists to prevent. |
| `RedisConfigurationError … Refusing to start` and a crash loop | `REDIS_URL` is missing or wrong on the worker. Fix and redeploy. Working as designed. |
| Fewer than `12 of 12 … SUPPRESSED` | `WORKER_CRON_JOBS` is not `none`. **STOP** and set it before crons fire. |
| A uvicorn banner / `Application startup complete` | You are running the API image. **STOP** — this is the second-API-replica trap. |
| Nothing after "starting up" | The process died before `startup`. Check Sentry (`component:worker`). |

The password is never logged. This is enforced by a test.

Now let it run for **30–60 minutes** with crons still off, and watch:

- Complete a work order, receive a PO line, or sign a visitor in. Within seconds the worker
  log should show the corresponding job, and the recipient's in-app bell should light up.
- `SELECT count(*) FROM operational_events WHERE notified_at IS NULL AND created_at > now() - interval '1 hour';`
  should trend toward 0 **once the sweeper is armed** (Stage 5, step 1) — before then, only
  events whose live enqueue succeeded get dispatched.
- Sentry, filtered `component:worker`, should be quiet.

**STOP** and investigate if the worker restarts repeatedly, or if the log shows
`job … expired` in volume you did not expect from §2.3.

### 5.6 Stage 5 — arm crons, one at a time

Change `WORKER_CRON_JOBS` on the worker service, redeploy, and **watch one full cycle** before
adding the next. Values are comma-separated job names; `all` and `none` are the special cases;
an unknown name is a **hard startup error**, not a silent skip.

```
WORKER_CRON_JOBS=relay_pending_notifications_job
WORKER_CRON_JOBS=relay_pending_notifications_job,run_oee_auto_calc_job
…and so on
```

> **This staged list is an allowlist, which is right for arming *up* and wrong for staying
> there.** An allowlist freezes the set — a cron added in a later release silently never
> registers on this worker. Once you are past the rollout, move to `all` (or unset), and use
> the `-` exclusion form for anything you want left off: `WORKER_CRON_JOBS=all,-<job>`. See
> §6.4.

Order, cheapest and safest first:

| # | Add | Watch for, after |
|---|---|---|
| 1 | `relay_pending_notifications_job` | Every 5 min: `relay sweeper re-enqueued N pending notification events`. `operational_events` pending count trends to 0 within the 24 h window. |
| 2 | `run_oee_auto_calc_job` | Next 02:30: new `oee_records` rows with `calculation_source='auto'` for yesterday, and **no** `manual` rows changed. |
| 3 | `check_low_stock_job` | Next 07:30: **one** aggregated digest item per tenant. No direct emails. |
| 4 | `poll_tracking_job` | Every :00/:30. No-op while `allow_carrier_egress` is off. Confirm it is not erroring. |
| 5 | `check_quote_expiring_job`, `check_calibrations_job` | Digest items only, counts matching Q and E from §3.2. |
| 6 | `send_daily_digest_job` | **First real email volume: D emails.** Confirm one arrives, is readable, and its links work (needs `FRONTEND_BASE_URL`). |
| 7 | `cleanup_old_logs_job` | Sunday 02:00. Physical deletes. Confirm the 90-day windows are what you want **before** arming. Audit logs are untouched. |
| 8 | `check_late_work_orders_job` | **Re-run the `L × R` query the same morning.** This is the big email burst. Consider closing out stale late WOs first. |
| 9 | `aggregate_ai_learning_job` | New `AIRecommendation` rows; watch for a secondary wave of notifications from the events it emits. |
| 10 | `archive_aged_audit_logs_job` | **Only after §8 (durable storage).** Then confirm the NDJSON file actually persists. |
| 11 | `run_mrp_auto_draft_job` | **Last, deliberately, on a morning someone can review the output.** Immediately after: `SELECT count(*) FROM purchase_orders WHERE status='DRAFT' AND created_at > now() - interval '2 hours';` and the same for work orders. |

Once every cron is wanted: `WORKER_CRON_JOBS=all`, or unset it. If some cron is deliberately
**not** wanted — MRP auto-draft is the live example — that is `all,-<job>`, not an allowlist of
the others. §6.4.

---

## 6. Rollback

### 6.1 How to stop

| Goal | Action | Effect |
|---|---|---|
| Stop scheduled work, keep request-driven jobs | Set `WORKER_CRON_JOBS=none`, redeploy | Crons stop. Notifications, webhooks, labels still process. |
| Stop **one** cron, keep the other eleven | Set `WORKER_CRON_JOBS=all,-<job>`, redeploy | That cron stops. Everything else — **including crons added in future releases** — stays armed. §6.4. |
| Stop everything | Railway → `werco-worker` → **Remove** / pause the service | No background work at all. **The API is unaffected** — it logs its queue target and serves normally without a worker. |
| Undo the whole change | Revert the branch and redeploy `werco-api` | Back to enqueues failing against localhost. Nothing is corrupted by this. |

Set `DEPLOY_WORKER_PRODUCTION` back to unset (or `false`) if you enabled the CI gate, or the
next merge to `main` will recreate the deploy.

### 6.2 What is left behind if you stop it mid-run

- **Each job runs in its own DB transaction** and commits its own writes. A job killed
  mid-run leaves whatever it had already committed; there is no partial-transaction state.
- **ARQ re-runs cancelled jobs.** `retry_jobs` defaults to true and `max_tries` to 5, so a job
  killed by a shutdown signal is **re-queued and executed again from the start** on the next
  worker boot. For an idempotent job that is fine. For `run_mrp_auto_draft_job` it means
  **another set of draft POs and WOs.** This is the strongest argument for arming MRP last and
  never restarting the worker mid-MRP-run.
- **Notifications are crash-safe.** `dispatch_notification_task` commits the notification rows
  and the `notified_at` marker in one transaction; a crash before commit leaves
  `notified_at IS NULL` and the sweeper re-picks it (within 24 h).
- **Queued jobs survive a worker restart** — they stay in `arq:queue` and are picked up on the
  next boot, unless their ~24 h body TTL expired first (then: `job … expired`, discarded).

### 6.3 What is NOT reversible — the line the runbook crosses

Once you cross these, no rollback undoes them:

| Step | Irreversible effect |
|---|---|
| §5.6 #6 `send_daily_digest_job` | **Emails sent.** Cannot be recalled. |
| §5.6 #8 `check_late_work_orders_job` | **`L × R` emails sent** to staff. Cannot be recalled. |
| §5.6 #11 `run_mrp_auto_draft_job` | **Draft POs and WOs created**, plus emails to managers. They can be deleted afterwards (soft-delete), but the records existed, are audited, and were visible to users. |
| §5.6 #7 `cleanup_old_logs_job` | **Physical DELETEs** of job records, notification logs, and read notifications older than 90 days. Not soft deletes. Not recoverable except from a database backup. (Audit logs are untouched.) |
| §5.6 #9 `aggregate_ai_learning_job` | Writes recommendations and emits events; the notifications those produce are sent. |
| §2.4 purge command | Queued jobs deleted permanently. |
| Any worker boot with `poll_tracking_job` armed **and** `allow_carrier_egress` on | Outbound carrier API calls. |

Everything before step 6 in §5.6 is reversible: stop the worker, delete the rows it wrote.

**Take a database backup before §5.6 step 7 and step 11.** See `docs/DATABASE_BACKUP.md`.

### 6.4 Turning ONE cron off, without freezing the rest

The live case: **stop the daily MRP auto-draft pass.** It creates draft purchase orders and
work orders for every active company every day, and someone has to triage them (§8.6). Leaving
it off is a supported, permanent configuration.

**The value.** On `werco-worker` → Variables:

```
WORKER_CRON_JOBS=all,-run_mrp_auto_draft_job
```

Redeploy, then confirm on the startup log — this line is the receipt:

```
ARQ worker cron: 1 of 12 cron jobs SUPPRESSED by WORKER_CRON_JOBS='all,-run_mrp_auto_draft_job': cron:run_mrp_auto_draft_job
ARQ worker cron: 11 job(s) armed, times in UTC
```

(`UTC` is whatever the container's zone resolves to — see point 1 below.)

| What you see | Verdict |
|---|---|
| `1 of 12 … SUPPRESSED`, naming `cron:run_mrp_auto_draft_job` | **Correct.** |
| `0 of 12` / no SUPPRESSED line at all | The variable did not take. The cron is still armed. **STOP.** |
| `ValueError: WORKER_CRON_JOBS names unknown cron job(s): -…` and a crash loop | Typo in the excluded name. The refusal is working as designed — an exclusion that matches nothing would have left the job armed. **But while it crash-loops there is NO worker at all, not just that one cron off:** see below. Fix the spelling and redeploy. |
| `ValueError: … uses all as a cron NAME …` and a crash loop | A stray comma, usually `all,` left behind after deleting the exclusion. Same blast radius as the row above. Delete the comma (or set the value to bare `all`). |
| More than 1 suppressed | You excluded more than you meant to, or the variable still holds an older allowlist. |

**A refused value takes the whole worker down — budget for that before you paste.** The parse
happens in the `WorkerSettings` class body, i.e. at *import* of `app.worker`, so the `ValueError`
escapes before arq ever constructs a Worker and the process exits non-zero. `railway.toml` sets
`restartPolicyType = "on_failure"` with `restartPolicyMaxRetries = 3` and deliberately no
`healthcheckPath`, so the deployment starts, dies, retries three times and ends **CRASHED with no
running worker behind it**. That is *not* the same containment as `WORKER_CRON_JOBS=none` (§5.2),
which leaves a healthy worker draining the queue — here **every** cron stops, including
`relay_pending_notifications_job` (the 5-minute sweeper that is the delivery backstop for
notifications whose after-commit enqueue was lost) and `poll_tracking_job`, **and the
enqueue-driven queue stops draining too**: emails, webhooks, receiving labels, WO completion
signals. If the correct spelling is not immediately to hand, set `WORKER_CRON_JOBS=none` to get a
healthy worker back first, then re-apply the exclusion.

**Do not use an allowlist of the other eleven.** It arms the same eleven crons today and rots
at the next release: a thirteenth cron added to `ALL_CRON_JOBS` would silently never register
on this worker, and nothing in the log would say so — "I enabled the cron and nothing
happened", one deploy late. The `-` form subtracts from whatever the release declares, so
future crons arrive armed. That is the whole reason exclusion exists.

**Two things that are not obvious:**

1. **Crons fire on container-local time, which is UTC unless `TZ` is set.** So the "6 AM" MRP
   cron is **01:00 Central**. If you are timing this change around a business day, that is the
   window you are actually moving — and if you set `TZ=America/Chicago` later, every cron time
   in this runbook shifts.
2. **This disables the SCHEDULE, not the capability.** `run_mrp_auto_draft_job` stays in
   `WorkerSettings.functions`, so the worker still knows how to run it; only the timed trigger
   is gone. A one-off pass can still be enqueued by name against the running worker:

   ```bash
   railway ssh --service werco-worker --environment production --project "$RAILWAY_PROJECT_ID" \
     python -c "import asyncio; from app.core.queue import enqueue_job; \
                print(asyncio.run(enqueue_job('run_mrp_auto_draft_job')))"
   ```

   **Same blast radius as one cron firing** — every active tenant, real draft POs/WOs, real
   `mrp.completed` emails to managers, and §6.2's re-run caveat applies (a worker restart
   mid-run re-executes it from the start, i.e. a second set of drafts). Treat the first one as
   §5.6 #11 tells you to: on a morning someone can review the output, with the follow-up count
   query ready. **This command is derived from the code, not from a run against production** —
   nothing in this repo has ever enqueued this job by hand. Try it once on staging first if you
   have one.

**What MRP still does, and what it stops doing.** Planning is unaffected: `POST /mrp/runs`
(ADMIN / MANAGER / SUPERVISOR, the MRP page) runs a tenant-scoped MRP pass in-request and
produces requirements, actions and shortages exactly as before. What stops is the *automatic
drafting* — `MRPAutoService` is reached from **no** HTTP endpoint, only from the worker job, and
`POST /mrp/actions/{id}/process` merely marks an action processed and tells you to create the
PO or WO manually. So with this cron off, MRP becomes review-then-act-by-hand. That is the
trade §8.6 describes; make it deliberately.

---

## 7. Turning on the CI deploy

Only after the service exists and a manual deploy has worked. Repo → Settings → Secrets and
variables → Actions → **Variables**:

- `DEPLOY_WORKER_PRODUCTION = true`
- `DEPLOY_WORKER_STAGING = true` (only if you create `werco-worker-staging`)

Until these are set, both workflow steps are skipped and merging changes nothing. The
production step runs **after** the RELEASE stamp, so the worker's Sentry events carry the same
release tag as the API's. There is no health-verification step for the worker because it
serves no HTTP — verify it from its startup log (§5.5).

---

## 8. What this runbook does not cover, and what you must decide

1. **Every live fact about your Railway project.** I had no authorization to query it and ran
   no Railway command against it. §2 and §4 are the checks; the answers are yours to obtain.
   In particular: whether a Redis service exists, what variables `werco-api` actually has,
   whether a worker service already exists, and what `werco-api`'s real start command is.
2. **What Railway does to a failing healthcheck on your plan.** §4.3. Confirm from Railway's
   current documentation before creating anything.
3. **`AUDIT_ARCHIVE_DIR` needs durable storage.** Railway services have no persistent volume by
   default. `archive_aged_audit_logs_job` writes NDJSON exports there monthly; on ephemeral
   disk they vanish with the container. Attach a volume or point it at object storage **before**
   arming that cron. It only exports and never deletes, so a lost export is a lost archive, not
   lost audit data — but it is still a compliance artifact you are supposed to have.
4. **Timezone.** UTC or Central? Unset means UTC, i.e. MRP at 01:00 Central and the late-WO
   email blast at 03:00 Central. Set `TZ=America/Chicago` if you want shop-local times.
5. **Whether the late-WO burst is acceptable at all.** If `L` is large because the shop has a
   long tail of stale released work orders, consider cleaning those up before arming
   `check_late_work_orders_job` rather than emailing 300 alerts about them.
6. **Whether `run_mrp_auto_draft_job` should run at all.** It is `AUTO_DRAFT`, not
   `AUTO_SUBMIT`, so nothing is sent to a supplier — but it creates records daily that someone
   must triage. If no one owns that triage, leave it off indefinitely: that is
   `WORKER_CRON_JOBS=all,-run_mrp_auto_draft_job`, **not** an allowlist of the other eleven.
   Leaving it off is a supported, permanent configuration — §6.4 has the procedure, the
   receipt to look for in the log, and what MRP does and does not still do without it.
7. **Replicas.** `numReplicas = 1` is in the config and must stay there. The cron scheduler is
   per-process: two replicas run every cron **twice**, meaning two sets of MRP drafts per day
   and every digest sent twice.
8. **The repo-root `railway.toml` hazard.** Because the worker's config sits at the repo root,
   any service that has an **empty Root Directory** and is ever deployed **from the repo root**
   would read it and start running arq. CI deploys `werco-api` from `backend/` and
   `werco-frontend` from `frontend/`, and `backend/tests/test_worker_deploy_config.py` asserts
   it keeps doing so. **If you ever enable Railway's GitHub integration on `werco-api`, set its
   Root Directory to `backend` first.**
9. **`--forwarded-allow-ips=*`** on the API remains an open finding from an earlier audit,
   unrelated to this work.

---

## 9. Quick reference

```bash
# Where does the API think its queue is?
curl -s https://<api>/health/ready | jq '.checks.job_queue_redis'

# What is in the queue? (from inside the container -- redis-cli is NOT in the image)
railway ssh --service werco-api --environment production --project "$RAILWAY_PROJECT_ID" \
  python -c "import os,redis; r=redis.from_url(os.environ['REDIS_URL']); print(r.zcard('arq:queue'))"

# Read-only variable dump (contains secrets -- do not share)
railway variables --service werco-api --environment production --project "$RAILWAY_PROJECT_ID"

# Worker logs
# (railway logs has NO --project flag -- link the project once, first)
railway link --project "$RAILWAY_PROJECT_ID"
railway logs --service werco-worker --environment production

# Kill all scheduled work immediately, keep request-driven jobs
#   Railway -> werco-worker -> Variables -> WORKER_CRON_JOBS=none -> redeploy

# Kill ONE cron, keep the rest (and keep future crons arriving armed) -- see §6.4
#   Railway -> werco-worker -> Variables -> WORKER_CRON_JOBS=all,-run_mrp_auto_draft_job -> redeploy
```

| Fact | Value |
|---|---|
| Queue name | `arq:queue` (a Redis **sorted set** — `ZCARD`, not `LLEN`) |
| Job body TTL | ~24 h (`arq:job:<id>`); expired jobs are logged `job … expired` and discarded |
| Registered job functions | 23 |
| Declared crons | 12 |
| Cron timezone | container-local; **UTC unless `TZ` is set** |
| Sweeper bounds | 24 h max age, 2 min grace, 500 per 5-minute pass |
| Cron selector | `WORKER_CRON_JOBS` — unset/`all` / `none` / comma-separated names (allowlist) / `-name` exclusions (`all,-run_mrp_auto_draft_job`). The two shapes cannot be mixed; an unknown name, **negated or not**, is a hard startup error |
| Worker start command | `arq app.worker.WorkerSettings` |
| Worker healthcheck | **none, by design** — the worker serves no HTTP |
