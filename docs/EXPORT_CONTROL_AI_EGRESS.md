# Export control for AI-external API tokens (ITAR / CUI)

This runbook covers the credential an **outside AI service** holds, such as Claude's custom
connector to the MCP door. It explains when that credential is let in, what it may read, what is
withheld from it and why, how an Admin marks a customer export-controlled, and where every
withheld record is logged. It describes the code as shipped in the Claude-plugin brief's P0
(2026-11-13): migration `119_api_token_scope`, `app/core/export_control.py`,
`app/services/export_control_service.py`, `app/services/export_control_redaction.py` and
`app/middleware/export_control.py`.

An **AI-external** API token is a per-user API token minted with `ai_external: true`. It is always
read-only, so every [read-only rule](MCP.md#41-read-only-api-tokens) applies to it as well. A
person signed in to the app, and a full API token, are not affected by anything here.

---

## 1. The gates, in order

`get_current_user` (`app/api/deps.py`) applies these to every `/api/v1` request an AI-external
token makes. The MCP door makes the same tool calls through the same stack, so they apply there
too.

| # | Check | Refusal | Ledger row? |
|---|---|---|---|
| 1 | The company's `allow_ai_egress` is on (`llm_client.ai_egress_allowed`, the single reader of the switch) | **403** `"AI egress is disabled for this company: an AI-external API token cannot be used until an Admin enables it."` | Yes: `ai_external_refused` / `ai_egress_disabled` |
| 2 | The method is `GET`, `HEAD` or `OPTIONS` | **403** `"Read-only API token cannot modify data."` | No |
| 3 | The matched route carries no tag in `WERCO_MCP_READ_ONLY_DENY_TAGS` (no matched route at all fails closed) | **403** `"Read-only API token cannot access this resource."` | No |
| 4 | `(method, route template)` is on the AI-external allowlist (§5) | **403** `"This resource is not available to an AI-external API token."` | Yes: `ai_external_refused` / `route_not_allowlisted` |
| 4b | Every query parameter (and value) is on that route's list in `AI_EXTERNAL_QUERY_PARAMS` (§5.1); an undeclared parameter is refused, not ignored | **403** `"This query parameter is not available to an AI-external API token: free-text search, customer filters and text sort keys are refused."` | Yes: `ai_external_refused` / `query_parameter_not_allowlisted` |
| 5 | Passed: a marker goes into `request.scope["state"]` for the response middleware | none | none |

The response middleware (`ExportControlMiddleware`, the innermost user middleware, so it sees
uncompressed JSON) then reads the marker:

- **Not JSON** (a PDF, CSV, XLSX, drawing, or a redirect to file storage; any status other than
  204 / 304 / 1xx): the response is replaced by **403** `"Read-only API token cannot download
  files."`. This applies to every read-only token. For an AI-external one it also writes a ledger
  row (`non_json_response`).
- **JSON, 2xx:** the response is buffered and redacted (§4). One `ai_external_redacted` ledger row
  per withheld record is written and committed, then the body is sent with a corrected
  `Content-Length` and no `ETag`. One exception: `GET /parts/by-number/{part_number}` that finds a
  **withheld** part answers the same **404** a number nobody carries gets (the lookup is still
  ledgered). A 200, even fully redacted, would confirm that the probed part number exists. The
  two 404s also **cost the same**: a number that matches nothing builds the same index and
  commits one `ai_external_lookup_miss` ledger row before it is answered, so response time does
  not tell a withheld part from a missing one either.
- **JSON, error status (a route's own error, not a gate refusal):** the body is replaced by one
  fixed text per status code, `"Request failed (<code> <phrase>); details are withheld from
  AI-external API tokens."`, with fresh headers. A **422** also carries `errors`: each one's `type`
  and location (`["query", "limit"]`) and `"msg": "Invalid value."`, never the value sent. A route
  error can echo the request (a 422's `input`, a 404 naming the number asked for), and scrubbing
  an echoed part number to `[export-controlled]` would itself confirm the probe. No ledger rows,
  except the part-number lookup's own 404 above (`ai_external_lookup_miss`).
- **A refusal marker on a non-error response** (something caught a gate's 403 and the route
  answered 2xx anyway): **503**, nothing of that body is sent. The refusal is still ledgered.
- **Any failure** building the index, redacting or writing the ledger: **503**
  `"Export-control check failed; nothing was sent."`, and no byte of the original body is sent.

The MCP door has an additional check. When the company's AI egress is off, an AI-external token
is answered HTTP **403** `{"detail": "AI egress is disabled …"}` at `/mcp` itself, by
`AiEgressDoorGuard` in `app/mcp/http.py`. This happens before the MCP transport runs, so
`initialize` and `tools/list` are refused as well as tool calls. That refusal is ledgered too.
A tool the token may not call is refused by name with the same API-layer text, before dispatch
(see [MCP.md → 4.2](MCP.md#42-ai-external-tokens-and-export-control)). That refusal writes no
ledger row, because nothing reached the API.

A request authenticated by a **read-only or AI-external** API token has its conditional headers
stripped (`If-None-Match`, `If-Match`, `If-Modified-Since`, `If-Unmodified-Since`, `If-Range`),
read from the JWT's own `api_scope` / `ai_ext` claims without a database call. Such a token
therefore never gets a 304, so a cached unredacted body cannot outlive a customer becoming
controlled. A full token, which carries neither claim, keeps its 304s. Removing the claims does
not help a narrowed token: the claim must match the token's row, or the token is refused 401.

---

## 2. When a customer is export-controlled (owner decisions, 2026-11-13)

A customer is **controlled** when either of these is true:

- **Any** `customer_contracts` row of the company for that customer has `itar_controlled`,
  `export_controlled` or `contains_cui` set, or has `default_data_classification` of
  `itar_export_controlled` or `cui`. Every contract row counts, whatever its `active` flag and
  effective / expiration dates, because ITAR obligations on technical data outlive the contract.
- **Any** `customer_handling_instructions` row has `default_data_classification` of
  `itar_export_controlled` or `cui`, linked to the customer directly or through its contract.

Further rules:

- A **soft-deleted customer still counts**: its historical records stay controlled.
- **ITAR outranks CUI.** When a customer has both, the classification recorded is
  `itar_export_controlled`.
- **These do not control a customer:** `contains_fci`, `restricted_customer`, and a handling
  instruction's `requires_export_review`.
- **The index is rebuilt on every request**, with no cache. A contract change applies from the
  next request.

**Only an Admin may mark or unmark a customer** (§6). No API token may do it, whatever its role.

---

## 3. Which records belong to a controlled customer

### Positive clearance (fail closed)

Work orders, parts and quotes name their customer only through a free-text `customer_name`. That
name is not validated, and renaming a customer does not update it. The rule is therefore
**positive clearance**, not a match against controlled names. A record joined by name is served
in full **only** when its name resolves to a known `Customer` row with no controlling contract.
Resolution normalizes the name (casefold, strip, collapse whitespace) and matches it against the
customer's `name` **or** `code`; soft-deleted customers are included.

A name that is blank, matches no customer, or matches a controlled customer is **withheld**. A
name that matches no customer might be a controlled customer misspelled, so it is also added to
the text pass. If a cleared customer and a controlled customer share a normalized name or code,
that key counts as controlled.

**A controlled customer's former names stay controlled.** Every name and code a controlled
customer has carried, read from its own `audit_log` rows (the CREATE row, each rename's UPDATE
row), counts as that customer's, and is added to the text pass. So renaming a controlled customer
frees nothing: if another customer later takes the old name, or already carries it as a name or
code, the records filed under the old name stay withheld. The same holds when the rename came
first and the customer was marked afterwards. Former names only ever control. A record is still
cleared only by a customer's **current** name or code, so an old name of an uncontrolled customer
resolves to nothing and is withheld as unlinkable. Two writes back this up:

- A **rename** (`PUT /customers/{id}` changing `name`) writes its audit row as required evidence.
  If the row cannot be written, the rename is refused **503** `"The audit record for this customer
  rename could not be saved; nothing was changed. Retry."` and nothing is saved. Other customer
  edits keep their best-effort audit.
- A customer that has any export-control contract or handling instruction cannot be
  **hard-deleted**: **400** `"Cannot hard delete: Customer has export-control contracts or handling
  instructions. Soft delete it instead."` A soft-deleted customer keeps its row, its names and its
  contracts.

The cost is over-redaction of an unrelated customer who happens to take the old name: its
records are withheld too, because a name cannot tell the two apart. Rename that customer to fix
it.

### Linkage map

| Record | Withheld when |
|---|---|
| Part | its `customer_name` is controlled or unresolvable; or it is blank on an engineering part (manufactured / assembly); or it is the part or a component part of a work order whose own name is controlled (one step only, not iterated, so a common component of one ITAR job does not spread across the shop). A blank-customer **material-supply** part (purchased, raw material, hardware, consumable) is generic stock and passes |
| Work order | its own name is controlled or matches no customer; or its part or any operation's component part belongs to a controlled customer. With no name, it takes its part's standing. With no name and no part, it takes its parent's standing. A controlled parent controls its children, transitively |
| Operation | its work order, or its component part, is withheld |
| Time entries, downtime events | they carry the work order's id, which identifies them as that work order's (there is no time-entry list route on the allowlist; time entries reach the connector through the work-order timeline) |
| Routing | its part is withheld |
| FAI | its part or its work order is withheld. Each row of the FAI log of record (`/quality/fai/log`) is judged like the FAI it names, by its `fai_id`, `part_id` and `work_order_id` |
| NCR | its part, work order or receipt is withheld |
| CAR | any of its links is withheld. Its OWN links (the CAR form, migration `122`): its part, its customer (`customer_id`) and the customer-name snapshot taken when that customer was chosen, judged on its own like any free-text name (a controlled customer's current or former name controls; a snapshot no customer carries is unresolvable). And its referrers: an NCR or a customer complaint that references it is withheld, or the complaint's customer is controlled or unresolvable. A CAR is cleared only when **every** link clears, so a CAR raised from a cleared NCR is withheld the moment its customer is set to a controlled one |
| Quote | its name is blank, controlled or matches no customer; or its work order, any line's part or work order, or the customer on the fabrication quote it came from is withheld |
| Purchase order | any line's part is withheld, or it has no lines |
| Receipt | its PO line's part is withheld |

Maintenance work orders are scoped to a work center, not a customer, so they pass. Vendors are
not customer data and pass, unless a record inside a vendor response is identified as withheld.

### Unlinkable records, withheld as "unknown"

The ERP cannot tie these to any customer, so they are withheld with `denial_reason`
`unlinkable_record` and classification `unknown`:

- standalone laser work orders (no part, no customer, no parent) and their operations;
- NCRs with no part, work order or receipt;
- CARs with no part or customer of their own that no NCR or complaint references;
- purchase orders and receipts with no part, or whose line parts are raw-material / hardware stock
  with a blank customer. The part record itself still passes; the buy cannot be tied to the job it
  was for;
- engineering parts with a blank or unresolvable customer name, and the work orders that take
  their standing from them;
- quotes with a blank customer name;
- any record id the index never enumerated. "Absent" never means "fine".

**Expect over-redaction.** Free-text names that drifted from the customer master after a rename, a
typo, or a name never entered are withheld until they resolve. That is the fail-closed direction.
Taking a renamed customer's old name for another customer does not clear those records either
(see former names, above). The connector sees more `[export-controlled]` than the data strictly
needs. To size it before go-live, run these read-only queries:

```sql
-- Work orders whose customer_name matches no customer of their company (by name or code)
SELECT count(*) FILTER (WHERE wo.customer_name IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM customers c WHERE c.company_id = wo.company_id
           AND (lower(btrim(c.name)) = lower(btrim(wo.customer_name))
             OR lower(btrim(c.code)) = lower(btrim(wo.customer_name))))) AS unmatched,
       count(*) FILTER (WHERE wo.customer_name IS NULL OR btrim(wo.customer_name) = '') AS blank,
       count(*) AS total
FROM work_orders wo WHERE wo.is_deleted = false;
-- Repeat for parts.customer_name and quotes.customer_name.
-- How many contracts exist at all (zero means nothing is controlled yet):
SELECT company_id, count(*) FROM customer_contracts GROUP BY company_id;
```

The SQL approximates the normalization (it does not collapse inner whitespace).

---

## 4. What an AI-external token sees

The **structural pass** decides whether a JSON object is withheld. It is withheld when any of
these hold:

- one of its identifying keys names a withheld record (`work_order_id`, `part_id`,
  `component_part_id`, `customer_id`, `operation_id`, `ncr_id`, `car_id`, `fai_id`, `quote_id`,
  `po_id` / `purchase_order_id`, `receipt_id`, the matching `*_number` keys, or `customer_name` /
  `customer`);
- it is a top-level record of a route whose objects identify themselves by `id`. For example, a
  CAR's body never names the NCR or complaint that places it, so `/quality/car` and
  `/quality/car/{car_id}` judge each CAR by its `id` (its own `part_id` / `customer_id` are checked
  as identifying keys too);
- the route's own path parameter names a withheld record. Then the **whole** response is that
  record's (for example `/work-orders/{work_order_id}/timeline`);
- it sits inside a withheld object;
- it is an item of a NESTED list that names its record only by `id` or link (`NESTED_SHAPES`): an
  operation detail's `all_operations` (its siblings, by `id`) and a work-order timeline's `items`
  (an operation's start / finish, by its `#operation-<id>` link). A cleared work order can hold a
  withheld operation, one whose component part is unlinkable, and that operation's name must not
  reach the token through a sibling's detail or the timeline. Every timeline item one operation
  owns also carries `operation_id`: its start and finish, its production receipts, process-step
  evidence and labor, and a blocker filed against it. A blocker links to `#job-blockers`, not to
  its operation, so its free-text title is withheld through that field.

Inside a withheld object a **keep-allowlist** applies. These stay true:

- ids;
- status, state, priority, sequence and version;
- date and time fields;
- booleans and nulls;
- numbers, both JSON numbers and numeric **strings** (a Decimal serializes as one), **only**
  under a key that names an id, count, quantity, duration, money amount, rate, percentage or
  sequence. The key must be an id (`id`, `*_id`, `*_ids`, a `*_by` user id, or a CAR's
  `assigned_to` user id), a status, a kept
  identifier, one of `char_number`, `line_number`, `step_number`, `balloon_number`, a
  pagination key or the production calendar's `start_minute` / `end_minute` /
  `production_day_start_minute` (a wall time in minutes after local midnight), or contain one of `quantity`, `qty`, `count`, `hours`, `minutes`, `seconds`,
  `days`, `duration`, `time`, `cost`, `price`, `total`, `amount`, `tax`, `shipping`, `stock`,
  `reorder`, `percent`, `pct`, `rate`, `sequence`, `version`, `priority`, `runs`, `piece`,
  `parts`, `order`, `rank`, `rating`, `score`, `confidence`, `efficiency`, `passed`, `failed`,
  `complete` or `short`. A numeric string under a text key (a numeric part number) is withheld
  anyway, except under the money keys `cost_material` (a CAR's material cost) and `material_cost`
  (a part's): a `Decimal` reaches the token as a numeric string and "material" is a text
  fragment, so these two amounts are named whole (`NUMERIC_STRING_KEEP_KEYS`) instead of being
  withheld as text. A key that names a dimension or measurement of the article is withheld even when it
  also names a count or total (`total_length`, `tolerance_percent`): `nominal`, `toleran…`,
  `measur…`, `dimension`, `diameter`, `thickness`, `length`, `width`, `height`, `depth`,
  `angle`, `radius`, `area`, `perimeter`, `volume`, `weight`, `mass`, `gauge`, `inch`, `torque`,
  `pressure`, `temperature`, `hardness`, `tensile`, or the word `mm` / `cm`. This is an
  allowlist: an FAI's `nominal`, `tolerance_plus`, `tolerance_minus` and `actual_value`, an NCR's
  `actual_value` / `required_value`, a legacy AI estimate's `flat_area` and `cut_length`, and a
  number under any key the list does not name, such as `assumed_inches` in an estimate's
  free-form JSON, are withheld. Every numeric field an allowlisted route types in its response
  schema is pinned kept or withheld by
  `tests/test_export_control_redaction.py`; today only `flat_area` and `cut_length` are withheld.
  Every `Decimal` field is pinned through the string path as well, since that is how it is sent.
  Members of a free-form JSON object keep a number only under their own counting key, so a
  legacy AI estimate's `totals.grand_total` stays and its `totals.material` does not;
- the business identifiers `work_order_number`, `wo_number`, `operation_number`, `ncr_number`,
  `car_number`, `fai_number`, `quote_number`, `po_number`, `receipt_number`, `lot_number`,
  `work_center_code` and `work_center_name`, and `job_number` (the FAI log's name for the work
  order's number);
- a closed-vocabulary category, only as one of its own values (`KEPT_ENUM_VALUES` in
  `app/services/export_control_redaction.py`): an operation's `hours_source` stays when it is
  `time_entries`, `manual`, `standard` or `none` (the schema's `Literal`, pinned by the test), and
  any other value under that key is withheld. Its companions on the work-order read keep through
  the rules above: `recorded_setup_hours` / `recorded_run_hours` (hours), `hours_include_backfill`
  (a boolean), and the `job_cost_actuals` figures and `issued_material_cost` (money).

**Every other value becomes `"[export-controlled]"`.** That includes part numbers, descriptions,
customer names, notes, instructions and specifications, nominal dimensions, tolerances and measured
values, and a numeric-looking string under a text key such as a numeric part number. Each withheld
object gains `"export_controlled": true`, so a consumer can tell "withheld" from "empty". Counts,
ids, statuses, dates, quantities and money stay true, so OEE and throughput figures are unaffected.

The **residual text pass** then runs over every remaining string in the response, including
cleared records, object keys and error `detail` text. It replaces each sensitive token,
case-insensitively, with `[export-controlled]`. The sensitive tokens are:

- the part numbers, aliases, customer part numbers and drawing numbers of every withheld part;
- controlled customers' names and codes;
- every free-text customer name that resolves to no customer.

A string that **is** one of those tokens, compared whole (case, surrounding and repeated
whitespace ignored), is replaced whatever its length, so a controlled customer coded `BAE` never
goes out as `BAE`, in a list such as the browse page's customer facet included. Inside a longer
string a token is matched only when it is at least 4 characters, so a short code cannot rewrite
ordinary words.

The input is never mutated. Records are redacted, not dropped, so list lengths and totals stay
true.

---

## 5. The AI-external allowlist

`app/core/export_control.py` → `AI_EXTERNAL_ALLOWED_ROUTES` (GET only, relative to `/api/v1`).
This is the brief's read set, plus the production calendar (P1.2) and the overdue supplier
re-evaluation list (P2.2), and nothing else:

| Area | Routes |
|---|---|
| Work orders | `/work-orders/`, `/work-orders/browse`, `/work-orders/{work_order_id}`, `/work-orders/by-number/{wo_number}`, `/work-orders/{work_order_id}/timeline` |
| Shop-floor operations | `/shop-floor/operations`, `/shop-floor/operations/{operation_id}` |
| Downtime | `/downtime/`, `/downtime/active`, `/downtime/by-work-center`, `/downtime/reason-codes`, `/downtime/{event_id}` |
| Work centers | `/work-centers/`, `/work-centers/types`, `/work-centers/{work_center_id}` |
| OEE | `/oee/records`, `/oee/records/{record_id}`, `/oee/dashboard`, `/oee/trends`, `/oee/six-big-losses/{work_center_id}`, `/oee/targets` |
| Parts and routings | `/parts/`, `/parts/{part_id}`, `/parts/by-number/{part_number}`, `/routing/`, `/routing/{routing_id}`, `/routing/by-part/{part_id}` |
| Quality | `/quality/ncr`, `/quality/ncr/{ncr_id}`, `/quality/car`, `/quality/car/{car_id}`, `/quality/fai`, `/quality/fai/{fai_id}`, `/quality/fai/log` (the FAI log of record) |
| Quotes | `/quotes/`, `/quotes/{quote_id}` |
| Purchasing and receiving | `/purchasing/vendors`, `/purchasing/vendors/reevaluation-due`, `/purchasing/vendors/{vendor_id}`, `/purchasing/purchase-orders`, `/purchasing/purchase-orders/{po_id}`, `/receiving/history`, `/receiving/receipt/{receipt_id}` |
| Maintenance work orders | `/maintenance/work-orders`, `/maintenance/work-orders/overdue`, `/maintenance/work-orders/{wo_id}` |
| Production calendar | `/production-calendar`, `/production-calendar/resolve` |

That is 48 routes. The MCP tool names they map to (5 convenience + 43 generated) are listed in
[MCP.md → 4.2](MCP.md#42-ai-external-tokens-and-export-control). Every other route, global search
and the shop-floor dashboard included, is refused by gate 4.

`/downtime/summary` is deliberately **not** on the list, although it is a downtime read. Its
Pareto `top_reasons` falls back to an event's free-text `description` in an object that carries no
record key. A controlled job's downtime text could not be linked and withheld there, so the route
is refused rather than risk a leak. The per-event downtime reads carry `work_order_id` and are
redacted.

The **production calendar** reads are reference data. The day start, the shift patterns (names,
codes, wall times, work-center ids) and the instant-to-day-and-shift resolver name no work order,
part or customer, so both are served in full and ledger nothing. They let the plugin bucket its own
figures by the shop's production day and shift. The calendar's two writes, `PUT
/production-calendar` and the working-calendar sync `POST` (a dry run included), are refused to
every read-only token like any other write.

The **Approved Supplier List** (P2.2) is vendor reference data. A vendor names no customer, part or
job, so the vendor reads and the overdue re-evaluation list (`/purchasing/vendors/reevaluation-due`,
`within_days` and `include_inactive` allowed) are served cleared, ASL fields included; the residual
text pass still removes a controlled part number someone typed into `approval_notes`. A purchase
order's `supplier_approval_warnings` are part of that order: each names a part, so inside an order
withheld for export control every warning is withheld with it (code, message and part number), while
a cleared order's warnings go out in full. `POST /purchasing/supplier-approval-check` is a `POST`
and stays refused to every read-only token. A part's `supply_scope` is withheld inside a withheld part
like its other text. A maintenance work order's equipment-asset fields (`asset_id`, `asset_tag`,
`asset_name`, `target_*`; P2.3) are equipment, not customer data, and pass with the rest of that
record; `/maintenance/work-orders` also takes `asset_id`, a filter over a kept id.

### 5.1 Query parameters

A filter is an oracle on the field it filters: `/parts/?search=` matches the part number, name and
description the redaction then hides, so "1 row" against "0 rows" works the withheld text out one
substring at a time. So every allowlisted route has its own list of query parameters an
AI-external token may send (`AI_EXTERNAL_QUERY_PARAMS` in `app/core/export_control.py`), and
anything else, an undeclared parameter included, is refused **403** at gate 4b and ledgered.
Refused, not stripped: silently ignoring a filter would hand an agent the wrong answer as if it
were the right one.

- **Refused:** `search` on `/parts/`, `/work-orders/`, `/work-orders/browse`,
  `/shop-floor/operations`, `/quotes/` and `/quality/fai/log`; `customer` on `/work-orders/browse`,
  `/quotes/` and `/quality/fai/log`;
  `sort=part` / `sort=customer` and `group=customer` / `group=part` on `/work-orders/browse`.
- **Allowed:** filters over fields the redaction keeps, such as ids, statuses, dates, counts,
  booleans, pagination and closed-vocabulary categories (`part_type`, `work_order_type`, downtime
  `category`), the OEE records' `shift_id`, the calendar resolver's `at` and
  `work_center_id`, the FAI log's date range, `status`, `part_id` and `work_order_id`, the
  maintenance work orders' `asset_id`, and the re-evaluation list's `within_days` and
  `include_inactive`.

A new query parameter on an allowlisted route is refused until it is classified; a guard test in
`backend/tests/api/test_ai_external_token.py` fails CI until it is.

**Adding a route is a code change with a test obligation.** Every entry needs a canary
conformance case in `backend/tests/api/test_ai_external_token.py`. That case seeds a controlled
record whose text carries a canary string, calls the route as an AI-external token, and proves the
canary is absent from the raw bytes. A guard test there fails CI when an entry has no such case.
If a new route's objects carry no identifying key, give it a `ROUTE_SHAPES` entry in
`export_control_redaction.py` too.

---

## 6. Marking a customer export-controlled (Admin)

Contracts are the only way the app marks a customer. Handling instructions also control a
customer, but nothing in the app writes them.

**In the app:** open **Customers**, select the customer, and find the **Export control** panel.
Only Admins see it. The panel shows a **Controlled · ITAR** / **Controlled · CUI** /
**Not controlled** badge and the customer's contracts, with a **Controls** marker on each contract
that controls the customer. Use **Add contract**, or **Edit** on a row, to set:

- the flags **ITAR controlled**, **Export controlled** and **Contains CUI**;
- the classification;
- the contract name and number;
- the effective and expiry dates;
- whether the contract is active;
- handling instructions.

A **reason** is required on every save. The dialog previews whether the save will make the
customer controlled, or stop this contract controlling it. Saves are not optimistic: a refusal
keeps the dialog open with the server's message.

**Through the API** (an Admin's interactive session only):

| Method | Endpoint | Notes |
|---|---|---|
| GET | `/customers/{customer_id}/export-control` | The customer's standing. `controlled` is derived from every contract and classified handling instruction, exactly as the redaction reads it. Also returns the winning `data_classification`, `controlling_contract_id`, and every contract with its own `controlling` flag. A soft-deleted customer is readable |
| POST | `/customers/{customer_id}/contracts` | **201**. Body: the contract fields plus a required `reason` (unknown keys are **422**). Returns the customer's export-control view |
| PUT | `/customers/{customer_id}/contracts/{contract_id}` | Changes only the supplied fields; `reason` is always required. A request that changes nothing writes nothing |

Refusals:

- Not an Admin: **403** `Insufficient permissions`.
- An API token of any role or scope: **403** `"An API token cannot read or change a customer's
  export control: an Admin does it, signed in to the app."`.
- Another company's customer or contract: **404**.
- A duplicate `contract_number` in the company: **409**.
- The audit row cannot be written: **503** `"The audit record for this export-control change could
  not be saved; nothing was changed. Retry."`, and nothing is saved.

**Audit.** Every save writes a REQUIRED `customer_contract` CREATE or UPDATE row in the same
transaction. The row carries the reason and the customer's controlled state before and after.
When the customer's standing changes (controlled ↔ not, or ITAR ↔ CUI), the save also writes a
`customer` `STATUS_CHANGE` row with the reason.

**There is no delete.** To unmark a customer, edit each controlling contract with the flags off
and the classification set to something other than `itar_export_controlled` / `cui`, with a
reason. Because every contract counts whatever its dates and `active` flag, setting a contract
inactive or letting it expire does **not** unmark the customer.

---

## 7. The ledger: `controlled_access_events`

The middleware writes the ledger in its own short-lived session, as one bulk insert committed
**before** the response is sent. If the insert fails, the caller gets 503 and nothing else.

| Column | Value |
|---|---|
| `action` | `ai_external_redacted` (one row per withheld record per request, deduplicated), `ai_external_refused` (one row per refused request) or `ai_external_lookup_miss` (one row per part-number lookup that matched nothing, §1) |
| `record_type` / `record_id` | the withheld record, for example `work_order` / `412`; `request` / NULL for a refusal; `part` / NULL for a lookup miss |
| `allowed` | always false |
| `denial_reason` | `export_controlled_customer`, `unlinkable_record`, `ai_egress_disabled`, `route_not_allowlisted`, `query_parameter_not_allowlisted`, `non_json_response` or `not_found` (a lookup miss) |
| `data_classification` | `itar_export_controlled`, `cui`, or `unknown` (unlinkable records and refusals) |
| `user_id` | the token's user |
| `request_id` / `source_ip` | the request's correlation id and client address |
| `extra_data` | `{"credential": {"kind": "api_token", "api_token_id", "jti_prefix", "label"}, "route", "customer_id", "contract_id"}` |

There is no screen or endpoint for the ledger yet. Read it with SQL against the company's rows:

```sql
SELECT occurred_at, action, denial_reason, record_type, record_id, data_classification,
       extra_data->'credential'->>'label' AS token, extra_data->>'route' AS route
FROM controlled_access_events
WHERE company_id = <company id>
ORDER BY occurred_at DESC
LIMIT 200;
```

Two limits apply:

- **The ledger is not tamper-evident.** Unlike `audit_log`, the table has no
  `UPDATE` / `DELETE` trigger and no hash chain. The tamper-evident records are the
  `customer_contract` / `customer` audit rows written when an Admin marks a customer.
- **The ledger grows with use.** A list call over a controlled customer's records writes one row
  per withheld record.

---

## 8. Hand-back: connecting Claude (after P0 ships)

1. **Create the user.** In **Users**, create **Werco Assistant** with role **Viewer**, active.
   Viewer is the narrowest role, and none of the allowlisted reads carries a role gate. An API
   token cannot be bound to a platform admin or superuser.
2. **Mint the token.** Go to **Admin → API Tokens** (`/admin/api-tokens`, Admin only) and choose
   **Issue token**:
   - **Acts as user:** Werco Assistant;
   - **Label:** for example `Claude connector`;
   - **Scope:** **Read-only**;
   - **AI-external:** checked (the default for read-only);
   - **Expires after:** leave it blank for a standing credential, or set a lifetime.

   The token is shown **once**. Copy it straight into step 4. Never paste it into a chat, a ticket
   or a committed file. The mint is audited (`API_TOKEN_ISSUED`, with scope and AI-external).
3. **Turn on AI egress only after the empowered official clears it.** Until then the token is
   refused 403 at the door, which is the intended state. When cleared, an Admin **signed in to the
   app** turns it on under **Admin Settings → AI Privacy** (`PUT /companies/me/ai-egress`). The flip's
   audit rows are required evidence (if they cannot be written nothing changes and the answer is
   503), the route refuses every API token **403**, and it is not an MCP tool — no agent can open
   or close its own connector. Turning it on also enables AI document extraction for the company;
   this one switch gates both.
4. **Add the connector in Claude.** Add a custom connector with the URL
   `https://<api-host>/mcp` (the production API host is listed in
   [DEPLOYMENT_RUNBOOK.md → URLs](DEPLOYMENT_RUNBOOK.md#urls)). Choose **No sign-in**, and add the
   request header `Authorization` = `Bearer <token>`.
5. **Verify.**
   - `tools/list` shows only the AI-external tools (MCP.md §4.2).
   - A work order of a controlled customer comes back with `[export-controlled]` text and
     `"export_controlled": true`, while the same work order is shown in full to a person signed in
     to the app.
   - New `ai_external_redacted` rows appear in the ledger.

**Turning it off.** Either action stops the connector at once, because the row and the switch are
re-read on every request:

- Turn **AI egress** off. The door answers 403, and so does every API route the token tries.
- **Revoke** the token from Admin → API Tokens with a reason. Every request after that is
  answered 401.

Deactivating Werco Assistant revokes its tokens too.

---

## 9. Known limits

- **Probes are closed where they were exact; order is not.** Free-text filters are refused
  (§5.1), a part-number lookup that finds a withheld part answers the not-found 404, and a route's
  error text never echoes the request (§1). What remains is ORDER: a list sorted by a withheld
  field (`/parts/` by part number, for example) still places a withheld row among the cleared
  ones, which bounds where its hidden value sorts, though not what it is. Counts are preserved by
  design. A work-order number lookup confirms that a work-order number exists; work-order numbers
  are kept identifiers anyway.
- **Read-only means read-only** (lead decision, 2026-11-15). A read-only or AI-external token
  (`app.core.credentials.is_narrowed_api_token`) sends no write request, and its reads cause no
  write either. The reads that reconcile on read — `GET /work-orders/`, `/work-orders/{id}`,
  `/shop-floor/dashboard`, `/shop-floor/operations` and `/shop-floor/operations/{id}` — skip the
  reconcile for it and serve the **stored** state: no operation or work-order completion, no
  finished-goods receipt, no backflush or tied-material consumption, no job hour or cost rollup,
  no scheduling refresh, no audit row, no `operation_completed` / `work_order_completed` event
  and so no notification. A person opening the same work order (or a full token) reconciles it
  exactly as before. The cost is freshness: until such a read happens, what Werco Assistant sees
  can lag what the floor's closed labor already recorded (an operation whose labor holds the full
  count still reads as in progress). Two writes remain, and both are about the credential, not the
  data:
  - every request touches the token's `last_used_at`, at most once per 5 minutes;
  - an AI-external response writes its `controlled_access_events` ledger rows (§7).

  GET **exporters** that write an `EXPORT` audit row before streaming the file (visitor log CSV,
  Import Center failed rows, consumable checkouts, and everything through `export_audit.log_export`)
  refuse a read-only or AI-external token **403** `"Read-only API token cannot download files."`
  *before* that row is written, so the audit trail never records an export the caller did not
  receive. A new exporter must call `refuse_file_export_to_narrowed_token` the same way.
- **Former customer names come from the audit log.** They are read from the customer's own
  `audit_log` rows (§3). A rename made outside the app, such as direct SQL, leaves no row, so
  that old name is not protected. Neither is a name a free-text record carries that was never a
  customer's name or code. Both resolve to no customer, so they are withheld until someone
  creates a customer under that name. A rename made through the app cannot leave no row: its row
  is required evidence.
- **The stdio bridge.** A bridge started with an AI-external token narrows its own listing from the
  token's unverified `ai_ext` claim and has no egress check of its own. The API layer refuses every
  call regardless, and that is the control.
- **The cost is per request.** The index is rebuilt on every AI-external response (it loads all of
  the company's parts, work orders, operations, PO lines, receipts, NCRs, quotes and quote lines),
  and each withheld record writes a ledger row, so `/parts/` writes one per blank-customer
  engineering part. "No cache in v1" was the agreed design; a load check (or a per-request-window
  cache) is an open follow-up. Prefer narrow, filtered reads.
- **Marking a customer is never an agent action.** The two contract writes are excluded from the
  MCP catalog (`EXCLUDED_OPERATIONS`), so an agent holding a person's own JWT cannot switch a
  customer's control off either; the routes also refuse every API token (403). Reading a
  customer's standing stays a tool.
- **Neither is flipping an egress switch** (lead decision, 2026-11-15). `PUT /companies/me/ai-egress`
  — and the SMS, carrier and label-print switches' routes — refuse every API token **403**
  (`API_TOKEN_EGRESS_SWITCH`) and are excluded from the MCP catalog, so the connector can never
  turn its own gate on, or another company integration's off.
- **Handling instructions have no writer** in the app. Contracts are the supported way to mark a
  customer.
- **CMMC.** This is an AS9100D / customer-contract control, not a CMMC artifact. The frozen
  `CMMC_LEVEL_2_COMPLIANCE.md` was not updated.

---

## 10. Code and tests

| Concern | Where |
|---|---|
| Token columns `scope`, `ai_external` | `app/models/api_token.py`, migration `119_api_token_scope` |
| Gates 1–5 | `app/api/deps.py` (`get_current_user`, `read_only_api_token_refusal`, `ai_external_route_allowed`, `ai_external_query_params_allowed`) |
| Allowlist, query-parameter lists, masked lookups, refusal and error texts, ledger vocabulary, marker | `app/core/export_control.py` |
| Index and ledger writes; contract write path | `app/services/export_control_service.py` |
| Redaction engine (pure) | `app/services/export_control_redaction.py` |
| Response middleware | `app/middleware/export_control.py` (registered first in `app/main.py`, so it is innermost) |
| Door egress guard, AI-external listing | `app/mcp/http.py` (`AiEgressDoorGuard`), `app/mcp/server.py` |
| Contract routes | `app/api/endpoints/customers.py`, `app/schemas/export_control.py` |
| No reconcile-on-read for a narrowed token | `is_narrowed_api_token` (`app/core/credentials.py`), checked in both `_reconcile_and_commit` copies (`app/api/endpoints/work_orders.py`, `shop_floor.py`) and before `GET /work-orders/{id}`'s component-quantity re-derivation |
| Egress switches refuse API tokens | `API_TOKEN_EGRESS_SWITCH` (`app/core/credentials.py`); `companies.py`, `integrations.py`, `receiving.py`; `catalog.EGRESS_SWITCH_OPERATIONS` |
| UI | `frontend/src/pages/ApiTokens.tsx`, `frontend/src/components/customers/CustomerExportControlPanel.tsx` |
| Tests | `backend/tests/api/test_ai_external_token.py` (gates, canary conformance per allowlisted route, ledger, 503, a narrowed token's reconcile-on-read GET writing nothing), `tests/api/test_egress_switch_credentials.py`, `tests/api/test_customer_export_control.py`, `tests/test_export_control_index.py`, `tests/test_export_control_redaction.py`, `tests/test_mcp_ai_external.py` |

Related: [API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients),
[MCP.md → 4. Auth model](MCP.md#4-auth-model),
[RBAC_PERMISSIONS.md → Credential policy](RBAC_PERMISSIONS.md#credential-policy-for-judgment-writes).
