# Role-Based Access Control (RBAC) Documentation

## Overview

Werco ERP implements a comprehensive RBAC system with 7 predefined roles. Permissions are enforced both on the backend (API endpoints) and frontend (UI elements).

## Roles

| Role | Description | Use Case |
|------|-------------|----------|
| **Admin** | Full system access | System administrators, IT staff |
| **Manager** | Department-wide access with approval capabilities | Department managers, production managers |
| **Supervisor** | Team-level access with create/edit permissions | Shift supervisors, team leads |
| **Operator** | View and update assigned work | Machine operators, production workers |
| **Quality** | Quality-specific actions | Quality inspectors, QC staff |
| **Shipping** | Shipping operations | Shipping clerks, warehouse staff |
| **Viewer** | Read-only access | Auditors, executives, guests |

## Access enforcement model

Permissions are enforced at two layers, and the two layers **intentionally differ for reads**:

- **Writes / state changes** (Create, Edit, Delete, Approve, Release, Send, Adjust, Transfer, Complete, Inspect, …) are enforced **server-side** via the `require_role` dependency on the endpoint. These are the authoritative access controls and match the matrix below.
- **Operational/domain reads** — the **View** rows for the operational modules below (e.g. Work Orders, Parts, BOMs, Routings, Inventory, Purchasing, Receiving, Customers, Quotes) — are **tenant-scoped** (every query is filtered to the caller's active company via `get_current_company_id`) and are available to **any authenticated user within that tenant**. The list/detail GET endpoints depend on `get_current_user` only and do **not** restrict reads by role. The **View** columns therefore describe the *intended in-app navigation* (which the frontend gates for usability), not a server-enforced read restriction. This is the current intended design: **read-broad / write-restricted**.
- **Administrative / governance reads are the exception and _are_ enforced server-side:** **Users** (`require_role([ADMIN, MANAGER])`), **Admin Settings** (`ADMIN`), and **Audit Logs** (`require_role([ADMIN, MANAGER])`).

> If the business requires least-privilege on domain reads (e.g. hiding vendor pricing / PO financials from Operator/Quality/Shipping at the API), enforce it **uniformly** by adding `require_role` to the read endpoints across modules, with authorization tests — not per-router. Until then, treat the **View** columns for operational modules as UI-visibility, not as a server-enforced control.

## Permission Matrix

### Work Orders

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | ✓ | | | | |
| Complete | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| Approve labor (TimeEntry) | ✓ | ✓ | ✓ | | ✓ | | |

> **Approve labor — endpoint mapping (Batch 11B / G5-A).** The shop-floor labor sign-off
> `POST /api/v1/shop-floor/time-entries/{id}/approve` and `…/unapprove` (which set / clear
> `TimeEntry.approved` + `approved_by`, the field the opt-in `REQUIRE_APPROVED_LABOR_FOR_COST` flag
> keys labor-cost rollups on) are enforced **in code** to this row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`). In
> addition to the role gate, **self-approval is
> forbidden**: a user cannot approve or unapprove their **own** TimeEntry (segregation of duties for
> the labor-cost gate) — that returns **403** even for an approver-role user. A cross-tenant id returns
> **404**. Both actions are audited (`time_entry_approve` / `time_entry_unapprove`).

> **Operator-qualification gate is record-only (Batch 11C / G5-B).** `POST /api/v1/shop-floor/clock-in`
> and `POST /api/v1/shop-floor/operations/{id}/start` stay **operator-facing** — open to **any
> authenticated user** (`get_current_user`), no new role gate. The G5-B qualification gate (no active
> `SkillMatrix` entry at level ≥ 2 for the work center, or a missing/expired required
> `OperatorCertification`) **only records** a tamper-evident `audit_log` row
> (`OPERATOR_QUALIFICATION_EXCEPTION`) + a warning event and surfaces a `qualification_exceptions`
> array on the response; it does **not** gate the operator's role or block the clock-in / start. The
> gate's lookups are tenant-scoped (every skill/cert/work-center query filters the active company).

### Parts

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | | | | | | |

### BOMs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

### Routings

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

### Inventory

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Adjust | ✓ | ✓ | ✓ | | | | |
| Transfer | ✓ | ✓ | ✓ | | | | |

### Purchasing

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Approve | ✓ | ✓ | | | | | |

> **Read enforcement:** Per the [Access enforcement model](#access-enforcement-model),
> Purchasing list/detail reads (`list_vendors`, `list_purchase_orders`, and the
> single-record GETs in `app/api/endpoints/purchasing.py`) are tenant-scoped but **not**
> role-restricted — any authenticated user in the tenant can read vendor and PO data, so
> the **View** row above reflects intended UI visibility rather than a server-enforced
> restriction. Only the write/approve actions (Create, Approve, send, line edits) are
> role-gated. Receiving (below) follows the same read-broad / write-restricted pattern.

### Receiving

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Inspect | ✓ | ✓ | | | ✓ | | |

> **Write enforcement:** The Create and Inspect rows above are now enforced **in code** on
> the canonical `/api/v1/receiving` endpoints (`app/api/endpoints/receiving.py`):
> `POST /receiving/receive` → `require_role([ADMIN, MANAGER, SUPERVISOR])` and
> `POST /receiving/inspect/{receipt_id}` → `require_role([ADMIN, MANAGER, QUALITY])`
> (superuser / Platform Admin bypass role checks, as elsewhere). This replaces a prior state
> where the receive endpoint was not role-restricted and a duplicate receiving/inspection
> path existed under `/api/v1/purchasing`; that duplicate has been removed, so `/api/v1/receiving`
> is the single source of truth. Receiving reads follow the same read-broad / write-restricted
> pattern noted for Purchasing above.

### Shipping

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | ✓ | |
| Complete | ✓ | ✓ | ✓ | | | ✓ | |
| Issue Certificate of Conformance | ✓ | ✓ | | | ✓ | | |

> **Certificate of Conformance — endpoint mapping (Batch 11C / G6-B).** Issuing a CoC
> `POST /api/v1/shipping/{shipment_id}/coc` (mint or return the existing frozen-snapshot CoC) is
> enforced **in code** to `require_role([ADMIN, MANAGER, QUALITY])`
> (`app/api/endpoints/shipping.py`) — a quality artifact, so the write is restricted (this is why the
> matrix row above does **not** include the **Shipping** role, which otherwise holds Shipping
> Create/Complete). Reading the CoC — `GET /shipping/{shipment_id}/coc` (metadata) and
> `GET /shipping/{shipment_id}/coc/pdf` (rendered PDF) — is open to **any authenticated user** in the
> active company (read-broad / write-restricted, like the other shipping reads). All three are
> tenant-scoped (cross-tenant `shipment_id` → **404**). A CoC is also **auto-issued on ship** when
> required; the auto-issue runs in the ship handler's context and is not separately role-gated beyond
> the existing ship permission.

### Quality

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Approve | ✓ | ✓ | | | ✓ | | |
| Calibration | ✓ | ✓ | | | ✓ | | |

> **Inspect — endpoint mapping.** The shop-floor inspection sign-off
> `POST /api/v1/shop-floor/operations/{operation_id}/inspection` (which records
> `WorkOrderOperation.inspection_complete = True` and clears the completion inspection quality gate)
> is enforced **in code** to this Inspect row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`,
> `mark_operation_inspected`). The role set matches the matrix exactly — this repo has no separate
> `INSPECTOR` role, so operation inspection is performed by Admin / Manager / Supervisor / Quality.

