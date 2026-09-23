# Hank, the AI shop teammate

Hank is the assistant inside Werco ERP/MES, named after the shop's yellow Labrador.
The product goal is dependable help with real shop work: understand the current
record, find the evidence, take an authorized action, and report its result. Hank
should be friendly and direct, with the dog's identity expressed through the name
and avatar rather than repeated jokes or pretending to be a human employee.

## Available now

Open **Hank** from the application header or with **Ctrl+.**. Starter questions
change with the current page: a work-order detail page offers a job briefing;
inventory and parts offer stock and part lookups; customer and purchasing pages
offer customer-order and PO searches. Incomplete starter questions let the employee
enter the relevant identifier before sending.

**My shift** opens a live, deterministic briefing of the employee's current
priorities without sending data to a model. Operators see jobs with their own
open time entries first; Quality starts with open NCRs; Shipping starts with
unshipped jobs due through the next two shop-local days. Admins, Managers, and
Supervisors start with shop blockers and late work. Purchasing and material
exceptions appear when the employee has the corresponding effective view
permission, including company role overrides.

Recorded issues assigned to the employee sort first within each section. Other
issues are labeled as shared; an open clock is not an inferred next assignment,
and a shipping due date is not a readiness or quality-release claim. Each
section shows at most five items, a count, links, and the time checked. Larger
results are labeled as partial; source scan caps make counts lower bounds.
Snoozed shared issues remain hidden until their snooze expires or the source
facts change. **Refresh** checks the live sources again. This checks
selected operational sources, not every ERP workflow.

Chat can look up work orders, operations and blockers, capacity and schedule
conflicts, inventory, customer orders, and general ERP records. A shop-briefing
request asks Hank to combine relevant lookups and suggest next actions with links.
It is an on-demand answer from available tools, not a continuous monitor or an
exhaustive inspection of every module. The panel shows lookup activity, source
links, a stop control, and retry for a failed chat turn. `my_shift_briefing` uses
the same role-aware briefing sources as **My shift**.

**Tasks** provides six reviewed workflows:

- **Repeat a job:** choose the source job, quantity and optional due date; review
  the plan summary, then create a draft using the existing duplication workflow.
  Production history and lot pins reset. Process-sheet revisions are resolved
  from current released records. Laser quantities derive from copied runs, and
  the result names any skipped operations or material ties.
- **Draft a purchase order:** choose a vendor and part, enter quantity, unit
  price and optional required date, then review and create a draft PO. The form
  starts with one line; the API and chat can prepare up to 50. It never approves,
  sends, or receives the PO.
- **Attach a PDF to a job:** choose an existing PDF and destination work order,
  review revision/status and both records, then attach it. An existing job or
  revision-history binding cannot be reassigned. This does not approve or read
  the file contents.
- **Receive a delivery:** review the selected PO lines, actual quantities, lots,
  certificate links and inspection choices, then post the complete delivery.
  Each line explicitly chooses inspection hold or dock-to-stock. No performed
  inspection is invented; Hank does not print labels automatically.
- **Report production:** report actual good/scrap quantities while clocked into
  an operation. Scrap requires a reason. An explicit NCR choice creates its
  quality record; an optional hold closes every open clock on that operation,
  including other employees, as disclosed in the preview. Reporting does not
  complete the operation or job.
- **Prepare a shipment:** reserve completed quantity in a pending shipment and
  link its packing slip. This does not dispatch goods, issue a CoC, purchase a
  carrier label or schedule a pickup.

The employee explicitly submits each reviewed action. Hank can also prepare a
proposal from an explicit chat request using `prepare_hank_task`; the review link
opens the saved proposal, and chat cannot execute it. Missing identifiers,
quantities or prices need employee input. The proposal is audited and committed
before another model call; it survives a later chat failure and can be recovered
in the task inbox without running the business action.

Task proposals and completion receipts persist under the active company,
employee and originating credential. Roles and effective permissions are
rechecked. Changed source records require a fresh proposal, and the original
request key/task id makes retries safe. **Refresh task status** resolves an
uncertain submission before another attempt. Required audit or receipt failure
rolls back the entire execution. Direct task forms work without an LLM call.

The persistent **Tasks** inbox brings saved proposals and receipts back after
closing Hank or signing in again. **Your tasks** offers **New task**, **New follow-up**, and a
**Task status** filter: All tasks, Awaiting review, Watching, Snoozed, Needs attention,
Completed, or Cancelled. Open a proposal that needs review or reopen a completed result.
**Refresh inbox** rechecks the list; **Load older tasks** fetches the next page
of 20; **Back to task inbox** returns from a proposal or receipt. Status filtering happens on the
server before pagination, so older matching tasks are not missed because the
latest page contains other statuses. Additional pages retain the same filter.
Refresh after an uncertain submission to find its actual saved status before
attempting another action. This is the employee's own task history under the
same company and credential; it does not show other employees' tasks or imply
that a reviewed proposal is executing in the background.

