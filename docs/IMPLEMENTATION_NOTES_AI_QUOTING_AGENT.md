# Implementation Notes: AI Quoting Agent (Sheet Metal)

## Summary
Implemented an end-to-end RFQ ingestion and AI-assisted quoting workflow integrated into existing ERP/MES quote architecture.

## Backend
- Added new DB models and migration:
- `rfq_packages`
- `rfq_package_files`
- `quote_estimates`
- `quote_line_summaries`
- `price_snapshots`
- Added RFQ API endpoints in `backend/app/api/endpoints/rfq_quotes.py`.
- Extended `GET /quotes/{id}` to include optional AI estimate payload.
- Added `POST /quotes/{id}/generate-pdf` for customer-ready quote PDF output.
- Added parsing service:
- XLSX BOM extraction
- PDF drawing metadata extraction
- DXF flat pattern geometry extraction
- STEP fallback bounding-box extraction with low-confidence flags
- Added pricing service with pluggable providers, caching, and source-attributed snapshots.
- Added deterministic sheet-metal costing + lead-time service.

## Frontend
- Added new page `frontend/src/pages/RFQQuoting.tsx`.
- Added route `/rfq-packages/new`.
- Added navigation entry `AI RFQ Quote`.
- Added quote page shortcut button to RFQ workflow.
- Added client API methods for RFQ package upload, estimate generation, approval, internal export, and customer PDF generation.

## Tests
Added backend coverage for:
- XLSX BOM parsing
- DXF geometry extraction
- Pricing cache and snapshot persistence
- Deterministic costing output
- API flow: upload RFQ package -> generate estimate -> export internal estimate

## Design Constraints Preserved
- No customer-facing operation-time estimate lines.
- Missing data is explicit in assumptions/missing-specs output.
- Fallback pricing and STEP geometry are clearly labeled.
