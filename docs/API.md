# Werco ERP API Documentation

This is a high-level overview of the Werco ERP API. For interactive documentation, visit `/api/docs` when the backend is running.

## Base URL

- Development: `http://localhost:8000/api/v1`
- Production: `https://werco-erp.yourdomain.com/api/v1`

## Authentication

Most endpoints require authentication using JWT tokens.

### Login

```http
POST /auth/login
Content-Type: application/json

{
  "email": "user@werco.com",
  "password": "password"
}
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### Using the Token

Include the token in the Authorization header:
```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

## Core Endpoints

### Work Orders

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/work-orders/` | List all work orders | Yes |
| POST | `/work-orders/` | Create work order | Yes |
| GET | `/work-orders/{id}` | Get work order by ID | Yes |
| PUT | `/work-orders/{id}` | Update work order | Yes |
| DELETE | `/work-orders/{id}` | Delete work order | Admin |
| POST | `/work-orders/{id}/release` | Release to production | Yes |
| POST | `/work-orders/{id}/start` | Start production | Yes |
| POST | `/work-orders/{id}/complete` | Complete work order | Yes |

#### Work Order Schema

```json
{
  "id": 1,
  "number": "WO-10001",
  "customer_name": "Acme Corp",
  "part_id": 123,
  "quantity": 100,
  "status": "planned",
  "priority": 2,
  "due_date": "2024-01-31",
  "created_at": "2024-01-01T10:00:00",
  "updated_at": "2024-01-01T10:00:00"
}
```

### Parts

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/parts/` | List all parts | Yes |
| POST | `/parts/` | Create part | Yes |
| GET | `/parts/{id}` | Get part by ID | Yes |
| PUT | `/parts/{id}` | Update part | Yes |
| DELETE | `/parts/{id}` | Delete part | Admin |
| GET | `/parts/{id}/bom` | Get BOM for part | Yes |

#### Part Schema

```json
{
  "id": 123,
  "number": "P-10001",
  "name": "Shaft Assembly",
  "description": "Main drive shaft assembly",
  "type": "manufactured",
  "unit_of_measure": "EA",
  "material_type": "ST-304",
  "is_active": true,
  "created_at": "2024-01-01T10:00:00"
}
```

### BOM (Bill of Materials)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/bom/` | List all BOMs | Yes |
| POST | `/bom/` | Create BOM | Yes |
| GET | `/bom/{id}` | Get BOM by ID | Yes |
| PUT | `/bom/{id}` | Update BOM | Yes |
| DELETE | `/bom/{id}` | Delete BOM | Admin |

#### BOM Item Schema

```json
{
  "id": 1,
  "bom_id": 10,
  "part_id": 123,
  "quantity": 2.0,
  "position": 1,
  "is_optional": false
}
```

### Work Centers

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/work-centers/` | List all work centers | Yes |
| POST | `/work-centers/` | Create work center | Yes |
| GET | `/work-centers/{id}` | Get work center by ID | Yes |
| PUT | `/work-centers/{id}` | Update work center | Yes |
| DELETE | `/work-centers/{id}` | Delete work center | Admin |

#### Work Center Schema

```json
{
  "id": 1,
  "name": "CNC Mill 1",
  "code": "CNC-001",
  "type": "cnc",
  "description": "Haas VF-3 CNC Milling Machine",
  "hourly_rate": 120.00,
  "is_active": true
}
```

### Routing

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/routing/` | List all routings | Yes |
| POST | `/routing/` | Create routing | Yes |
| GET | `/routing/{id}` | Get routing by ID | Yes |
| PUT | `/routing/{id}` | Update routing | Yes |

#### Routing Operation Schema

```json
{
  "id": 1,
  "routing_id": 10,
  "sequence": 10,
  "operation_code": "MILL-100",
  "description": "Rough mill to blueprint",
  "work_center_id": 1,
  "setup_time": 0.5,
  "run_time": 2.5,
  "notes": "Use roughing tool"
}
```

