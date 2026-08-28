# Work Order Templates — a named catalog of jobs the shop re-runs

**Date:** 2026-08-25 · **Amended:** 2026-08-27 — a template whose source work order was deleted now **reads through the tombstone and still works** (owner decision; see [A deleted source does not stop a template](#a-deleted-source-does-not-stop-a-template)). Anything you read elsewhere claiming it is flagged unavailable and refused 409 predates that amendment.
**Status:** shipped on `feat/work-order-templates` (`0bfda8b`) — backend (migration `087`, one new table, one new router) plus the Templates tab on `/work-orders`
**Feature:** A named, searchable list of the jobs this shop runs over and over. Picking one produces a new **DRAFT** work order with the same plan — through the *existing* duplicate engine, with the *existing* role gate, and reaching no dispatch board until a planner releases it.

Code: `backend/app/models/work_order_template.py` · `backend/app/services/work_order_template_service.py` · `backend/app/api/endpoints/work_order_templates.py` · `backend/app/schemas/work_order_template.py` · `backend/alembic/versions/087_work_order_templates.py`. Each carries a long docstring arguing its own decisions; this document is the operator- and planner-facing view of the same argument.

## Why this exists

Two doors already led to "run that job again", and neither is a catalog.

**Import Nest Package re-extracts and auto-releases.** The three nest-writing doors — `POST /work-orders/laser-nest-packages/standalone/import`, `POST /work-orders/{id}/laser-nest-packages/import`, and the manual nest add `POST /work-orders/{id}/laser-nests/manual` — all **force-set `RELEASED`** on the laser work order (`backend/app/api/endpoints/work_orders.py`: pinned at the constructor where the door creates the work order, assigned on the way through where it already exists). So re-running last month's 40-nest package meant re-uploading the PDFs, re-confirming every extracted row in the wizard, and then finding the job already on the dispatch board before anyone had looked at it. The re-extraction is also not free and not deterministic: sheet descriptors come from an LLM pass, and the same PDF run twice can spell one sheet two ways (see `docs/API.md` → Laser Nests → "Sheet descriptors are canonicalized on every write").

**Duplicate copies the plan correctly, but you have to go find the job first.** `POST /work-orders/{id}/duplicate` already does the right thing — plan onto a fresh DRAFT, production record left behind — and it has been the recommended path. It just has no index. "The Miratech housing set" is a fact in somebody's head; recovering it means scrolling a work-order list for a job number nobody wrote down, and picking the *wrong* month's job silently produces a plan that is one operation out of date.

A template closes exactly that gap: it puts a **name** on the exemplar, and nothing else.

## What a template IS

**A name, an optional note, an optional default quantity, and a POINTER at the work order whose plan it stands for.** That is the whole row (`work_order_templates`). It holds no operations, no nests, no material ties, no status, and no quantity produced. Nothing on the shop floor reads the table — `dispatch_service`, the kiosk and the wallboard have never heard of it.

`POST /work-order-templates/{id}/use` resolves the pointer and hands the work order to `work_order_duplicate_service.duplicate_work_order` — **the same copy engine `POST /work-orders/{id}/duplicate` uses**. Everything that verb decides holds here byte-for-byte, and everything it refuses is refused here too. A template adds a name and a lookup; it adds **no authority**.

### A pointer, not a frozen plan — and the drift is intended

State the consequence plainly rather than letting somebody file it as a bug:

**Edit the source work order and the template follows.** Add an operation, soft-delete a nest, cancel a material tie, flip `sequential_operations` — the template's *next* use reflects the edit. That is the intended behaviour: the exemplar **is** the plan, and a planner who improves the master job expects the improvement to carry to the next run. It is also why process-sheet steps can legitimately differ between two uses six months apart: the duplicate service re-snapshots them from each family's *currently released* revision (`_resnapshot_process_sheet_steps`), so a released Rev B replaces Rev A on the new traveler without anyone touching the template.

**What keeps that honest is the live plan summary.** Every template read — list *and* detail — returns a `plan` object computed from the source work order at read time: operation count, live nest count, total planned runs, open material tie count, the distinct work centers in sequence order, the work-order type, `sequential_operations`, priority, the source's status and quantity. Nothing in it is stored, so nothing in it can go stale. A *stored* `nest_count` would be wrong the first time somebody soft-deleted a nest on the source, and the planner would pick a template believing it carries 21 nests and get 20. The rule this encodes: **the planner is shown what they will actually get, at the moment they pick it.**

### The rejected alternative

Freezing the plan into `work_order_template_operations` / `_nests` / `_allocations` was considered and rejected outright. It would mean a **second copy service** re-deciding everything the existing ~1,300-line one already decides: what scales with the ordered quantity and what does not (`run_time_hours` scales, `setup_time_hours` does not), how a nest tie's `qty_planned` is derived, which omissions are *skips* and which *refuse the whole call*, whether a lot pin carries. Every future fix would then have to be made twice, or the two would drift.

This system has already paid that bill once: the office and shop-floor predecessor gates were two copies of one rule and drifted apart, which is how an operation could be hidden from the dispatch board and still startable by badge scan (`CLAUDE.md` → READY promotion). **One copy engine, one set of rules.** The cost of that choice is the drift described above, and the live plan summary is the mitigation.

## The DRAFT guarantee — and what it actually rests on

Using a template produces a work order in `DRAFT` whose operations are all `PENDING`. Two things enforce it: `work_order_duplicate_service._copy_header` hard-codes `status=WorkOrderStatus.DRAFT` and `_copy_operations` hard-codes `status=OperationStatus.PENDING`, and `work_order_template_service._assert_landed_as_draft` re-checks the result **inside the caller's `atomic_transaction`**, so anything that ever landed non-DRAFT rolls the whole use back instead of committing. That re-check is redundant today and is kept deliberately — see [Why the redundant check earns its keep](#why-the-redundant-check-earns-its-keep).

**Now note what the guarantee rests on, because it is very easy to state backwards.** The dispatch query does **not** exclude DRAFT work orders. `dispatch_service.queued_operations_query` filters on **operation** status — `QUEUE_OPERATION_STATUSES`, i.e. `READY` / `IN_PROGRESS` — and on the parent work order being non-terminal (`TERMINAL_WO_STATUSES` is `COMPLETE` / `CLOSED` / `CANCELLED`) and not soft-deleted. `DRAFT` is **not** terminal, so a DRAFT work order carrying a `READY` operation *would* appear on the dispatch board and in the kiosk queue. A template's output is off the board because **its operations are born `PENDING` and nothing promotes a DRAFT** — every promotion seam runs off release or off a completion, and the read-path heal (`work_order_state_service._promote_stranded_ready_operations`) carries an explicit DRAFT carve-out, because Release is the authorization step and a GET must never put unreleased work on the floor's board. Do not restate this as *"the board filters out drafts"* — not in a doc, not in a comment, not in a test name. The two sentences describe different mechanisms and only one of them is true: the moment somebody hand-sets one of those operations to `READY` (which `PUT /work-orders/operations/{id}` permits — it refuses only `COMPLETE`), the "filters drafts" version becomes false and the operation is on the kiosk with the work order still unreleased.

### Import Nest Package is unchanged, and still releases

None of the three import doors changed. They still force-set `RELEASED`, and that is deliberate rather than an oversight this feature forgot to clean up: nest import is how the laser floor gets loaded, and it is used at the point where the planner has *just* reviewed every extracted row in the wizard, so releasing is what they meant. Templates are the **draft door** — the path for re-running a plan the planner has not re-reviewed, where landing on the board unreviewed is exactly the wrong outcome. Two doors, two postures, on purpose.

If the release-on-import posture is ever revisited, that is its own change with its own argument; do not fold it into this feature.

### Why the redundant check earns its keep

`_assert_landed_as_draft` is dead code today and is a deliberate exception to "don't write dead code". The failure mode is what justifies it: if a future change to `_copy_header` — or a new keyword argument threaded through it — ever made the copy land `RELEASED`, templates would silently become another release-forcing door in this system, and it would look identical to the planner right up to the moment unreviewed work appeared on the floor. That is not a defect a test on some other file catches at the right time, and the audit chain would only explain it afterwards. So the promise is asserted where it is relied on, and it fails loudly.

## A deleted source does not stop a template

**Owner decision, 2026-08-27, overriding the original design:** *"templates need to stay even if there is no work order present for it."* A template is a catalog entry, and it must not stop working because somebody deleted a job.

So when the source work order is soft-deleted:

- the template still appears in `GET /work-order-templates`, **fully summarised** — real operation count, nest count, planned runs, open ties, work centers, exactly as before the delete;
- `plan.available` stays **true** and `plan.unavailable_reason` stays **null**;
- `plan.source_work_order_deleted` is **true** — the disclosure, not a gate;
- `POST /work-order-templates/{id}/use` returns **201** and produces the same DRAFT it always would. `duplicate_work_order` never asked whether its source was deleted; it copies the object it is handed.

The UI matches: the row renders at full opacity with its real counts, Use stays enabled, and the deleted source is a **muted** note ("Its source work order was deleted — the saved plan still copies.") rather than the red unavailable line. Admins and managers get a link to the Deleted tab from it, because *"where did that job go?"* is a real question — but it is context, not a remedy, and the note no longer instructs anyone to restore anything.

### The FK is what makes read-through a complete answer

This is the load-bearing fact and it is easy to walk past, because it lives in the schema rather than in the service:

> `work_order_templates.source_work_order_id` is **`NOT NULL`**, a **plain `ForeignKey("work_orders.id")`**, with **no `ON DELETE`** — in the model and in migration `087` alike.

Postgres therefore refuses to remove a `work_orders` row while a template still points at it. Two consequences follow:

1. **"No work order present" can only ever mean *soft-deleted*.** The row cannot be physically gone. A soft-deleted work order keeps every operation, laser nest, material tie and process-sheet step it had, so the plan is always still sitting there to be read.
2. **A frozen snapshot of the plan would buy nothing.** It would exist to survive a disappearance that cannot happen. Read-through covers 100% of the reachable cases by dropping one predicate from one resolver; the snapshot would cover the same cases with a second copy service and all the drift [The rejected alternative](#the-rejected-alternative) describes. That rejection still stands, and this is now the second reason for it.

That FK guarantee used to be an **accident** — a hard delete hit a `ForeignKeyViolation` and surfaced as a 500. It is now depended upon, so it is enforced legibly: `DELETE /work-orders/{id}?hard_delete=true` refuses **409** naming the templates saved from the job, raised before the first mutation. **Soft delete is deliberately unaffected**, which is the entire point — a soft-deleted work order keeps every template working. (Hard delete is draft/cancelled-only anyway, so this refusal is rare by construction.)

### The invariant-3 tension, stated rather than argued away

Invariant 3 asks for an `is_deleted` predicate on every read of a soft-deletable model, and names four legitimate non-filterers: delete, restore, a duplicate probe, and a **historical record**. A template's source is the historical-record shape — the template permanently names the job it was saved from, and the FK above means it owns that row for the row's whole life.

The counter-argument is real and is not waved off: **minting a new work order from a deleted plan is closer to *selection* than to reading a record**, and selection is exactly what invariant 3 gates. It is *decided*, not resolved. What carries the decision is the asymmetry that stays gated:

| Action | Deleted source |
|---|---|
| Read / summarise an existing template | Allowed — reads through (`resolve_source_work_order`, no filter) |
| **Use** an existing template | Allowed — 201, a normal DRAFT |
| **Save a NEW template** from that work order | **404** — filter kept (`resolve_catalogable_work_order`) |

An already-saved template is a catalog entry; a new one is a fresh selection. Where the filter lives is the whole story: `resolve_source_work_order` is the read-through resolver, `resolve_catalogable_work_order` wraps it and adds the tombstone filter back for the selection half, and the batched read in `plan_summaries_for` changes **in lockstep** with the former (a summary that filtered would render blank over a tombstone — a silent blank, found by a planner pressing Use).

Note one knock-on the no-restore-verb argument does not cover: because "Save as template" is 404 against a deleted work order, **deleting a template whose source is already deleted cannot be undone in one click** — restore the work order first, then re-save. A two-step recovery, not a lost one; recorded here rather than patched, since nobody has asked for a template restore verb.

### `available: false` still exists, and still means something

`plan.available = false` is now reserved for the much narrower case: **the source row could not be resolved at all** — a cross-tenant id, or a row that somehow escaped the FK. `plan.unavailable_reason` is then `"source_work_order_missing"`, and `POST …/use` answers **409**. The template is still listed, never hidden: hiding it is the mask trap invariant 3 documents after the 2026-08-16 vendor sweep — the planner's entry silently vanishes from the picker with nothing anywhere saying why — and auto-deleting it destroys a curated name as a side effect of something else.

The old value `"source_work_order_deleted"` was **retired** as an `unavailable_reason`; leaving a token whose text says *deleted* while deletion no longer makes anything unavailable would actively mislead a client rendering it verbatim. **Treat the set as open** — render an unrecognized value verbatim rather than dropping the row, the same rule the duplicate skip reasons carry. (Old clients that still map the retired token are harmless: nothing emits it any more.)

## Deleting a template

`DELETE /work-order-templates/{id}` is a **soft delete** (invariant 3): `is_deleted` / `deleted_at` / `deleted_by`, one `log_delete` audit row, nothing physically removed. It removes a name from a picker and nothing else — the work orders the template produced are ordinary work orders and are untouched, and so is the source work order it pointed at. A second delete answers **404**, which is what makes the tombstone filter observable.

**The unique name index is partial, so deleting a template frees its name immediately.** `uq_work_order_templates_company_name_live` is `UNIQUE (company_id, name) WHERE NOT is_deleted`. An unconditional constraint would burn "Miratech nest group" forever the first time somebody tidied the list, and force the planner to invent "Miratech nest group 2". Two implementation details are load-bearing and are documented at length in the model and in migration `087`: the predicate is written `NOT is_deleted` (one string that compiles on both Postgres and SQLite), and it is declared as **both** `postgresql_where` and `sqlite_where` — a `postgresql_where` alone degrades to a *full* unique index on the SQLite test backend, which would make the test suite enforce a rule production does not.

Because the index is partial, the service's own duplicate-name probe reads **live rows only**. That is *not* the usual invariant-3 exception where a duplicate probe must keep matching tombstones (a deleted vendor still owns its code) — here a deleted template genuinely does not own its name, so probing tombstones would refuse a name the database would happily accept. The probe is additionally **case-insensitive**, deliberately stricter than the byte-wise index: two templates differing only in case are indistinguishable in a picker, and the picker is the entire feature. The index backs a **narrower** race than the probe covers, and the difference is worth stating: it is over the stored bytes, so two concurrent saves of the *same* name race safely (the loser gets a **409**, not a 500), but two concurrent saves of `Bracket set` and `bracket set` both pass the probe and both commit. The window is small and the outcome is cosmetic — two near-identical picker rows, either renameable — which is why it is not closed with a `lower(name)` expression index: reflection of expression indexes is unreliable enough that the migration's own `_has_index` guard could not see one, trading a cosmetic race for a broken idempotency guard.

**There is no restore verb, on purpose.** A template holds no information that cannot be reproduced in one click: open the source work order, press "Save as template" again, and the result is identical — because the plan was never stored in the template. (One case escapes that: if the source work order has *itself* been deleted, "Save as template" is 404, so the recovery is restore-the-work-order-then-re-save — two steps rather than one. Recorded, not patched.) The tombstone exists so nothing is physically destroyed (invariant 3's letter), not because the row needs an undo path. (It is *not* needed to keep audit rows readable: `_live_template_or_404` 404s a deleted template anyway, and every audit row already carries the name verbatim in `resource_identifier` and `extra_data.template_name`, so the chain stays legible with or without the row.) Contrast `restore_vendor`, which exists because a vendor row carries irreplaceable history and an approval flag; a template carries neither.

Deleting is also the **only** verb here that requires no reason, unlike receipt void, NCR void, vendor delete or part renumber. Those unwind or re-identify a *production* record; this removes a shortcut. Demanding a written justification for tidying a list trains people to type "x", which is worse for the chain than not asking — it makes every other required reason in the system look like a formality.

## Quantity, and the nest-derived override

`quantity_ordered` on the use request is **optional**, which is what makes the click-once case click-once. The server resolves the **first POSITIVE** value of:

1. `request.quantity_ordered` — what the planner typed for this run;
2. `template.default_quantity` — the prefill saved with the template;
3. `source.quantity_ordered` — whatever the exemplar job ran.

All three non-positive is a **422** naming the template and the source, *not* a fabricated `1`. A quantity of one on a job that should have run fifty is a plan nobody approved, and the only way to reach that state is a legacy source work order with a zero quantity. Each candidate must be positive to *win*, not merely present — `quantity_ordered` carries a `> 0` CHECK on new rows, so a zero candidate would otherwise fail deeper in with a message about a constraint instead of about the template.

**For a nest-bearing template the server overrules all of it.** A laser work order's `quantity_ordered` is *defined* as the sum of its nests' `planned_runs` (`laser_nest_service._recompute_child_quantity_ordered`, re-asserted by every nest mutation path), so the duplicate service derives it and ignores what was sent. The resolved value is still computed and still passed, because the underlying call requires a positive number. `plan.nest_count > 0` is the flag that tells a client this is the case *before* the call — that is what it is for.

> **Always quote the stored quantity off the RESPONSE, never off the form.** The response envelope carries the work order the server actually wrote. Reading back the value the user typed is how a planner ends up told they ordered 3 when the server stored 21. The audit row records `requested_quantity` alongside the stored `quantity` only when the two differ, which is the same key and the same condition the duplicate service uses.

## Due date always starts blank

`due_date` is optional and is **never inherited from the source**. The source's due date belongs to the run that already happened; carrying it forward would make the new job overdue the instant it exists — red on the dispatch board, and counted against OTD — for a promise nobody made. `null` means unscheduled, which reads as "not promised yet" everywhere, where a stale date reads as "late".

Like `WorkOrderDuplicateRequest` and unlike `WorkOrderCreate`, the field carries **no "not in the past" validator**: a template is most often used to re-run something that is already late.

`must_ship_by` is not carried either — that refusal lives in the duplicate service, where it belongs: it is the *original* order's promise and it outranks `due_date` in OTD/OTIF scoring.

## The skip envelope — and why a skipped tie is safety information

`POST /work-order-templates/{id}/use` returns **201** with the **exact same `WorkOrderDuplicateResponse` envelope** `POST /work-orders/{id}/duplicate` returns:

```json
{
  "work_order": { "…": "the same shape GET /work-orders/{id} returns" },
  "skipped_operations": [
    {"source_operation_id": 812, "operation_number": "10", "sequence": 10, "reason": "laser_nest_deleted"}
  ],
  "skipped_material_allocations": [
    {"source_allocation_id": 44, "part_id": 91, "source_work_order_operation_id": 812, "reason": "part_not_available"}
  ]
}
```

Reusing the envelope is not tidiness. It means the skip lists reach the **same result view** the Duplicate dialog already renders, so the two paths cannot report an omission differently, and a client that already handles one handles the other.

**Both lists empty is the "clean copy" signal. A non-empty list is not an error** — the work order was created and is a valid draft — **but the draft is missing something the source had, and the planner has to be told.** For a material tie, spell the consequence out: a skipped tie means the new job carries **no demand** for that material, so **no shortage is ever raised**, the work runs, the sheet is physically consumed, and **stock is never deducted** — until an inventory count disagrees, months later, with nothing in the record explaining why. That is strictly worse than a loud failure, and it is why the skip is surfaced in two channels (the response *and* the template's `USE_TEMPLATE` audit row, from one `model_dump()` of the same objects) rather than logged.

The reason vocabulary is the duplicate service's, unchanged: operations `laser_nest_deleted`; ties `part_not_available`, `part_not_tieable`, `operation_not_copied`, `nest_runs_unavailable`. See `docs/API.md` → Work Orders → "Duplicating a work order" for what each one means. **Treat the list as open** — render an unrecognized value verbatim rather than dropping the row.

## What a template deliberately does NOT carry

A template inherits the duplicate service's omission list wholesale. **Instructions carry; the production record does not.**

Not copied, because copying it would fabricate history on a job that has not run — which an AS9100D reader would take for a real record: `quantity_complete` / `quantity_scrapped` and their scrap reasons, actual dates / hours / cost, lot and serial numbers, release info, `current_operation_id`, and time entries.

Not copied, as decisions rather than oversights:

| Omitted | Why |
|---|---|
| `unit_number` | Identity, not history. A template run is the **next** unit, not the same one — two work orders both claiming to build unit 2410048 would put that claim on the kiosk, the dispatch board and the TV wall. The planner types the new unit on the draft. |
| `parent_work_order_id` | The result is an **independent** work order. Re-attaching it to the source's assembly parent would add a second laser child against demand the first already satisfied, and the parent's rollup would count both. |
| `must_ship_by` | The original order's promise; it outranks `due_date` in OTD/OTIF. Carrying it would silently override the due date just supplied. |
| `run_order` | A manager's dispatch ranking for one machine's board, not part of the plan. A 40-nest template arriving pre-ranked would displace the sequence already set at that laser. |
| `scheduled_start` / `scheduled_end` | `SchedulingService` output for the *source's* dates; release reschedules anyway. |
| Lot **pins** on copied ties | Every tie lands inert (`qty_consumed = 0`, `status = open`) with its pinned lot and pinned inventory item **cleared**. A pin says "consume from *this* lot", and the lot the source job pinned was very likely consumed by the source job. |

What *does* carry: every operation with its setup/run instructions, work center, inspection flags and component fields; live laser nests (CNC number, planned runs, material/thickness/sheet size, and the **shared** drawing `document_id` reference); open material ties with `qty_planned` recomputed; `sequential_operations`; and a re-snapshot of the process-sheet steps.

One field group is deliberately **not** verbatim: a copied nest's `material` / `thickness` / `sheet_size` are re-canonicalized by `laser_nest_text.normalize_*` on the way across, so re-running a pre-normalization job cannot re-inject a legacy spelling (`144x60` beside `144 x 60`).

## Templates and routings — complements, not competitors

A `Routing` already templates a **part's** operations: revision-controlled, released, per-part, copied onto a new work order at creation by `create_routing_operations_for_work_order`. It is a controlled engineering record and it stays the right place for "how this part is made".

A template is a different object and covers what a routing structurally cannot:

- **Routings are keyed to a part** (`routings.part_id` is `nullable=False`). A standalone laser nest work order has **no part at all**, so a routing cannot describe one. A template can, because it points at a work order.
- **Routings hold no laser nests.** The nest package, the per-nest CNC numbers, planned runs and sheet descriptors live on the work order.
- **Routings hold no material ties.** `work_order_material_allocations` is a work-order-level object, so the tie set that makes stock deplete cannot be expressed on a routing.
- **Routings have no `sequential_operations`.** Sequenced-routing vs. same-work-center dispatch-pool is a **per-work-order** setting (migration `081`), and it is exactly the setting a repeat batch job needs to carry.

So: routings are the released engineering answer to *how a part is made*; templates are the planner's answer to *which job we run again, with its nests, its ties and its pooling*. Neither replaces the other, and a template naming a work order that was itself built from a routing is the normal case.

## Audit trail

Every write goes through `AuditService` (invariant 2) and nothing here commits — the router wraps each write in `atomic_transaction`, so the new work order, its operations, nest package, nests, ties and every audit row commit together or not at all.

| Verb | Chain rows |
|---|---|
| `POST /work-order-templates` | `log_create` on `work_order_template`, `extra_data` naming `source_work_order_id` / `source_work_order_number` / `default_quantity` |
| `PUT /work-order-templates/{id}` | `log_update` with before/after `name` / `notes` / `default_quantity` |
| `DELETE /work-order-templates/{id}` | `log_delete` (soft), `extra_data.name_released_for_reuse = true` so a chain reader can tell a later template of the same name from this one |
| `POST /work-order-templates/{id}/use` | **Two** rows: the duplicate service's own work-order `log_create` (which names the source work order), **plus** a `USE_TEMPLATE` row against the *template* naming the work order it produced, the quantity stored, the operation / nest / tie counts, and both skip lists |

The second `USE_TEMPLATE` row is the only place the fact that a **catalog entry** — rather than a planner browsing the list — produced this job exists. It is also what makes *"how often do we actually run this template"* answerable from the chain instead of from nothing.

## API surface

Prefix `/api/v1/work-order-templates`. **Every verb, reads included, requires ADMIN / MANAGER / SUPERVISOR** — the same trio `POST /work-orders/{id}/duplicate` and `POST /work-orders/` require. See `docs/API.md` → Work Orders → "Work order templates" for the full contract and `docs/RBAC_PERMISSIONS.md` → Work Orders for the matrix rows.

| Method | Path | Notes |
|---|---|---|
| GET | `""` | The catalog, each entry with its live `plan`. Query `search` (case-insensitive substring on name **and** notes). Unpaged — a curated set in the tens, not a feed |
| GET | `/{id}` | One template + live `plan`. **404** when not live in the active company |
| POST | `""` | `{source_work_order_id, name, notes?, default_quantity?}` → **201**. **404** source not live in company; **409** duplicate live name |
| PUT | `/{id}` | `{name?, notes?, default_quantity?}`, `extra="forbid"`. Explicit `null` **clears** `notes` / `default_quantity`; an omitted key leaves it alone |
| DELETE | `/{id}` | Soft delete → **200** `{message, id}`. Name freed immediately; a second delete is **404** |
| POST | `/{id}/use` | `{quantity_ordered?, due_date?}` → **201** `WorkOrderDuplicateResponse` |

`source_work_order_id` is deliberately **absent from the update schema**. Re-pointing a template at a different work order under the same name silently changes what every future click produces, with the only thing anyone reads unchanged. Save a new template and delete the old one — both halves are then on the chain.

## UI

Templates are a **tab on `/work-orders`** (`?tab=templates`), not a separate route: they are a way of creating work orders, and burying them behind their own nav entry is how a catalog stops being used. A tab is also the only shape that *works*: `/work-orders/templates` would be swallowed by the `/work-orders/:id` route in `App.tsx` and resolve as a work order whose id is the word "templates". The tab title is registered in `utils/routeMeta.ts` under the query key `'/work-orders?tab=templates'`. "Save as template" appears on the work-order list row actions and on the WorkOrder detail action bar; a "New from template" button sits in the page header. **Every control is gated on the existing `work_orders:edit` permission**, which maps to exactly ADMIN / MANAGER / SUPERVISOR (plus PLATFORM_ADMIN, which `require_role` admits everywhere) — so a hidden button and a refused call agree, which is the standing rule for nav/route gating.

The result of a use renders through the **same** skip view the Duplicate dialog uses — one shared renderer, so the two paths cannot describe an omission differently.

Two client rules worth repeating because they are correctness, not styling: quote the stored quantity off the **response**, and render an unknown `unavailable_reason` or skip `reason` **verbatim** rather than dropping the row. A third, since 2026-08-27: **`source_work_order_deleted` is disclosure, never a gate** — do not disable Use, dim the row, or hide the plan counts on it.

## Traps

- **Do not write "the dispatch board filters out drafts."** It does not. See [The DRAFT guarantee](#the-draft-guarantee--and-what-it-actually-rests-on).
- **Do not add a frozen-plan table.** That is a second copy service; see [The rejected alternative](#the-rejected-alternative).
- **Do not make `source_work_order_id` editable.**
- **Do not hide, auto-delete, or re-refuse a template whose source was deleted.** It reads through the tombstone and still produces a DRAFT; see [A deleted source does not stop a template](#a-deleted-source-does-not-stop-a-template).
- **Do not put the `is_deleted` filter back on `resolve_source_work_order`** "for invariant-3 consistency", and do not add an `ON DELETE` to `source_work_order_id`. The bare FK is what guarantees a missing source can only ever be a tombstone.
- **Do not add a restore verb** "for symmetry" — re-saving from the same work order is the undo, and it is one click.
- **Do not "align" the template gate with anything looser than the duplicate endpoint's trio.** A template that could route around a gate a planner would have hit by hand is a one-click hole in the create path.
- **Do not remove `_assert_landed_as_draft`** because coverage says it never fires.
- **Do not read `plan.*` as stored data.** It is computed per read. A soft-deleted source still yields a **full** summary with `available: true` and `source_work_order_deleted: true`; only an *unresolvable* source gives `available: false` with everything else null/zero.

## Not built (and why)

- **Pagination on the catalog.** A curated list a planner maintains by hand, in the tens. Paginate it when it stops being that, not before.
- **A `template_id` FK on `work_orders`.** The `USE_TEMPLATE` audit row already carries the lineage, and a column would be a second, mutable copy of a fact the tamper-evident chain already holds.
- **A validity gate at save time.** A job whose part is currently retired, or whose process-sheet family has no released revision, can still be catalogued. Refusing to *save* would block cataloguing a job that will be perfectly fine next month, and the refusal would name a condition the planner cannot see from the save dialog. Both refusals land at **use** time, where they are actionable.
- **Templates in the kiosk / on the dispatch board.** Nothing on the floor reads this table, and nothing should: a template is not work.
