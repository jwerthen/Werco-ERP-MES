# Material price history

Open **Purchasing → Material Price History**, or use **Price history** on Purchasing or Materials & Supplies. A material's row action opens its history directly at `/purchasing/price-history?part=<id>`.

The workspace automatically reads existing purchase orders. It includes any inventory item actually purchased, including raw materials, purchased parts, hardware, consumables, and inactive items with purchase history. No backfill, new data entry, or database migration is required.

## Using the workspace

- Search by item name or part number; narrow by inventory type or latest price movement.
- Sort by purchase date, percentage price increase/decrease, or part number. Each item shows its latest unit cost, change from the previous PO, latest supplier, and a recent-purchase sparkline.
- Select an item to see its cost chart, latest cost, previous-PO change, low/high costs, and ordered value.
- Filter that item's history by supplier or purchase period. The chart, statistics, and PO comparisons use those filters; the inventory list remains an all-time overview.
- Open a PO number to inspect the source purchase order. The table is newest first, with pagination and keyboard-accessible horizontal scrolling on narrow screens.
- **Export CSV** downloads every matching purchase for the selected item, including pages beyond the visible table.

## Cost definitions

Only approved, sent, partially received, received, and closed POs count. Draft, pending approval, cancelled, and deleted POs are excluded. Deleted items are excluded; inactive items and historical suppliers are retained.

One observation is one item on one PO. Multiple lines for that item are combined: unit cost is `sum(quantity ordered × unit price) / sum(quantity ordered)`. Ordered value excludes header tax and shipping and does not represent invoice payments or received value.

The previous cost is the preceding PO within the selected supplier/date scope. Date uses PO order date, falling back to creation date; creation time and PO ID break same-day ties. The first purchase has no comparison. A zero previous cost has no percentage comparison, but its absolute change remains available.

Legacy orders missing both dates and lines with invalid numeric costs or quantities are omitted instead of assigning invented dates or breaking the workspace.

The chart shows purchase order sequence with evenly spaced points. Sparklines show the latest 12 purchases; the main chart shows at most 500 and explicitly indicates truncation. Statistics, the paginated table, and CSV retain the full matching history.

Existing PO lines do not store historical UOM or currency. Units therefore reflect the current part catalog, and amounts use the application's `$` display convention without asserting a recorded currency or performing conversion. These limitations are also explained in the page's calculation details.

## API and access

- `GET /api/v1/purchasing/price-history`: `search`, `part_type`, `trend` (`all`, `up`, `down`, `unchanged`, `new`), `sort` (`recent`, `increase`, `decrease`, `name`), `page`, `page_size`.
- `GET /api/v1/purchasing/price-history/{part_id}`: `vendor_id`, inclusive `start_date`/`end_date`, `page`, `page_size`.

Page sizes are capped at 100. Overview movement counts honor search/type before the movement filter or pagination. Detail `part` and `vendor_options` retain all-time context; `stats`, `history`, and `chart` honor the current filters.

Both endpoints require effective `purchasing:view` permission, including company role overrides. PO headers, lines, items, and vendors are independently company-scoped. Reads do not modify purchase or inventory records.

## Verification

Backend coverage: `backend/tests/api/test_material_price_history.py`.

Frontend interaction coverage: `frontend/src/pages/MaterialPriceHistory.test.tsx`, plus navigation permission and route metadata tests.

Desktop/mobile browser coverage: `frontend/e2e/material-price-history.spec.ts`. Browser responses are synthetic and intercepted; these tests do not access production purchasing data.
