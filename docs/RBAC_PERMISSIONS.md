# Role-Based Access Control (RBAC) Documentation

## Overview

Werco ERP implements a comprehensive RBAC system with 7 predefined roles. Permissions are enforced both on the backend (API endpoints) and frontend (UI elements).

## Roles

| Role | Description | Use Case |
|------|-------------|----------|
| **Admin** | Full system access | System administrators, IT staff |
| **Manager** | Department-wide access with approval capabilities | Department managers, production managers |
| **Supervisor** | Team-level access with create/edit permissions | Shift supervisors, team leads |
| **Operator** | View and update assigned work | Machine operators, production workers |
| **Quality** | Quality-specific actions | Quality inspectors, QC staff |
| **Shipping** | Shipping operations | Shipping clerks, warehouse staff |
| **Viewer** | Read-only access | Auditors, executives, guests |

## Access enforcement model

Permissions are enforced at two layers, and the two layers **intentionally differ for reads**:

- **Writes / state changes** (Create, Edit, Delete, Approve, Release, Send, Adjust, Transfer, Complete, Inspect, …) are enforced **server-side** via the `require_role` dependency on the endpoint. These are the authoritative access controls and match the matrix below.
- **Operational/domain reads** — the **View** rows for the operational modules below (e.g. Work Orders, Parts, BOMs, Routings, Inventory, Purchasing, Receiving, Customers, Quotes) — are **tenant-scoped** (every query is filtered to the caller's active company via `get_current_company_id`) and are available to **any authenticated user within that tenant**. The list/detail GET endpoints depend on `get_current_user` only and do **not** restrict reads by role. The **View** columns therefore describe the *intended in-app navigation* (which the frontend gates for usability), not a server-enforced read restriction. This is the current intended design: **read-broad / write-restricted**.
- **Administrative / governance reads are the exception and _are_ enforced server-side:** **Users** (`require_role([ADMIN, MANAGER])`), **Admin Settings** (`ADMIN`), and **Audit Logs** (`require_role([ADMIN, MANAGER])`).
- **Bulk data export is its own access category, not a domain read, and _is_ enforced server-side** — `require_role([ADMIN, MANAGER])` plus an `EXPORT` audit row on every one of them. See [Bulk data export is not a domain read](#bulk-data-export-is-not-a-domain-read) immediately below, and [Bulk Data Export](#bulk-data-export) in the matrix for the route list.

> If the business requires least-privilege on domain reads (e.g. hiding vendor pricing / PO financials from Operator/Quality/Shipping at the API), enforce it **uniformly** by adding `require_role` to the read endpoints across modules, with authorization tests — not per-router. Until then, treat the **View** columns for operational modules as UI-visibility, not as a server-enforced control.

### Bulk data export is not a domain read

**Handing over a whole dataset as a file is server-gated to Admin / Manager and audited, and is
deliberately outside the read-broad rule above.** The read-broad paragraph still stands exactly as
written — it is, and remains, the rule for domain reads. This is a separate category sitting beside
it, not an amendment to it.

**Why the two are different exposures.** A domain read returns **one record** through the UI, to
someone who had to navigate to it and who can carry away only what is on the screen in front of
them. A bulk export returns the **entire dataset as a file, in a single request** — the parts master
with `standard_cost`, the full inventory valuation, every PO line with `unit_price` and vendor,
every quote with its customer contacts. That is a disclosure event rather than navigation: the file
leaves the system, keeps its value after the account is disabled, and is the shape a departing
employee or a compromised low-privilege session actually uses. Auditing follows from the same fact —
if a dataset can leave in one request, there has to be a record of it having left.

**Why this is not the per-router least-privilege the paragraph above warns against.** The objection
there is to tiering *reads* one router at a time, which produces a system where the same record is
visible on one screen and refused on another. This carve-out does the opposite: it changes nothing
about who can open a record on screen (a Supervisor, Operator, Quality, Shipping or Viewer user
still reads every one of these datasets in the UI), and the gate is applied **uniformly to every
bulk-export surface in the system at once** — which is precisely the "uniformly, not per-router"
discipline being asked for. The trigger is the shape of the response, not the identity of the
router.

**The boundary: one record is not a dataset.** Single-record document routes are on the other side
of the line and are deliberately untouched — a CoC PDF, one quote PDF, one nest drawing, one
work-order traveler, one estimate breakdown, a kiosk document view. Two of those are load-bearing on
the shop floor (an Operator must be able to pull the traveler and open the controlled drawing at the
point of use), so gating them would break production, not tighten it. The test for whether a new
route falls under this rule is: *does one request return a whole table's worth of rows as a file?*
If the path carries a record id, the answer is no. Where a surface already sits at a **stricter**
tier, that tier stands — this rule is a floor, never a loosening.

## Permission Matrix

### Work Orders

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | ✓ | | | | |
| Start operation (office verb) | ✓ | ✓ | ✓ | | ✓ | | |
| Start operation (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Complete (office verb) | ✓ | ✓ | ✓ | | ✓ | | |
| Complete (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Hold / resume operation (shop-floor verb) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Resolve / dismiss the blocker behind a hold | ✓ | ✓ | ✓ | | | | |
| Approve labor (TimeEntry) | ✓ | ✓ | ✓ | | ✓ | | |
| View material ties | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Tie / edit / untie material | ✓ | ✓ | ✓ | | | | |

> **Delete — code now matches the matrix (Admin + Manager).** `DELETE /api/v1/work-orders/{id}`
> (`app/api/endpoints/work_orders.py`) was previously gated **stricter than this matrix** —
> `require_role([ADMIN])` — while the **Delete** row above already listed Admin **and** Manager. The
> gate is now `require_role([ADMIN, MANAGER])`, so a Manager can soft-delete a work order as documented.
> Soft/hard-delete and restore behavior is otherwise unchanged (default soft delete; `hard_delete=true`
> only for draft/cancelled WOs). See `docs/API.md` → Work Orders.

> **Approve labor — endpoint mapping (Batch 11B / G5-A).** The shop-floor labor sign-off
> `POST /api/v1/shop-floor/time-entries/{id}/approve` and `…/unapprove` (which set / clear
> `TimeEntry.approved` + `approved_by`, the field the opt-in `REQUIRE_APPROVED_LABOR_FOR_COST` flag
> keys labor-cost rollups on) are enforced **in code** to this row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`). In
> addition to the role gate, **self-approval is
> forbidden**: a user cannot approve or unapprove their **own** TimeEntry (segregation of duties for
> the labor-cost gate) — that returns **403** even for an approver-role user. A cross-tenant id returns
> **404**. Both actions are audited (`time_entry_approve` / `time_entry_unapprove`).

> **Start operation — the office verb is now gated; the shop-floor verb is not** (`feat/work-center-op-pool`).
> The **Start operation** row above is split across two endpoints exactly like the **Complete** row is:
> - `POST /api/v1/work-orders/operations/{id}/start` — the **office** verb — is enforced in code to
>   `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`. It previously had **no role gate at all**
>   (bare `get_current_user`), so any authenticated tenant user — **Viewer and Shipping included** —
>   could stamp `actual_start` / `started_by`, move the work order to IN_PROGRESS, and write rows onto
>   the tamper-evident chain from a page they were only meant to read. That gap predates this work; it
>   was masked while the office path ran a **stricter** predecessor gate than the shop floor and so
>   refused nearly every operation such a user could reach. With same-work-center operations promoting
>   to READY together on a pooled work order (`docs/API.md` → Work Orders → "READY promotion: a sequenced ROUTING or a DISPATCH POOL"), the
>   operations a Viewer can reach are exactly the ones the gate no longer refuses, so the hole became
>   reachable. The gate **matches its office twin** `…/operations/{id}/complete` —
>   being able to start an operation but not complete it, or the reverse, is incoherent — which is why
>   QUALITY is included here for the same reason it is there.
> - `PUT /api/v1/shop-floor/operations/{id}/start` — the **operator** verb — is deliberately
>   **unchanged** and stays open to any authenticated user, per the note directly below. Operators
>   start work there and on the kiosk, never through the office page, so **no operator lost a
>   capability**; the Operator ✓ on the Start operation row is that verb.
>
> **The shop-floor rows above are ticked for every role on purpose — that is what the code does, not
> an aspiration.** `PUT /shop-floor/operations/{id}/start` and `POST /shop-floor/operations/{id}/complete`
> both take a bare `Depends(get_current_user)`, so a **Viewer** or **Shipping** user can clock in and
> then complete an operation — booking it at target quantity, receiving finished goods, consuming tied
> material and writing chain rows. The new labor-evidence gate on floor completion does **not** close
> this: it is a record-quality check, not an authorization one, and the same caller satisfies it with
> one extra clock-in request. This is **pre-existing** and was not introduced by the pooling work, but
> pooling widens its reach (1 reachable operation on a batch WO becomes N). Ticking the cells honestly
> is deliberate: a matrix that claimed a gate here would be worse than one that admits there isn't one.
> Closing it means `require_role([...])` on those two verbs — an open owner decision, tracked, not done.
>
> No role gained anything. The office verb has **no UI caller today** — the app's only Start control
> (`ShopFloorSimple`) calls the shop-floor verb — so the gate closes an API-reachable hole rather than
> hiding a button; wire any future office Start control to `work_orders:edit` so the hidden control
> and the refused call agree. See `docs/API.md` → Work Orders.

> **Hold / resume are operator-facing; resolving the blocker behind a hold is not.** Both rows above
> are new to this matrix; **neither gate changed**. `PUT /api/v1/shop-floor/operations/{id}/hold` and
> `PUT /api/v1/shop-floor/operations/{id}/resume` (`app/api/endpoints/shop_floor.py`) take a bare
> `Depends(get_current_user)` — no role gate, matching the other operator write verbs (clock-in,
> production, complete, reduce-production) — and are tenant-scoped (a cross-tenant id → **404**) and
> audited (resume writes a **`STATUS_CHANGE`** row carrying old→new status,
> `extra_data.transition = "resume_operation"`, and the ids of any blocker still open at resume). The
> **blocker** verbs are a tier up, and on a different router: `PUT /api/v1/work-order-blockers/{id}`
> (acknowledge / assign / dismiss) and `POST /api/v1/work-order-blockers/{id}/resolve`
> (`app/api/endpoints/work_order_blockers.py`) are `require_role([ADMIN, MANAGER, SUPERVISOR])` — the
> Work Orders **Edit** row. Being mounted outside `/api/v1/shop-floor` also puts them outside the
> kiosk path fence, so a badge-minted crew-station token is **403** there whatever badge was scanned.
>
> **That asymmetry is the design, and it is why resume is safe to leave role-open.** Resuming moves
> an *operation status*; resolving a blocker closes the *quality/material finding* that stopped the
> job, and only the second is a supervisory judgement. So resume deliberately does **not** resolve
> the blocker — it returns the still-open ones on the response (BLK-4, warn-and-record) so operation
> status and blocker status can be seen to diverge rather than diverging silently.
>
> **What changed is reach, not authorization.** The crew-station queue read now surfaces the work
> center's `ON_HOLD` operations (`docs/API.md` → Shop Floor → "Held work"), and resume sits inside
> the kiosk path fence on none of its deny lists, so a **badge-scanned Operator can take a job off
> hold from the shared terminal** instead of walking to a desk. The kiosk can already *place* a hold;
> a control with no inverse on the same terminal was the defect. Any authenticated user could always
> call resume — no role gained anything here either. See [docs/KIOSK.md](KIOSK.md) → Held work and
> resume.
>
> **Reach is bounded by two write-side refusals, not by a role.** Because resume is role-open, the
> limits on what it can do have to live in the endpoint: it **409**s on a cancelled (soft-deleted)
> laser nest's tombstone operation (resuming one would undo a soft delete — invariant 3), and it
> **restores rather than promotes**, flooring at `PENDING` and delegating any lift to `READY` to the
> shared promotion rule. So a role-open verb still cannot perform the **release** that
> `POST /work-orders/{id}/release` owns — a hold placed on a `PENDING` operation, or on one whose work
> order is still `DRAFT`, resumes to `PENDING` and stays off the board.
>
> **A shared crew station is not an identified caller, and the held payload reflects that.** The
> station token is a 24-hour shared-PIN credential on an unattended tablet with no idle logout, so
> the blocker's free text (`note`, `title`) is **not sent** to it — only `category`, `severity`, the
> attribution and two booleans. The same read over a **user session** returns the full block. This
> mirrors the wallboard's standing rule for unattended shop screens (no NCR titles/descriptions);
> see `docs/API.md` → Shop Floor → "Disclosure (`held`)".
>
> That withholding is scoped to the **blocker's** free text. The same queue read **does** send the
> job's five office-authored guidance fields (work-order notes / special instructions, and the
> operation's description / setup / run text) to a station principal — a recorded exception (owner
> decision, 2026-08-14), on the reasoning that planning text exists to reach the person doing the
> work, and explicitly **not** a relaxation of the rule above. No role or permission changed. See
> `docs/KIOSK.md` → "Disclosure: this free text does reach a crew station".

> **Release stays the authorization boundary even though a read can now promote operations.** READY
> promotion runs from a reconcile-on-read seam (one of four) — so a work order released before the
> pooling rule shipped repairs itself when anyone loads it (`docs/API.md` → Work Orders → "A read
> heals a stranded work order"). That read is performed under the **reader's** own token, and the
> **View** row above is ticked for every role, so a Viewer's `GET /work-orders/{id}` can flip
> operations PENDING → READY. Two things keep this off the **Release** row: the promotion **never
> touches a DRAFT work order**, so no read by any role can put unreleased work on the floor's board —
> `POST /work-orders/{id}/release` (Admin / Manager / Supervisor) remains the only way work reaches
> the board the first time, and it remains the record of *who* authorized production — and promotion
> grants no capability the predecessor gate did not already allow, so the reachable-operation
> observation in the note above is unchanged in kind by it. Reconcile-on-read already performed
> reader-triggered writes (completion rollups, audited to the requesting user); this adds no new role,
> no new endpoint, and no new gate.

> **Operator-qualification gate is record-only (Batch 11C / G5-B).** `POST /api/v1/shop-floor/clock-in`
> and `PUT /api/v1/shop-floor/operations/{id}/start` stay **operator-facing** — open to **any
> authenticated user** (`get_current_user`), no new role gate. The G5-B qualification gate (no active
> `SkillMatrix` entry at level ≥ 2 for the work center, or a missing/expired required
> `OperatorCertification`) **only records** a tamper-evident `audit_log` row
> (`OPERATOR_QUALIFICATION_EXCEPTION`) + a warning event and surfaces a `qualification_exceptions`
> array on the response; it does **not** gate the operator's role or block the clock-in / start. The
> gate's lookups are tenant-scoped (every skill/cert/work-center query filters the active company).

> **Over-count correction — operator self-service (no new role) + a role-gated office twin.**
> `POST /api/v1/shop-floor/operations/{id}/reduce-production` (walk back good-count an operator
> OVER-reported on their **own unapproved** labor — open clock-in first, then their own earlier
> unapproved sessions — before completion; a miscount fix, not scrap) is
> **operator-facing**, open to **any authenticated user** (`get_current_user`), matching the other
> operator write verbs (clock-in, production, complete, hold). It adds **no new role or permission**.
> Authorization is by **evidence, not role**: the walk-back is bounded to the caller's **own
> unapproved** entries on the operation (crew-safe — never another operator's count; **approved**
> labor is excluded — approval is the immutability boundary, G5-A) and is refused **409** once
> the operation is COMPLETE or the WO is terminal (post-completion corrections stay an
> office/supervisor task — and that referral is now honest, since the office twin below **accepts** a
> COMPLETE operation where it used to hit the identical refusal). It is
> tenant-scoped (a cross-tenant id → **404** before any mutation) and writes a tamper-evident
> `audit_log` row (action `reduce_operation_production`, old→new quantity + the operator-supplied
> reason). See `docs/API.md` → Shop Floor → "Over-count correction".
>
> The **office twin** `POST /api/v1/work-orders/operations/{id}/reduce-production` is enforced
> **in code** to `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above
> (an Operator gets **403**): it corrects recorded production on **any operator's unapproved**
> labor record, which is a supervisory power, not self-service. No clock-in is required. Approved
> entries stay excluded on this path too — the front door for signed-off labor is
> `POST /shop-floor/time-entries/{id}/unapprove` (the audited Approve-labor row, which forbids
> self-unapproval), then reduce. Same tenant-scoped **404** and tamper-evident audit row as the
> shop-floor verb.
>
> **The office/operator split on a COMPLETE operation is a role decision, not a mechanism detail.**
> The office verb passes `allow_completed_operation=True` and the operator verb does not, so a
> completed operation is correctable **only** by Admin / Manager / Supervisor; a **terminal work
> order** is refused on both, with its own distinct message so neither verb makes a referral the
> other cannot honor. The refusal being relaxed dated from a production incident and was justified
> on "downstream inventory / cost / FG effects have fired and cannot be walked back" — the reasoned
> **material return** above is that walk-back, which is what makes the relaxation a supervised
> correction rather than a loosened control. Note that the two powers travel together by design:
> the same tier that can lower a completed operation's count is the tier that can hand the material
> back, so neither half can be exercised without the authority to do the other. The supervisor's
> optional note is recorded on
> the **audit row only**, never written onto another operator's labor record. In the UI this is
> the **Correct count** action on the work-order detail page, gated on `work_orders:edit`. See
> `docs/API.md` → Work Orders → "Over-count correction … (supervisor/office)".

> **Renumber a part — endpoint mapping.** `POST /api/v1/parts/{id}/renumber` (change a part's
> number in place, retiring the old one) is enforced in code to `require_role([ADMIN, MANAGER])` —
> **deliberately narrower than the Parts Edit row above**, which reaches Supervisor. Renumbering is a
> controlled change to an article's identity under AS9100D 8.5.2, so it sits with `POST
> /parts/{id}/revision` (ADMIN/MANAGER) and `DELETE /parts/{id}` (ADMIN) — its sibling identity verbs
> on the same router — not with the tier that edits a description. **A Supervisor gets 403.**
>
> The client gates the control on the new `parts:renumber` permission, held by `platform_admin`,
> `admin` and `manager` only, so the hidden control and the refused call agree. Note the companion
> read, `GET /parts/{id}/renumber-impact`, is open to any authenticated tenant user (like the
> backflush readiness read): it discloses facts about this company's own catalog, and a screen that
> could not show them would be asking for a decision nobody could make. It is a **pure read** — no
> audit row, no event, structurally (the service takes no `AuditService`).
>
> Two things are RBAC-relevant beyond the gate. First, the Item Number input on the Materials screen
> and the Part Number input on Part Edit **stay disabled for every role, including admin** — enabling
> either would route a rename through the blind-`setattr` `PUT`, where it would carry no reason, no
> audit identity, no collision check and no repair of the operation links the number stands in for.
> The dedicated verb is the only door. Second, the write is on the tamper-evident chain as one
> `resource_type='part'` UPDATE row filed under the **OLD** number, carrying the required reason —
> so "who renumbered this, from what, and why" is answerable by searching the number someone actually
> holds on paper. See `docs/API.md` → Parts → "Renumbering a part".

> **Inline due-date edit on the work-order LIST — endpoint mapping.** Rescheduling a job from the
> Work Orders list (the pencil on the Due Date column) is not a new verb: it is `PUT
> /api/v1/work-orders/{id}` carrying `due_date` plus the row's optimistic-lock `version`, so it is the
> Work Orders **Edit** row above — `require_role([ADMIN, MANAGER, SUPERVISOR])`, which `require_role`
> itself also admits superuser and platform-admin to. The client gates the pencil on
> `work_orders:edit`, whose holders are exactly that set, so the hidden control and the refused call
> agree; **an Operator, Quality, Shipping or Viewer user gets 403**, and hiding the control is not the
> enforcement.
>
> Two things about it are RBAC-relevant beyond the gate. First, unlike Duplicate above, there **is** a
> status gate: changing a due date on a COMPLETE/CLOSED/CANCELLED work order is refused **409** by the
> endpoint, because that date is the promise date the job's delivery performance was scored against —
> no role can do it, the trio included. Both UI surfaces (the list and the work-order detail page) hide
> the pencil on a terminal WO to match. Second, the write is on the tamper-evident chain like any other
> header update: one `work_order` `UPDATE` row whose `extra_data.changes` carries the `due_date` old
> and new values, attributed to the acting user — editing from a list rather than the detail page
> changes nothing about what is recorded. See `docs/API.md` → Work Orders → "Due date on a finished
> job".

> **Duplicate a work order — endpoint mapping.** `POST /api/v1/work-orders/{id}/duplicate` (copy a
> job's *plan* — operations, laser nests, open material ties, re-snapshotted process-sheet steps —
> onto a new **DRAFT** work order) is enforced **in code** to `require_role([ADMIN, MANAGER,
> SUPERVISOR])` — the Work Orders **Create** row above. It reaches no wider than the trio already
> holds: it is a create verb (Create), it mints operations (`POST /work-orders/{id}/operations`, same
> trio), it mints nests (the laser-nest note below, same trio) and it mints material ties (the Material
> ties note below, same trio). **An Operator gets 403**, matching every other planning act.
>
> Three things about it are RBAC-relevant beyond the gate itself. First, there is **no status gate** —
> duplicating a COMPLETE job is the headline case — so read-broad/write-restricted is the only control
> in front of it; the tenant scope is what does the rest (a source outside the active company or
> soft-deleted → **404**, never 403, and never a "exists elsewhere" leak). Second, it **refuses** two
> conditions the create path would also have refused (**409** on a soft-deleted produced part, **409
> `PROCESS_SHEET_UNAVAILABLE`** on a sheet family with no released revision) — one button is not a
> licence to route around a gate a planner would have hit by hand. Third, every write it performs is on
> the tamper-evident chain: one `work_order` `log_create` carrying `source_work_order_id` /
> `source_work_order_number` (the duplicate holds **no FK** back to its source, so that row is the only
> record of the lineage) plus the deliberate omissions, and one row per copied nest and per copied tie,
> byte-parallel to the import and tie-creation paths. In the UI this is the **Duplicate** action on the
> work-order detail header and the Work Orders row/mobile-card actions, gated on `work_orders:edit` so
> the hidden control and the refused call agree. See `docs/API.md` → Work Orders → "Duplicating a work
> order".

> **Laser-nest manual entry + reference PDF — endpoint mapping.** Manually keying a laser nest and
> all per-nest mutations follow the Work Orders **Create / Edit / Delete** rows above —
> `require_role([ADMIN, MANAGER, SUPERVISOR])`: `POST /api/v1/work-orders/{id}/laser-nests/manual`
> (create), `PATCH /api/v1/laser-nests/{id}` (edit), `POST /api/v1/laser-nests/{id}/attach-document`
> and `DELETE /api/v1/laser-nests/{id}/document` (attach/detach the reference PDF), and
> `DELETE /api/v1/laser-nests/{id}` (soft-delete; the operation goes `ON_HOLD`). This matches the
> laser-nest **package import** endpoints — the per-WO pair (`…/{id}/laser-nest-packages/preview`
> and `…/import`) and the no-WO **standalone** pair
> (`…/laser-nest-packages/standalone/preview|import`, whose import creates a fresh part-less
> laser-cutting work order) — and the
> stateless PDF field-extraction endpoint `POST /api/v1/laser-nests/extract` (same ADMIN/MANAGER/
> SUPERVISOR gate; no DB write, no audit). The
> **exception** is the operator-readable inline PDF preview `GET /api/v1/laser-nests/{id}/document`,
> which is open to **any authenticated user** (`get_current_user`) so operators can view the shop
> drawing — read-only and still tenant-scoped (a cross-tenant or soft-deleted nest → **404**). All
> writes are audited; nests are soft-deleted, never hard-deleted. See `docs/API.md` → Laser Nests.

> **Dispatch run order — endpoint mapping.** Setting the order operators see work in is a planner
> act, gated to the Work Orders **Edit** row above —
> `require_role([ADMIN, MANAGER, SUPERVISOR])`, tenant-scoped:
> `GET /api/v1/shop-floor/dispatch-board` (the manager board — every active work center with its
> live queue, plus any deactivated work center still holding queued work as a flagged
> `is_active: false` read-only column; a **zero-write read**, no audit rows) and
> `PUT /api/v1/shop-floor/work-centers/{id}/run-order` (rewrite one work center's dense 1..N rank;
> a work center outside the active company or inactive → **404**, indistinguishable from missing).
> The rewrite writes **one** tamper-evident `audit_log` row per manager action — a `work_center`
> `log_update` carrying the old → new operation-id lists — not one row per operation. Operators
> **consume** the resulting order through the read-broad
> `GET /api/v1/shop-floor/work-center-queue/{id}` (unchanged gate: any authenticated user, or a
> crew-station kiosk token for its own work center) and can never set it. `run_order` is
> **advisory**: it orders and labels the queue and adds **no** gate — start eligibility stays
> entirely with the existing operation gates and predecessor rules. In the UI this is the
> **Dispatch Board** page (`/dispatch`), route-gated on **`work_orders:edit`** (admin / manager /
> supervisor) rather than the read-only `work_orders:view` that `/scheduling` uses. See
> `docs/API.md` → Shop Floor → "Dispatch run order" and [docs/KIOSK.md](KIOSK.md).

> **Scanner resolve-action is read-only and open to any authenticated user (A0.4).**
> `POST /api/v1/scanner/resolve-action` (the QR traveler / badge scan resolver,
> `app/api/endpoints/scanner.py`) carries no role gate (`get_current_user` only) — it mirrors the
> read-broad shop-floor reads it sits in front of. It is **read-only** (no audit rows, no
> operational events, no auth side effects; a badge scan is a lookup only — badge **login** stays
> exclusively on the auth routes: passwordless on `POST /auth/employee-login`, and, since badge
> sign-in was added to the password form, badge **plus password** on `POST /auth/login`) and
> **tenant-scoped** (a cross-tenant code, or a
> soft-deleted work order, resolves to `kind: "unknown"`). URL-shaped traveler codes resolve too;
> the URL's host is deliberately **not** validated — a scanned URL carries no tenant authority, and
> tenancy always derives from the authenticated caller. The per-action gating it reports
> (`legal_actions` / `blockers`) reflects operation / time-entry **state**, not role — the
> shop-floor write verbs it mirrors (clock-in, production, complete, hold, resume) are themselves
> operator-facing (any authenticated user), so the resolver bypasses no role check. See
> `docs/API.md` → Scanner.

> **Operation sequencing (pool vs routing) is an Edit-row capability — no new role, no new permission.**
> `WorkOrder.sequential_operations` (migration `081`) decides whether a work order's operations are a
> sequenced routing or a work-center dispatch pool. It is set on **create** (`POST /api/v1/work-orders/`,
> Work Orders **Create** row) and changed only through `PUT /api/v1/work-orders/{id}`, which already
> carries `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above. Nothing was
> added to the role matrix and no permission string was minted. Two properties matter for review rather
> than for gating: turning sequencing **on** is refused (**409**) while any operation the strict rule
> would block has already been worked, and an accepted flip writes one `AuditService.log_status_change`
> row per operation it demotes READY → PENDING (`extra_data.transition = "sequential_operations_enabled"`),
> so the rule change and every status it moved are attributable on the tamper-evident chain. Operators
> gain and lose nothing: the setting changes which operations reach READY, and the shop-floor start /
> complete verbs read the same resolved rule, so a hidden card and a refused badge scan agree. See
> `docs/API.md` → Work Orders → "READY promotion: a sequenced ROUTING or a DISPATCH POOL".

> **Unit # is an Edit-row field — no new role, no new permission, and it is deliberately ungated on
> the floor screens.** `WorkOrder.unit_number` (migration `083`) is the build identity of a
> one-unit-per-work-order job. It is set on **create** (`POST /api/v1/work-orders/`, Work Orders
> **Create** row) and changed or cleared only through `PUT /api/v1/work-orders/{id}`, which already
> carries `require_role([ADMIN, MANAGER, SUPERVISOR])` — the Work Orders **Edit** row above. Nothing
> was added to the role matrix, no permission string was minted, and the change is audited through
> the generic `log_update` field diff like any other header field.
>
> The disclosure side is the part worth reviewing. The field is returned to **both** un-badged
> principals: the crew station's shared-PIN **station** token (on the queue and `held` rows) and a
> **display** token on the public TV, where — unlike `customer_name` — it is **not** behind
> `show_customer_names`. That is a decision, not a default: a bounded (`String(50)`) build number is
> not customer data, and its boundedness is exactly what makes it showable on an unattended screen
> where the work order's unbounded free-text `notes` (which is where this number used to be typed)
> is not. It widens neither the five-key guidance exception recorded above nor the blocker free-text
> withholding. See `docs/API.md` → Work Orders → "Unit #" and `docs/WALLBOARD.md` →
> "Unit # — ungated".

> **Material ties — endpoint mapping.** Tying stock material to a work order (the tie that makes
> inventory deplete as work completes) is a **planning act**, gated to the Work Orders **Edit** row
> above. On `app/api/endpoints/work_order_materials.py`:
>
> | Verb | Endpoint | Gate |
> |------|----------|------|
> | List ties | `GET /api/v1/work-orders/{id}/material-allocations` | `get_current_active_user` — any authenticated tenant user |
> | Tie material | `POST /api/v1/work-orders/{id}/material-allocations` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Edit a tie | `PATCH /api/v1/work-orders/{id}/material-allocations/{allocation_id}` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Untie | `DELETE /api/v1/work-orders/{id}/material-allocations/{allocation_id}` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Read a tie's per-lot consumption | `GET /api/v1/work-orders/{id}/material-allocations/{allocation_id}/consumption` | `get_current_active_user` — any authenticated tenant user |
> | **Return consumed material** | `POST /api/v1/work-orders/{id}/material-allocations/{allocation_id}/return` | `require_role([ADMIN, MANAGER, SUPERVISOR])` |
> | Tie a nest at creation | `POST /api/v1/work-orders/{id}/laser-nests/manual` and the four `…/laser-nest-packages/{preview,import}` routes, via the optional `material_part_id` | `require_role([ADMIN, MANAGER, SUPERVISOR])` — the endpoints' own pre-existing gate |
>
> **The return verb sits in the same tier as the tie verbs, and outside the kiosk path fence, for a
> stronger reason than the rest of them.** Every other verb on this router manages a **planning row**;
> the return is the one that **moves stock** and writes tamper-evident ledger rows. Reading it as
> "just another tie mutation" and later relaxing it to operator self-service would be the mistake this
> paragraph exists to prevent: **moving material back with a reason is a bigger power than tying it,
> not a smaller one.** It is a supervised reversal of a physical fact, it credits specific heat/cert
> lots, and it is the only self-service path that can lower a tie's `qty_consumed`. Keeping it under
> `/work-orders` rather than `/api/v1/shop-floor` means a crew-station or kiosk-scoped operator token
> is path-fenced away from it entirely — an operator who over-reported a count asks a supervisor,
> exactly as they do for the office reduce-production verb. The read half
> (`…/consumption`) is deliberately **broad**, like the tie list: it discloses ledger facts about
> material the company already owns, and a return dialog that could not show which lots the material
> goes back to would be asking for a confirmation nobody could give.
>
> **The matrix rows above hold now that a UI exists** (the work-order Materials panel, the nest
> wizard's sheet-part picker, the Dispatch Board chip, the kiosk deduction line). Nothing was
> re-gated: the nest-creation paths already required Admin / Manager / Supervisor, which is the same
> set the tie verbs require, so a nest tie created inside the import transaction cannot be a
> privilege escalation around `POST …/material-allocations`.
>
> The read is deliberately broad — a tie is shop-visible context ("this job burns *that* sheet"),
> and the same rows are already reachable through lot traceability. **Operator gets 403 on all four
> mutating verbs**; deciding what material a job consumes — or handing it back — is not operator
> self-service, unlike the
> shop-floor production verbs. The router is mounted under `/work-orders`, **not**
> `/api/v1/shop-floor`, so kiosk-scoped operator tokens are **path-fenced away from it** entirely —
> a crew-station token cannot tie, untie or return material even if it reached the route.
>
> **The floor reads ties without reaching that router, and the fence is unchanged.** The Dispatch
> Board's `material_tie` and the kiosk's `material_ties` ride
> `GET /shop-floor/dispatch-board` (Admin / Manager / Supervisor) and
> `GET /shop-floor/work-center-queue/{id}` + `GET /shop-floor/my-active-job` (any authenticated user,
> **or** a crew-station token for its own work center) — reads the fence already permits, carrying
> data rather than granting a new capability. The same precedent as `scrap_reason_codes`. Both are
> **pure reads**: they post no `ISSUE`, write no audit row and reconcile nothing. Note the small
> disclosure widening this implies for a **station** principal — an unattended, PIN-unlocked terminal
> with no operator identity can now see material part numbers and on-hand stock for the parts tied to
> its own work center's queued operations (not an inventory browser, but not nothing).
>
> Every lookup (work order, part, operation, pinned lot, allocation) is **tenant-scoped**: a
> cross-tenant id is **404**, never 403, so an id cannot be probed. Every create / edit / untie /
> return writes
> a tamper-evident `audit_log` row on resource type `work_order_material_allocation`. Untie is
> `status = cancelled` — the row is **never physically deleted**, because the ledger's
> `allocation_id` back-reference must keep resolving — and is refused **409** while the **ledger**
> still shows material issued against the tie (the signed ISSUE − RETURN net, not the `qty_consumed`
> cache, so a fully returned tie can be untied); the 409 names `POST …/return` with
> `intent: "return_and_untie"`, which credits the material back to its source lots **and** cancels the
> tie in one transaction.
> A return additionally requires a **non-blank reason** and writes it on the ledger row, in the audit
> description and in `extra_data.reason`; it appends compensating `RETURN` transactions and never
> edits a historical row.
>
> **The consumption itself has no endpoint and no separate gate.** It runs inside the existing
> completion paths — `apply_operation_completion_inventory_effects` when an **operation** completes and
> `apply_completion_inventory_effects` when the **work order** does — so whoever is authorized to
> complete the work is what authorizes the resulting stock movement, including the operator-facing
> kiosk and shop-floor completion verbs and the reconcile-on-read GET. **The reversal is the
> asymmetric case, deliberately**: consuming needs no gate beyond the authority to complete the work,
> while un-consuming has its own endpoint, its own Admin/Manager/Supervisor gate and a mandatory
> reason. That asymmetry is the point — production authorizes depletion because production happened;
> nothing on the floor happens that authorizes putting material back, so somebody has to say why. **The attributed actor is
> whoever completed the operation the material was consumed against** — with the trigger at operation
> completion that is the operator who finished nest 1 of 3, not whoever later closed the job. (Through
> PR 2 it was the other way round and this paragraph said so; the reconcile-on-read GET is still the
> one path with no meaningful actor, which is exactly why it is **not** a per-operation trigger — see
> `docs/MATERIAL_CONSUMPTION_PLAN.md` → Residual gaps.) **No role gained a capability — and one role LOST one.** An Operator
> could already drive consumption by completing a job's last operation, so what changed is when and how
> often, not who. In the other direction, `POST /api/v1/work-orders/operations/{id}/complete` — the
> **office** operation-complete verb — was gated to **Admin / Manager / Supervisor / Quality** in the same
> change, matching `complete-work-order` (its larger sibling, which completes every operation on the work
> order). Quality is included deliberately: refusing it a single operation while allowing it the whole work
> order would be incoherent. `reduce-production` stays stricter for a reason that does not apply here — it
> rewrites other operators' recorded labor.
> It had been open to any authenticated tenant user, **Viewer and Shipping included**, while its office
> siblings were already gated. That gap predates this work,
> but it became load-bearing the moment completing an operation began decrementing stock: a Viewer could
> move inventory and write hash-chain rows from a page they were only supposed to read. The UI button is
> gated to the same tier. **Operators are unaffected** — they complete work through
> `/api/v1/shop-floor/operations/{id}/complete` and the kiosk, which is the documented design above, not
> through the office page. This is deliberate: material depletes
> because production happened, not because someone was granted an inventory power. The stock
> decrement, the `ISSUE` ledger row, and any `ALLOCATION_SHORTAGE` are audited on the hash chain
> regardless of which path drove them. Lifecycle side effects follow the same rule — nest re-import
> and work-order delete cancel or refuse ties under **their own** existing gates (Admin / Manager /
> Supervisor for import; Admin / Manager for WO delete). See `docs/API.md` → Work Orders →
> "Material ties" and [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md).

### Parts

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| **Arm automatic BOM backflush** (`backflush_components`) | ✓ | ✓ | ✓ | | | | |
| View backflush readiness / dry-run preview | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Deactivate / reactivate** (`POST /parts/{id}/deactivate` · `/activate`) | ✓ | ✓ | | | | | |
| Delete | ✓ | | | | | | |
| Restore a deleted part | ✓ | ✓ | | | | | |

> **The backflush row is the ordinary Edit row, and that is a recorded decision, not an omission**
> (`feat/backflush-exposure`, PR 4.5). `Part.backflush_components` opts a part into **automatic
> consumption of its BOM/routing components out of stock** on every future work-order completion — a
> permanent, shop-wide policy change that writes the lots it drew onto the as-built record. It is set as
> an ordinary field on `PUT /parts/{id}` (and on `PUT /materials/{id}`, which writes the same `parts`
> rows through the same schema), so its authorization tier is whatever those endpoints already enforce:
> **`require_role([ADMIN, MANAGER, SUPERVISOR])` — the same permission as editing a description.** The
> owner chose this over a dedicated reasoned verb limited to Admin/Manager. Three consequences are
> **accepted residuals**, recorded in `docs/CMMC_LEVEL_2_COMPLIANCE.md`: a **supervisor** can arm it;
> **no reason is captured** (the audit row records who, when, false→true, and the readiness verdict in
> `extra_data` — not why); and **a concurrent flip does not 409**, because `Part` maps no `version`
> column, so optimistic locking on parts is cosmetic and last write wins.
>
> **The role gate is not the only gate.** Enabling is refused **409** while the part's readiness check
> reports a blocking diagnostic, through one shared function (`assert_backflush_change_allowed`) defined
> in `parts.py` and **imported** by `materials.py` — a gate in only one of the two files would not be a
> gate. **Disabling is never refused.** Neither door accepts the field on **create**: it is absent from
> `PartBase`/`PartCreate`, so `POST /parts/`, `POST /materials/` and both CSV importers cannot set it,
> and a part is always created **off**.
>
> **⚠️ But the gate protects the instant of the flip and nothing after it, which matters for how the
> permission should be read.** The readiness check runs the part's **BOM** explosion only — the routing
> half is not evaluated at part scope at all — it is evaluated **once**, and it is never re-run on a BOM
> edit, a routing change, a release or a completion. Every input it read stays editable afterwards by the
> **same ADMIN/MANAGER/SUPERVISOR tier** through `boms:edit` / `routings:edit`. A **BOM**-line write now
> at least *says so* — it returns a `backflush_armed_warning` and stamps `extra_data.backflush_armed_parts`
> on its own `bom_line` audit row — but it does not re-check and does not refuse, and the **routing** edit
> path is still entirely unaware. So the recorded verdict `backflush_readiness: "clean"` asserts
> less than a reader assumes: not "this part's demand resolves correctly", only "no blocking diagnostic
> in its BOM at that instant". What backs it afterwards is a completion-time refusal
> (`BACKFLUSH_DEMAND_REFUSED`), which is a net, not a second gate. **Practically: granting `boms:edit` or
> `routings:edit` is, for an armed part, granting the ability to change what automatically leaves stock.**
>
> **Auditing who armed it is ONE query, through either door.** `PUT /materials/{id}` writes the same
> `parts` row and logs the change as `resource_type="part"`, not `"material"`, so the trail is not split
> by which URL was used (`create_material` / `delete_material` still log `"material"`). The canonical
> query recipe lives in [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → the 2026-07-27
> (PR 4.5) changelog row, item (3) — written down once, on purpose.
>
> The two read companions — `GET /parts/{id}/backflush-readiness` and
> `GET /work-orders/{id}/backflush-preview` — follow the read-broad rule at the top of this document
> (any authenticated user in the tenant) and are **pure reads: they write nothing**, not even an audit
> row. See `docs/API.md` → Parts → Part Schema, and
> [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md) → "Exposing the flag (PR 4.5)".

> **Deactivate / reactivate a part — ADMIN / MANAGER, deliberately narrower than the Edit row.**
> `POST /parts/{id}/deactivate` and `POST /parts/{id}/activate` (shipped with the Combine/Merge SKUs
> feature, to retire the SKU a combine folded away) are `require_role([ADMIN, MANAGER])` — the same
> identity tier as `POST /parts/{id}/renumber` and `POST /parts/{id}/revision`, not the
> `PUT /parts/{id}` tier that reaches Supervisor. **A Supervisor gets 403.** Deactivation removes the
> part from every picker, search and purchasing signal in the app, so a required `reason` is captured
> on the deactivate side (the activate side's is optional — the permissive direction is visible the
> moment it happens); both write a `resource_type="part"` UPDATE row on the tamper-evident chain.
>
> **These two verbs are the ONLY non-delete writers of `parts.is_active`, and that is an
> authorization decision, not a plumbing one.** `is_active` doubles as the **soft-delete mask**
> (`delete_part` sets `is_deleted` AND `is_active=false` AND `status='obsolete'` together), so adding
> `is_active` or `status` to `PartUpdate` would hand every **Supervisor** a way to clear a delete mask
> through an ordinary blind-`setattr` form save on `PUT /parts/{id}` or `PUT /materials/{id}`, neither
> of which filters `is_deleted` on its lookup. That is the 2026-08-16 `Vendor` trap invariant 3
> records. Both verbs additionally **404 a soft-deleted part** (*"restore it first"*) for the same
> reason.
>
> **A restore returns the RECORD, not the permission (behaviour change).**
> `POST /parts/{id}/restore` used to hard-code `is_active = True`. It now returns the part to the
> `is_active` it had before the delete (`COALESCE(is_active_before_delete, false)`, migration `086`),
> and an unknowable prior value — NULL, meaning the part was deleted before `086` — resolves
> **INACTIVE**. The reasoning is invariant 3's, and it is an RBAC argument: putting a retired part
> number back into use is an engineering decision that must be *made*, by someone holding the
> activate verb, with an audit row saying so — never inherited as a side effect of undoing a delete.
> Restoring too restrictively costs one explicit audited re-activation and is visible immediately;
> restoring too permissively is indistinguishable from a legitimate approval and is never detected.
> Same control migration `082` established for `Vendor`. Because a restrictive restore obliges shipping
> the screen that undoes it, **Materials** has an **In Use / Retired** view that reaches a retired part so
> the audited activate verb can switch it back on — the obligation the Vendors **Active / Inactive /
> Deleted** switch discharges on that side. ⚠️ The **Parts** page has no equivalent view yet, so an
> inactive-restored *engineering* part is still only reachable by calling the endpoint directly. See
> `docs/API.md` → Parts.

### BOMs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit — header (`PUT /bom/{id}`) and lines (`POST`/`PUT`/`DELETE` on items). **Status-dependent — see below** | ✓ | ✓ | ✓ | | | | |
| **View unit-of-measure mismatch report** (`GET /bom/uom-mismatches`) | ✓ | ✓ | ✓ | | | | |
| **BOM Unit Mismatches page** (`/bom/uom-mismatches`) — nav entry + route, `boms:edit` | ✓ | ✓ | ✓ | | | | |
| Delete (`DELETE /bom/{id}`) — **soft**, draft only | ✓ | ✓ | | | | | |
| **Restore** (`POST /bom/{id}/restore`) | ✓ | ✓ | | | | | |
| Release (`POST /bom/{id}/release`) | ✓ | ✓ | | | | | |
| **Unrelease** (`POST /bom/{id}/unrelease`) | ✓ | ✓ | | | | | |

> **Edit is status-dependent, and that is a second gate on top of the role.** A role tick above
> means "may attempt the verb"; whether it succeeds depends on the BOM's `status`, which is a
> controlled-document state:
>
> | BOM `status` | Header `PUT /bom/{id}` | Line writes | `release` | `unrelease` | `delete` |
> |---|---|---|---|---|---|
> | `draft` | all fields | allowed | ✓ | 400 *"BOM is not released"* | ✓ (soft) |
> | `released` | **`description` only** — anything else **400** | **400** | 400 *already released* | ✓ | 400 |
> | anything else (legacy/corrupt) | **400** | **400** | **400** | ✓ — **normalises back to `draft`** | 400 |
>
> The workflow for changing a released BOM is therefore **unrelease → edit → release**, three
> audited rows on the chain rather than one silent in-place mutation. That is also the answer for
> the unit-of-measure worklist above: a mismatched line on a *released* BOM cannot be corrected in
> place — unrelease it, fix the line, release it again.
>
> **`status` is not writable through `PUT /bom/{id}` at all.** The field was removed from
> `BOMUpdate`. Previously it was an unvalidated free string on a verb open to **Supervisor**, one
> tier wider than the Admin/Manager `release` verb it shadowed — so a Supervisor could
> `PUT {"status": "released"}` and bypass both the release role gate and its "cannot release a BOM
> with no items" precondition, producing an approved controlled document with `approved_by` /
> `approved_at` **NULL**: an approved document with no approver. Sending `"draft"` un-released a BOM
> without clearing the approver. Both transitions now belong exclusively to `release` / `unrelease`,
> which are Admin/Manager and which stamp or clear the approval evidence. A legacy client that still
> sends `status` gets a 200 with the field ignored and the true status echoed back.
>
> **`unrelease` is also the de-corruption door.** It refuses only a BOM that is *already* `draft`;
> anything else it withdraws to `draft`, audited with the actual prior status. That matters because
> the removed free-string field could have written junk (`"RELEASED"`, `"obsolete"`, anything), and
> every other verb requires a draft — without this, `BOM.part_id` being UNIQUE would leave such a
> part permanently unable to hold a working BOM.

> **The unit-of-measure mismatch report is gated to the Edit tier, not the View tier — deliberately**
> (`feat/backflush-exposure`, PR 4.5). It is a **remediation worklist**, not a browse view: every row on
> it is a BOM line someone has to go and correct before the part it belongs to can be armed for automatic
> backflush. ADMIN / MANAGER / SUPERVISOR is exactly the set that can act on a row — edit the line
> (`PUT /bom/items/{id}`) or arm the flag (`PUT /parts/{id}`) — so handing the list to a role that can do
> neither buys nothing. It is a **pure read: it writes nothing**, not even an audit row. Note this is
> *narrower* than the two backflush read companions above, which are open to any authenticated tenant
> user because they answer a question about one part a user is already looking at. See `docs/API.md` →
> BOM (Bill of Materials).
>
> **The screen carries the same gate, in both places it can be enforced.** PR 4.5 shipped the report
> API-only; the follow-up added the **BOM Unit Mismatches** page at **`/bom/uom-mismatches`** (sidebar:
> Engineering → *BOM Unit Mismatches*, directly after *Bill of Materials*). Both the nav entry and the
> route require **`boms:edit`**, whose role set — `{platform_admin, admin, manager, supervisor}` — is
> exactly the endpoint's `require_role([ADMIN, MANAGER, SUPERVISOR])`. So the link is not rendered for a
> role that cannot act on a row, **and** the route guard refuses a deep link from one; the API gate then
> refuses the fetch regardless, which is the enforcement that actually counts. The route entry is
> `{ prefix: '/bom/uom-mismatches', permission: 'boms:edit' }` and wins over the `/bom` → `boms:view`
> entry because `getRouteAccessRequirement` matches the **longest** prefix — a page gated *more* tightly
> than the parent it sits under, which is the reason that longest-prefix rule exists. Nav gating needed a
> small mechanism that did not exist before: `NavItem` gained an optional `permission`, and
> `visibleNavigation` filters items **and** collapsible-group children by it, dropping a group left with
> no visible children so no group opens onto nothing. Every pre-existing nav item carries no
> `permission`, so nothing else changed.
>
> **What the page can and cannot do is part of the gate's rationale.** It is **read-only**: rows
> deep-link to `/bom?id={bom_id}` and to the assembly part, and there is no inline BOM-line editor,
> because BOM-line create/update/delete wrote **no audit rows at all** when it was built — making this
> screen the primary remediation flow would have put a compliance-critical correction on an un-audited
> endpoint. **That blocker is now closed** (all three verbs audit as `bom_line`), so an inline editor is
> no longer refused on compliance grounds; it is simply not built. The
> corrections themselves therefore run through the ordinary, already-gated BOM edit path. See
> `docs/API.md` → BOM → *Where this is worked — the BOM Unit Mismatches screen*, and
> [docs/MATERIAL_CONSUMPTION_PLAN.md](MATERIAL_CONSUMPTION_PLAN.md) → "Exposing the flag (PR 4.5)" for
> the run-report → correct-lines → re-check-readiness → arm sequence.

### Routings

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Copy | ✓ | ✓ | | | | | |
| Generate from drawing (AI) | ✓ | ✓ | ✓ | | | | |
| Edit (draft routing) | ✓ | ✓ | ✓ | | | | |
| Edit time standards (released routing) | ✓ | ✓ | | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

> **Edit row splits by routing status — endpoint mapping (`feat/routing-editable-time-standards`).**
> `PUT /api/v1/routing/{routing_id}/operations/{operation_id}` (`update_operation`,
> `app/api/endpoints/routing.py`) carries the decorator-level
> `require_role([ADMIN, MANAGER, SUPERVISOR])` — the **Edit (draft routing)** row, where every
> operation field is editable. On a **released** routing the same endpoint allows in-place edits to
> **time standards only** (`setup_hours`, `run_hours_per_unit`, `move_hours`, `queue_hours`,
> `cycle_time_seconds`, `pieces_per_cycle`) and gates that path **in code** to **Admin / Manager**
> only — a **Supervisor** hitting the released-edit path receives **403**
> (*"Editing a released routing's time standards requires the Admin or Manager role."*). This mirrors
> **Release** (also Admin/Manager-only): editing live released content is release-adjacent authority,
> so it is held to the release role set rather than the broader draft-edit set. Changing any
> non-time-standard (process) field on a released routing returns **400** (create a new revision
> instead); an **obsolete** routing is fully locked (**400**). Adding/deleting/reordering operations
> on a released routing also returns **400** (process is frozen on release). Superuser / Platform
> Admin bypass role checks, as elsewhere. Every applied change is tamper-evidently audit-logged; see
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → CONFIGURATION MANAGEMENT (CM).

> **Copy & AI generation — endpoint mapping (`feat/process-sheets-library`).**
> `POST /api/v1/routing/{routing_id}/copy` (`copy_routing`, `app/api/endpoints/routing.py`) carries
> `require_role([ADMIN, MANAGER])` — deliberately **narrower than Create** (no Supervisor). The
> two-step AI generation flow — `POST /routing/generate-from-drawing` then
> `POST /routing/create-from-generation` — carries `require_role([ADMIN, MANAGER, SUPERVISOR])`, the
> Create role set. Both paths produce **draft** routings (Release stays Admin/Manager), and the copy
> endpoint writes a tamper-evident `audit_log` CREATE with `extra_data.copied_from` (the source
> routing id) — see `docs/API.md` → Routing and the
> [CMMC change log](CMMC_LEVEL_2_COMPLIANCE.md) entry dated 2026-07-06.

### Work Centers

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (`GET /work-centers/`, `/{id}`, `/types`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create (`POST /work-centers/`) | ✓ | ✓ | | | | | |
| Edit (`PUT /work-centers/{id}`) | ✓ | ✓ | | | | | |
| Set current status (`POST /work-centers/{id}/status`) | ✓ | ✓ | | | | | |
| Deactivate (`DELETE /work-centers/{id}`) | ✓ | | | | | | |
| Import CSV (`POST /work-centers/import-csv`) | ✓ | ✓ | | | | | |

> **Status is Admin / Manager, and that is a tightening.** `POST /work-centers/{id}/status`
> previously carried a bare `get_current_user` — **any** authenticated user in the tenant could
> flip a machine to `offline`/`maintenance`, and no audit row named who. It is the only writer
> of `WorkCenter.current_status` outside the CSV importer, and the flag drives what the dispatch
> board and the operator kiosk show. The tier matches **Edit**, not **Deactivate**: a status flip
> is reversible, and Edit already lets a Manager flip `is_active`. Superuser / Platform Admin
> bypass, as elsewhere.
>
> **The frontend gate is paired with it, in the same change.** The inline status `<select>` on
> each row is gated to the same role set and renders as a read-only `StatusBadge` for everyone
> else — otherwise the control would render, accept a change, fire the request, and surface the
> refusal only as a 403 toast afterwards.
>
> ⚠️ **The page and the endpoints disagree about who Manager is, and the page is the narrower
> one.** `/work-centers` *is* route-guarded — `App.tsx` → `routeAccessRequirements` maps the
> prefix to **`admin:settings`**, which only **Admin** and **Platform Admin** hold. So a
> **Manager holds every endpoint permission in this table except Deactivate, yet is routed to
> `/unauthorized` when opening the page**, and the nav entry is shown to them by a different
> rule. Nobody who can currently reach the page is refused by the client gate above; it is
> defense in depth today and becomes load-bearing when the route tier is aligned. Tracked
> separately — do not "fix" it by widening the status endpoint instead.
>
> **Deactivate stays Admin-only and is server-GATED**: refused **409** while any live operation
> still references the machine (see `docs/API.md` → Work Centers). Every state-changing endpoint
> in this table writes a tamper-evident `audit_log` row (`resource_type = "work_center"`);
> create/status were the last two that did not.

### Maintenance (PM schedules, maintenance work orders, event log)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (schedules, work orders, overdue, calendar, dashboard, history) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit PM schedule (`POST`/`PUT /maintenance/schedules`) | ✓ | ✓ | ✓ | | | | |
| Deactivate PM schedule (`DELETE /maintenance/schedules/{id}`) | ✓ | ✓ | ✓ | | | | |
| Create / edit maintenance WO (`POST`/`PUT /maintenance/work-orders`) | ✓ | ✓ | ✓ | | | | |
| Start / complete maintenance WO (`POST .../{id}/start`, `.../complete`) | ✓ | ✓ | ✓ | ✓ | | | |
| Add event log entry (`POST /maintenance/log`) | ✓ | ✓ | ✓ | ✓ | | | |

> ⚠️ **This table is new because the router had NO role gating at all.** All sixteen handlers were
> `Depends(get_current_user)`, so a **Viewer could create, start and complete maintenance work
> orders** on any machine in the tenant — while the sibling supplier-scorecard router already gated
> its writes to Admin / Manager.
>
> **The split follows the work-order permissions the frontend already uses.** Planning verbs
> (schedules, opening/editing a maintenance WO) match `work_orders:create` / `work_orders:edit`
> (Admin / Manager / Supervisor). Performing verbs (start / complete / log) additionally admit
> **Operator**, mirroring `work_orders:complete` — the maintenance tech doing the work signs in as
> one. Superuser / Platform Admin bypass, as elsewhere.
>
> **Reads stay open to every role.** `/maintenance` is route-guarded on `work_orders:view`
> (`App.tsx` → `routeAccessRequirements`), which every role holds, so refusing reads would break the
> page for its intended audience.
>
> ⚠️ **The client has no matching gate yet.** `Maintenance.tsx` renders its Create / Start /
> Complete controls unconditionally, so a Viewer or Operator will see a control the server now
> refuses and get a 403 toast after clicking. Pairing the client gate with the server gate — the way
> the work-center status `<select>` is paired above — is a follow-up.

> **Tenancy: ten of the sixteen handlers took no company argument.** `start` and `complete`
> resolved the work order by bare id, and `dashboard` / `calendar` / `history` / `overdue`
> aggregated across every tenant. All are scoped now, and `work_center_id`, `schedule_id` and
> `maintenance_wo_id` are validated against the caller's company (flat **404**, never 403). See
> `docs/API.md` → Maintenance for the three production 500s this also fixed and the removal of the
> unaudited status write from two GETs.

> **Every state change writes a tamper-evident `audit_log` row** — `maintenance_schedule`,
> `maintenance_work_order` (with `STATUS_CHANGE` on start and complete) and `maintenance_log`. The
> router previously wrote none, so who started or closed a PM job was unrecoverable, and PM records
> are AS9100D-auditable quality records.

> **Before deploying, check for legacy cross-tenant rows.** The new write guards stop *new*
> mis-tenanted rows; they do nothing about rows written before them. Such a row is owned by the
> caller's company and so passes every scoping filter, while the serializer used to render the
> foreign machine's name. The serializers now null the related field, but the rows themselves are a
> data problem:
>
> ```sql
> SELECT 'maintenance_work_orders' AS t, m.id, m.company_id AS owner, w.company_id AS fk_owner
>   FROM maintenance_work_orders m JOIN work_centers w ON w.id = m.work_center_id
>  WHERE w.company_id <> m.company_id
> UNION ALL SELECT 'maintenance_work_orders.schedule', m.id, m.company_id, s.company_id
>   FROM maintenance_work_orders m JOIN maintenance_schedules s ON s.id = m.schedule_id
>  WHERE s.company_id <> m.company_id
> UNION ALL SELECT 'maintenance_schedules', s.id, s.company_id, w.company_id
>   FROM maintenance_schedules s JOIN work_centers w ON w.id = s.work_center_id
>  WHERE w.company_id <> s.company_id
> UNION ALL SELECT 'maintenance_logs', l.id, l.company_id, w.company_id
>   FROM maintenance_logs l JOIN work_centers w ON w.id = l.work_center_id
>  WHERE w.company_id <> l.company_id;
> ```
>
> Empty result → nothing to do. Non-empty → those rows need repointing at a machine the owning
> company actually has.

### Supplier Scorecards, Audits & Approved Supplier List

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard, ranking, history, lists, detail, due-soon) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / edit scorecard | ✓ | ✓ | | | | | |
| Auto-calculate scorecard (`POST .../calculate/{vendor_id}`) | ✓ | ✓ | | | | | |
| Create / edit supplier audit | ✓ | ✓ | | | | | |
| Create / edit ASL entry | ✓ | ✓ | | | | | |

> **The write tiers are unchanged** — this router already gated its writes to Admin / Manager. The
> table is recorded here because it never was, and because the reads are open to every role: a
> supplier's score, audit result and approval status are visible to anyone signed in.
>
> **What changed is tenancy, not roles.** Fifteen of sixteen handlers reached outside the caller's
> company, including three cross-tenant **writes** (`PUT` on scorecard / supplier audit / ASL entry
> each resolved its row by bare id — the scorecard handler took a `company_id` dependency and never
> used it). Every state change now also writes a tamper-evident `audit_log` row
> (`supplier_scorecard`, `supplier_audit`, `approved_supplier`); the router previously wrote none,
> so who downgraded a supplier to `Disqualified` or flipped an ASL entry to `removed` was
> unrecoverable. See `docs/API.md` → Supplier Scorecards.

> **Before deploying, check for legacy cross-tenant rows** (same reasoning as Maintenance above):
>
> ```sql
> SELECT 'supplier_scorecards' AS t, x.id, x.company_id AS owner, v.company_id AS fk_owner
>   FROM supplier_scorecards x JOIN vendors v ON v.id = x.vendor_id WHERE v.company_id <> x.company_id
> UNION ALL SELECT 'supplier_audits', a.id, a.company_id, v.company_id
>   FROM supplier_audits a JOIN vendors v ON v.id = a.vendor_id WHERE v.company_id <> a.company_id
> UNION ALL SELECT 'approved_supplier_list', p.id, p.company_id, v.company_id
>   FROM approved_supplier_list p JOIN vendors v ON v.id = p.vendor_id WHERE v.company_id <> p.company_id;
> ```
>
> A legacy ASL row is the one that also needs manual repair rather than just repointing: it
> permanently consumes the vendor's single **global** ASL slot, and the owning company can neither
> see nor edit it (both endpoints are now scoped), so it gets a 400 "Vendor already has an ASL
> entry" with no self-service recovery.

### Process Sheets

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | ✓ | | |
| Edit (draft sheet + steps) | ✓ | ✓ | ✓ | | ✓ | | |
| Delete (draft only) | ✓ | ✓ | ✓ | | ✓ | | |
| New revision | ✓ | ✓ | ✓ | | ✓ | | |
| Release | ✓ | ✓ | | | ✓ | | |
| Obsolete | ✓ | ✓ | | | ✓ | | |

> **Role split — endpoint mapping (`feat/process-sheets-library`).** All `/api/v1/process-sheets`
> writes are gated by decorator-level `require_role` in `app/api/endpoints/process_sheets.py`:
> **authoring** (create, header edit, step CRUD, soft-delete, new-revision) carries
> `AUTHOR_ROLES = [ADMIN, MANAGER, SUPERVISOR, QUALITY]`; **release** and **obsolete** carry
> `RELEASE_ROLES = [ADMIN, MANAGER, QUALITY]`. Unlike Routings, **Quality** participates in both
> sets — process sheets are inspection documents, and quality owns released inspection content
> (release-adjacent authority), while release stays narrower than authoring, mirroring the
> Routings draft-edit vs release split. Mutability is status-gated in the service: only **draft**
> sheets are editable — header edits, step CRUD, and delete on a released/obsolete sheet return
> **409** (create a new revision instead). GET endpoints depend on `get_current_user` only
> (tenant-scoped, read-broad — see the access enforcement model above). Superuser / Platform Admin
> bypass role checks, as elsewhere.

### Inventory

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Adjust | ✓ | ✓ | ✓ | | | | |
| Issue | ✓ | ✓ | ✓ | | | | |
| Receive | ✓ | ✓ | ✓ | | | | |
| Transfer | ✓ | ✓ | ✓ | | | | |
| **Preview a SKU combine** | ✓ | ✓ | ✓ | | | | |
| **Combine two SKUs** (`inventory:combine`) | ✓ | ✓ | | | | | |
| Create location | ✓ | ✓ | | | | | |
| Create / complete cycle count | ✓ | ✓ | ✓ | | | | |
| Start (open) cycle count | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| Record count on an item | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |

> **Issue — now enforced in code (Admin / Manager / Supervisor).** `POST /api/v1/inventory/issue`
> (`app/api/endpoints/inventory.py`) was previously gated only by `get_current_user` — **any**
> authenticated user in the tenant could issue stock off a lot, with no role restriction. It is now
> `require_role([ADMIN, MANAGER, SUPERVISOR])`, the same set as the sibling stock-mutating
> `/inventory/adjust`, which is what the **Issue** row above records. The endpoint is additionally
> marked **deprecated** in favor of a planned work-order-scoped `POST /work-orders/{id}/issue-material`
> (that replacement is not implemented yet — `/inventory/issue` is still the supported path). See
> `docs/API.md` → Inventory.

> **Receive / Transfer — now enforced in code (Admin / Manager / Supervisor).** `POST
> /api/v1/inventory/receive` and `POST /api/v1/inventory/transfer` previously depended on
> `get_current_user` only, so **any** authenticated user in the tenant — Viewer included — could
> create stock and write a ledger row. Both now carry `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> matching the sibling stock mutators `/inventory/issue` and `/inventory/adjust` and the PO-receipt
> path `POST /receiving/receive`, which writes the same `inventory_items` /
> `inventory_transactions` tables. The **Transfer** row above was previously intended policy the
> server did not enforce; it is now an enforced control, and the new **Receive** row records the
> gate that closed the same gap on the receive path. (An earlier revision of this section described
> both as un-gated drift — that is no longer true.)
>
> The UI layer now agrees with the server on this one: `frontend/src/pages/Inventory.tsx` hides the
> **Receive Inventory** button and drops the per-row **Transfer** action (column and mobile-card
> affordance) for roles without `inventory:adjust` / `inventory:transfer`, which resolve to exactly
> Admin / Manager / Supervisor (+ Platform Admin, who bypasses `require_role` server-side). Per the
> [Access enforcement model](#access-enforcement-model) the UI gate remains cosmetic — the server
> gate is the control.

> **Combine two SKUs — endpoint mapping, and why the write is NARROWER than Adjust.**
>
> | Step | Endpoint | Gate |
> |------|----------|------|
> | Preview (pure read — writes nothing) | `POST /inventory/combine/preview` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
> | Perform the combine | `POST /inventory/combine` | `require_role([ADMIN, MANAGER])` |
>
> **A Supervisor can look, and gets 403 on the write.** That split is the whole point. Adjusting one
> lot corrects a **count**; folding two part numbers together changes which **article** the material
> *is* — a controlled change to article identity under AS9100D 8.5.2. So the write sits with
> `POST /parts/{id}/renumber` and `POST /parts/{id}/revision` (both ADMIN/MANAGER), **not** with the
> stock-mutator tier that `inventory:adjust` / `inventory:transfer` occupy. The preview stays wider
> because a supervisor investigating *"why do we have this sheet on two numbers?"* needs to be able to
> look, and it is structurally incapable of writing anything (`build_combine_preview` takes no
> `AuditService` and no actor id — the same posture as `GET /parts/{id}/renumber-impact` and the
> backflush-readiness reads).
>
> **The client permission key is `inventory:combine`**, held by `platform_admin`, `admin` and
> `manager` only (`frontend/src/utils/permissions.ts`), so the hidden control and the refused call
> agree. It is deliberately a **narrower** key than `inventory:adjust`, which reaches Supervisor —
> a separate key rather than a reuse, because the two authorize genuinely different acts.
>
> **It is FRONTEND-ONLY, and that is the `parts:renumber` pattern, not an omission.** The key is
> deliberately absent from the backend's `role_permission.py` `ALL_PERMISSIONS` /
> `PERMISSION_CATEGORIES`, exactly as `parts:renumber` is. Per the
> [Access enforcement model](#access-enforcement-model): **the key gates the button; the backend
> `require_role` is the enforcement.** Adding it to the backend catalog would imply a second
> authorization surface that nothing consults.
>
> The paired part-activation verbs shipped with this feature — `POST /parts/{id}/deactivate` and
> `POST /parts/{id}/activate`, used to retire the folded-away SKU — carry the **same**
> `require_role([ADMIN, MANAGER])` gate, for the same reason. See the Parts section below and
> `docs/API.md` → Inventory → "Combining two SKUs".

> **Cycle counts — endpoint mapping.** On `app/api/endpoints/inventory.py`:
>
> | Step | Endpoint | Gate |
> |------|----------|------|
> | Create + enroll stock rows | `POST /inventory/cycle-counts` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
> | Open for counting | `POST /inventory/cycle-counts/{id}/start` | `require_role(COUNT_WRITE_ROLES)` = everyone except Viewer |
> | Record a counted quantity | `POST /inventory/cycle-counts/{id}/items/{item_id}/count` | `require_role(COUNT_WRITE_ROLES)` = everyone except Viewer |
> | Complete + post adjustments | `POST /inventory/cycle-counts/{id}/complete` | `require_role(STOCK_MUTATOR_ROLES)` = `[ADMIN, MANAGER, SUPERVISOR]` |
>
> **`start` and `record_count` now exclude Viewer — the only cycle-count policy change.** Both were
> bare `get_current_user`, so **Viewer** — the read-only role, which
> `frontend/src/utils/permissions.ts` grants `inventory:view` and nothing else — could open a count
> and write the counted quantities a manager's ledger-posting `complete` derives its adjustment
> from. That is a write, and a quality record at that, so it does not belong to a read-only role.
>
> The gate is deliberately defined by **exclusion**
> (`COUNT_WRITE_ROLES = [ADMIN, MANAGER, SUPERVISOR, OPERATOR, QUALITY, SHIPPING]`, in
> `app/api/endpoints/inventory.py`): every *working* role keeps both verbs, so the entire shop-floor
> counting path is preserved and only the read-only role loses access. Counting is the operator
> task; the privileged steps stay *creating* the count (which enrolls the stock rows) and
> *completing* it (which posts the variance to the ledger), both unchanged at
> `[ADMIN, MANAGER, SUPERVISOR]`.
>
> **Do not narrow `start` further.** An earlier revision of this pass gated it to Admin / Manager /
> Supervisor; that was reverted before merge, because combined with the `record_count` IN_PROGRESS
> requirement below it would leave an operator unable to work a `SCHEDULED` count at all (unable to
> open one, and **409** on any attempt to count into it) — a shop-floor capability regression rather
> than a hardening. The operator path — `start` a scheduled count, then `record_count` into it — is
> pinned by
> `backend/tests/api/test_inventory_hardening.py::test_operator_can_start_a_scheduled_count_and_record_into_it`,
> and every non-Viewer role is pinned by `test_start_allowed_for_every_working_role` /
> `test_record_count_allowed_for_every_working_role`.
>
> Beyond authorization, the hardening pass added **integrity guards and audit coverage** to these
> two steps:
>
> - `start` returns **409** on a terminal count (`COMPLETED` / `CANCELLED`) — re-opening a completed
>   count would let a second `complete` double-post the same physical variance to the ledger.
> - `start` writes a `cycle_count` STATUS_CHANGE audit row on the real `SCHEDULED → IN_PROGRESS`
>   transition, and preserves the original `started_at` when an already-started count is re-assigned
>   (audited as an UPDATE of `assigned_to`, not a fabricated second transition).
> - `record_count` returns **409** unless the parent count is `IN_PROGRESS`. A counted quantity is
>   the quality record the variance adjustment derives from: once the count is closed that record is
>   evidence and must not be overwritten, and before it is opened there is nothing to count against.
> - `record_count` writes a `cycle_count_item` **UPDATE** audit row on every counted quantity. A
>   re-POST while the count is still `IN_PROGRESS` is legal (a genuine re-count) but silently
>   replaces `counted_quantity` / `variance` / `counted_by` — the audit row is the only surviving
>   record of what it overwrote.
> - `POST /inventory/cycle-counts` writes a `cycle_count` **CREATE** audit row carrying the declared
>   scope (`warehouse`, `location_code`, `part_id`) and the number of stock rows enrolled, so "who
>   scoped this count, and which rows did `complete` later adjust" is answerable from the hash chain.
>
> Also changed by the same hardening pass:
> `record_count` is now **tenant-scoped** — it resolves the parent count *and* the count item
> against the active company (**404** otherwise), where it previously matched on ids alone. That was
> a real authorization defect in the code, though not an exploitable one in the field: the only
> writer of `cycle_count_items` is `POST /inventory/cycle-counts`, which omitted the NOT NULL
> `company_id` stamp and therefore always failed with `IntegrityError` whenever it enrolled a row,
> so no cross-tenant `cycle_count_items` row could exist to be written onto (rows predating
> migration `026_add_multi_tenancy` were backfilled to the single seeded company). `POST
> /inventory/cycle-counts` now enrolls only the active company's inventory rows and stamps
> `company_id` on each `CycleCountItem`, and `.../complete` adjusts only this company's stock.
> See `docs/API.md` → Inventory for the lifecycle guards (**409** on terminal counts, **404** on an
> unresolvable `location_code`) and the audit rows these steps now write.

### Purchasing

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Approve | ✓ | ✓ | | | | | |
| Delete / restore vendor (soft) | ✓ | ✓ | | | | | |
| Delete / restore purchase order (soft) | ✓ | ✓ | | | | | |

> **Read enforcement:** Per the [Access enforcement model](#access-enforcement-model),
> Purchasing list/detail reads (`list_vendors`, `list_purchase_orders`, and the
> single-record GETs in `app/api/endpoints/purchasing.py`) are tenant-scoped but **not**
> role-restricted — any authenticated user in the tenant can read vendor and PO data, so
> the **View** row above reflects intended UI visibility rather than a server-enforced
> restriction. Only the write/approve actions (Create, Approve, send, line edits) are
> role-gated. Receiving (below) follows the same read-broad / write-restricted pattern.

> **Vendor / PO soft-delete + restore — endpoint mapping.** `DELETE /purchasing/vendors/{id}`,
> `POST /purchasing/vendors/{id}/restore`, `DELETE /purchasing/purchase-orders/{id}`, and
> `POST /purchasing/purchase-orders/{id}/restore` (`app/api/endpoints/purchasing.py`) are enforced **in
> code** to the two **Delete / restore** rows above — `require_role([ADMIN, MANAGER])`. `Vendor` and
> `PurchaseOrder` gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`), so these are
> **soft** deletes (never physical — invariant #3): the row is flagged `is_deleted`, drops out of all
> reads, and is restorable. Both the delete and the restore write a tamper-evident `audit_log` row.
> Guardrails are server-enforced: a **vendor** delete also deactivates it (`is_active=false`, after
> recording the prior value so the restore can put it back — see below) and is refused (**400**)
> while it has an active PO; a **PO** delete is refused (**400**) when any line has
> received material (void the receipts first); and opening a PO against a soft-deleted/inactive vendor
> is refused (**404**). See `docs/API.md` → Purchasing.
>
> **The PO delete is reachable from two pages** — the Purchasing PO list and the **open-PO list on
> Warehouse → Receiving** — but it is one endpoint under one gate. The Receiving surface does **not**
> inherit the Receiving rows below: a Supervisor who may receive, correct a receipt and clear an
> inspection hold still sees **no** delete control, because this stays `require_role([ADMIN,
> MANAGER])`.

> **PO restore — the read and the verb are gated differently, on purpose.** Restoring a PO needs two
> things: a way to *see* deleted POs, and permission to bring one back. They sit at different tiers,
> and the split is deliberate rather than an oversight:
>
> | Half | Where | Gate |
> |---|---|---|
> | **See** deleted POs | `GET /purchasing/purchase-orders?deleted_only=true` — the only read in the API that returns a soft-deleted PO | **Any authenticated user in the tenant** (`get_current_user`) |
> | **Restore** one | `POST /purchasing/purchase-orders/{id}/restore` | **Admin / Manager** (`require_role([ADMIN, MANAGER])`) — the *Delete / restore purchase order (soft)* row above |
>
> **Why the read carries no new gate:** `deleted_only=true` returns rows the same reader could
> already see *before* they were deleted — PO list/detail reads are tenant-scoped but not
> role-restricted (see *Read enforcement* above), so a PO that was readable on Monday does not become
> sensitive by being deleted on Tuesday. Gating the view would protect nothing while making the
> feature unusable: a manager cannot restore what a screen refuses to list. The privileged act is
> changing state, and that gate lives where the state changes — on the verb.
>
> **The UI is STRICTER than the server, and knowing which way round matters.** The Restore *button*
> matches the verb exactly: Purchasing → Purchase Orders → **Deleted** (an Active / Deleted segmented
> control) renders it only for Admin / Manager (`canRestorePO` in
> `frontend/src/pages/Purchasing.tsx`, a constant kept separate from the delete gate so a future
> change to one cannot silently move the other), so a hidden control and a refused call agree. The
> *view*, though, is narrower in the UI than on the server: `/purchasing` is gated on
> `purchasing:view` (`routeAccessRequirements` in `App.tsx`), which per
> `frontend/src/utils/permissions.ts` belongs to platform_admin / admin / manager / supervisor /
> viewer — so an **operator, quality or shipping** user cannot open the page at all, while their
> token *can* read the same rows straight from `GET /purchasing/purchase-orders`. Do not read the
> table above as "the UI reflects the endpoint": it does not, and the gap is the endpoint's, not the
> screen's.
>
> That looseness is **not specific to `deleted_only`** — it is how every read on this endpoint has
> always behaved, deleted or live, and gating the flag alone would be theatre (the same rows are
> readable pre-delete and via `?status=closed`) while breaking the feature, since a supervisor could
> no longer locate a PO for a manager to restore. The right fix is to bring `GET
> /purchasing/purchase-orders` and `GET /purchasing/purchase-orders/{id}` as a whole under
> `require_role([PLATFORM_ADMIN, ADMIN, MANAGER, SUPERVISOR, VIEWER])` so the server matches
> `purchasing:view` — vendor names, unit pricing and order totals are not an operator's or shipping
> clerk's to read. **That is an open owner decision and a separate change.** It applies verbatim to
> `GET /purchasing/vendors` — including its `deleted_only` view — for the same reasons and with the
> same non-fix; the vendor half of this note is below.
>
> Both directions stay audited (`log_delete` with `soft_delete=true`; `log_update` with
> `action="restore"`), so who deleted and who restored are both on the chain.
>
> **Vendor restore is gated the same way, and it now has a screen.** It was for a long time the
> asymmetric case — the verb existed and was audited while nothing could reach it — and that is
> closed: `GET /purchasing/vendors?deleted_only=true` lists the tenant's soft-deleted vendors, and
> Purchasing → **Vendors** → **Deleted** renders them with a **Restore** control. The split is
> identical to the PO one above, deliberately, so there is one posture to learn rather than two:
>
> | Half | Where | Gate |
> |---|---|---|
> | **See** deleted vendors | `GET /purchasing/vendors?deleted_only=true` — the only read in the API that returns a soft-deleted vendor | **Any authenticated user in the tenant** (`get_current_user`) |
> | **Restore** one | `POST /purchasing/vendors/{id}/restore` | **Admin / Manager** (`require_role([ADMIN, MANAGER])`) — the *Delete / restore vendor (soft)* row above |
>
> **Why the read carries no new gate** — same argument, and it is not a copy for tidiness: a vendor
> that was readable on Monday does not become sensitive by being deleted on Tuesday, vendor
> list/detail reads are tenant-scoped but not role-restricted (see *Read enforcement* above), and
> gating the view would protect nothing while making the feature unusable — a manager cannot restore
> what a screen refuses to list. The privileged act is changing state, and that gate lives on the
> verb. The **UI** matches the verb exactly (`canRestoreVendor` in
> `frontend/src/pages/Purchasing.tsx`, a constant kept separate from `canRestorePO` and from the
> delete gate so a change to one cannot silently move the others), so a hidden control and a refused
> call agree. The same UI-is-stricter-than-the-server caveat applies as for POs: `/purchasing` is
> gated on `purchasing:view`, so operator / quality / shipping users cannot open the page while their
> token can still read the rows straight from the endpoint. That is the endpoint's gap, not the
> screen's, and it is the open owner decision noted above.
>
> **One thing the role gate does *not* decide: what a restore reactivates.** Restoring a vendor puts
> back the `is_active` it had when it was deleted — it is **not** an unconditional re-activate, which
> is what the verb used to do. A supplier the shop deliberately switched off before deleting comes
> back switched off, and a manager who restores it has not thereby re-approved it. A vendor deleted
> **before** migration `082` has no recorded prior state and, by owner decision, restores **inactive**
> as well — the safe reading of an unknown approval flag is *off*, so the restore never hands back an
> active-looking supplier on a guess. That is an
> AS9100D 8.4 property of the approved-supplier list rather than an RBAC one, but it is the reason a
> Manager-level restore is safe to offer at all: the verb can undo a records mistake without being
> able to hand back supplier approval as a side effect. Re-activating stays a separate, separately
> audited `PUT /purchasing/vendors/{id}` under the same Admin / Manager gate, performed from
> Purchasing → **Vendors** → **Inactive** → **Edit** (the third view of the same segmented control;
> a restored-but-switched-off vendor is on no other screen, so without it that "separate, deliberate
> step" would have no way to be taken). The UI gate there is `canCreateVendor`
> (`purchasing:approve` — the same Admin / Manager set the `PUT` enforces), not `canRestoreVendor`:
> the button calls the update endpoint, so it must match the update endpoint. See `docs/API.md` →
> Purchasing → *Restoring a vendor returns the record, not the approval*.

### Receiving

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Correct receipt (in place) | ✓ | ✓ | ✓ | | | | |
| Void receipt (soft-delete) | ✓ | ✓ | | | | | |
| Clear inspection hold (waive) | ✓ | ✓ | ✓ | | ✓ | | |
| Print / reprint receiving label | ✓ | ✓ | ✓ | | | | |
| Configure print profile | ✓ | | | | | | |

> **Write enforcement:** The Create and Inspect rows above are now enforced **in code** on
> the canonical `/api/v1/receiving` endpoints (`app/api/endpoints/receiving.py`):
> `POST /receiving/receive` → `require_role([ADMIN, MANAGER, SUPERVISOR])` and
> `POST /receiving/inspect/{receipt_id}` → `require_role([ADMIN, MANAGER, QUALITY, SUPERVISOR])`
> — the receive-capable roles may also complete incoming inspection, plus Quality; adding
> Supervisor is an owner-approved spec change (2026-07-16, previously excluded)
> (superuser / Platform Admin bypass role checks, as elsewhere). This replaces a prior state
> where the receive endpoint was not role-restricted and a duplicate receiving/inspection
> path existed under `/api/v1/purchasing`; that duplicate has been removed, so `/api/v1/receiving`
> is the single source of truth. Receiving reads follow the same read-broad / write-restricted
> pattern noted for Purchasing above.

> **Correct / void receipt — endpoint mapping.** Fixing a mis-keyed receipt is enforced **in code** on
> `app/api/endpoints/receiving.py`:
> - **`PATCH /receiving/receipt/{receipt_id}`** (correct in place — new total quantity + optional
>   traceability fields, required reason) → `require_role([ADMIN, MANAGER, SUPERVISOR])`, the **Correct
>   receipt** row — the same receive-tier set that may `POST /receiving/receive` and post inventory
>   adjustments.
> - **`POST /receiving/receipt/{receipt_id}/void`** (soft-delete + full reversal, required reason) →
>   `require_role([ADMIN, MANAGER])`, the **Void receipt** row — deliberately tighter (void is delete
>   authority; Supervisor can correct but not void).
>
> `POReceipt` gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`); void is a soft
> delete (invariant #3) and is **terminal — there is no restore** (re-receive to redo). Both actions
> require a non-blank `reason`, are fully tamper-evidently audited, and reconcile the PO line, PO
> status, and (dock-to-stock) inventory — the historical `RECEIVE` transaction is never mutated;
> reversal is a signed compensating `ADJUST`. Corrections/voids are refused after the receipt is
> inspected, or once its stock has been allocated/consumed. See `docs/API.md` → Receiving & Inspection.

> **Clear inspection hold — endpoint mapping, and why it matches Inspect exactly.**
> **`POST /receiving/receipt/{receipt_id}/clear-inspection`** (waive an inspection hold placed by
> mistake, required reason) → `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`, the **Clear
> inspection hold** row. This is the non-destructive third post-receipt verb: the receipt and its
> lot/heat/cert are kept exactly as keyed, it is re-classified dock-to-stock
> (`inspection_status = not_required`), the material posts into inventory, and it drops off the
> inspection queue.
>
> **The role list is deliberately identical to Inspect, and that identity is the control.** An earlier
> draft excluded Supervisor on a segregation-of-duties argument: Supervisor is the receiving-clerk tier
> (it holds **Create**, so it is the tier that ticks the "requires inspection" box in the first place),
> and letting it waive the hold would let the same person quietly undo their own click. **The owner
> overruled that, and the reason is a records-integrity one, not convenience.**
>
> Excluding Supervisor closed the **honest** exit and left the **dishonest** one open. A supervisor
> facing a mis-ticked receipt with no manager on site did not leave it sitting on the queue — they put
> it through **Inspect → Visual → Pass**, because Supervisor holds Inspect and always has. That writes
> a named inspector and a timestamp onto an inspection that never happened: a **fabricated quality
> record**, and precisely the AS9100D records-integrity defect PR #127 fixed in code — reintroduced by
> a human instead of by the code. Whoever is trusted to record that a lot **passed** is already trusted
> to record that it never needed inspecting, and of the two records that tier can produce, the
> reasoned, attributed, hash-chained waiver is the strictly more truthful one. So the gate now grants
> the truthful record rather than forcing the false one.
>
> **Keep the two lists in lockstep.** If Inspect and Clear-hold ever diverge again, the tier holding
> Inspect and not Clear-hold gets the fabricated pass back as its only exit. **Void** is the one that
> stays tighter (Admin / Manager) — that is *delete* authority, a different question from whether an
> inspection was owed.
>
> Lockstep includes **`PLATFORM_ADMIN`**, which is why `Receiving.tsx`'s `canClearInspection` lists it
> and its neighbours (`canCorrectReceipt` / `canVoidReceipt` / `canDeletePO`) do not. Platform admin
> sits outside the matrix below — `api/deps.py :: require_role` admits it before the list is read — so
> the frontend convention elsewhere is to omit it and simply be conservative. Here that would be a
> *behavioural* divergence, not a cosmetic one: `frontend/src/utils/permissions.ts` grants
> `platform_admin` the `receiving:inspect` permission that drives the Inspect button, so omitting it
> from Clear-hold would put a context-switched platform admin on the queue holding Inspect and **not**
> Clear-hold — the exact one-way door this list closes, made worse by the fact that the old
> "ask a manager or Quality" hint has been removed. A parametrized frontend test asserts the general
> rule (*no role sees Inspect without Clear Hold*) rather than the four names, so the pair cannot
> drift apart again silently.
>
> | Action | Roles | Rationale |
> |---|---|---|
> | Receive (sets the flag) | Admin / Manager / **Supervisor** | Receive-tier clerical work. |
> | Correct receipt | Admin / Manager / **Supervisor** | Same receive-tier — fixing your own keying. |
> | Inspect (resolves the hold with a real inspection) | Admin / Manager / **Quality** / **Supervisor** | Completing a *real* inspection is receive-tier work, plus Quality. |
> | **Clear inspection hold (waives the hold)** | Admin / Manager / **Quality** / **Supervisor** | **Same list as Inspect, on purpose** — the tier that can record a pass must also be able to record the truthful waiver, or it records the false pass instead. |
> | Void receipt | Admin / Manager | Delete authority, tighter still. |
>
> **What this gate is NOT, stated plainly because this document is what an auditor is shown:** it is
> **not** a two-person rule and never was. Admin, Manager and Supervisor all hold `receiving:create`,
> so every role that can clear a hold can also have placed one — anyone here can waive their own
> mis-click. The control this endpoint provides is **attribution and visibility**, not separation of
> duties: see the paragraph below for what it actually enforces.
>
> The gate is not the only control: the endpoint **requires a non-blank `reason`** (1–500 chars,
> trimmed), writes a tamper-evident `audit_log` **status change** (`pending_inspection` → `accepted`)
> carrying that reason, and emits a `receipt_inspection_cleared` operational event — so a waiver is
> always attributable to a named user with a stated justification (invariant #2). It refuses **409**
> for any receipt not currently `pending_inspection`, which is also the replay guard. Records
> integrity is preserved: `inspection_status` becomes `not_required`, **never `passed`**, and
> `inspection_method` / `inspected_by` / `inspected_at` stay NULL — no inspection happened, so the
> record must not claim one (AS9100D, the PR #127 rule). The waiver is also stamped outside the
> Admin/Manager-only audit log, which matters precisely because **Quality and Supervisor can perform
> this waiver but cannot read `GET /audit/`** — but the two stamps are not equally durable, and the
> difference is worth stating:
>
> - The **`RECEIVE` ledger row** carries `reason_code="INSPECTION_HOLD_CLEARED"` plus the reason in
>   its notes. This is the **durable in-app trace for non-audit-readers**: `inventory_transactions`
>   is append-only in practice (corrections are compensating rows, never edits — invariant 3/6) and
>   it is rendered in-app on Warehouse → Inventory → **Stock Movements**, which Supervisor and
>   Quality can both reach with `inventory:view`.
> - The line appended to **`receipt.notes`** is a **convenience copy on the record itself, not a
>   protected one.** Clearing a hold leaves `inspection_status = not_required`, which is *not* in
>   `_INSPECTED_STATUSES`, so the receipt stays correctable — and `PATCH /receiving/receipt/{id}`
>   (Admin / Manager / **Supervisor**) assigns `receipt.notes` wholesale from the payload, which the
>   Correct dialog prefills into a plain textarea. A supervisor correcting a quantity the next day
>   can retype that box and drop the waiver line without noticing it was one. Nothing is *lost* when
>   that happens — the immutable `audit_log` status-change row and the correction's own `log_update`
>   (carrying old → new `notes`) both survive, as does the ledger row above — but do not describe the
>   on-receipt stamp as tamper-evident. It is durable against every tier except the one that both
>   writes and edits it.
>
> The endpoint additionally fires the `receipt.inspection_cleared` notification to **Manager +
> Quality**.
>
> **The previously recorded "known gap" is closed by this role list**, and closed in the direction the
> owner chose: Supervisor was added here rather than removed from Inspect. The gap was that a
> Supervisor blocked from the waiver still held Inspect and could stamp a fabricated Visual pass. It is
> no longer mitigated by UI wording — the honest verb is simply available to the same tier. **Owner
> decision — see `docs/API.md` → Receiving & Inspection.**

> **Not every control on the Receiving page is gated by the rows above.** The open-PO list on the
> Receive tab carries a **Delete PO** control wired to `DELETE /purchasing/purchase-orders/{po_id}` —
> a *Purchasing* permission (**Admin / Manager**, the *Delete / restore purchase order* row in the
> Purchasing matrix), not a Receiving one, and it is the tighter of the two lists. Gate any new
> control on the page against the endpoint it calls, not against the page it lives on. See
> `docs/API.md` → Receiving & Inspection → *Deleting an open PO from the Receiving page*.

> **Thermal receiving-label printing (ProxyBox / WHTP203e).** Manually (re)printing the
> 4×6 receiving label — `POST /receiving/receipt/{receipt_id}/print-label` — is enforced
> to **Admin / Manager / Supervisor** via `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> the same gate as `POST /receiving/receive`. Configuring the per-company print profile —
> `GET` / `PUT /receiving/print-profile` (ProxyBox base URL / target / API key, copies,
> paper size, and the `auto_print_on_receipt` + `allow_print_egress` toggles) — is
> **admin-only** via `get_admin_user`, so only an admin can enter the printer credential
> or flip the outbound-egress kill switch (default OFF, audited as a status change). See
> [docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md).

### Shipping

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | ✓ | |
| Complete | ✓ | ✓ | ✓ | | | ✓ | |
| Rate-shop / validate address (`shipping:rate`) | ✓ | ✓ | ✓ | | | ✓ | |
| Buy label / BOL / schedule pickup (`shipping:label`) | ✓ | ✓ | ✓ | | | ✓ | |
| Void / refund label (`shipping:void`) | ✓ | ✓ | ✓ | | | ✓ | |
| Issue Certificate of Conformance | ✓ | ✓ | | | ✓ | | |

> **Carrier-integration write actions — endpoint mapping (multi-carrier shipping integration).**
> The carrier actions on `app/api/endpoints/shipping.py` —
> `POST /shipping/validate-address`, `POST /shipping/{id}/rate-shop`,
> `POST /shipping/{id}/buy-label`, `POST /shipping/{id}/buy-bol`,
> `POST /shipping/{id}/schedule-pickup`, `POST /shipping/{id}/void-label`, and
> `POST /shipping/{id}/refund` — are enforced **in code** to
> `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])` (`CARRIER_WRITE_ROLES`). They transmit
> customer data to a carrier (gated by the per-company `allow_carrier_egress` kill switch in the
> service) and move money (label/BOL/void/refund are audited), so they carry the same role set that
> may complete a shipment. The new permission strings `shipping:rate`, `shipping:label`, and
> `shipping:void` (in `app/models/role_permission.py`, granted to Admin / Manager / Supervisor /
> Shipping) drive the **frontend** `PermissionGate` / `usePermissions` visibility; the
> `require_role` lists above are the authoritative server-side control. The read endpoints
> (`GET /shipping/{id}/rates`, `GET /shipping/{id}/tracking`) stay open to any authenticated tenant
> user (read-broad / write-restricted). See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md) and `docs/API.md` →
> Shipping. The inbound carrier tracking webhook (`POST /webhooks/carriers/{provider}`) is
> **unauthenticated by design** — see the Admin → Integrations note below.

> **Complete (mark shipped) — endpoint mapping (2026-06-09).** The Shipping **Complete** action
> `POST /api/v1/shipping/{shipment_id}/ship` (`mark_shipped`) is now enforced **in code** to the
> Complete row via `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])`
> (`app/api/endpoints/shipping.py`). **This is a permission change:** the endpoint was previously open
> to **any authenticated user**, who could close a work order by shipping it; a non-privileged user now
> receives **403**. Marking shipped is the terminal shipping action that transitions the work order to
> `CLOSED`, so it carries the **Complete** permission (not the broader View/Create reads).
>
> **Certificate of Conformance — endpoint mapping (Batch 11C / G6-B).** Issuing a CoC
> `POST /api/v1/shipping/{shipment_id}/coc` (mint or return the existing frozen-snapshot CoC) is
> enforced **in code** to `require_role([ADMIN, MANAGER, QUALITY])`
> (`app/api/endpoints/shipping.py`) — a quality artifact, so the write is restricted (this is why the
> matrix row above does **not** include the **Shipping** role, which otherwise holds Shipping
> Create/Complete). Reading the CoC — `GET /shipping/{shipment_id}/coc` (metadata) and
> `GET /shipping/{shipment_id}/coc/pdf` (rendered PDF) — is open to **any authenticated user** in the
> active company (read-broad / write-restricted, like the other shipping reads). All three are
> tenant-scoped (cross-tenant `shipment_id` → **404**). A CoC is also **auto-issued on ship** when
> required; the auto-issue runs in the ship handler's context and is not separately role-gated beyond
> the existing ship permission.

### Quality

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Approve | ✓ | ✓ | | | ✓ | | |
| Calibration | ✓ | ✓ | | | ✓ | | |
| Manage scrap reason codes | ✓ | ✓ | | | ✓ | | |
| Void / restore NCR | ✓ | ✓ | | | ✓ | | |

> **Inspect — endpoint mapping.** The shop-floor inspection sign-off
> `POST /api/v1/shop-floor/operations/{operation_id}/inspection` (which records
> `WorkOrderOperation.inspection_complete = True` and clears the completion inspection quality gate)
> is enforced **in code** to this Inspect row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`,
> `mark_operation_inspected`). The role set matches the matrix exactly — this repo has no separate
> `INSPECTOR` role, so operation inspection is performed by Admin / Manager / Supervisor / Quality.
>
> **Scrap reason codes (Lean Phase 1) — read-broad / write-restricted.** Managing the tenant's
> structured scrap vocabulary (`POST /api/v1/quality/scrap-reason-codes`,
> `PUT /quality/scrap-reason-codes/{id}`) is a quality-system configuration task, enforced **in
> code** via `require_role([ADMIN, MANAGER, QUALITY])` (`SCRAP_REASON_WRITE_ROLES` in
> `app/api/endpoints/scrap_reasons.py`) — the same write set that owns the NCR/CAR vocabulary. The
> **read** (`GET /quality/scrap-reason-codes`) depends on `get_current_user` only — any
> authenticated user in the tenant, including Operators via the kiosk/desktop scrap pickers — so
> the matrix row above reflects the server-enforced **write** control. There is no delete endpoint:
> retirement is `is_active: false` (historical scrap rows reference these ids — traceability).
>
> **Void / restore NCR — endpoint mapping.** `DELETE /quality/ncr/{ncr_id}` (void) and
> `POST /quality/ncr/{ncr_id}/restore` (`app/api/endpoints/quality.py`) are enforced **in code** to the
> **Void / restore NCR** row above — `require_role([ADMIN, MANAGER, QUALITY])`. `NonConformanceReport`
> gained `SoftDeleteMixin` (migration `071_soft_delete_purchasing_ncr`); a void is a soft delete
> (invariant #3) that also moves the NCR to the existing `VOID` status, requires a **non-blank
> `reason`**, and is **refused (400)** while the NCR still actively gates a work order (an
> `OPEN`/`ACKNOWLEDGED` `WorkOrderBlocker` references it). Restore reopens it to `OPEN`. Both are
> tamper-evidently audited — the void writes a status-change **and** a delete row, closing a prior gap
> where the `PUT /quality/ncr/{id}` update path wrote no `audit_log` row at all. See `docs/API.md` →
> Quality.

### Operator Certifications & Training

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | | | ✓ | | |
| Edit | ✓ | ✓ | | | ✓ | | |
| Delete | ✓ | ✓ | | | ✓ | | |

### Skill Matrix

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |

> **Write enforcement — these role sets are new (defaults chosen for this fix, 2026-06-09).** The
> seven write endpoints on the operator-certifications router
> (`app/api/endpoints/operator_certifications.py`, mounted at `/api/v1/operator-certifications`) are now
> enforced **in code**; the RBAC matrix previously had **no rows** for these record types and the writes
> were open to any authenticated user.
> - **Certifications + training writes** —
>   `POST/PUT/DELETE …/certifications/{…}` (`create_certification` / `update_certification` /
>   `delete_certification`) and `POST/PUT …/training/{…}` (`create_training` / `update_training`) —
>   require `require_role([ADMIN, MANAGER, QUALITY])` (`CERT_TRAINING_WRITE_ROLES`). These are
>   operator-qualification / conformance records that Quality owns alongside Admin/Manager.
> - **Skill-matrix writes** — `POST …/skill-matrix/` (`create_skill_entry`, which upserts) and
>   `PUT …/skill-matrix/{entry_id}` (`update_skill_entry`) — require
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`SKILL_MATRIX_WRITE_ROLES`), because skill-matrix
>   entries are competency assessments performed by Supervisors (and above).
>
> Any other authenticated role now receives **403**. **Read** endpoints (the certifications dashboard /
> list / by-user / by-id, training list / by-user, and the skill-matrix check / by-user /
> by-work-center / list) stay open to **any authenticated user**, tenant-scoped — the read-broad /
> write-restricted model. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Writes are audited + FK-validated.** Each write now records a tamper-evident `audit_log` row
> (resource types `operator_certification` / `training_record` / `skill_matrix`; create / update /
> delete). The create endpoints (and `update_training`'s re-pointed `work_center_id`) reject a
> `user_id` / `work_center_id` that does not belong to the active company with **422** before insert
> (cross-tenant FK-injection guard). See `docs/API.md` and `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`.

### Engineering Change Orders (ECO)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit | ✓ | ✓ | | | | | |
| Submit / Approve / Reject | ✓ | ✓ | | | | | |
| Implement / Complete | ✓ | ✓ | | | | | |
| Add / Edit task, Add approval | ✓ | ✓ | | | | | |

> **ECO mutations are Admin / Manager (enforced in code).** Every state-changing ECO endpoint
> (`POST /eco/eco/`, `PUT /eco/eco/{id}`, and the `submit` / `approve` / `reject` / `implement` /
> `complete` transitions, plus `tasks` create/update and `approvals` create) is gated with
> `require_role([ADMIN, MANAGER])` (`app/api/endpoints/engineering_changes.py`). Any other authenticated
> role receives **403**. The read endpoints (list, get, dashboard, list approvals, affected items) remain
> open to all authenticated users. Previously these mutations were available to **any** authenticated
> user — this row records the tightened authorization landed in WO-completion remediation Batch 11A
> (G4-Fix1), alongside the ECO router's tenant scoping and audit logging.

### Users

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | | | | | |
| Create | ✓ | | | | | | |
| Edit | ✓ | | | | | | |
| Delete | ✓ | | | | | | |
| Roles | ✓ | | | | | | |
| Own profile + notification settings (`/users/me/*`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Self-scoped `/users/me/*` routes are open to every authenticated role — by construction, not by
> grant.** `GET /users/me`, `PUT /users/me/phone`, `GET`/`PUT /users/me/notification-preferences`,
> and `POST /users/me/test-sms` (`app/api/endpoints/users.py`) carry **no `require_role`**: they
> read and write only `current_user` and **never accept a user id**, so there is no id to authorize
> and no path to another user's record. The backing UI is **My Settings** (`/settings`), routed for
> all authenticated roles. Notes:
> - **Phone changes are audited** on both the self-service path (`extra_data.source =
>   "self_service"`) and the Admin `POST /users/` / `PUT /users/{id}` paths — the phone is the
>   destination of every SMS alert. Numbers are normalized to E.164; an invalid number is **400**.
> - **`phone` is field-minimized**: it serializes only in the self-profile / Admin-Manager
>   user-management `UserResponse`, never in general user serialization. See
>   [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#phone-is-field-minimized).
> - **`POST /users/me/test-sms` targets the caller's own number only** (never a caller-supplied
>   destination), is gated by the company `allow_sms_egress` kill switch, and is rate-limited
>   **3/minute** per IP.
> - Saving preferences cannot bypass a **mandatory** channel: the dispatcher re-applies the
>   catalog's `mandatory_channel` at send time regardless of the stored row, so a mandatory-critical
>   event can never be fully muted.

> **User writes are Admin-only, and both `require_role([ADMIN])`** — `POST /users/` (create),
> `PUT /users/{id}` (edit, incl. role assignment), `DELETE /users/{id}` (deactivate), and
> `POST /users/{id}/unlock` (clear the 5-failed-logins/30-minute lockout: resets
> `failed_login_attempts`, clears `locked_until`) all gate to
> **Admin** (`app/api/endpoints/users.py`). The **View** rows are the governance-read exception noted
> above: `GET /users/` (list) and `GET /users/{id}` are `require_role([ADMIN, MANAGER])`, so a
> **Supervisor** gets a **failed load** (403), not a read — user records are *not* on the read-broad
> domain default. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **`platform_admin` is never assignable from a tenant path, and admins cannot self-elevate.** Both
> user-write endpoints now enforce the same guards as user import (below, under Bulk Imports):
> - **`POST /users/` and `PUT /users/{id}` reject `role = platform_admin` with 400**
>   (`"Platform admin role cannot be assigned"`). `platform_admin` is the cross-company Werco
>   oversight role and can never be minted from a tenant-scoped path — not by create, update,
>   approval (`POST /users/{id}/approve`, `"…cannot be assigned through approval"`), or import — even
>   by a company Admin.
> - **Self role-escalation guard:** on `PUT /users/{id}`, an Admin editing **their own** record cannot
>   change **their own** role (**400**, `"You cannot change your own role"`); editing their own
>   name/email/other fields stays allowed. This mirrors the delete endpoint's "cannot deactivate
>   yourself" self-guard, so an Admin cannot self-elevate and a role change to one's own account must
>   be made by a different Admin.
>
> Every user mutation — create, update (including any role change), approve, password-reset,
> deactivate, activate, and unlock — is recorded in the tamper-evident audit log (unlock as a
> `STATUS_CHANGE` `locked` → `unlocked` when the lock was still in force, as an `UPDATE` of the two
> fields when it only cleared residual state — an expired lock or attempts short of 5 — and the
> endpoint is idempotent: a no-op unlock writes no row); the self-service
> `POST /users/change-password` likewise records a `PASSWORD_CHANGE` audit event (mirroring
> `reset-password`; the password/hash is never included).
>
> **Password-strength policy.** A password set on any of these paths — `POST /users/` (create),
> `POST /users/{id}/reset-password`, and self-service `POST /users/change-password` — must satisfy
> the server-side strength policy (≥ 12 chars; and no common weak substring from the ~37-entry
> blocklist in `app/schemas/user.py`), the **same policy** as `POST /auth/register`. There are **no
> character-class requirements**: the uppercase / lowercase / number / special-char rules were removed
> on 2026-07-29 in favor of length + blocklist per NIST SP 800-63B §5.1.1.2, and the blocklist was
> expanded in the same change. The same policy also governs
> the **first-admin `admin_password`** on the two company-creation paths — the unauthenticated
> `POST /companies/register` (company self-registration) and platform-admin `POST /platform/companies`.
> The user CSV import applies it per row to user-supplied passwords; operator auto-generated (badge)
> passwords are exempt. See `docs/API.md` → Users.
>
> **Badge printing (A0.4).** The badge print sheet `/print/badges` (opened from the Users page via
> multi-select → "Print Badges") is **frontend-gated by `canManageUsers`** (=
> `users:create` OR `users:edit`) — both the Users-page button
> (`frontend/src/utils/permissions.ts`) and the `/print/badges` route (route map in
> `frontend/src/App.tsx`) require it. After user management was aligned to Admin-only, only **Admin**
> holds those permission strings (plus Platform Admin / superuser), so badge printing is now
> **effectively Admin-only**: a **Manager** (who holds `users:view` — the read-only list) and a
> **Supervisor** (who holds no `users:*` permission) never see the Print Badges control or reach the
> route. No new endpoint or permission string was added: badges are client-rendered QR codes of
> `users.employee_id`, and the page loads its data from the existing `GET /api/v1/users/`, which is
> server-enforced to `require_role([ADMIN, MANAGER])` (see the access-enforcement note above). The
> badge gate is therefore **narrower than** that read split — a Manager can open the Users page and
> read the list but cannot print badges — consistent with the Admin-only user-write posture in the
> note directly above.

### Bulk Imports (Import Center / Excel Migration Kit)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Download templates (`GET /import/templates*`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Import users (`POST /users/import-csv`) | ✓ | | | | | | |
| Import parts / materials | ✓ | ✓ | ✓ | | | | |
| Import customers / vendors / work centers | ✓ | ✓ | | | | | |
| Import routings (`POST /routing/import/preview`, `/import/commit`) | ✓ | ✓ | ✓ | | | | |
| Import open work orders (`POST /work-orders/import`) | ✓ | ✓ | ✓ | | | | |
| Import open purchase orders (`POST /purchasing/purchase-orders/import`) | ✓ | ✓ | | | | | |

> **Endpoint mapping (A0.2 Excel migration kit, enforced in code).** All rows above apply
> identically to dry-run (`?dry_run=true`) and commit calls.
> - **Templates are open to any authenticated user** (`get_current_user`): the XLSX templates are
>   static workbooks containing **no tenant data**, so listing/downloading them carries no read risk.
> - **Open-WO import mirrors Work Orders → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/work_orders.py`) — importing an
>   open work order creates+releases a WO through the same generation path as `POST /work-orders/`,
>   so it carries exactly the WO Create/Release role set.
> - **Open-PO import is Admin / Manager only — deliberately narrower than WO import**:
>   `require_role([ADMIN, MANAGER])` (`app/api/endpoints/purchasing.py`). Imported POs land directly
>   in **`sent` (issued)** status, and the interactive PO `/send` transition is Admin/Manager-only —
>   allowing Supervisor here would let a spreadsheet issue POs its holder cannot issue in the UI
>   (privilege escalation via import).
> - **User import is Admin-only and cannot mint `platform_admin`**: a row with
>   `role = platform_admin` is rejected per-row (`"role 'platform_admin' cannot be assigned via
>   import"`), and `platform_admin` is excluded from the advertised valid-roles list. The
>   platform-admin role is the cross-company Werco oversight role and must never be assignable from
>   a tenant spreadsheet, even by a company Admin.
> - **Routing import mirrors Routings → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/routing.py`) on both the
>   `/routing/import/preview` (dry-run) and `/routing/import/commit` endpoints — it creates **draft**
>   routings through the same path as `POST /routing/`, so it carries exactly the Routings Create role
>   set (Release stays Admin/Manager — imported routings land as draft and must be released
>   separately). The **frontend** gates the Routing page **Import Routings** button (which opens the
>   `RoutingImportWizard` dry-run/commit modal) on the `routings:create` permission via
>   `hasPermission` (`frontend/src/pages/Routing.tsx`), matching this server-side role set —
>   operator / quality / shipping / viewer never see the button. The Import Center's **Routings** tab
>   (`mode: 'linked'`) only surfaces the template download + column hints and links to the Routing
>   page; the upload/preview/commit lives in the wizard, not in the Import Center.
> - The entity-import role sets (parts/materials → A/M/S; customers/vendors/work centers → A/M) are
>   unchanged from the pre-existing CSV imports and match each module's Create row above.
> - **Audit:** every committed import row writes a tamper-evident `audit_log` entry tagged
>   `source = "import"`; dry runs write nothing (savepoint rollback). See `docs/API.md` →
>   Bulk Imports & Templates and `docs/EXCEL_MIGRATION_RUNBOOK.md`.

### Bulk Data Export

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Export any bulk dataset (CSV / XLSX) | ✓ | ✓ | | | | | |

**This row is server-enforced**, unlike the **View** rows for the same modules — see
[Access enforcement model](#access-enforcement-model). Supervisor, Operator, Quality, Shipping and
Viewer can still *read* every one of these datasets in the UI; what they cannot do is download the
whole set as a file.

Every route below is `require_role([ADMIN, MANAGER])`, tenant-scoped via `get_current_company_id`,
and writes an `EXPORT` audit row through `AuditService`:

| Endpoint | Dataset (`resource_type`) | Notably discloses |
|---|---|---|
| `GET /api/v1/exports/work-orders/export` | `work_order` | customer name / customer PO, scrap quantities |
| `GET /api/v1/exports/parts/export` | `part` | **`standard_cost`**, reorder points, customer part numbers |
| `GET /api/v1/exports/inventory/export` | `inventory_item` | **`unit_cost` + computed `total_value`**, lot / serial, locations |
| `GET /api/v1/exports/purchase-orders/export` | `purchase_order` | vendor identity, subtotal / tax / shipping / total |
| `GET /api/v1/exports/purchase-orders/lines/export` | `purchase_order_line` | **`unit_price`** per line — the open purchase commitments |
| `GET /api/v1/exports/quotes/export` | `quote` | **customer contact + email (PII)**, subtotal / total |
| `GET /api/v1/exports/inventory/transactions/export` | `inventory_transaction` | the whole valuation ledger incl. free-text `notes` |
| `GET /api/v1/analytics/custom-report/export` | `custom_report` | a tenant-authored query over a whole data source — unbounded by construction |

> **Two surfaces sit at a stricter tier and stay there.** `GET /visitor-logs/export.csv` is
> `require_role([ADMIN, MANAGER])` because **visitor PII is a documented exception to read-broad**
> (its list endpoint is gated too) — a PII decision, not this one, and it is the audit-shape
> precedent the seven `/exports/*` routes now follow.
> `GET /estimate-workbench/{id}/export/audit.xlsx|.json|customer.pdf` is
> `require_role([ADMIN, MANAGER, SUPERVISOR])` and is **out of scope**: the path carries an estimate
> id, so it is one record, not a dataset. The gate above is a **floor** — an existing stricter tier
> is never loosened to meet it.

> **The audit row records the request, never the payload** (`app/services/export_audit.py`):
> `action="EXPORT"`, `resource_type` = the dataset, the row count in the description, and
> `new_values` carrying the format, the columns actually disclosed and the filters that selected the
> rows. It is committed **before** the file streams, so an abandoned download is still on record. A
> **403 leaves no row** — a refusal disclosed nothing.
>
> **Caller-supplied text is fenced on the way in, because on the way out there is no remedy.** An
> `audit_log` row is un-`UPDATE`-able and un-`DELETE`-able (the `008`/`060` triggers) and is covered
> by the integrity hash, so anything the request puts in `new_values` is permanent. Two fences,
> because the two inputs have different shapes: `columns` is fenced by **allowlist** (only names the
> endpoint recognizes are recorded — there is a known set to intersect against), and filter *values*
> are fenced by **length** (free text by definition, so `max_length` on each `Query` — a value past
> the bound is a **422** and writes nothing, and the bound is set at the width of the column the
> filter compares against so nothing that could match a row is ever refused). `export_audit._cap`
> re-applies the length bound inside the shared seam as a backstop for a future exporter that forgets
> to declare one. Coverage: `backend/tests/api/test_export_gate_and_audit.py`.
>
> **"Audited" here is best-effort, as it is everywhere else in the app.** `AuditService.log` never
> propagates an audit failure to the caller (`app/services/audit_service.py`), so if the chain write
> itself fails the export still streams and goes unrecorded. That is the repo-wide contract, not
> something specific to exports — the same is true of the visitor-log export and every audited write
> path — but read the row above as "every export writes an audit row on the success path", not as an
> absolute guarantee that no byte can leave without one.

> **Not bulk exports, deliberately ungated:** single-record documents — `GET /shipping/{id}/coc/pdf`,
> `POST /quotes/{id}/generate-pdf`, `GET /laser-nests/{id}/document`,
> `GET /shop-floor/documents/{id}/inline` (gating it would break the kiosk document viewer),
> `GET /documents/{id}/download`, `GET /po-upload/pdf/{path}`,
> `GET /rfq-packages/{id}/internal-estimate-export` — and `GET /import/templates/{entity}`, a static
> workbook containing no tenant data. The frontend `<DataTable>` CSV button is also untouched: it
> serializes rows the client already fetched through ordinary reads, so gating it would require
> gating the reads.

### Analytics

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Flow / WIP-aging / adoption (Lean Phase 1) | ✓ | ✓ | ✓ | | | | |
| FPY / scrap Pareto (Lean Phase 1) | ✓ | ✓ | ✓ | | ✓ | | |
| Predictive forecasts (delivery / capacity / inventory demand) | ✓ | ✓ | ✓ | | | | |
| Export (`GET /custom-report/export`, audits an `EXPORT` action) | ✓ | ✓ | | | | | |

> **Lean Phase 1 analytics reads are role-gated in code.** `GET /api/v1/analytics/flow`,
> `GET /analytics/wip-aging`, and `GET /analytics/adoption` require
> `require_role([ADMIN, MANAGER, SUPERVISOR])`; `GET /analytics/fpy` and
> `GET /analytics/scrap-pareto` additionally admit **Quality**
> (`require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`) — yield and scrap categorization are
> quality-system reads. All five are read-only and tenant-scoped (`app/api/endpoints/analytics.py`).
> The pre-existing View row (overview / KPIs / trends / quality metrics) remains any-authenticated.
>
> **Predictive analytics are role-gated AND now tenant-scoped.**
> `GET /api/v1/analytics/predict/delivery/{work_order_id}`, `GET /analytics/predict/capacity` and
> `GET /analytics/predict/inventory-demand` require `require_role([ADMIN, MANAGER, SUPERVISOR])`.
> All three are read-only and write nothing (no ledger row, no audit row, no event).
>
> **The role gate was already there; tenancy was not.** `PredictionService` was constructed with a
> session and **no `company_id`**, so every read underneath these three routes ran unscoped — an
> invariant-1 defect, and the role gate did nothing to contain it: the ADMIN / MANAGER / SUPERVISOR
> check confirms the caller holds a privileged role *in their own company*, then the service read
> every company's data. `/predict/delivery/{work_order_id}` was the worst of the three — it resolved
> a caller-supplied sequential primary key with **no ownership check at all**, returning any tenant's
> work-order header plus its sequenced routing (machine and hours per step), which for a job shop is
> the process plan itself.
>
> Each route now takes `company_id: int = Depends(get_current_company_id)` — the **active** company,
> so platform-admin context switching is honoured — and passes it to the constructor, matching the
> sibling `AnalyticsService(db, company_id)` in the same router. `company_id` is a constructor
> argument rather than a per-call one so an unscoped construction is a `TypeError` rather than a
> silent platform-wide read. A foreign `work_order_id` now 404s **byte-identically to an absent one**
> (`{"detail": "Work order not found"}`), so the refusal is not an existence oracle (#189
> convention). Tenancy is pinned by `backend/tests/api/test_prediction_tenancy.py`.
>
> **Operational note for multi-company installs:** because these figures previously summed across
> every tenant, they will **drop** after this change — see
> [Predictive analytics behavior change](API.md#predictive-analytics-behavior-change) in `docs/API.md`.
> The old numbers were wrong; the smaller ones are correct.
>
> **`GET /reports/ship-otd` is any-authenticated (pre-existing reports posture).** The Lean Phase 1
> ship-based OTD/OTIF detail report follows `reports.py`'s convention — `get_current_user` only, no
> role gate, tenant-scoped. **Observation (compliance review, 2026-07-10):** this report exposes
> customer-name delivery rollups (per-customer OTD %, late counts) to every role in the tenant,
> including Operator/Viewer, under that pre-existing posture. If reports are later role-tiered,
> this endpoint should be revisited with the rest of `/reports/*`.

### OEE

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard / trends / six-big-losses / list records & targets) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Write (auto-calculate / create-edit-delete records & targets) | ✓ | ✓ | ✓ | | | | |

> **Write enforcement (read-broad / write-restricted).** The OEE **write/mutation** endpoints —
> `POST /api/v1/oee/calculate/{work_center_id}`, `POST`/`PUT`/`DELETE /oee/records`, and
> `POST`/`PUT`/`DELETE /oee/targets` — are now enforced **in code** to the Write row via
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`OEE_WRITE_ROLES` in `app/api/endpoints/oee.py`),
> matching the sibling Analytics-write posture. **This is a permission change:** these endpoints were
> previously open to any authenticated user. OEE **read** endpoints (`/oee/dashboard`, `/oee/trends`,
> `/oee/six-big-losses/{wc}`, and the list/get GETs for records and targets) depend on
> `get_current_user` only — they are tenant-scoped but not role-restricted, so operators/viewers can
> still load OEE dashboards. The **View** row therefore reflects intended UI visibility; the **Write**
> row is a server-enforced control. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Audit coverage (2026-06-09).** The OEE write endpoints now also write a tamper-evident `audit_log`
> row on every record/target create/update/delete (and the auto-calc upsert), so OEE mutations are on
> the hash chain alongside the role gate. No role change — audit-trail coverage only.

### Werco Copilot (read-only AI chat)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Chat (`POST /copilot/chat`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Endpoint vs. tool-level access.** The endpoint (`app/api/endpoints/copilot.py`) requires only
> an authenticated user (`get_current_user`) — it is **strictly read-only** (every copilot tool
> wraps an existing read path; nothing can be created, updated, or deleted), so the chat itself
> carries no role gate. **Tool-level access mirrors each tool's source endpoint**: all eight v1
> tools wrap any-authenticated reads. The `search_erp` tool **excludes employee (`user`-type)
> results entirely** — data minimization, so employee names/emails never enter model prompts
> regardless of the caller's role; the **Admin/Manager-only** gate on user results inside global
> search now applies to `GET /search` only. The tool registry
> (`CopilotToolSpec.allowed_roles` in `app/services/copilot_service.py`) supports fully
> role-restricted tools for the future: such tools are omitted from other roles' tool lists and
> refuse politely if invoked anyway.
>
> **Tenant scope is never model-controlled.** `company_id` is injected server-side from the
> active company (`get_current_company_id`) into every tool call; tool input schemas carry no
> tenant identifier, and undeclared input keys supplied by the model (including a `company_id`)
> are dropped before dispatch. Per-user rate limit: 20 requests/minute default
> (`COPILOT_RATE_LIMIT_PER_MINUTE`). See [docs/API.md](API.md) → Werco Copilot.

### Visitor Logs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View / search log (`visitor_logs:view`) | ✓ | ✓ | ✓ | | | | |
| Sign in / sign out a visitor | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Export log (CSV) | ✓ | ✓ | | | | | |
| Back-enter an offline visit (`POST /manual`) | ✓ | ✓ | | | | | |
| Delete (soft) a visitor record | ✓ | ✓ | | | | | |
| Manage sign-in stations (create / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/visitor-logs`, `app/api/endpoints/visitor_logs.py`).** The two
> **visitor write** endpoints — `POST /sign-in` and `POST /sign-out` — are gated by the dedicated
> `get_signin_principal` dependency, which accepts **either** a PIN-minted station signin token
> (`type="signin"`, the lobby tablet) **or any authenticated staff user**. So the "Sign in / sign
> out" row is open to every authenticated user (and to an unattended station tablet), not a role —
> it is **not** the `require_role` model the rows below use. The **list** endpoint `GET /` is
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (this is the server-enforced read gate the
> `visitor_logs:view` permission and the `/visitor-log` route mirror — visitor PII is *not* on the
> read-broad domain default). **Export** (`GET /export.csv`, audits an `EXPORT` action), **soft-delete**
> (`DELETE /{id}`), **staff back-entry** (`POST /manual` — records an offline visit with its actual
> past times; a staff access token only, the PIN-minted station token is **rejected**), and **all
> station administration** (`POST /stations`, `GET /stations`, `POST /stations/{id}/revoke`,
> `POST /stations/{id}/reset-pin`) are `require_role([ADMIN, MANAGER])`.
> Every query is tenant-scoped (staff via `get_current_company_id`; the tablet via the authoritative
> `signin_stations` row, never the client `cid`); visitor records are soft-deleted, never
> hard-deleted; and every state change is tamper-evidently audited (station writes record the station
> label as the actor). See [docs/API.md](API.md) → Visitor Logs and
> [docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md).
>
> **`station-login` is the only new public write surface.** `POST /visitor-logs/station-login` is
> unauthenticated by design — a tablet cannot present a JWT — but it is **PIN-gated**: it verifies the
> shared station PIN against the bcrypt `pin_hash` and a bad/revoked station or wrong PIN returns
> **401** (indistinguishable; the failed attempt is audited). It is therefore not on this permission
> matrix. Like the inbound carrier webhook, trust is established without a user role.

### Crew-Station Kiosk

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Manage kiosk stations (create / list / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/shop-floor/kiosk-stations` + `POST /auth/kiosk-badge-token`).**
> All four station-administration endpoints (`POST /kiosk-stations`, `GET /kiosk-stations`,
> `POST /kiosk-stations/{id}/revoke`, `POST /kiosk-stations/{id}/reset-pin`, in
> `app/api/endpoints/shop_floor.py`) are `require_role([ADMIN, MANAGER])` — the same set as
> visitor sign-in stations. Everything the crew terminal itself does is **not** on the role
> matrix, because neither of its credentials is a role-bearing user session:
>
> - The **station token** (`type="kiosk"`, PIN-minted via the public rate-limited
>   `POST /shop-floor/kiosk-stations/station-login`) carries no user identity and is honored by
>   exactly two things — the roster-enriched work-center-queue read (its own bound work center
>   only, via the dedicated `get_kiosk_or_user` dependency) and the badge-token mint. Every other
>   endpoint rejects it with **401**; tenant scope and revocation come from the authoritative
>   `kiosk_stations` row, never the client `cid`.
> - The **badge-minted operator token** (`POST /auth/kiosk-badge-token`, station-token-gated) is
>   a 5-minute `scope="kiosk"` access token for the badge-identified user — on the allowed paths
>   the operator IS `current_user`, so the shop-floor endpoints' existing role/tenant/audit rules
>   apply unchanged and every labor mutation is attributed to the operator, never the station.
>   Outside `/api/v1/shop-floor/*` (+ `POST /auth/employee-logout`) the token is **403**
>   (path-fenced in `get_current_user`). No refresh token is ever minted.
>
> Station lifecycle (create / reset-PIN / revoke), station-login failures, and badge-token
> issuance/failures all write tamper-evident audit rows. See [docs/API.md](API.md) →
> Authentication → Kiosk station tokens, and [docs/KIOSK.md](KIOSK.md) → Crew station mode.

### Admin

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Settings | ✓ | | | | | | |
| Integrations (`admin:integrations`) | ✓ | | | | | | |
| Audit Logs | ✓ | ✓ | | | | | |
| AI usage & cost summary (`/ai-usage/summary`) | ✓ | ✓ | | | | | |
| AI egress kill switch (`PUT /companies/me/ai-egress`) | ✓ | | | | | | |
| SMS egress kill switch (`PUT /companies/me/sms-egress`) | ✓ | | | | | | |
| Wallboard display tokens (`/auth/display-token` issue/list/revoke + setup-code reissue) | ✓ | ✓ | | | | | |
| Visitor sign-in stations (`/visitor-logs/stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| Crew kiosk stations (`/shop-floor/kiosk-stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| System | ✓ | | | | | | |

> **Integrations (carrier-account credentials + shipping profile) — endpoint mapping.** The
> carrier-integration admin console — `app/api/endpoints/integrations.py`, mounted under
> `/api/v1/admin/settings` — is enforced **in code** to `require_role([ADMIN])` on every route:
> the carrier-account CRUD (`GET`/`POST`/`PUT`/`DELETE …/carrier-accounts`), the credential-only
> `POST …/carrier-accounts/{id}/test-connection`, and the company shipping-profile
> `GET`/`PUT …/shipping-profile` (which holds the `allow_carrier_egress` kill switch). Carrier
> secrets are write-only (Fernet-encrypted, never returned — only `api_key_last4` /
> `has_webhook_secret`); deletes are soft deletes; create/update/delete and the egress toggle are
> audited. The new `admin:integrations` permission string (granted to **Admin** in
> `app/models/role_permission.py`) drives the frontend Carrier Integrations tab's visibility. See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md).
>
> **Inbound carrier webhook is unauthenticated (by design).** `POST /api/v1/webhooks/carriers/{provider}`
> (`app/api/endpoints/carrier_webhooks.py`) has **no auth dependency** — a carrier cannot present a
> JWT. Trust is established by **HMAC signature** verification against the stored per-tenant webhook
> secret, and the owning tenant is resolved **only from stored shipment data**
> (`Shipment.aggregator_shipment_id`), never from caller input. A request that matches no secret or no
> shipment is dropped with **204** (no existence oracle). It is therefore not on this permission matrix.
>
> **Intentionally-unauthenticated endpoints (the full set).** Five write/verify surfaces establish trust
> *without* a user role — and so none appears on the role matrix: the **carrier webhook** above (HMAC
> signature), the visitor **`station-login`** (a shared station PIN mints a scoped `signin` token — see
> Visitor Logs above), the crew-kiosk **`station-login`** (`POST /shop-floor/kiosk-stations/station-login`,
> a shared station PIN mints a scoped `kiosk` token — see Crew-Station Kiosk above), the wallboard
> **display-token** verification (a scoped `display` JWT — see below), and the wallboard
> **setup-code claim** (`POST /auth/display-token/claim`, a single-use 15-minute pairing code —
> stored only as a SHA-256 hash — exchanged for the display JWT; rate-limited 10/minute per IP,
> uniform 404 for every failure — see below). Each binds the request to a
> tenant through stored server-side state, never caller-supplied identity.

> **Audit-log access (tenant-scoped).** The **Audit Logs** row above covers audit *retrieval*:
> `GET /api/v1/audit/`, `/audit/summary`, `/audit/actions`, `/audit/resource-types`
> (`require_role([ADMIN, MANAGER])`). These are **tenant-scoped** — each filters by the caller's
> active company (`get_current_company_id`), so Admin/Manager see only their own company's audit
> data.
>
> **Audit-integrity endpoints (`/api/v1/audit/integrity/*`).** These verify the tamper-evident
> hash chain and are authorized separately from retrieval:
>
> | Endpoint | Role | Scope |
> |----------|------|-------|
> | `GET /audit/integrity/status` | **Platform Admin only** (`require_platform_admin`) | Global chain |
> | `GET /audit/integrity/verify` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/verify-recent` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/record/{sequence_number}` | **Admin** (`require_role([ADMIN])`) | **Own active company only** |
>
> The three aggregate endpoints are Platform-Admin-only because the hash chain is a single global
> sequence interleaved across all tenants — its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to one company without leaking other tenants' data. The per-record
> endpoint serves a company Admin's "are *my* records intact?" need: a company-scoped Admin may
> verify only a record belonging to their active company, and a cross-tenant record returns
> **404** (not 403) so it can't be used to probe for another company's records. Platform Admins /
> superusers may verify any record (superuser bypasses role checks, as elsewhere).

> **AI usage & cost summary.** `GET /api/v1/ai-usage/summary` (`app/api/endpoints/ai_usage.py`)
> is enforced **in code** via `require_role([ADMIN, MANAGER])` and is **tenant-scoped** to the
> caller's active company. It returns read-only per-task / per-model aggregates over the
> `ai_usage_events` LLM telemetry ledger (operational telemetry, not audit data — see
> [docs/API.md](API.md) → AI Usage Telemetry). Note the **Manager allowance is currently dormant
> in the UI**: the only consuming surface is the Admin Settings → AI Usage & Cost tab, and
> `/admin/settings` is AdminRoute-gated (admin role / superuser), so Managers can exercise this
> permission only via direct API calls today.

> **AI egress kill switch (`PUT /api/v1/companies/me/ai-egress`).** Enforced **in code** via
> `require_role([ADMIN])` — **Admin-only**, matching the sibling CUI egress kill switches
> (`allow_carrier_egress` / `allow_print_egress`, also Admin-only): flipping the CUI boundary is a
> decision reserved to Admins. It only ever mutates the caller's **own active company**
> (`get_current_company_id`; the company is never taken from the request body). Flipping the
> `Company.allow_ai_egress` CUI control writes tamper-evident `audit_log` rows (a field update **and**
> an `ai_egress_enabled` / `ai_egress_disabled` status change). The toggle is surfaced in the UI at
> **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`); within that tab the control is
> interactive for ADMIN (enabling egress requires explicit confirmation) and read-only for
> other roles. See [docs/API.md](API.md) →
> Company (self-service) and [docs/AI_QUOTING_AGENT_RUNBOOK.md](AI_QUOTING_AGENT_RUNBOOK.md).

> **SMS egress kill switch (`PUT /api/v1/companies/me/sms-egress`).** Enforced **in code** via
> `require_role([ADMIN])` — **Admin-only**, for the same reason as the AI switch above: it gates all
> outbound notification SMS to **Twilio, which sits outside the CUI boundary**, so flipping it is a
> CUI-boundary decision reserved to Admins. It only ever mutates the caller's **own active company**
> (`get_current_company_id`; never taken from the request body). Flipping `Company.allow_sms_egress`
> writes tamper-evident `audit_log` rows **twice** — a field update **and** an
> `sms_egress_enabled` / `sms_egress_disabled` status change. Every company is created **OFF**, and
> the switch is re-resolved **fail-closed before every send** (unknown tenant, missing company row,
> or a DB error all deny), so turning it off also stops messages already queued in ARQ. Surfaced in
> the UI at **Admin Settings → SMS Privacy** (`/admin/settings?tab=smsprivacy`). Note this is only
> the *company* half of the gate — SMS additionally requires a **per-user** opt-in with a saved
> phone number (self-service, see Users above), and only `sms_eligible` catalog events are
> offerable. See [docs/API.md](API.md) → Company (self-service) and
> [docs/NOTIFICATIONS.md](NOTIFICATIONS.md#sms-channel-twilio).

> **Wallboard display tokens (`/auth/display-token`, A0.5).** Issue / list / revoke / setup-code
> reissue (`POST /auth/display-token/{id}/setup-code`) are enforced
> **in code** via `require_role([ADMIN, MANAGER])` and tenant-scoped to the active company;
> issuance, revocation, setup-code reissue, and each successful TV claim write tamper-evident
> `audit_log` rows. The TV-side **claim** (`POST /auth/display-token/claim`) is the one **public**
> display-token endpoint (see the intentionally-unauthenticated set above): a single-use,
> 15-minute setup code — hashed at rest — is exchanged for the display JWT, re-minted from the
> `display_tokens` row so revocation semantics are unchanged. **A display token is not a role
> and carries no user identity** — it is a single-endpoint credential for an unattended TV. What it
> **can** do: authenticate the read-only `GET /shop-floor/wallboard` (via the dedicated
> `get_display_or_user` dependency), scoped to the issuing company (taken from the `display_tokens`
> DB row, never from the client). A per-display **`show_customer_names`** flag (Boolean, default
> `false`; migration `072`) additionally gates whether the board reveals work-order **customer
> names**; for **signed-in** callers of the same endpoint that content is gated by **role** —
> only **Platform Admin / Admin / Manager** see customer names, and every other role (Supervisor /
> Operator / Quality / Shipping / Viewer) plus every un-flagged display token gets the redacted,
> public-safe board (`docs/WALLBOARD.md` → Customer names — gated). What it **cannot** do: reach any other endpoint (`verify_token`
> accepts only `type == "access"` JWTs, so a display token gets **401** everywhere else), write
> anything (the wallboard endpoint performs zero writes), or outlive revocation/expiry (the DB row
> is re-checked on every request; a revoked token dies on the TV's next ~30s poll). As with AI
> usage above, the **Manager allowance is currently UI-dormant**: the managing surface is Admin
> Settings → Wallboard Displays and `/admin/settings` is AdminRoute-gated, so Managers can exercise
> it only via direct API calls today. See [docs/API.md](API.md) → Authentication → Display tokens
> and [docs/WALLBOARD.md](WALLBOARD.md).

## Backend Implementation

### Using `require_role` Dependency

```python
from app.api.deps import require_role
from app.models.user import UserRole

@router.post("/work-orders")
def create_work_order(
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    # Only admin, manager, and supervisor can create work orders
    ...
```

### Available Roles

```python
class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"
```

## Frontend Implementation

### Using Permission Components

```tsx
import { PermissionGate, CanCreate, CanEdit, CanDelete, AdminOnly } from './components/PermissionGate';

// Single permission check
<PermissionGate permission="work_orders:create">
  <CreateButton />
</PermissionGate>

// Any of multiple permissions
<PermissionGate anyOf={['work_orders:edit', 'work_orders:delete']}>
  <ActionMenu />
</PermissionGate>

// Convenience components
<CanCreate resource="work_orders">
  <CreateButton />
</CanCreate>

<AdminOnly>
  <AdminPanel />
</AdminOnly>
```

### Using Permission Hook

```tsx
import { usePermissions } from './hooks/usePermissions';

function MyComponent() {
  const { can, canAny, isAdmin, role } = usePermissions();
  
  if (can('work_orders:create')) {
    // Show create button
  }
  
  if (isAdmin) {
    // Show admin features
  }
}
```

### Protected Routes

```tsx
import { ProtectedRoute, AdminRoute } from './components/ProtectedRoute';

<Route path="/admin" element={
  <ProtectedRoute requireAdmin>
    <AdminPage />
  </ProtectedRoute>
} />

<Route path="/users" element={
  <ProtectedRoute permission="users:view">
    <UsersPage />
  </ProtectedRoute>
} />
```

## Superuser Override

Users with `is_superuser=true` bypass all permission checks. This is reserved for system administrators who need full access regardless of role assignment.

## Adding New Permissions

1. **Backend**: Add new endpoint with `require_role()` dependency
2. **Frontend**: 
   - Add permission to `Permission` type in `utils/permissions.ts`
   - Add to appropriate role arrays in `ROLE_PERMISSIONS`
   - Use `PermissionGate` or `usePermissions` in components

## Security Notes

- Permissions are checked on BOTH frontend (UI) and backend (API)
- Frontend checks are for UX only - they can be bypassed
- Backend checks are the authoritative security layer
- Always verify permissions server-side before performing actions