### Shop Floor

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/shop-floor/dashboard` | Shop floor dashboard | Yes |
| GET | `/shop-floor/my-active-job` | Get current user's active job | Yes |
| POST | `/shop-floor/clock-in` | Clock in to operation | Yes |
| POST | `/shop-floor/clock-out/{id}` | Clock out with production data | Yes |
| GET | `/shop-floor/work-center-queue/{id}` | Get work center queue | Yes |

#### Clock Out Schema

```json
{
  "time_entry_id": 1234,
  "quantity_completed": 50,
  "quantity_rejected": 2,
  "scrap_reason": "Drill bit broke",
  "notes": "Replaced drill bit, resumed operation"
}
```

### Quality

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/quality/inspections/` | List inspections | Yes |
| POST | `/quality/inspections/` | Create inspection | Yes |
| GET | `/quality/inspections/{id}` | Get inspection by ID | Yes |
| POST | `/quality/inspections/{id}/approve` | Approve inspection | Quality |

### QMS Standards & Audit Readiness

Standards/clause/evidence management for AS9100D, ISO 9001, CMMC and similar quality systems, all
under `/qms-standards`. Every endpoint is **tenant-scoped to the caller's active company**
(`get_current_company_id`). Reads (list / get / detail) are available to **any authenticated user**
in the tenant, while writes are **role-gated** — the read-broad / write-restricted model documented
in `RBAC_PERMISSIONS.md`.

**Standards**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/` | List standards with compliance-summary counts (`active_only` filter) | Yes |
| POST | `/qms-standards/` | Create standard | Admin / Manager / Quality |
| POST | `/qms-standards/{standard_id}/upload-pdf` | AI clause extraction from an uploaded PDF | Admin / Manager / Quality |
| GET | `/qms-standards/audit-readiness` | Audit-readiness dashboard summary across active standards | Yes |
| GET | `/qms-standards/{standard_id}` | Get standard with all clauses and evidence | Yes |
| PUT | `/qms-standards/{standard_id}` | Update standard | Admin / Manager / Quality |
| DELETE | `/qms-standards/{standard_id}` | Delete standard and all its clauses/evidence | Admin |

**Clauses**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/{standard_id}/clauses` | List clauses for a standard (flat list) | Yes |
| POST | `/qms-standards/{standard_id}/clauses` | Add a clause | Admin / Manager / Quality |
| POST | `/qms-standards/{standard_id}/clauses/bulk` | Bulk-import clauses (e.g. from a parsed document) | Admin / Manager / Quality |
| PUT | `/qms-standards/clauses/{clause_id}` | Update clause, incl. compliance-status assessment | Admin / Manager / Quality |
| DELETE | `/qms-standards/clauses/{clause_id}` | Delete a clause and its evidence links | Admin / Manager |

**Auto-evidence discovery**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/qms-standards/clauses/{clause_id}/auto-evidence` | Discover live ERP/MES evidence for a single clause (read-only, nothing persisted) | Yes |
| POST | `/qms-standards/{standard_id}/auto-link` | Auto-discover and persist evidence links for all clauses in a standard | Admin / Manager / Quality |

**Evidence links**

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/qms-standards/clauses/{clause_id}/evidence` | Link evidence to a clause | Admin / Manager / Quality |
| PUT | `/qms-standards/evidence/{evidence_id}` | Update evidence, incl. verification | Admin / Manager / Quality |
| DELETE | `/qms-standards/evidence/{evidence_id}` | Remove an evidence link | Admin / Manager / Quality |

> **PDF clause extraction:** `POST /qms-standards/{standard_id}/upload-pdf` requires a text-based
> PDF (≤ 20 MB; scanned/image-only PDFs are rejected) and a configured `ANTHROPIC_API_KEY` — it
> returns **500** if the key is missing. Claude extracts the numbered clauses and persists them
> against the standard.

> **Deletes are soft (records retained):** the three `DELETE` endpoints above return **204** but
> do not physically remove rows — the standard / clause / evidence is marked deleted and disappears
> from all reads (including the nested clauses/evidence on `GET /qms-standards/{standard_id}`), while
> the record is retained for AS9100D traceability. All QMS create / update / delete operations — plus
> a status-change entry when a clause's `compliance_status` changes — are captured in the tamper-evident
> audit trail (`GET /api/v1/audit/`).

#### Audit-Readiness Summary Schema (`GET /qms-standards/audit-readiness`)

