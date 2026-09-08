# Advisory stock-piece observations

This is the first physical-stock foundation for quote nesting. It records what a
person reports measuring, against an as-of inventory source snapshot. A record
does not establish available material, certification, ownership, a remnant price,
or an exclusive allocation. The displayed status is **Recorded observation —
availability and eligibility unverified.**

## Decisions and evidence

| Classification | Decision | Reason |
| --- | --- | --- |
| VERIFIED | Existing `InventoryItem` rows and their movement ledger remain operational authority. | A row may represent multiple pieces or a non-sheet UOM. Its quantity is not a physical piece count. Existing receipt, issue, transfer, adjustment, completion and return writers do not participate in exclusive piece claims. |
| VERIFIED | Keep one company-unique label and append corrections or withdrawals. | Overwriting a measured outline, source lot or observer would destroy the evidence needed to explain an earlier decision. |
| VERIFIED | Capture inventory item, Part and movement watermark in one SQL statement. | Under PostgreSQL READ COMMITTED, separate statements could otherwise describe different source moments. |
| VERIFIED | Source IDs are immutable evidence locators without operational foreign keys. | Removing live source rows must neither erase history nor prevent an honest withdrawal. New RECORDED writes still verify the exact tenant item/Part relationship in both service and database guards. |
| ASSUMPTION | A manually applied, case-sensitive label identifies one reported physical piece. | The platform cannot establish that the label exists on a rack or prevent someone describing one physical object under two different labels. |
| CONFIGURATION | Grade, thickness, location, grain, ownership and certification are reported evidence, including explicit unknowns. | No name-based matching, UOM conversion, price allocation or certification approval is inferred. Approved eligibility rules belong to a later versioned policy. |
| OPEN QUESTION | Physical verification, label assignment, storage, eligibility and valuation approval procedures. | These must be established before observations become selectable available stock or carry quote credit. |

## Data and transaction boundary

Migration `104_stock_piece_observations` follows `103_nesting_spacing_policies`
and creates only `stock_pieces` and `stock_piece_observations`, their indexes,
constraints and trigger functions. It seeds no company records, changes no
operational columns, and creates no scheduled work or environmental settings.

The header owns immutable company/label/creator identity and a current observation
counter. Each observation stores reported geometry/specification, canonical
content hash and byte count, observer and observation time, immutable source
snapshot/hash, reason, authenticated actor/token attribution and UUID request
identity. Observation time is explicit UTC; the UI displays shop-local Central
time. Geometry and dimensions use canonical inch decimal strings with at most
nine fractional places. This serialization precision is not a claim about shop
measurement accuracy.

The service acquires a tenant/request advisory lock, recovers an identical
same-actor and same-credential retry, then locks the target header for a correction
or withdrawal. It checks the expected header version and source fingerprint before
writing. Header advancement, observation insertion and required audit share one
transaction. A different command under an existing request key conflicts. A
failed audit rolls the entire command back, including a newly inserted header.

Database guards reject changed header identity, skipped counters, observation
UPDATE/DELETE/TRUNCATE and cross-company or mismatched source relationships.
WITHDRAWN can follow only RECORDED and must preserve prior measurement and source
evidence. RLS is enabled and PUBLIC, `anon` and `authenticated` table and sequence
privileges are revoked. Equivalent guards exist in model bootstrap and migration
DDL. The service provides atomic header/observation completeness; database guards
do not make arbitrary direct header inserts a supported write interface.

Downgrade is tested as a schema round trip on disposable data. It deletes these
new history tables and is **not** the operational rollback for a deployed register
containing observations. Retain the additive schema when rolling application code
back; preserve recorded history.

## Source drift and geometry limits

The snapshot includes item/Part metadata and movement count, maximum transaction
ID and maximum transaction timestamp. Coverage includes direct-item movements
and same-Part movements without an item ID. A legacy unattributed movement may
therefore flag another lot's observations conservatively. This watermark detects
new movement/source changes; it is not an immutable digest of every historical
ledger field or proof of present stock. Current evidence is compared on read;
GET requests never reconcile, reserve, consume or alter either system.

RECORDED writes require the reviewed source fingerprint. A source can change
after that snapshot because observation recording deliberately takes no
operational stock locks. The response and subsequent reads expose current drift
without altering saved evidence. Retry recovery precedes source/version checks,
so a successful command can be recovered after its source changes. WITHDRAWN
does not require a still-existing live source.

Supported reported outlines are unknown, rectangle, circle, and polygon with
holes, plus bounded unavailable zones. The API enforces structure, exact units,
finite/bounded decimal coordinates, unique zone IDs, at most 2,000 total source
vertices and 16 zones, and a 128 KiB canonical measurement payload. Unknown
geometry cannot carry positioned zones. The existing request-body cap also
applies. There is no server topology approval in this increment. Self-crossing or
mis-measured geometry must not acquire nesting or eligibility authority simply
because its evidence was recorded. Exact source outlines remain reviewable;
bounding extents never substitute for the reported shape.

There is no import of predicted leftovers, automatic parent/child generation,
reserve/consume action, quote total change, remnant credit, DXF-to-stock conversion
or machine-control behavior in this increment.

## Verification and follow-on activation

- SQLite/API tests cover strict inputs, actual role/token/company restrictions,
  read purity, source drift, request recovery, audit rollback and unchanged stock.
- `scripts.verify_stock_piece_postgres` checks both migration and model-bootstrap
  guards in UUID-named disposable PostgreSQL schemas. It exercises inherited
  PUBLIC-grant revocation, immutable history, source deletion plus withdrawal,
  concurrent counter updates, duplicate labels and request keys, and repeated
  migration round trips.
- `scripts.verify_stock_piece_api_postgres` uses actual JWT authentication and
  independent database sessions to race same-key creates, corrections,
  withdrawals and labels; it verifies actor/tenant isolation and required-audit
  rollback while quantities and the movement ledger stay unchanged.
- The existing E2E PostgreSQL operations verifier invokes both checks before its
  seeded browser workflow. Each refuses non-test or remote database targets and
  removes only its own generated schema.

The next activation design must cover every existing inventory writer, exact
physical verification, material/grade/certification compatibility and valuation,
and an agreed quantity reconciliation rule. Reservation exclusivity cannot be
achieved by placing a claim table beside uncoordinated inventory writers.
