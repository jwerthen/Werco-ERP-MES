# Excel Migration Runbook

**Who this is for:** the owner and office staff doing the one-time move from Excel spreadsheets onto the ERP for go-live. No programming knowledge required.

**What this covers:** the order to load your data in (and why the order matters), how to use the Import Center safely, how to rehearse before cutover day, and what to do when something goes wrong.

> The companion API reference for these endpoints is in [API.md](API.md) → *Bulk Imports & Templates*. Role requirements are in [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md) → *Bulk Imports*.

---

## The three golden rules

1. **Always validate first.** Every import on the Import Center page is a two-step flow: **Validate file (dry run)** → review the preview → **Commit import**. A dry run writes *nothing* to the database — not even audit entries — no matter what is in the file. Never commit a file you haven't just validated.
2. **Follow the load order.** Each step below creates the records the next step looks up. Loading out of order doesn't corrupt anything — the rows just fail with "not found" errors — but it wastes your time.
3. **Excel is retired on a date, not gradually.** After cutover, paper travelers may *mirror* the system during the transition period, but the Excel workbooks may not be updated again. A spreadsheet that keeps changing after go-live becomes a second source of truth, and then nobody trusts either one.

---

## File basics (read once)

- **Formats:** Excel **`.xlsx`** or **`.csv`** (UTF-8). The importer reads only the **first sheet** of a workbook.
- **Templates:** every direct-import type has a **Download template (.xlsx)** button in the Import Center. Each template has:
  - an **Import** sheet — the styled header row plus one *guidance row* whose cells start with `# `. Guidance rows are skipped automatically on import; you can leave them in or delete them.
  - an **Examples** sheet with realistic filled-in rows. The importer never reads this sheet, so the examples can't be imported by accident.
- **Headers:** column names are matched case-insensitively and spaces/dashes become underscores (`Part Number` = `part_number`). Extra columns the importer doesn't know are ignored. If two columns collapse to the *same* name (e.g. `Part Number` and `part-number`), the whole file is rejected so data can't silently merge.
- **Guidance rows vs. real data:** only a `#` **followed by a space** marks a skipped row. A part number like `#10-32X1/2` is real data and imports normally.
- **Limits:** 10 MB per file, 10,000 data rows per file. Split bigger files and import in batches.
- **Partial success:** on commit, each row (or each PO, for purchase orders) is saved independently. Bad rows are skipped and reported; good rows land. After a partial commit, fix the failed rows in a **new file containing only those rows** — re-committing the full original file will report the already-imported rows as duplicates.
- **Audit trail:** every committed import row is recorded in the tamper-evident audit log, tagged with source `import`, attributed to the signed-in user who ran it.

---

## The migration sequence

Run the steps in this order. The **Why this order** column tells you what breaks downstream if a step is skipped.

| # | Step | Where | How |
|---|------|-------|-----|
| 1 | Work centers | Import Center → **Work Centers** | Template + dry run + commit |
| 2 | Users / operators | Import Center → **Employees / Users** | Template + dry run + commit |
| 3 | Customers | Import Center → **Customers** | Template + dry run + commit |
| 4 | Vendors | Import Center → **Vendors** | Template + dry run + commit |
| 5 | Parts | Import Center → **Parts** | Template + dry run + commit |
| 6 | Materials & supplies | Import Center → **Materials & Supplies** | Template + dry run + commit |
| 7 | BOMs | BOM page import wizard (Import Center links there) | Upload + review mapping + commit |
| 8 | Routings | Routing page (no spreadsheet import — see step) | Manual / copy / AI-assist, then **Release** |
| 9 | Inventory on hand | Warehouse → Inventory tab (no spreadsheet import — see step) | Receive / Adjust with lot numbers |
| 10 | Open purchase orders | Import Center → **Open Purchase Orders** | Template + dry run + commit |
| 11 | Open work orders | Import Center → **Open Work Orders** | Template + dry run + commit |

### Step 1 — Work centers

