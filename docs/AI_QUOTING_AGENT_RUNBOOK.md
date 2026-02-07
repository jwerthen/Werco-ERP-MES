# AI Quoting Agent (Sheet Metal) Runbook

## Scope
This feature supports sheet metal quoting for:
- Laser cutting
- Bending
- Welding/assembly (rule-based)
- Deburr
- Powder coat / paint style finishing
- Basic hardware insertion

## Supported Input Files
- `.pdf` drawing packages
- `.xlsx` / `.xls` BOM files
- `.dxf` flat patterns
- `.step` / `.stp` CAD files (fallback parsing)

## User Workflow
1. Open `Sales & Shipping -> AI RFQ Quote`.
2. Upload RFQ files as one package.
3. Click `Generate AI Estimate`.
4. Review:
- Part summaries
- Total cost breakdown
- Lead time range
- Assumptions
- Missing / Needs Review list
5. Click `Approve & Create Quote`.
6. Export:
- `Export Internal Estimate` for internal review record
- `Generate Customer Quote PDF` for customer-facing quote

## API Endpoints
- `POST /api/v1/rfq-packages/`
- `POST /api/v1/rfq-packages/{id}/generate-estimate`
- `GET /api/v1/quotes/{id}` (includes AI estimate block when available)
- `POST /api/v1/quotes/{id}/generate-pdf`

Additional operational endpoints:
- `POST /api/v1/rfq-packages/{id}/approve-create-quote`
- `GET /api/v1/rfq-packages/{id}/internal-estimate-export`

## Pricing Providers and Configuration
Pricing is pluggable through `MaterialPriceService` in `backend/app/services/rfq_pricing_service.py`.

Default providers:
- `InternalPriceList` (ERP/quote config tables)
- `WebLookup` placeholder (disabled unless controlled implementation exists)

Caching:
- Snapshot cache in `price_snapshots`
- Cache freshness from quote setting `rfq_price_cache_hours` (default `12`)
- If lookup fails: uses last-known snapshot and flags fallback

## Cost Model Configuration
Primary settings are read from `quote_settings`. If missing, defaults are applied.

Key settings:
- `rfq_scrap_factor`
- `rfq_laser_rate_per_hour`
- `rfq_brake_rate_per_hour`
- `rfq_welding_rate_per_hour`
- `rfq_assembly_rate_per_hour`
- `rfq_shop_overhead_pct`
- `rfq_sec_per_bend`
- `rfq_bend_setup_minutes`
- `rfq_laser_setup_minutes`
- `rfq_weld_minutes_per_part`
- `rfq_assembly_minutes_per_part`
- `rfq_finish_rate_per_sqft`
- `rfq_base_queue_days`
- `rfq_daily_capacity_hours`
- `rfq_outside_service_buffer_days`
- `rfq_consumables_factor_pct`

Margin:
- Request-level `target_margin_pct` in estimate generation payload
- Falls back to `default_markup_pct` if not provided

## Confidence, Assumptions, and Missing Specs
The parser never silently invents required fields.

Rules:
- If strong evidence exists, a value may be inferred and logged to `assumptions` with confidence.
- Missing critical fields are listed in `missing_specs`.
- STEP fallback geometry is explicitly marked low confidence.

Confidence fields:
- `material`
- `thickness`
- `geometry`
- `finish`

Overall confidence is reduced by missing-spec penalties.

## STEP Fallback Behavior
If STEP cannot be fully parsed:
- Attempts bounding-box geometry extraction from point data.
- Marks geometry as low confidence.
- Adds explicit warning to assumptions.
- Recommended operator action: provide flat pattern DXF for release-quality quote.

## Customer PDF Rules
Customer PDF includes:
- Customer and RFQ reference
- Part summary table (part/qty/material/thickness/finish)
- Total quote and lead time
- Assumptions and exclusions

Customer PDF excludes:
- Operation-level time line items

## Operational Notes
- Parsing is resilient per file. One bad file should not abort all parsing.
- File parse state is tracked per RFQ package file (`pending`, `parsed`, `parsed_with_fallback`, `error`).
- Logs include parsing failures and fallback conditions.
