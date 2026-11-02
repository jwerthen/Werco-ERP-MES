# Hank Drawing Engineering

Open **Hank → Drawing Engineering** (or `/hank-engineering`) in the signed-in ERP.
This is an implemented review and entry workspace. It does not run engineering
writes through chat tools, the Hank task executor, or a background model job.
The model remains unchanged.

## From drawing to records

1. Choose a local fabrication, assembly or detail PDF, up to 20 MB / 25 pages.
   Native PDF text is read on the device with the bundled PDF renderer. No PDF
   bytes are uploaded and no AI/OCR service is called. The screen retains the
   source filename, SHA-256 and page text, and renders the source alongside review.
2. Review extracted drawing number, revision, title, customer, printed PNs,
   explicit component table quantities/units, and literal manufacturing notes.
   Supported tables have labeled PN, quantity, UOM and description columns,
   separated by pipes, tabs or distinct column gaps. Extraction is deliberately
   partial, never a claim that every dimension or note was interpreted. Scanned,
   unlabeled, ambiguous and multi-line values require entry from the rendered
   sheet. Geometry, weld symbols and tolerances are not inferred.
3. Inspect every sheet for ITAR, CUI, do-not-transmit or uncertain restrictions.
   Observed text markings block entry/export. A clean text scan is not clearance;
   the author must confirm visual screening. If uncertain, stop and ask Jon.
4. Correct/add Parts and BOM lines, record page evidence, and explicitly confirm
   each row. Unknown revision, make/buy choice, unit, quantity or time remains
   blank and blocks entry. Proposed PNs need Jon's specific approval recorded in
   the evidence. Raw material PNs equal the reviewed grade (for example `A36`),
   with the full printed specification in Description. Job quantity has no default
   and is separate from the quantity per one BOM parent.
5. Enter the reviewed owner routing: component PN, description, sequence, exact
   observed work center, approved setup minutes and run minutes per owner unit
   (per assembly for assembly-owned routing), instructions and inspection points. Each manufacturing requirement must appear in operation instructions.
   No estimated labor standards, center substitutions or stock claims are made.
   The author records omitted details, conflicts, assumptions and their explicit
   resolutions, then confirms Engineering entry authorization.
6. **Match existing records** reads the current company's complete part catalog,
   including materials, inactive records and BOM components. It compares exact
   normalized PNs, revisions, types, units and names; owner drawing/customer also
   must match. Existing BOMs and routings must match the reviewed content and
   revision. A conflict stops entry and links to the ordinary revision controls.
   Released records are never silently un-released, updated or duplicated.
7. **Save missing Parts**, **Save Draft BOM**, and **Save Draft Routing** use the
   existing signed-in application's authoring endpoints, exactly as normal forms
   do. Each explicit UI action repeats matching first, respects server RBAC and
   tenancy, records source evidence in descriptions/notes/instructions, and reads
   saved records back. There is no engineering action in `prepare_hank_task` and
   no model access to these mutations. Do not use APIs or page scripts to operate
   this workflow from an external agent; use its visible UI controls.
8. Open the saved BOM and Routing links in their existing screens for authorized
   Engineering review/release, then return and match again. This does not release
   production. A requested Draft WO requires an explicit job quantity and its
   authorization source, matching released Engineering, and no component routing
   that would unexpectedly add operations. Nested component BOMs and inspection
   ordering that the native generator would change are blocked before creation.
   The screen creates the normal Draft WO,
   reads its actual number/link/status, and checks operations and component
   quantities against the reviewed BOM × job count. It has no WO-release control.
   Jon must separately name an observed WO before authorizing production release.

The owner is the assembly for a multi-part job, or the Manufactured part for a
single-piece job. Component parts normally have no routing in the assembly-owned
pattern. Nested/component-routed jobs, multiple existing routing revisions,
outside processing, or conflicting existing engineering require review in the
ordinary Engineering screens; this workspace refuses to guess or overwrite them.

## Recovery, authorization and evidence

- Signed-in authors need Parts, BOM and Routing create permissions; WO creation
  additionally needs Work Order create permission. Existing server role rules,
  company scoping, document release controls and audit trails remain authoritative.
- User/company/read-only scope changes clear the screen. Every asynchronous step
  checks the session before continuing. A busy lock prevents double submissions.
- Once entry starts the reviewed plan is frozen. Receipts show actual record IDs,
  links, status and created/matched disposition, including a routing header if an
  operation save fails later. These steps are separate transactions, not an atomic
  multi-record import. An uncertain/partial save stops entry and disables automatic
  retry. Reopen the recorded links and match/search before manual recovery.
- A Draft WO with the same intake marker is detected among current jobs. Reloading
  is not permission to retry an uncertain save; inspect Work Orders first.
- Download review/receipts before leaving. The review stays in browser memory and
  is not auto-restored after reload. The exported JSON is a review record, not an
  approval token or server audit. Source references in saved engineering records
  and normal server audits provide durable attribution. Routing header creation
  now has an audit entry in addition to its existing operation audit entries.
- No cost/price, inventory, performed inspection, production completion or release
  is fabricated. Existing controls still apply if an authorized person chooses
  to edit a linked record outside this workspace.

General **Read documents** intake still supports filing metadata. Its drawing
review links here; it is not an alternate engineering-creation path.

## Regression coverage

- Parser/review tests: explicit table evidence, missing quantities/revisions,
  scanned pages, restriction stops, grade-only material SKUs, required approvals,
  unapproved time standards, duplicate PNs and record conflicts.
- Entry tests: exact-match reuse, guarded writes, BOM quantities per parent,
  released record conflicts, partial receipts, session changes, uncertain saves,
  duplicate WO detection and Draft-WO quantity verification.
- API integration: actual Parts/BOM/Routing/release/Draft-WO authoring, revision
  guards, operator denial and audit attribution. Engineering remains unavailable
  as a server-side Hank task.
- Browser acceptance: a synthetic PDF through review, matching, entry, native
  Engineering release screens and Draft WO readback; restricted PDF stop. All
  browser acceptance requests use synthetic local data.
