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

### Receiving

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Inspect | ✓ | ✓ | | | ✓ | | |

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
