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
| 5 | Passed: a marker goes into `request.scope["state"]` for the response middleware | none | none |

The response middleware (`ExportControlMiddleware`, the innermost user middleware, so it sees
uncompressed JSON) then reads the marker:

- **Not JSON** (a PDF, CSV, XLSX, drawing, or a redirect to file storage; any status other than
  204 / 304 / 1xx): the response is replaced by **403** `"Read-only API token cannot download
  files."`. This applies to every read-only token. For an AI-external one it also writes a ledger
  row (`non_json_response`).
- **JSON, 2xx:** the response is buffered and redacted (§4). One `ai_external_redacted` ledger row
  per withheld record is written and committed, then the body is sent with a corrected
  `Content-Length` and no `ETag`.
- **JSON, error status:** only the residual text pass (§4) runs. It writes no ledger rows.
- **Any failure** building the index, redacting or writing the ledger: **503**
  `"Export-control check failed; nothing was sent."`, and no byte of the original body is sent.

The MCP door has an additional check. When the company's AI egress is off, an AI-external token
is answered HTTP **403** `{"detail": "AI egress is disabled …"}` at `/mcp` itself, by
`AiEgressDoorGuard` in `app/mcp/http.py`. This happens before the MCP transport runs, so
`initialize` and `tools/list` are refused as well as tool calls. That refusal is ledgered too.
A tool the token may not call is refused by name with the same API-layer text, before dispatch
(see [MCP.md → 4.2](MCP.md#42-ai-external-tokens-and-export-control)). That refusal writes no
ledger row, because nothing reached the API.

A request authenticated by any API token (full ones included) has its conditional headers
stripped (`If-None-Match`, `If-Match`, `If-Modified-Since`, `If-Unmodified-Since`, `If-Range`).
An API token therefore never gets a 304, so a cached unredacted body cannot outlive a customer
becoming controlled.

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

### Linkage map

| Record | Withheld when |
|---|---|
| Part | its `customer_name` is controlled or unresolvable; or it is blank on an engineering part (manufactured / assembly); or it is the part or a component part of a work order whose own name is controlled (one step only, not iterated, so a common component of one ITAR job does not spread across the shop). A blank-customer **material-supply** part (purchased, raw material, hardware, consumable) is generic stock and passes |
| Work order | its own name is controlled or matches no customer; or its part or any operation's component part belongs to a controlled customer. With no name, it takes its part's standing. With no name and no part, it takes its parent's standing. A controlled parent controls its children, transitively |
| Operation | its work order, or its component part, is withheld |
| Time entries, downtime events | they carry the work order's id, which identifies them as that work order's (there is no time-entry list route on the allowlist; time entries reach the connector through the work-order timeline) |
| Routing | its part is withheld |
| FAI | its part or its work order is withheld |
| NCR | its part, work order or receipt is withheld |
| CAR | an NCR or a customer complaint that references it is withheld, or the complaint's customer is controlled or unresolvable |
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
- CARs that no NCR or complaint references;
- purchase orders and receipts with no part, or whose line parts are raw-material / hardware stock
  with a blank customer. The part record itself still passes; the buy cannot be tied to the job it
  was for;
- engineering parts with a blank or unresolvable customer name, and the work orders that take
  their standing from them;
- quotes with a blank customer name;
- any record id the index never enumerated. "Absent" never means "fine".

**Expect over-redaction.** Free-text names that drifted from the customer master after a rename, a
typo, or a name never entered are withheld until they resolve. That is the fail-closed direction.
Nothing leaks, but the connector sees more `[export-controlled]` than the data strictly needs. To
size it before go-live, run these read-only queries:

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
- it is a top-level record of a route whose objects identify themselves only by `id`. For
  example, a CAR carries no foreign key at all;
- the route's own path parameter names a withheld record. Then the **whole** response is that
  record's (for example `/work-orders/{work_order_id}/timeline`);
- it sits inside a withheld object.

Inside a withheld object a **keep-allowlist** applies. These stay true:

- ids;
- status, state, priority, sequence and version;
- date and time fields;
- JSON numbers, booleans and nulls;
- the business identifiers `work_order_number`, `wo_number`, `operation_number`, `ncr_number`,
  `car_number`, `fai_number`, `quote_number`, `po_number`, `receipt_number`, `lot_number`,
  `work_center_code` and `work_center_name`.

**Every other string becomes `"[export-controlled]"`.** That includes part numbers, descriptions,
customer names, notes, instructions and specifications, and a numeric-looking string under a
text key such as a numeric part number. Each withheld object gains `"export_controlled": true`, so
a consumer can tell "withheld" from "empty". Counts, ids, statuses, dates and quantities stay
true, so OEE and throughput figures are unaffected.

The **residual text pass** then runs over every remaining string in the response, including
cleared records, object keys and error `detail` text. It replaces each sensitive token,
case-insensitively, with `[export-controlled]`. The sensitive tokens are:

- the part numbers, aliases, customer part numbers and drawing numbers of every withheld part;
- controlled customers' names and codes;
- every free-text customer name that resolves to no customer.

Tokens shorter than 4 characters are not matched.

The input is never mutated. Records are redacted, not dropped, so list lengths and totals stay
true.

---

## 5. The AI-external allowlist

`app/core/export_control.py` → `AI_EXTERNAL_ALLOWED_ROUTES` (GET only, relative to `/api/v1`).
This is the brief's read set and nothing else:

| Area | Routes |
|---|---|
| Work orders | `/work-orders/`, `/work-orders/browse`, `/work-orders/{work_order_id}`, `/work-orders/by-number/{wo_number}`, `/work-orders/{work_order_id}/timeline` |
| Shop-floor operations | `/shop-floor/operations`, `/shop-floor/operations/{operation_id}` |
| Downtime | `/downtime/`, `/downtime/active`, `/downtime/by-work-center`, `/downtime/reason-codes`, `/downtime/{event_id}` |
| Work centers | `/work-centers/`, `/work-centers/types`, `/work-centers/{work_center_id}` |
| OEE | `/oee/records`, `/oee/records/{record_id}`, `/oee/dashboard`, `/oee/trends`, `/oee/six-big-losses/{work_center_id}`, `/oee/targets` |
| Parts and routings | `/parts/`, `/parts/{part_id}`, `/parts/by-number/{part_number}`, `/routing/`, `/routing/{routing_id}`, `/routing/by-part/{part_id}` |
| Quality | `/quality/ncr`, `/quality/ncr/{ncr_id}`, `/quality/car`, `/quality/car/{car_id}`, `/quality/fai`, `/quality/fai/{fai_id}` |
| Quotes | `/quotes/`, `/quotes/{quote_id}` |
| Purchasing and receiving | `/purchasing/vendors`, `/purchasing/vendors/{vendor_id}`, `/purchasing/purchase-orders`, `/purchasing/purchase-orders/{po_id}`, `/receiving/history`, `/receiving/receipt/{receipt_id}` |
| Maintenance work orders | `/maintenance/work-orders`, `/maintenance/work-orders/overdue`, `/maintenance/work-orders/{wo_id}` |

That is 44 routes. The MCP tool names they map to (5 convenience + 39 generated) are listed in
[MCP.md → 4.2](MCP.md#42-ai-external-tokens-and-export-control). Every other route, global search
and the shop-floor dashboard included, is refused by gate 4.

`/downtime/summary` is deliberately **not** on the list, although it is a downtime read. Its
Pareto `top_reasons` falls back to an event's free-text `description` in an object that carries no
record key. A controlled job's downtime text could not be linked and withheld there, so the route
is refused rather than risk a leak. The per-event downtime reads carry `work_order_id` and are
redacted.

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
| `action` | `ai_external_redacted` (one row per withheld record per request, deduplicated) or `ai_external_refused` (one row per refused request) |
| `record_type` / `record_id` | the withheld record, for example `work_order` / `412`; `request` / NULL for a refusal |
| `allowed` | always false |
| `denial_reason` | `export_controlled_customer`, `unlinkable_record`, `ai_egress_disabled`, `route_not_allowlisted` or `non_json_response` |
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
   refused 403 at the door, which is the intended state. When cleared, an Admin turns it on under
   **Admin Settings → AI Privacy** (`PUT /companies/me/ai-egress`, audited). Turning it on also
   enables AI document extraction for the company; this one switch gates both.
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

- **Existence oracles remain.** A list filter or `search=` parameter carrying a controlled part
  number returns a redacted row, which confirms that the string exists. Sort order and counts are
  preserved by design.
- **Read-only means no non-GET request, not "causes no writes".** Allowlisted reads that reconcile
  on read can still write:
  - `GET /work-orders/`, `/work-orders/{id}` and `/shop-floor/operations…` can commit status
    reconciliation and audit rows, attributed to Werco Assistant with the token's
    `extra_data.credential`;
  - every request touches the token's `last_used_at`, at most once per 5 minutes.
- **The stdio bridge.** A bridge started with an AI-external token narrows its own listing from the
  token's unverified `ai_ext` claim and has no egress check of its own. The API layer refuses every
  call regardless, and that is the control.
- **The cost is per request.** The index is rebuilt on every AI-external response, and each
  withheld record writes a ledger row. Prefer narrow, filtered reads.
- **Handling instructions have no writer** in the app. Contracts are the supported way to mark a
  customer.
- **CMMC.** This is an AS9100D / customer-contract control, not a CMMC artifact. The frozen
  `CMMC_LEVEL_2_COMPLIANCE.md` was not updated.

---

## 10. Code and tests

| Concern | Where |
|---|---|
| Token columns `scope`, `ai_external` | `app/models/api_token.py`, migration `119_api_token_scope` |
| Gates 1–5 | `app/api/deps.py` (`get_current_user`, `read_only_api_token_refusal`, `ai_external_route_allowed`) |
| Allowlist, refusal texts, ledger vocabulary, marker | `app/core/export_control.py` |
| Index and ledger writes; contract write path | `app/services/export_control_service.py` |
| Redaction engine (pure) | `app/services/export_control_redaction.py` |
| Response middleware | `app/middleware/export_control.py` (registered first in `app/main.py`, so it is innermost) |
| Door egress guard, AI-external listing | `app/mcp/http.py` (`AiEgressDoorGuard`), `app/mcp/server.py` |
| Contract routes | `app/api/endpoints/customers.py`, `app/schemas/export_control.py` |
| UI | `frontend/src/pages/ApiTokens.tsx`, `frontend/src/components/customers/CustomerExportControlPanel.tsx` |
| Tests | `backend/tests/api/test_ai_external_token.py` (gates, canary conformance per allowlisted route, ledger, 503), `tests/api/test_customer_export_control.py`, `tests/test_export_control_index.py`, `tests/test_export_control_redaction.py`, `tests/test_mcp_ai_external.py` |

Related: [API.md → API tokens](API.md#api-tokens-bots-and-mcp-clients),
[MCP.md → 4. Auth model](MCP.md#4-auth-model),
[RBAC_PERMISSIONS.md → Credential policy](RBAC_PERMISSIONS.md#credential-policy-for-judgment-writes).
