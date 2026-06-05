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

### Quality

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Approve | ✓ | ✓ | | | ✓ | | |
| Calibration | ✓ | ✓ | | | ✓ | | |

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
