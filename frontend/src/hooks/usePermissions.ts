/**
 * React hook for permission checking
 */

import { useMemo } from 'react';
import { useAuth } from '../context/AuthContext';
import {
  Permission,
  hasPermission,
  hasAnyPermission,
  hasAllPermissions,
  isAdmin,
  canManageUsers,
  canApprove,
  getPermissionsForRole,
} from '../utils/permissions';

/**
 * Hook to check user permissions
 * 
 * Usage:
 *   const { can, canAny, canAll, isAdmin } = usePermissions();
 *   
 *   if (can('work_orders:create')) { ... }
 *   if (canAny(['work_orders:edit', 'work_orders:delete'])) { ... }
 */
export function usePermissions() {
  const { user } = useAuth();
  
  return useMemo(() => ({
    /**
     * Check if user has a specific permission
     */
    can: (permission: Permission): boolean => {
      if (user?.is_superuser) return true;
      return hasPermission(user?.role, permission);
    },
    
    /**
     * Check if user has ANY of the permissions
     */
    canAny: (permissions: Permission[]): boolean => {
      if (user?.is_superuser) return true;
      return hasAnyPermission(user?.role, permissions);
    },
    
    /**
     * Check if user has ALL of the permissions
     */
    canAll: (permissions: Permission[]): boolean => {
      if (user?.is_superuser) return true;
      return hasAllPermissions(user?.role, permissions);
    },
    
    /**
     * Check if user is admin or superuser
     */
    isAdmin: isAdmin(user?.role, user?.is_superuser),
    
    /**
     * Check if user can manage other users
     */
    canManageUsers: user?.is_superuser || canManageUsers(user?.role),
    
    /**
     * Check if user can approve/release items
     */
    canApprove: user?.is_superuser || canApprove(user?.role),
    
    /**
     * Get all permissions for current user's role
     */
    permissions: user?.role ? getPermissionsForRole(user.role) : [],
    
    /**
     * Current user's role
     */
    role: user?.role,
    
    /**
     * Is superuser
     */
    isSuperuser: user?.is_superuser ?? false,
  }), [user]);
}

export default usePermissions;