**New follow-up** saves an explicit personal watch on a selected work order:

- **Active blockers are cleared:** start with a job that has open or acknowledged
  blockers. Hank finishes when a later check finds none, including any blockers
  raised after the watch started. This does not establish that the job is ready
  or authorized for production.
- **A new PDF is attached:** optionally choose a document type. Existing matching
  attachments form the baseline; an older upload newly attached after the watch
  starts can qualify. The result links the PDF and reports its revision/status.
  Hank does not verify its contents or approve a certificate.

Review the selected condition, then **Start follow-up**. An active task shows **Waiting
for first check** until checked. **Check now** evaluates the live evidence;
**Snooze 1 hour** pauses checks, **Resume** resumes the same condition and baseline,
and **Stop follow-up** stops it while keeping its history. **Refresh follow-up
status** recovers an uncertain command or reloads worker progress. Reuse the same
creation attempt after an interrupted response to avoid starting a second watch.

With its worker schedule enabled, Hank checks due follow-ups periodically, at most
100 per five-minute pass. Check the displayed last-check time for freshness;
queue load and downtime can delay checks. Manual checks work independently of the
schedule. A matching condition finishes the watch once and saves a receipt plus
one in-app notification only for its owner when **Follow-up alerts** is enabled.
Muting alerts keeps completed results in the task inbox. Unchanged checks stay quiet; there is
no email or SMS. Active watches are capped at 50 per employee/company and creation
refuses source baselines larger than 500 matching records.
Failed scheduled checks show an error and wait 30 minutes before retrying; the
last-check time remains the last successful observation. **Check now** can retry
sooner. A stopped task that was never checked displays **Not checked**.

Follow-ups require an interactive account with effective work-order view access.
Current owner/company/access are checked every time. Lost access or a removed job
stops checks with **Needs attention** and a generic explanation, without an alert.
After access/source availability returns, **Resume** can retry the same follow-up.
Read-only company sessions can review saved history but cannot control watches.

**Preferences** lets each employee explicitly choose how Hank works with them in
the current company:

| Setting | Choices and effect |
| --- | --- |
| Briefing detail | **Standard** (default) shows at most five items per section; **Concise** shows at most three. Counts, freshness, partial-coverage labels and source links remain. |
| Show this area first | **Default for my role**, My work, Shop, Quality, Purchasing, Inventory or Shipping. Moves an available section first without hiding other sections or granting access. |
| Handoff format | **Bullets** (default) or **Checklist** for chat handoffs. A checklist is presentation, not a completion or approval record. A current explicit request takes precedence. |
| Follow-up alerts | On by default. Turn off to retain completed follow-up receipts without new in-app completion alerts. It does not stop checking or remove existing notifications. Re-enabling does not replay alerts for completed watches. |

Changes take effect after **Save preferences**. **Restore defaults** resets these
choices while retaining task/watch history and the audit trail. **Reload saved
preferences** replaces unsaved selections with the server's current values; use it
after a conflicting save or uncertain response. Opening the editor with no saved
choices returns defaults without creating a record. Editing requires an
interactive session in a writable company. Preferences contain only these typed
choices, not freeform instructions, learned employee behavior, or production policy.

Document search finds metadata by title, document number, filename, part id, or
work-order id. Results include revision and status, with at most ten records and
an indication when more match. A recent document can still be draft or obsolete;
Hank must not treat upload recency as manufacturing approval. Search does not read
PDF contents.

**Document intake** analyzes a batch of 1–5 PDF, Word (`.docx`) or Excel (`.xlsx`,
`.xls`) files (10 MB per file, 25 MB per batch). PDFs allow at most 25 pages;
Office files allow 25 text sections and 120,000 characters. It classifies purchase orders, vendor quotes,
packing slips, material certificates, drawings or other documents and extracts
printed fields and up to 50 lines with source/excerpt evidence and uncertainty.
Scanned PDFs use visual reading. Word paragraphs/tables and Excel sheet/cell values
are read as text, without running macros, calculating formulas or following links.
Cached formula results and unverifiable evidence remain uncertain. Image-only Word
documents need a PDF export for visual analysis. Resolve tracked Word revisions
before uploading, or export the final view as PDF. Legacy `.doc` files need saving as `.docx`.
Excel files with populated hidden rows, columns or sheets need a reviewed visible copy.
The employee can preview evidence, download the original, correct reviewed fields and choose exact
part, job, supplier, PO or receipt references. Matches are suggestions, filtered
by current source permissions. Identical files submitted through intake are
flagged; creating another copy requires explicit acknowledgement.

