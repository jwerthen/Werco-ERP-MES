/**
 * Permissions Utility Tests
 */

import {
  hasPermission,
  hasAnyPermission,
  hasAllPermissions,
  isAdmin,
  canManageUsers,
  canApprove,
  getPermissionsForRole,
  ROLE_LABELS,
  ROLE_DESCRIPTIONS,
} from './permissions';

describe('permissions utility', () => {
  describe('hasPermission', () => {
    it('returns true for admin with defined permissions', () => {
      expect(hasPermission('admin', 'users:create')).toBe(true);
      expect(hasPermission('admin', 'parts:delete')).toBe(true);
      expect(hasPermission('admin', 'work_orders:release')).toBe(true);
    });

    it('returns true when role has specific permission', () => {
      expect(hasPermission('operator', 'work_orders:view')).toBe(true);
      expect(hasPermission('operator', 'parts:view')).toBe(true);
    });

    it('returns false when role lacks permission', () => {
      expect(hasPermission('operator', 'users:create')).toBe(false);
      expect(hasPermission('viewer', 'parts:create')).toBe(false);
    });

    it('returns false for unknown role', () => {
      expect(hasPermission('unknown_role' as any, 'parts:view')).toBe(false);
    });

    it('returns false for undefined role', () => {
      expect(hasPermission(undefined, 'parts:view')).toBe(false);
    });

    it('handles manager permissions', () => {
      expect(hasPermission('manager', 'parts:create')).toBe(true);
      expect(hasPermission('manager', 'work_orders:create')).toBe(true);
      expect(hasPermission('manager', 'purchasing:approve')).toBe(true);
    });

    it('handles supervisor permissions', () => {
      expect(hasPermission('supervisor', 'work_orders:create')).toBe(true);
      expect(hasPermission('supervisor', 'work_orders:edit')).toBe(true);
    });

    it('handles quality role permissions', () => {
      expect(hasPermission('quality', 'quality:view')).toBe(true);
      expect(hasPermission('quality', 'quality:inspect')).toBe(true);
      expect(hasPermission('quality', 'quality:approve')).toBe(true);
    });

    it('handles shipping role permissions', () => {
      expect(hasPermission('shipping', 'shipping:view')).toBe(true);
      expect(hasPermission('shipping', 'shipping:create')).toBe(true);
      expect(hasPermission('shipping', 'shipping:complete')).toBe(true);
    });

    it('handles viewer role (read-only)', () => {
      expect(hasPermission('viewer', 'parts:view')).toBe(true);
      expect(hasPermission('viewer', 'parts:create')).toBe(false);
      expect(hasPermission('viewer', 'work_orders:view')).toBe(true);
      expect(hasPermission('viewer', 'work_orders:edit')).toBe(false);
    });
  });

  describe('hasAnyPermission', () => {
    it('returns true if user has at least one permission', () => {
      expect(hasAnyPermission('operator', ['work_orders:view', 'users:create'])).toBe(true);
    });

    it('returns false if user has none of the permissions', () => {
      expect(hasAnyPermission('viewer', ['parts:create', 'parts:edit', 'parts:delete'])).toBe(false);
    });

    it('returns true for admin with valid permissions', () => {
      expect(hasAnyPermission('admin', ['users:create', 'parts:delete'])).toBe(true);
    });

    it('returns false for empty permissions array', () => {
      expect(hasAnyPermission('admin', [])).toBe(false);
    });

    it('returns false for undefined role', () => {
      expect(hasAnyPermission(undefined, ['parts:view'])).toBe(false);
    });
  });

  describe('hasAllPermissions', () => {
    it('returns true if user has all permissions', () => {
      expect(hasAllPermissions('manager', ['parts:view', 'parts:create', 'parts:edit'])).toBe(true);
    });

    it('returns false if user lacks any permission', () => {
      expect(hasAllPermissions('operator', ['work_orders:view', 'users:create'])).toBe(false);
    });

    it('returns true for admin with all valid permissions', () => {
      expect(hasAllPermissions('admin', ['parts:view', 'users:create', 'admin:settings'])).toBe(true);
    });

    it('returns true for empty permissions array', () => {
      expect(hasAllPermissions('viewer', [])).toBe(true);
    });

    it('returns false for undefined role', () => {
      expect(hasAllPermissions(undefined, ['parts:view'])).toBe(false);
    });
  });

  describe('isAdmin', () => {
    it('returns true for admin role', () => {
      expect(isAdmin('admin')).toBe(true);
    });

    it('returns true for superuser', () => {
      expect(isAdmin('operator', true)).toBe(true);
    });

    it('returns false for non-admin roles', () => {
      expect(isAdmin('manager')).toBe(false);
      expect(isAdmin('supervisor')).toBe(false);
      expect(isAdmin('operator')).toBe(false);
      expect(isAdmin('quality')).toBe(false);
      expect(isAdmin('shipping')).toBe(false);
      expect(isAdmin('viewer')).toBe(false);
    });

    it('returns false for undefined role', () => {
      expect(isAdmin(undefined)).toBe(false);
    });
  });

  describe('canManageUsers', () => {
    it('returns true for admin', () => {
      expect(canManageUsers('admin')).toBe(true);
    });

    it('returns true for manager', () => {
      expect(canManageUsers('manager')).toBe(true);
    });

    it('returns false for other roles', () => {
      expect(canManageUsers('operator')).toBe(false);
      expect(canManageUsers('viewer')).toBe(false);
    });

    it('returns false for undefined role', () => {
      expect(canManageUsers(undefined)).toBe(false);
    });
  });

  describe('canApprove', () => {
    it('returns true for admin', () => {
      expect(canApprove('admin')).toBe(true);
    });

    it('returns true for manager', () => {
      expect(canApprove('manager')).toBe(true);
    });

    it('returns false for operator', () => {
      expect(canApprove('operator')).toBe(false);
    });

    it('returns false for viewer', () => {
      expect(canApprove('viewer')).toBe(false);
    });

    it('returns false for undefined role', () => {
      expect(canApprove(undefined)).toBe(false);
    });
  });

  describe('getPermissionsForRole', () => {
    it('returns array for admin', () => {
      const perms = getPermissionsForRole('admin');
      expect(Array.isArray(perms)).toBe(true);
      expect(perms.length).toBeGreaterThan(0);
    });

    it('returns fewer permissions for viewer than admin', () => {
      const adminPerms = getPermissionsForRole('admin');
      const viewerPerms = getPermissionsForRole('viewer');
      expect(viewerPerms.length).toBeLessThan(adminPerms.length);
    });

    it('viewer permissions all end with :view', () => {
      const viewerPerms = getPermissionsForRole('viewer');
      const allAreView = viewerPerms.every(p => p.endsWith(':view'));
      expect(allAreView).toBe(true);
    });
  });

  describe('ROLE_LABELS', () => {
    it('has labels for all roles', () => {
      expect(ROLE_LABELS).toHaveProperty('admin');
      expect(ROLE_LABELS).toHaveProperty('manager');
      expect(ROLE_LABELS).toHaveProperty('supervisor');
      expect(ROLE_LABELS).toHaveProperty('operator');
      expect(ROLE_LABELS).toHaveProperty('quality');
      expect(ROLE_LABELS).toHaveProperty('shipping');
      expect(ROLE_LABELS).toHaveProperty('viewer');
    });

    it('labels are human readable', () => {
      expect(ROLE_LABELS.admin).toBe('Administrator');
      expect(ROLE_LABELS.manager).toBe('Manager');
      expect(ROLE_LABELS.operator).toBe('Operator');
    });
  });

  describe('ROLE_DESCRIPTIONS', () => {
    it('has descriptions for all roles', () => {
      expect(ROLE_DESCRIPTIONS).toHaveProperty('admin');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('manager');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('supervisor');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('operator');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('quality');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('shipping');
      expect(ROLE_DESCRIPTIONS).toHaveProperty('viewer');
    });

    it('descriptions are non-empty strings', () => {
      Object.values(ROLE_DESCRIPTIONS).forEach(desc => {
        expect(typeof desc).toBe('string');
        expect(desc.length).toBeGreaterThan(0);
      });
    });
  });
});