- **Before you start:** make sure your **work center types** (machining, welding, inspection, …) are configured in **Admin Settings → Work Center Types**. The import rejects a type the system doesn't know.
- **Endpoint:** `POST /api/v1/work-centers/import-csv` (Admin or Manager).
- **Template:** `work-centers`.
- **Required columns:** `code` (unique, uppercased), `name`, `work_center_type`.
- **Optional columns:** `description`, `hourly_rate`, `capacity_hours_per_day` (default 8), `efficiency_factor` (default 1.0), `building`, `area`.
- **Why first:** routings (step 8) attach every operation to a work center, and day-1 floor queues are organized by work center. Nothing downstream can be built without them.
- **Common errors:** unknown `work_center_type` (add it in Admin Settings first); duplicate `code`.

### Step 2 — Users / operators

- **Endpoint:** `POST /api/v1/users/import-csv` (**Admin only**).
- **Template:** `users`.
- **Required columns:** `employee_id` (unique badge/employee number), `first_name`, `last_name`.
- **Optional columns:** `email` (generated from `employee_id` when blank), `password` (auto-generated for operators; **required** for non-operators — per row, or via the **Default Password** box on the upload form), `role` (default `operator`; valid: `operator`, `supervisor`, `manager`, `admin`, `viewer`, `quality`, `shipping`), `department`.
- **`platform_admin` cannot be imported.** That is the cross-company Werco oversight role; a row with `role` = `platform_admin` is rejected. This is deliberate — a company spreadsheet must never mint a cross-company administrator.
- **Why now:** every later import is performed *by* a signed-in user and attributed in the audit log; operators need badge accounts before day-1 clock-ins; and certification records (next note) attach to users.
- **Certifications note:** operator certifications and the skill matrix are **not** part of the spreadsheet import. Enter them in the **Operator Certifications** module after users exist. Missing certs do **not** block day-1 clock-ins — the qualification gate records exceptions, it doesn't stop work — so certs can be backfilled in week 1.
- **Common errors:** "Employee ID already exists" / "Email already registered" (duplicates are checked case-insensitively); "password is required for non-operator roles".

### Step 3 — Customers

- **Endpoint:** `POST /api/v1/customers/import-csv` (Admin or Manager).
- **Template:** `customers`.
- **Required column:** `name` (unique).
- **Optional columns:** `code` (generated when blank), `contact_name`, `email`, `phone`, `address_line1`, `city`, `state`, `zip_code`, `payment_terms` (default Net 30), `requires_coc` (default true), `requires_fai` (default false).
- **Why now:** parts can carry a `customer_name`, and the open-work-order import (step 11) looks up its `customer` column against existing customers — a name that doesn't exist yet fails the row.

### Step 4 — Vendors

- **Endpoint:** `POST /api/v1/purchasing/vendors/import-csv` (Admin or Manager).
- **Template:** `vendors`.
- **Required column:** `name`.
- **Optional columns:** `code` (generated when blank), `contact_name`, `email`, `phone`, `payment_terms`, `lead_time_days` (default 14), `is_approved`, `is_as9100_certified`, `is_iso9001_certified`.
- **Why now:** the open-PO import (step 10) requires each row's `vendor_code` to already exist.
- **Tip:** if you let the system generate vendor codes, download/print the vendor list afterward — you'll need the exact codes for the open-PO spreadsheet.

### Step 5 — Parts

- **Endpoint:** `POST /api/v1/parts/import-csv` (Admin, Manager, or Supervisor).
- **Template:** `parts`.
- **Required columns:** `part_number` (unique, uppercased), `name`, `part_type` (`manufactured` or `assembly`).
- **Optional columns:** `revision` (default A), `description`, `unit_of_measure` (default each), `standard_cost`, `lead_time_days`, `is_critical`, `requires_inspection` (default true), `customer_name`, `customer_part_number`, `drawing_number`.
- **Why now:** BOMs, routings, inventory, PO lines, and work orders all reference parts. This is the backbone load — take the dry-run review seriously here.

### Step 6 — Materials & supplies