Saving a filing plan creates no ERP document. The separate reviewed execution
either creates a **draft document**, or explicitly **files and releases a
certificate and attaches it to a selected receipt**. The latter requires the
receipt's exact part/supplier and an empty certificate slot; it never replaces an
existing certificate or changes inspection/accepted quantities. PO/receipt
selections on a draft are retained evidence references, not replacement PO source
files or certificate bindings. Source changes invalidate the plan. Filing,
receipt linkage and required audits commit together, with a replayable receipt.

Intake sources, analysis, plans and receipts are private to the submitting
employee/company. The queued worker performs extraction; uploading alone does
not mean analysis finished. Refresh to recover an interrupted request. **Retry**
can requeue queued/failed work or reclaim analysis after 15 minutes; cancelling
prevents a late result from replacing the decision. Retrying an upload uses the
same request key, and stored bytes survive an uncertain database commit. Intake
requires an interactive Admin, Manager or Quality account and company AI egress
for analysis; **File PDF without analysis** remains a separate non-AI path.

**Upload documents** is available directly from Chat. After analysis, choose **Use in chat**
to attach the saved extraction (up to five documents). Remove an attachment to exclude it
from the next request. Attachments follow the current employee/company session and
are cleared when the conversation is cleared. Hank reads saved, source-cited evidence
in small batches; follow-up questions do not upload or extract the document again.

For an existing supplier PO, choose **Create purchase order**. Review the printed
PO number, dates, supplier, every part, stocking unit, quantity and unit price.
Select existing vendor/part records when there is no certain match. The separate
task review shows all proposed changes before creation. **Add to receiving** records
the imported order as sent, making its open lines available in Receiving; this
requires purchasing approval authority and does not email the vendor or receive
material. Leave it unchecked to create a draft. Tax, freight and currency conversion
need review in Purchasing; a differing printed total is flagged. The original file remains attached
as source evidence. An existing PO number or previously imported copy blocks another
creation, and retrying a completed task returns the same PO. All extracted lines
must be reviewed; an incomplete extraction cannot create a partial PO.

For a packing slip or received-material list, choose **Receive materials**. Hank
matches the printed PO and part numbers against open receiving lines and suggests
the delivered quantity, packing slip, lot and heat. Review the source, select or confirm
the PO, resolve unmatched lines and units, and explicitly choose inspection for each
received line. **Review receiving task** creates the existing reviewed proposal;
only submitting that proposal posts receipts and updates receiving/inventory under
the usual permissions. Unknown or ambiguous quantities and unit conversions are not
invented. Repeated part/lot rows need manual review. A duplicate document or packing slip
with earlier receipts requires explicit acknowledgement of additional material.
The task retains its source document and version; changed source/PO data requires a new
review, and retrying the same completed task returns its original receipt.

**Operational evidence** provides readiness gaps, released document links and
prior-run notes, purchasing impact and same-part alternatives, shipping packet
links, and recorded lot/serial genealogy. Every report is a fresh, bounded read
with source links and coverage limits. Readiness checks recorded blockers,
required traveler evidence, inspections and permitted material/quality sources;
it never authorizes production. Supplier follow-up text is a copyable draft and
is never sent automatically. Prior-run notes are historical observations, and
document metadata is not a summary or approval of file contents.

**Handoffs** send an explicitly submitted job summary, completed/remaining work,
problems, quantities and document links to one selected employee in the same
company. Only sender and recipient can read it. The recipient acknowledges and
then completes it; the sender can cancel. These transitions retain evidence and
send private in-app notices, not email or SMS. Either participant can add up to
ten PNG/JPEG photos to an active handoff (10 MB and 20 megapixels each). Photos
are source evidence, not AI interpretations or approved instructions.

**Routines** save an ordered procedure of 1–12 supported steps. Admins, Managers
and Supervisors can draft/edit; Admins and Managers approve. Editing returns a
procedure to draft, and an existing run retains the approved version it started
with. An employee advances one step at a time with review notes or the required
completed task, intake or handoff evidence. Business actions still require their
own reviewed submission and current permissions. A routine approval does not
grant permission, approve product quality or execute unattended work. Receiving,
job-readiness and shipping templates are starting points, not seeded approvals.