### Engineering Change Orders (ECO)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit | ✓ | ✓ | | | | | |
| Submit / Approve / Reject | ✓ | ✓ | | | | | |
| Implement / Complete | ✓ | ✓ | | | | | |
| Add / Edit task, Add approval | ✓ | ✓ | | | | | |

> **ECO mutations are Admin / Manager (enforced in code).** Every state-changing ECO endpoint
> (`POST /eco/eco/`, `PUT /eco/eco/{id}`, and the `submit` / `approve` / `reject` / `implement` /
> `complete` transitions, plus `tasks` create/update and `approvals` create) is gated with
> `require_role([ADMIN, MANAGER])` (`app/api/endpoints/engineering_changes.py`). Any other authenticated
> role receives **403**. The read endpoints (list, get, dashboard, list approvals, affected items) remain
> open to all authenticated users. Previously these mutations were available to **any** authenticated
> user — this row records the tightened authorization landed in WO-completion remediation Batch 11A
> (G4-Fix1), alongside the ECO router's tenant scoping and audit logging.

### Users

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | | |
| Create | ✓ | ✓ | | | | | |
| Edit | ✓ | ✓ | | | | | |
| Delete | ✓ | | | | | | |
| Roles | ✓ | | | | | | |

### Analytics

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Export | ✓ | ✓ | | | | | |

### OEE

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard / trends / six-big-losses / list records & targets) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Write (auto-calculate / create-edit-delete records & targets) | ✓ | ✓ | ✓ | | | | |

