# Piece observations

Open **Warehouse → Inventory → Piece observations** at `/warehouse?tab=inventory&inventory_tab=observations`.

This is an advisory register of individually labeled, reported measurements. Every record is marked **“Recorded observation — availability and eligibility unverified.”** Register totals count observation identities, including withdrawn records; they are not available sheet counts.

## Record a piece

1. Choose **Record piece observation**. The server exposes this action to users with effective `inventory:view` and an existing inventory mutator role (Admin, Manager, or Supervisor), with the existing platform exemptions and read-only company restrictions. Other authorized inventory readers can inspect the register and history.
2. Enter the actual physical label. Labels are case-sensitive, trimmed, and unique within the company. Search ERP sources by Part number/name or lot, page through results, and explicitly select the exact inventory row. The selected Part, lot, location, status, UOM, aggregate on-hand amount, certificate text and movement watermark are source evidence. They do not populate measured dimensions, grade, grain or certification.
3. Enter reported geometry in **inches**, using decimals or exact fractions such as `1 1/8`. Familiar `.125` spelling is accepted; saved values are canonical decimal strings with at most nine decimal places. Values are never rounded through millimeters or floating-point conversion. Fractions that cannot be represented exactly within that precision are refused.
4. Choose unknown, rectangle, circle, or polygon. Circle entry uses center coordinates and radius. Polygon entry uses one `X, Y` pair per line, with optional hole rings; the closing edge is implied. The preview draws the reported outline and holes, including outlying reported features, instead of substituting a bounding rectangle. Optional unavailable zones require an explicit label, reason and circle/polygon outline. Blue depicts the reported piece and amber depicts reported zones. Structural acceptance and a preview do not establish valid topology or physical usability.
5. Describe the measurement method, original evidence units, observer, observed date/time in Central time, and evidence/correction reason. All entered geometry stays in inches even when original evidence was measured in millimeters. Unknown optional thickness, grade, grain, location, ownership and certification are stored explicitly as unknown. A certification note does not verify a certificate. The authenticated recorder and recording time are stored separately from the reported observer/time.
6. Select **Save observation**. Saving creates immutable evidence and a required audit record. It changes no inventory quantity, allocation, movement ledger, work order, quote, reservation, piece valuation or remnant credit.

Bounds are technical input limits, not shop policy: at most 128 KiB of canonical evidence, 2,000 combined source vertices, 16 holes and 16 unavailable zones. Dimensions must be positive and no larger than 100,000 inches; coordinates are bounded to ±100,000 inches. Unknown geometry cannot position zones. The API does not perform nesting-kernel topology verification in this increment.

## History, correction and withdrawal

Select a label to inspect the exact observation, source snapshot, evidence hash, reported fields and immutable timeline. Source drift is computed at read time. “No detected source change” does not prove the piece is still present. A changed or missing source is a review warning; the original snapshot remains unchanged. The watermark includes direct-item movements and unattributed movements for the same Part, so equal balances after consumption/receipt still reveal turnover, and some warnings can concern another lot.

**Record correction** appends evidence with a required reason and explicit current source selection. It uses the version that was opened. If someone recorded a newer observation, the command conflicts; reopen the latest history rather than silently replacing it. A source change also conflicts: refresh the source choices, explicitly reselect the reviewed source, and submit a new command.

**Withdraw observation** appends a reason, observer and observation time while retaining prior measurement and source evidence unchanged. It remains possible after the live source disappears. Withdrawal does not scrap, consume, move, or release material. A withdrawn observation remains visible in history and in the register.

An interrupted save retains its exact request key and body while the editor remains open. **Retry same request** recovers the receipt without creating another observation. Fields remain locked while that result is uncertain; do not assume that an interrupted response means nothing was saved. A rejected source/version request offers explicit review and editing. Cancel, navigation and browser close use the shared unsaved-change guard. Switching user/company remounts the observation panel and aborts pending UI requests; the server independently checks the command's expected company. An abort does not undo a committed record.

## Separation from Material Nesting

The register has no DXF import, automatic adoption of predicted leftovers, “Use in nest,” available/reservable state, monetary credit or operational stock creation. Material Nesting still opens as a fresh empty estimate. Predicted leftover regions remain review-only with zero credit. Exclusive piece allocation, physical parent/child lineage, certified eligibility and valuation require later, separately coordinated inventory work.

The register filter searches labels on the currently loaded page only; page controls cover the rest of the register. Reads do not reconcile or rewrite observations. For API, concurrency and rollback details see [API.md](API.md) and [STOCK_OBSERVATION_ARCHITECTURE.md](STOCK_OBSERVATION_ARCHITECTURE.md).