The combined **Work** queue labels actual saved work as **Working**, **Waiting on
you**, **Waiting on someone**, or **Finished**, with refresh and links back to each
record. Waiting can mean another employee or an external event. Lists are bounded
and signal partial coverage. A
queued intake is waiting for worker processing; a saved task proposal or routine
step is waiting for an employee, not running in the background.

**Hold to talk** adds an editable transcript to the composer only when the
browser has local English speech recognition available. It never sends the
message or falls back to a remote speech provider. Unsupported browsers keep
typing/scanning available. **Scan job or operation label** accepts a keyboard
scanner or typed traveler code through the existing scanner resolver. A scan
selects context; it does not start labor or execute a task. Session/company changes
stop dictation and discard stale asynchronous results.

**File PDF without analysis** preserves the direct document-filing workflow:

1. An Admin, Manager, or Quality user with write access selects a PDF up to 25 MB.
2. The employee reviews the title, document type, revision, and optional notes.
3. If the form was opened on a work-order detail page and that job resolves,
   **Also attach to [job number]** is available and initially unchecked. The
   destination stays fixed while that form is open.
4. **Upload and release PDF** submits the existing document upload workflow.
   Success creates a new released document under the employee's identity and
   returns a link in Hank. An uncertain upload shows an error and asks the employee
   to check Documents before trying again.

The file is stored through the ERP document service, not sent to the chat model.
This form does not extract fields, interpret drawings, or replace an existing
revision. Revision replacement and other associations remain in Documents. The
existing server role, tenant, and required-audit controls still decide whether an
upload succeeds; the UI does not grant authority.

Chat conversation history remains client-held; task proposals and receipts are
durable. Personal preferences are explicitly saved; chat conversation memory remains
client-held and is not inferred into saved employee instructions.
Background monitoring covers the two explicit follow-up conditions above, and
request-driven intake jobs analyze submitted documents. Explicit handoffs are the
employee messaging path. Hank does not set arbitrary reminders or operate every
ERP function; routine approval and certificate release have the narrow meanings
described above.

## Implementation order

The next eight capabilities are delivered in the accepted order:

| Order | Capability | Concrete behavior |
| --- | --- | --- |
| 1 | Document intake | PDF/Word/Excel analysis, source evidence and uncertainty, exact record suggestions, reviewed PO import/receiving/filing and PDF certificate-to-receipt linkage. |
| 2 | Readiness | Live gaps and source coverage before work; no automatic production authorization. |
| 3 | Guided receiving | Reviewed actual quantities, trace details and inspection choices, with atomic delivery receipts and stock effects. |
| 4 | Fast production reporting | Reviewed good/scrap, optional NCR/hold, job/operation scan, and local-only browser dictation where supported. |
| 5 | Shipping preparation | Existing packet evidence and reviewed pending shipments; no dispatch or CoC issuance. |
| 6 | Shop knowledge | Released source links, prior-run observations and recorded lot/serial genealogy. |
| 7 | Purchasing impact | Outstanding supply, same-part alternatives, potential downstream jobs and unsent supplier-message drafts. |
| 8 | Handoffs | Explicit participant messages, acknowledgement/completion and verified source photos. |

The combined Work queue and approved routines connect these capabilities while
preserving each record's permissions and required employee action.

The original five accepted priorities were implemented first, in this order:

| Priority | Employee experience | Implementation status / next work |
| --- | --- | --- |
| 1 — Shift briefings | “Here’s what needs your attention today,” tailored to the employee's role and recorded ownership. | Implemented: live, permission-scoped sections, personal open-clock jobs, shop/quality/material/purchasing exceptions, shipping due-date review, freshness and partial-coverage labels. No inferred next-job assignment or background polling. |
| 2 — Complete task workflows | “Prepare a repeat job,” “Draft this PO,” or “File this material cert.” Hank previews exact changes, executes the authorized submission, and links the result. | Implemented and expanded: six reviewed task kinds plus separate PDF intake/filing. Chat may prepare audited task proposals but cannot execute them. Includes caller/credential identity, source-freshness checks, required audits, atomic receipts and safe retry/recovery. |
| 3 — Follow-through inbox | See what needs employee input and what finished; reopen saved proposals and result receipts. | Implemented: persistent personal task inbox, server-side status filtering and pagination, refresh/recovery, current-permission checks, and source/result links. No background execution is implied by a saved proposal. |
| 4 — Useful alerts | Hear when blockers clear or a new matching PDF arrives. | Implemented: explicit personal work-order watches, periodic/manual checks, one-shot owner-only in-app receipts, current-permission checks, and snooze/resume/stop controls. Deployment must enable the Hank-only worker schedule. No blanket job readiness or certificate approval claim. |
| 5 — Remembered preferences | Hank remembers preferred briefings, work areas, handoff formats and alert choices. | Implemented: explicit personal/company preferences, save/reset/reload controls, version checks and required audits. Choices change presentation and in-app alerts only; they do not grant access or alter production policy. Company learning does not become an employee's personal instruction. |