```json
{
  "total_standards": 2,
  "total_clauses": 142,
  "compliant": 120,
  "partial": 8,
  "non_compliant": 3,
  "not_assessed": 9,
  "not_applicable": 2,
  "compliance_percentage": 85.7,
  "total_evidence_links": 310,
  "verified_evidence": 240,
  "unverified_evidence": 70,
  "clauses_needing_review": 4
}
```

#### Clause Auto-Evidence Schema (`GET /qms-standards/clauses/{clause_id}/auto-evidence`)

```json
{
  "clause_id": 42,
  "clause_number": "8.5.2",
  "discovered_evidence": [
    {
      "evidence_type": "ncr",
      "title": "Non-Conformance Reports (NCR)",
      "description": "12 NCRs processed in last 12 months, 2 currently open",
      "module_reference": "/quality/ncr",
      "total_count": 12,
      "recent_count": 7,
      "health_status": "healthy",
      "health_detail": "All NCRs resolved within SLA",
      "examples": [],
      "suggested_compliance": "compliant"
    }
  ],
  "overall_suggested_compliance": "compliant"
}
```

### Purchasing

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/purchasing/pos/` | List purchase orders | Yes |
| POST | `/purchasing/pos/` | Create purchase order | Yes |
| GET | `/purchasing/pos/{id}` | Get PO by ID | Yes |
| POST | `/purchasing/po-upload` | Upload PO from PDF | Yes |

> Material receiving and incoming inspection are **not** under `/purchasing`. They live under
> `/receiving` (see below). The duplicate `/purchasing/receiving*` endpoints were removed.

### Receiving & Inspection

Canonical material-receiving and incoming-inspection endpoints, all under `/receiving`.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/receiving/open-pos` | List POs available for receiving (sent/partial) | Yes |
| GET | `/receiving/po/{po_id}` | Get full PO detail for receiving | Yes |
| POST | `/receiving/receive` | Receive material against a PO line | Admin / Manager / Supervisor |
| GET | `/receiving/inspection-queue` | List receipts pending inspection | Yes |
| GET | `/receiving/receipt/{receipt_id}` | Get receipt detail | Yes |
| POST | `/receiving/inspect/{receipt_id}` | Complete inspection (accept/reject, auto-NCR on rejection) | Admin / Manager / Quality |
| GET | `/receiving/history` | Receiving history with inspection results | Yes |
| GET | `/receiving/stats` | Receiving statistics for dashboard | Yes |
| GET | `/receiving/locations` | Receivable inventory locations | Yes |

### Inventory

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/inventory/` | List inventory items | Yes |
| POST | `/inventory/adjust` | Adjust inventory | Yes |
| GET | `/inventory/{part_id}` | Get inventory for part | Yes |

### Shipping

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/shipping/orders/` | List shipping orders | Yes |
| POST | `/shipping/orders/` | Create shipping order | Yes |
| POST | `/shipping/orders/{id}/ship` | Mark as shipped | Yes |

### Reports

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/reports/work-orders` | Work order report | Yes |
| GET | `/reports/production` | Production report | Yes |
| GET | `/reports/quality` | Quality report | Yes |
| POST | `/reports/custom` | Generate custom report | Yes |

### Analytics

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/analytics/overview` | Analytics overview | Yes |
| GET | `/analytics/production-trends` | Production trends | Yes |
| GET | `/analytics/quality-metrics` | Quality metrics | Yes |

### Users (Admin)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/users/` | List all users | Admin |
| POST | `/users/` | Create user | Admin |
| PUT | `/users/{id}` | Update user | Admin |
| DELETE | `/users/{id}` | Delete user | Admin |

### Admin Settings (Admin)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/admin/settings` | Get system settings | Admin |
| PUT | `/admin/settings` | Update system settings | Admin |
| GET | `/admin/settings/audit-log` | Settings/quote-config change history (filterable, up to 1yr) | Admin |

> **Settings-audit tenancy:** `GET /admin/settings/audit-log` reads the `SettingsAuditLog` trail
> (admin / quote-config changes) and is **scoped to the caller's active company**
> (`get_current_company_id`). Writes to this trail are tagged with that same active company, so a
> platform admin's changes attribute to the company they have switched into — matching the
> `/audit/*` (`AuditLog`) attribution. This is a separate trail from `/audit/*` and is **not** part
> of the tamper-evident hash chain.