- **Endpoint:** `POST /api/v1/materials/import-csv` (Admin, Manager, or Supervisor).
- **Template:** `materials`.
- **Required columns:** `part_number` (unique, uppercased), `name`, `part_type` (`purchased`, `raw_material`, `hardware`, or `consumable`).
- **Optional columns:** `description`, `unit_of_measure` (default each), `standard_cost`, `lead_time_days`, `reorder_point`, `reorder_quantity`.
- **Why now:** BOM buy-components and most open-PO lines point at materials. Same master table as parts — part numbers must be unique across both.

### Step 7 — BOMs

- **Where:** the **BOM page import wizard** — the Import Center's BOMs tab links you there. This is a different flow from the other imports: you upload a spreadsheet (or PDF/Word drawing), the system proposes a **column mapping**, and you review the mapping and line items before committing.
- **Endpoints:** `POST /api/v1/bom/import/preview` then `POST /api/v1/bom/import/commit` (Admin, Manager, or Supervisor).
- **Template:** the `bom` template from the Import Center gives you a clean column layout (`part_number`, `description`, `quantity`, `unit_of_measure`, `item_type`, `line_type`) if your Excel BOMs are messy.
- **Why now:** assemblies need a **released** BOM before work orders behave correctly (master-data health flags this), and MRP component demand comes from BOMs. Components that don't exist yet can be created during commit, but the cleaner path is parts/materials first.

### Step 8 — Routings (no spreadsheet import)

Be aware: **there is no bulk spreadsheet import for routings.** Routings are built on the **Routing** page, one part at a time:

- create the routing manually (sequence, operation, work center, setup/run times),
- or **copy** a similar part's routing and edit it,
- or use the AI **generate-from-drawing** assist and review the result.

Then **release** each routing. This is usually the longest step of the migration — budget real days for it, and prioritize the parts that have open work orders (step 11 *refuses* any part without a released routing).

### Step 9 — Inventory on hand (no spreadsheet import)

Also honest: **there is no bulk upload endpoint for inventory.** On-hand stock is entered on the **Warehouse → Inventory** tab using **Receive** (preferred — it captures lot numbers for traceability) or **Adjust**. The Import Center's Inventory tab offers a starter CSV (`part_number, warehouse, location, quantity_on_hand, lot_number, unit_cost`) — use it as an **offline counting worksheet** to organize the physical count, then key the results in.

- **Why now:** parts must exist before stock can be received against them; MRP, shortage checks, and day-1 picks read these balances.
- **Do a real count.** Migrating an Excel inventory number nobody has verified just moves a wrong number into a better system.

### Step 10 — Open purchase orders

- **Endpoint:** `POST /api/v1/purchasing/purchase-orders/import` (**Admin or Manager only**).
- **Import Center tab:** **Open Purchase Orders**. **Template:** `purchase-orders`.
- **Required columns:** `vendor_code` (must exist — step 4), `part_number` (must exist — steps 5/6), `quantity` (> 0), `unit_price` (≥ 0).
- **Optional columns:** `po_number` (rows sharing the same `po_number` become **lines of one PO**; blank = a single-line PO with a generated number), `promised_date`.
- **What it creates — read this:**
  - Imported POs land in **`sent` (issued)** status — they are immediately receivable on day 1, which is the point: these are POs your vendors already have.
  - **`order_date` is left blank on purpose.** The real order date predates this system and is unknown; the system does not fabricate provenance. Blank means "ordered before migration".
  - A PO imports **whole or not at all**: if any line of a `po_number` group fails validation, the sibling lines are skipped too (reported as "skipped: row N in the same purchase order failed validation"). All lines of one PO must carry the same `vendor_code`.
- **Scope:** import only POs that are still **open** (not yet fully received). Historical/closed POs stay in the Excel archive.

### Step 11 — Open work orders

- **Endpoint:** `POST /api/v1/work-orders/import` (Admin, Manager, or Supervisor — the same roles that can create a work order by hand).
- **Import Center tab:** **Open Work Orders**. **Template:** `work-orders`.
- **Required columns:** `part_number` (must exist **with a released routing** — steps 5 and 8), `quantity` (> 0).
- **Optional columns:** `wo_number` (generated when blank; must be unique — checked case-insensitively), `due_date` (**past dates are allowed** — open jobs can genuinely be overdue), `customer` (existing customer code *or* name), `customer_po`, `priority` (1–10, 1 = highest, default 5), `completed_through_seq`.
- **What it creates:** a real work order with its operations generated from the part's released routing, **released** so it appears in floor queues immediately.

