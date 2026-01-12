/**
 * usePermissions Hook Tests
 */

import { renderHook } from '@testing-library/react';
import { usePermissions } from './usePermissions';
import { useAuth } from '../context/AuthContext';

// Mock the auth context
jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

describe('usePermissions', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('with admin user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 1,
          email: 'admin@werco.com',
          full_name: 'Admin User',
          role: 'admin',
          is_active: true,
          is_superuser: true,
        },
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('can() returns true for any permission', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('users:create')).toBe(true);
      expect(result.current.can('parts:delete')).toBe(true);
      expect(result.current.can('random:permission')).toBe(true);
    });

    it('canAny() returns true', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAny(['users:create', 'parts:delete'])).toBe(true);
    });

    it('canAll() returns true', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAll(['users:create', 'parts:delete', 'settings:manage'])).toBe(true);
    });

    it('isAdmin returns true', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isAdmin).toBe(true);
    });

    it('isSuperuser returns true', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isSuperuser).toBe(true);
    });

    it('role is admin', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.role).toBe('admin');
    });
  });

  describe('with operator user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 2,
          email: 'operator@werco.com',
          full_name: 'Operator User',
          role: 'operator',
          is_active: true,
          is_superuser: false,
        },
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('can() returns true for allowed permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('work_orders:view')).toBe(true);
      expect(result.current.can('parts:view')).toBe(true);
    });

    it('can() returns false for disallowed permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('users:create')).toBe(false);
      expect(result.current.can('parts:delete')).toBe(false);
    });

    it('canAny() returns true when at least one permission matches', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAny(['work_orders:view', 'users:create'])).toBe(true);
    });

    it('canAny() returns false when no permissions match', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAny(['users:create', 'settings:manage'])).toBe(false);
    });

    it('canAll() returns false when missing any permission', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAll(['shop_floor:view', 'users:create'])).toBe(false);
    });

    it('isAdmin returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isAdmin).toBe(false);
    });

    it('isSuperuser returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isSuperuser).toBe(false);
    });

    it('role is operator', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.role).toBe('operator');
    });
  });

  describe('with manager user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 3,
          email: 'manager@werco.com',
          full_name: 'Manager User',
          role: 'manager',
          is_active: true,
          is_superuser: false,
        },
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('has management permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('parts:create')).toBe(true);
      expect(result.current.can('work_orders:create')).toBe(true);
      expect(result.current.can('users:view')).toBe(true);
    });

    it('isAdmin returns false (manager is not admin)', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isAdmin).toBe(false);
    });
  });

  describe('with no user (unauthenticated)', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: null,
        isAuthenticated: false,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('can() returns false for all permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('parts:view')).toBe(false);
      expect(result.current.can('shop_floor:view')).toBe(false);
    });

    it('canAny() returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAny(['parts:view', 'shop_floor:view'])).toBe(false);
    });

    it('canAll() returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.canAll(['parts:view'])).toBe(false);
    });

    it('isAdmin returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isAdmin).toBe(false);
    });

    it('isSuperuser returns false', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.isSuperuser).toBe(false);
    });

    it('role is undefined', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.role).toBeUndefined();
    });
  });

  describe('with quality user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 4,
          email: 'quality@werco.com',
          full_name: 'Quality User',
          role: 'quality',
          is_active: true,
          is_superuser: false,
        },
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('has quality-specific permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('quality:view')).toBe(true);
      expect(result.current.can('quality:inspect')).toBe(true);
      expect(result.current.can('quality:approve')).toBe(true);
    });

    it('lacks admin permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('users:create')).toBe(false);
      expect(result.current.can('settings:manage')).toBe(false);
    });
  });

  describe('with viewer user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: {
          id: 5,
          email: 'viewer@werco.com',
          full_name: 'Viewer User',
          role: 'viewer',
          is_active: true,
          is_superuser: false,
        },
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('has view-only permissions', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('parts:view')).toBe(true);
      expect(result.current.can('work_orders:view')).toBe(true);
    });

    it('cannot create, edit, or delete', () => {
      const { result } = renderHook(() => usePermissions());
      expect(result.current.can('parts:create')).toBe(false);
      expect(result.current.can('parts:edit')).toBe(false);
      expect(result.current.can('parts:delete')).toBe(false);
    });
  });
});