> **Write enforcement (read-broad / write-restricted).** The OEE **write/mutation** endpoints —
> `POST /api/v1/oee/calculate/{work_center_id}`, `POST`/`PUT`/`DELETE /oee/records`, and
> `POST`/`PUT`/`DELETE /oee/targets` — are now enforced **in code** to the Write row via
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`OEE_WRITE_ROLES` in `app/api/endpoints/oee.py`),
> matching the sibling Analytics-write posture. **This is a permission change:** these endpoints were
> previously open to any authenticated user. OEE **read** endpoints (`/oee/dashboard`, `/oee/trends`,
> `/oee/six-big-losses/{wc}`, and the list/get GETs for records and targets) depend on
> `get_current_user` only — they are tenant-scoped but not role-restricted, so operators/viewers can
> still load OEE dashboards. The **View** row therefore reflects intended UI visibility; the **Write**
> row is a server-enforced control. Superuser / Platform Admin bypass role checks, as elsewhere.

### Admin

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Settings | ✓ | | | | | | |
| Audit Logs | ✓ | ✓ | | | | | |
| System | ✓ | | | | | | |

> **Audit-log access (tenant-scoped).** The **Audit Logs** row above covers audit *retrieval*:
> `GET /api/v1/audit/`, `/audit/summary`, `/audit/actions`, `/audit/resource-types`
> (`require_role([ADMIN, MANAGER])`). These are **tenant-scoped** — each filters by the caller's
> active company (`get_current_company_id`), so Admin/Manager see only their own company's audit
> data.
>
> **Audit-integrity endpoints (`/api/v1/audit/integrity/*`).** These verify the tamper-evident
> hash chain and are authorized separately from retrieval:
>
> | Endpoint | Role | Scope |
> |----------|------|-------|
> | `GET /audit/integrity/status` | **Platform Admin only** (`require_platform_admin`) | Global chain |
> | `GET /audit/integrity/verify` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/verify-recent` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/record/{sequence_number}` | **Admin** (`require_role([ADMIN])`) | **Own active company only** |
>
> The three aggregate endpoints are Platform-Admin-only because the hash chain is a single global
> sequence interleaved across all tenants — its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to one company without leaking other tenants' data. The per-record
> endpoint serves a company Admin's "are *my* records intact?" need: a company-scoped Admin may
> verify only a record belonging to their active company, and a cross-tenant record returns
> **404** (not 403) so it can't be used to probe for another company's records. Platform Admins /
> superusers may verify any record (superuser bypasses role checks, as elsewhere).

## Backend Implementation

### Using `require_role` Dependency

```python
from app.api.deps import require_role
from app.models.user import UserRole

@router.post("/work-orders")
def create_work_order(
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    # Only admin, manager, and supervisor can create work orders
    ...
```

### Available Roles

```python
class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"
```

## Frontend Implementation

### Using Permission Components

```tsx
import { PermissionGate, CanCreate, CanEdit, CanDelete, AdminOnly } from './components/PermissionGate';

// Single permission check
<PermissionGate permission="work_orders:create">
  <CreateButton />
</PermissionGate>

// Any of multiple permissions
<PermissionGate anyOf={['work_orders:edit', 'work_orders:delete']}>
  <ActionMenu />
</PermissionGate>

// Convenience components
<CanCreate resource="work_orders">
  <CreateButton />
</CanCreate>

<AdminOnly>
  <AdminPanel />
</AdminOnly>
```

### Using Permission Hook

```tsx
import { usePermissions } from './hooks/usePermissions';

function MyComponent() {
  const { can, canAny, isAdmin, role } = usePermissions();
  
  if (can('work_orders:create')) {
    // Show create button
  }
  
  if (isAdmin) {
    // Show admin features
  }
}
```

### Protected Routes

```tsx
import { ProtectedRoute, AdminRoute } from './components/ProtectedRoute';

<Route path="/admin" element={
  <ProtectedRoute requireAdmin>
    <AdminPage />
  </ProtectedRoute>
} />

<Route path="/users" element={
  <ProtectedRoute permission="users:view">
    <UsersPage />
  </ProtectedRoute>
} />
```

## Superuser Override

Users with `is_superuser=true` bypass all permission checks. This is reserved for system administrators who need full access regardless of role assignment.

## Adding New Permissions

1. **Backend**: Add new endpoint with `require_role()` dependency
2. **Frontend**: 
   - Add permission to `Permission` type in `utils/permissions.ts`
   - Add to appropriate role arrays in `ROLE_PERMISSIONS`
   - Use `PermissionGate` or `usePermissions` in components

## Security Notes

- Permissions are checked on BOTH frontend (UI) and backend (API)
- Frontend checks are for UX only - they can be bypassed
- Backend checks are the authoritative security layer
- Always verify permissions server-side before performing actions