#### `completed_through_seq` — paper history, in plain English

This column is how you tell the system "this job is already partway done on paper." Put in the **sequence number of the last routing operation that was already finished** before migration (e.g. `20` if ops 10 and 20 are done). The import then:

- marks every operation **up to and including** that sequence as **complete**, at the full target quantity;
- makes the **next** operation **ready**, so it shows in the right work center's queue on day 1 — the floor sees real queues, not a pile of jobs all starting from op 1;
- sets the work order to **in progress** (or just **released**, if nothing was complete yet).

**What it deliberately does NOT do:** it does **not** invent start/finish timestamps, does **not** name an operator who "did" the work, and records **no labor hours** — because that evidence doesn't exist in this system, and fabricating it would poison cycle-time and labor reports and the AS9100D story. The audit trail records exactly **which operation sequences were seeded as paper-complete**, and each one is tagged with source `import`, so an auditor (or you, in six months) can always distinguish paper history from work actually performed in the system.

- **Limits:** a `completed_through_seq` that covers *every* operation is rejected — only **open** work orders can be imported. Finished jobs belong in the Excel archive, not in the system.
- **Common errors:** "part 'X' not found"; "part 'X' has no released routing — import/release the routing first"; "customer 'X' not found"; "wo_number 'X' already exists".

---

## The dry-run discipline

Every direct import follows the same loop:

1. **Validate file (dry run).** The server fully processes the file — every lookup, every rule, even the routing expansion on work orders — then rolls everything back. Zero writes, guaranteed.
2. **Read the preview.** It shows: total rows, **would-create** count, skipped count, and a per-row error table (row number, identifier, plain-English reason). Work-order previews additionally show each WO's operations-complete count and which operation becomes ready; PO previews show how rows grouped into POs, with line counts and totals. Numbers the system will generate show as "(generated at commit)" — numbers are only reserved when you commit.
3. **Fix and re-validate** until the error table is empty, or until every remaining error is one you've consciously decided to handle later (e.g. three parts whose routings aren't released yet).
4. **Commit.** The commit button is disabled until you've validated, and blocked entirely if the preview shows nothing would be created. Commit re-processes **the same file** — if you edit the file, validate again.

Picking a new file clears the previous preview automatically — you can never commit against a stale preview.

---

## The rehearsal protocol

**Do the full sequence twice as a rehearsal, with your real exported Excel files, before cutover day.** Not sample data — the actual exports, warts and all. The first rehearsal finds the data problems (duplicate part numbers, customers spelled three ways, missing vendor codes); the second proves your fixes worked and gives you a realistic time estimate for cutover day.

**How to rehearse without polluting anything:**

- **Dry-run rehearsal (recommended, safe anywhere):** run every direct-import step in dry-run mode only, in order, and review every preview. Because a dry run writes nothing, this is safe even in your production company. This is the rehearsal to do twice. Note its honest limits: the BOM wizard previews but steps 7–9 (BOM commit, routings, inventory) can't be exercised end-to-end without committing, and dry-run "not found" errors for steps 10–11 are expected when the prerequisite steps weren't committed — focus those previews on file-format and column errors.
- **Full commit rehearsal (optional, scratch environment only):** if you want to rehearse commits end-to-end — including BOMs, a few routings, and inventory — do it in a **development environment with a scratch database** (the dev Docker stack against a throwaway database, re-seeded from scratch; see [DEVELOPMENT.md](DEVELOPMENT.md)), or in a **separate practice company** created by your platform admin. **Be honest with yourself about resets: there is no "undo import" button.** Committed rows can only be cleaned up one-by-one (and traced records soft-delete rather than vanish), so never commit-rehearse in the production company. Resetting between rehearsals means wiping and re-creating the scratch database or practice company, not deleting rows.

**Keep a migration log** during rehearsals: each step, file name, row counts (total / created / skipped), time taken, and every error you had to fix. Cutover day should be an execution of that log, not an adventure.

---

## Cutover-day checklist

The night before / morning of:

- [ ] **Freeze Excel.** Announce the freeze: from this moment, the workbooks are read-only. Any change after the freeze either waits for the new system or is written on the paper traveler.
- [ ] **Export fresh files** from the frozen workbooks — not the rehearsal copies.
- [ ] Run the sequence in order, **dry-run-first on every step**, committing on clean previews. Use your rehearsal log as the script.
- [ ] After each commit, **verify counts**: the created-count on screen vs. the row count in the source file; spot-check a handful of records in the app.
- [ ] After step 11, walk the floor queues with a supervisor: does each work center's queue match reality? Are the "current operations" right? Fix discrepancies now (the `completed_through_seq` values are the usual culprit).
- [ ] Confirm receiving can see the open POs (Warehouse → Receiving → open POs list).
- [ ] First operator clock-ins on the real queues.

**The retirement doctrine:** declare, in writing, "Excel is retired as of <date>." After that date:

- **Paper may mirror** — travelers can shadow the system during the transition weeks while the floor builds the habit; the system remains the source of truth and paper gets reconciled into it.
- **Excel may not.** Nobody updates the old workbooks, ever. They are kept as a frozen, read-only archive (you'll want them for historical lookups), but a workbook that keeps living becomes a competing source of truth and quietly kills the migration.

---

## Troubleshooting

| Symptom / message | What it means | What to do |
|---|---|---|
| "Please upload a CSV or Excel (.xlsx) file" | Wrong file type (`.xls`, `.numbers`, etc.) | Re-save as `.xlsx` or `.csv` |
| "Could not read the .xlsx file…" | Corrupt or non-standard workbook | Open in Excel, File → Save As → Excel Workbook (.xlsx) |
| "CSV must be UTF-8 encoded" | Legacy CSV encoding | Re-save the CSV as UTF-8, or just use `.xlsx` |
| "Duplicate column: 'X' and 'Y' both map to 'z'…" | Two headers collapse to the same name after normalization (e.g. `Part Number` and `part-number`) | Rename or delete one of the columns; the file is refused whole so data can't silently merge |
| "Missing required columns: …" | A required header is absent (or misspelled beyond recognition) | Compare your header row against the template's Import sheet |
| "File must include a header row" | The first non-empty row didn't contain headers | Put the column names in row 1 |
| Rows from the template's grey italic row showing up — or NOT showing up — in the import | Guidance rows: any row whose **first cell starts with `# ` (hash + space)** is skipped | Leave template guidance rows alone; never start a real first-cell value with `# `. A bare `#` (like part `#10-32X1/2`) imports fine |
| "File is too large (max 10 MB)" / "Too many rows (max 10000)…" | File caps | Split the file and import in batches |
| 403 / "permission" error on upload | Your role can't run this import | Users: **Admin** only. Customers, vendors, work centers, open POs: **Admin/Manager**. Parts, materials, open WOs: **Admin/Manager/Supervisor**. Anyone signed in can download templates |
| "role 'platform_admin' cannot be assigned via import" | A user row tried to grant the cross-company oversight role | Use a normal role; platform admins are provisioned outside tenant imports, by design |
| "…already exists" on a row you don't think is a duplicate | Duplicate detection is **case-insensitive**: `wo-1001` collides with `WO-1001`, `EMP-7` with `emp-7`, and in-file duplicates are caught too | Find and fix the casing variant; don't just re-submit |
| "part 'X' has no released routing" (open-WO import) | The routing exists but wasn't **released**, or doesn't exist | Step 8: build/release the routing, then re-import just the failed rows |
| "skipped: row N in the same purchase order failed validation" | POs import whole-or-not-at-all — one bad line skips its whole `po_number` group | Fix the named row; re-import that PO's rows together |
| Committed a file twice by mistake | Rows with system-generated numbers may have imported twice (rows with explicit numbers are rejected as duplicates) | Find the duplicates (sort by created date), remove/cancel them, and note it in your migration log |

---

*Every committed import row is on the tamper-evident audit trail (source `import`), and paper-completed work-order operations are recorded as exactly that — see [API.md](API.md) → Bulk Imports & Templates for the contract details.*
