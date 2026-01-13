/**
 * Role-Based Access Control (RBAC) Utilities
 * 
 * Defines permissions for each role and provides helper functions
 * for checking user access to features and actions.
 */

import { UserRole } from '../types';

/**
 * Permission actions that can be performed in the system
 */
export type Permission =
  // Work Orders
  | 'work_orders:view'
  | 'work_orders:create'
  | 'work_orders:edit'
  | 'work_orders:delete'
  | 'work_orders:release'
  | 'work_orders:complete'
  // Parts
  | 'parts:view'
  | 'parts:create'
  | 'parts:edit'
  | 'parts:delete'
  // BOMs
  | 'boms:view'
  | 'boms:create'
  | 'boms:edit'
  | 'boms:delete'
  | 'boms:release'
  // Routings
  | 'routings:view'
  | 'routings:create'
  | 'routings:edit'
  | 'routings:delete'
  | 'routings:release'
  // Inventory
  | 'inventory:view'
  | 'inventory:adjust'
  | 'inventory:transfer'
  // Purchasing
  | 'purchasing:view'
  | 'purchasing:create'
  | 'purchasing:approve'
  // Receiving
  | 'receiving:view'
  | 'receiving:create'
  | 'receiving:inspect'
  // Shipping
  | 'shipping:view'
  | 'shipping:create'
  | 'shipping:complete'
  // Quality
  | 'quality:view'
  | 'quality:inspect'
  | 'quality:approve'
  | 'quality:calibration'
  // Users
  | 'users:view'
  | 'users:create'
  | 'users:edit'
  | 'users:delete'
  | 'users:roles'
  // Analytics
  | 'analytics:view'
  | 'analytics:export'
  // Admin
  | 'admin:settings'
  | 'admin:audit_logs'
  | 'admin:system';

/**
 * Permission matrix defining which roles have which permissions.
 * 
 * Role hierarchy (from most to least privileged):
 * - admin: Full system access
 * - manager: Department-wide access, can approve/release
 * - supervisor: Team-level access, can create/edit
 * - operator: Can view and update assigned work
 * - quality: Quality-specific actions
 * - shipping: Shipping-specific actions
 * - viewer: Read-only access
 */
const ROLE_PERMISSIONS: Record<UserRole, Permission[]> = {
  admin: [
    // Admin has ALL permissions
    'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete',
    'parts:view', 'parts:create', 'parts:edit', 'parts:delete',
    'boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release',
    'routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release',
    'inventory:view', 'inventory:adjust', 'inventory:transfer',
    'purchasing:view', 'purchasing:create', 'purchasing:approve',
    'receiving:view', 'receiving:create', 'receiving:inspect',
    'shipping:view', 'shipping:create', 'shipping:complete',
    'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
    'users:view', 'users:create', 'users:edit', 'users:delete', 'users:roles',
    'analytics:view', 'analytics:export',
    'admin:settings', 'admin:audit_logs', 'admin:system',
  ],
  
  manager: [
    'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:delete', 'work_orders:release', 'work_orders:complete',
    'parts:view', 'parts:create', 'parts:edit',
    'boms:view', 'boms:create', 'boms:edit', 'boms:delete', 'boms:release',
    'routings:view', 'routings:create', 'routings:edit', 'routings:delete', 'routings:release',
    'inventory:view', 'inventory:adjust', 'inventory:transfer',
    'purchasing:view', 'purchasing:create', 'purchasing:approve',
    'receiving:view', 'receiving:create', 'receiving:inspect',
    'shipping:view', 'shipping:create', 'shipping:complete',
    'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
    'users:view', 'users:create', 'users:edit',
    'analytics:view', 'analytics:export',
    'admin:audit_logs',
  ],
  
  supervisor: [
    'work_orders:view', 'work_orders:create', 'work_orders:edit', 'work_orders:release', 'work_orders:complete',
    'parts:view', 'parts:create', 'parts:edit',
    'boms:view', 'boms:create', 'boms:edit',
    'routings:view', 'routings:create', 'routings:edit',
    'inventory:view', 'inventory:adjust', 'inventory:transfer',
    'purchasing:view', 'purchasing:create',
    'receiving:view', 'receiving:create',
    'shipping:view', 'shipping:create', 'shipping:complete',
    'quality:view', 'quality:inspect',
    'users:view',
    'analytics:view',
  ],
  
  operator: [
    'work_orders:view', 'work_orders:complete',
    'parts:view',
    'boms:view',
    'routings:view',
    'inventory:view',
    'quality:view',
    'analytics:view',
  ],
  
  quality: [
    'work_orders:view', 'work_orders:complete',
    'parts:view',
    'boms:view',
    'routings:view',
    'inventory:view',
    'receiving:view', 'receiving:inspect',
    'quality:view', 'quality:inspect', 'quality:approve', 'quality:calibration',
    'analytics:view',
  ],
  
  shipping: [
    'work_orders:view',
    'parts:view',
    'inventory:view',
    'shipping:view', 'shipping:create', 'shipping:complete',
    'analytics:view',
  ],
  
  viewer: [
    'work_orders:view',
    'parts:view',
    'boms:view',
    'routings:view',
    'inventory:view',
    'purchasing:view',
    'receiving:view',
    'shipping:view',
    'quality:view',
    'analytics:view',
  ],
};

