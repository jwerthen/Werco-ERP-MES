/**
 * Permission Gate Components
 * 
 * Components to conditionally render based on user permissions.
 */

import React, { ReactNode } from 'react';
import { Permission } from '../utils/permissions';
import { usePermissions } from '../hooks/usePermissions';

interface PermissionGateProps {
  /** Permission required to render children */
  permission?: Permission;
  /** Any of these permissions required */
  anyOf?: Permission[];
  /** All of these permissions required */
  allOf?: Permission[];
  /** Require admin role */
  requireAdmin?: boolean;
  /** Content to render if permission granted */
  children: ReactNode;
  /** Content to render if permission denied (optional) */
  fallback?: ReactNode;
}

/**
 * Conditionally render children based on user permissions.
 * 
 * Usage:
 *   <PermissionGate permission="work_orders:create">
 *     <CreateButton />
 *   </PermissionGate>
 *   
 *   <PermissionGate anyOf={['work_orders:edit', 'work_orders:delete']}>
 *     <ActionButtons />
 *   </PermissionGate>
 *   
 *   <PermissionGate permission="admin:settings" fallback={<AccessDenied />}>
 *     <AdminPanel />
 *   </PermissionGate>
 */
export function PermissionGate({
  permission,
  anyOf,
  allOf,
  requireAdmin,
  children,
  fallback = null,
}: PermissionGateProps): JSX.Element {
  const { can, canAny, canAll, isAdmin } = usePermissions();
  
  let hasAccess = true;
  
  if (requireAdmin) {
    hasAccess = isAdmin;
  } else if (permission) {
    hasAccess = can(permission);
  } else if (anyOf) {
    hasAccess = canAny(anyOf);
  } else if (allOf) {
    hasAccess = canAll(allOf);
  }
  
  return <>{hasAccess ? children : fallback}</>;
}

/**
 * Render children only for admin users
 */
export function AdminOnly({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }): JSX.Element {
  return (
    <PermissionGate requireAdmin fallback={fallback}>
      {children}
    </PermissionGate>
  );
}

/**
 * Render children only for users who can create items
 */
export function CanCreate({
  resource,
  children,
  fallback = null,
}: {
  resource: 'work_orders' | 'parts' | 'boms' | 'routings' | 'purchasing' | 'receiving' | 'shipping' | 'users';
  children: ReactNode;
  fallback?: ReactNode;
}): JSX.Element {
  return (
    <PermissionGate permission={`${resource}:create` as Permission} fallback={fallback}>
      {children}
    </PermissionGate>
  );
}

/**
 * Render children only for users who can edit items
 */
export function CanEdit({
  resource,
  children,
  fallback = null,
}: {
  resource: 'work_orders' | 'parts' | 'boms' | 'routings' | 'users';
  children: ReactNode;
  fallback?: ReactNode;
}): JSX.Element {
  return (
    <PermissionGate permission={`${resource}:edit` as Permission} fallback={fallback}>
      {children}
    </PermissionGate>
  );
}

/**
 * Render children only for users who can delete items
 */
export function CanDelete({
  resource,
  children,
  fallback = null,
}: {
  resource: 'work_orders' | 'parts' | 'boms' | 'routings' | 'users';
  children: ReactNode;
  fallback?: ReactNode;
}): JSX.Element {
  return (
    <PermissionGate permission={`${resource}:delete` as Permission} fallback={fallback}>
      {children}
    </PermissionGate>
  );
}

export default PermissionGate;