### Audit Log

Tamper-evident audit trail (CMMC Level 2 AU-3.3.8). Audit rows are **tenant-tagged** with
`company_id`, so retrieval and the per-record lookup are **scoped to the caller's active
company**. The integrity hash chain itself is a single global sequence interleaved across all
tenants, so the aggregate chain-verification endpoints are **platform-admin only**.

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/audit/` | List audit logs for the active company (filterable) | Admin / Manager |
| GET | `/audit/summary` | Audit activity summary for the active company | Admin / Manager |
| GET | `/audit/actions` | Distinct action types in the active company | Admin / Manager |
| GET | `/audit/resource-types` | Distinct resource types in the active company | Admin / Manager |
| GET | `/audit/integrity/status` | Global chain status (counts, sequence range) | Platform Admin |
| GET | `/audit/integrity/verify` | Full hash-chain verification (optional range) | Platform Admin |
| GET | `/audit/integrity/verify-recent` | Verify the N most recent records | Platform Admin |
| GET | `/audit/integrity/record/{sequence_number}` | Verify a single record | Admin (own company only) |

> **Tenancy:** the four retrieval endpoints filter by the active company (`get_current_company_id`),
> returning only that tenant's audit data. `/integrity/record/{sequence_number}` lets a
> company-scoped Admin verify one record **belonging to their active company**; a record from
> another tenant returns **404** (not 403, so cross-tenant probing can't confirm the record
> exists). Platform Admins / superusers may inspect any record.
>
> **Why the aggregate `/integrity/*` endpoints are Platform-Admin only:** the hash chain is one
> global sequence spanning every tenant, so its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to a single company without leaking other tenants' data. A company
> Admin's "are my records intact?" need is served by the per-record endpoint above.

## Common Response Formats

### Success Response
```json
{
  "id": 1,
  "created_at": "2024-01-01T10:00:00",
  "updated_at": "2024-01-01T10:00:00"
}
```

### Error Response
```json
{
  "detail": "Error message description"
}
```

### Validation Error (422)
```json
{
  "detail": [
    {
      "loc": ["body", "field_name"],
      "msg": "Field is required",
      "type": "value_error.missing"
    }
  ]
}
```

### Not Found error (404)
```json
{
  "detail": "Resource not found"
}
```

### Unauthorized error (401)
```json
{
  "detail": "Could not validate credentials"
}
```

## Pagination

List endpoints support pagination via query parameters:

```
GET /work-orders/?page=1&limit=50&sort=created_at&order=desc
```

Parameters:
- `page`: Page number (default: 1)
- `limit`: Items per page (default: 50, max: 100)
- `sort`: Field to sort by
- `order`: Sort direction (`asc` or `desc`)

Response:
```json
{
  "items": [...],
  "total": 234,
  "page": 1,
  "limit": 50,
  "pages": 5
}
```

## Rate Limiting

API endpoints are rate limited:
- Default: 100 requests per 60 seconds per IP
- Health check endpoints: Exempt from rate limiting

Rate limit headers are included in responses:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 75
X-RateLimit-Reset: 1704097200
```

## CORS

Cross-Origin Resource Sharing is configured to allow requests from:
- Development: `http://localhost:3000`, `http://localhost:8000`
- Production: Your configured frontend domain

## Health Check

```http
GET /health
```

Response:
```json
{
  "status": "healthy",
  "app": "Werco ERP",
  "environment": "production",
  "version": "1.0.0"
}
```

## Error Codes

| Status Code | Description |
|-------------|-------------|
| 200 | Success |
| 201 | Created |
| 204 | No Content |
| 400 | Bad Request |
| 401 | Unauthorized |
| 403 | Forbidden |
| 404 | Not Found |
| 422 | Validation Error |
| 429 | Too Many Requests |
| 500 | Internal Server Error |

## Interactive Documentation

When the backend is running, visit:
- **Swagger UI**: `/api/docs` - Interactive API explorer
- **ReDoc**: `/api/redoc` - Alternative documentation view
- **OpenAPI JSON**: `/api/openapi.json` - Raw specification

For more details on specific endpoints, use the interactive documentation above.