/**
 * Runtime permission overrides loaded from backend.
 * When set, these take precedence over default ROLE_PERMISSIONS.
 */
let customPermissions: Record<string, Permission[]> | null = null;

/**
 * Load custom permissions from backend. Call this on app initialization.
 */
export function setCustomPermissions(permissions: Record<string, string[]> | null): void {
  customPermissions = permissions as Record<string, Permission[]> | null;
}

/**
 * Get current permissions for a role (custom if loaded, else default)
 */
function getCurrentRolePermissions(role: UserRole): Permission[] {
  if (customPermissions && customPermissions[role]) {
    return customPermissions[role];
  }
  return ROLE_PERMISSIONS[role] || [];
}

/**
 * Check if a user has a specific permission
 */
export function hasPermission(userRole: UserRole | undefined, permission: Permission): boolean {
  if (!userRole) return false;
  return getCurrentRolePermissions(userRole).includes(permission);
}

/**
 * Check if a user has ANY of the specified permissions
 */
export function hasAnyPermission(userRole: UserRole | undefined, permissions: Permission[]): boolean {
  if (!userRole) return false;
  return permissions.some(p => hasPermission(userRole, p));
}

/**
 * Check if a user has ALL of the specified permissions
 */
export function hasAllPermissions(userRole: UserRole | undefined, permissions: Permission[]): boolean {
  if (!userRole) return false;
  return permissions.every(p => hasPermission(userRole, p));
}

/**
 * Check if user is an admin or superuser
 */
export function isAdmin(userRole: UserRole | undefined, isSuperuser?: boolean): boolean {
  return isSuperuser === true || userRole === 'admin';
}

/**
 * Check if user can manage other users
 */
export function canManageUsers(userRole: UserRole | undefined): boolean {
  return hasPermission(userRole, 'users:create') || hasPermission(userRole, 'users:edit');
}

/**
 * Check if user can approve/release items
 */
export function canApprove(userRole: UserRole | undefined): boolean {
  return hasAnyPermission(userRole, ['work_orders:release', 'boms:release', 'routings:release', 'purchasing:approve']);
}

/**
 * Get all permissions for a role
 */
export function getPermissionsForRole(role: UserRole): Permission[] {
  return getCurrentRolePermissions(role);
}

/**
 * Role display names
 */
export const ROLE_LABELS: Record<UserRole, string> = {
  admin: 'Administrator',
  manager: 'Manager',
  supervisor: 'Supervisor',
  operator: 'Operator',
  quality: 'Quality',
  shipping: 'Shipping',
  viewer: 'Viewer',
};

/**
 * Role descriptions
 */
export const ROLE_DESCRIPTIONS: Record<UserRole, string> = {
  admin: 'Full system access including user management and system settings',
  manager: 'Department-wide access with approval and release capabilities',
  supervisor: 'Team-level access with create and edit permissions',
  operator: 'View and update assigned work orders',
  quality: 'Quality inspection and approval capabilities',
  shipping: 'Shipping and fulfillment operations',
  viewer: 'Read-only access to all data',
};