For each new task, show **what Hank found**, **what will change**, **what requires
the employee**, and **what actually completed**. A permission failure should name
the required role or next workflow. An interrupted or timed-out write needs an
“outcome unknown” recovery path, not an automatic retry that could create a second
record. A proposed owner or due date is never a recorded assignment until the ERP
confirms it.

## Implementation boundaries

- The existing MCP gateway already describes and dispatches broad authenticated
  ERP operations. It is a useful foundation for future actions, but it is not
  connected to Hank's chat tool registry. MCP read/write annotations are hints;
  they do not supply an employee-intent or approval workflow.
- The ERP already has separate Action Inbox sensors, a morning brief, contextual
  recommendations, and allowlisted apply/automation machinery. Company snapshot
  context can already include tenant-level learned preferences. These are useful
  foundations. Hank's own stores record reviewed actions, explicit personal
  follow-ups and typed employee preferences. See
  [Always-On AI](AI_ALWAYS_ON.md).
- Hank has sixteen read tools and one proposal-preparation tool. Tenant scope is
  injected by the server and cannot be chosen by model arguments. Chat can save
  audited `awaiting_review` proposals but cannot execute business actions. The
  document upload form calls the existing write route after user submission.
- `/api/v1/hank/briefing` is an authenticated read, with each source gated by
  effective module permissions. It reuses Operations Inbox sensors and triage,
  adds personal open-clock and shipping due-date reads, and makes no database
  changes. It works without an Anthropic key or LLM call. The first briefing
  phase introduces no migration, environment variable, background job, or
  deployment procedure.
- `/api/v1/copilot/chat`, `Copilot*` contracts, the OpenAPI tag, `COPILOT_*` tuning
  variables, and `copilot_chat`/`copilot_panel` telemetry keys retain their names.
  Prompt `copilot_chat` is version 1.7.0. Task workflows require additive migration
  108 for `hank_tasks`, with matching PostgreSQL RLS/grant guards in migration
  and model bootstrap. No new role or environment setting is introduced; see
  [deployment ordering](DEPLOYMENT.md#hank-task-storage).
- The inbox/history increment reuses migration108 task storage. It introduces
  no schema, environment, background-worker or deployment-procedure change.
- Follow-ups also reuse migration108 and add no environment variable. They add
  `check_hank_watches_job`, enabled in production through the existing selector's
  exact one-job allowlist; other schedules stay disabled. API, worker and frontend
  must match. See [follow-up deployment](DEPLOYMENT.md#hank-follow-up-worker).
- Personal preferences add migration 109 after 108, with the same PostgreSQL
  default-deny protections. Reads without saved choices create no row. The worker
  reads the current alert choice when completing a watch; task results and audits
  persist even when alerts are muted. No new environment variable or cron is added.
  See [preference deployment](DEPLOYMENT.md#hank-personal-preferences).
- The expansion adds migration **110_hank_workflows** for intake batches/files,
  handoffs, routines and runs. It seeds no approved procedures or work. The
  request-driven `process_hank_intake_file_job` needs a matching worker and shared
  storage but no new cron/environment variable. Document analysis uses versioned
  `hank_document_intake` prompt 1.2.0 through the shared model router, company
  AI-egress gate and usage telemetry. Direct reports, forms, handoffs, routines
  and queue reads do not require an LLM. See [deployment](DEPLOYMENT.md#hank-workflows-and-document-intake).
- Document chat, receiving and PO import reuse those tables without a new migration. Deploy the
  matching API, worker and frontend so new extraction preserves printed units and
  delivered quantities. The default Sonnet tier handles chat and document extraction;
  existing explicit model overrides remain available. Stable tool/system prefixes
  and growing chat context use five-minute prompt caching. Office extraction sends
  bounded native text in one model request. Source previews and receiving/PO matching
  need no LLM call, and database read transactions close before model calls. Actual cache
  reads, writes, tokens and estimated costs remain visible in AI Usage & Cost.

See [API](API.md#hank-ai-shop-teammate),
[permissions](RBAC_PERMISSIONS.md#hank-ai-chat-and-pdf-filing),
[configuration](ENVIRONMENT_VARIABLES.md#hank-read-only-ai-chat), and
[the existing MCP gateway](MCP.md).
