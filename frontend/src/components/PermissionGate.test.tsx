/**
 * PermissionGate Component Tests
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { PermissionGate, AdminOnly, CanCreate, CanEdit, CanDelete } from './PermissionGate';
import { useAuth } from '../context/AuthContext';

// Mock the auth context
jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;

// Helper to create mock user with all required fields
const createMockUser = (overrides: {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  role: 'admin' | 'manager' | 'supervisor' | 'operator' | 'quality' | 'shipping' | 'viewer';
  is_superuser: boolean;
}) => ({
  ...overrides,
  version: 1,
  employee_id: `EMP${overrides.id.toString().padStart(3, '0')}`,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
});

describe('PermissionGate', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('with admin user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: createMockUser({
          id: 1,
          email: 'admin@werco.com',
          first_name: 'Admin',
          last_name: 'User',
          role: 'admin',
          is_superuser: true,
        }),
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('renders children for admin', () => {
      render(
        <PermissionGate permission="parts:create">
          <div>Protected Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('Protected Content')).toBeInTheDocument();
    });

    it('renders children with requireAdmin', () => {
      render(
        <PermissionGate requireAdmin>
          <div>Admin Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('Admin Content')).toBeInTheDocument();
    });
  });

  describe('with operator user', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: createMockUser({
          id: 2,
          email: 'operator@werco.com',
          first_name: 'Operator',
          last_name: 'User',
          role: 'operator',
          is_superuser: false,
        }),
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('renders children when user has permission', () => {
      render(
        <PermissionGate permission="work_orders:view">
          <div>Work Orders Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('Work Orders Content')).toBeInTheDocument();
    });

    it('does not render children when user lacks permission', () => {
      render(
        <PermissionGate permission="users:create">
          <div>Admin Only Content</div>
        </PermissionGate>
      );
      expect(screen.queryByText('Admin Only Content')).not.toBeInTheDocument();
    });

    it('renders fallback when user lacks permission', () => {
      render(
        <PermissionGate 
          permission="users:create"
          fallback={<div>Access Denied</div>}
        >
          <div>Admin Only Content</div>
        </PermissionGate>
      );
      expect(screen.queryByText('Admin Only Content')).not.toBeInTheDocument();
      expect(screen.getByText('Access Denied')).toBeInTheDocument();
    });
  });

  describe('with unauthenticated user', () => {
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

    it('does not render children when permission required', () => {
      render(
        <PermissionGate permission="parts:view">
          <div>Protected Content</div>
        </PermissionGate>
      );
      expect(screen.queryByText('Protected Content')).not.toBeInTheDocument();
    });

    it('renders fallback when not authenticated', () => {
      render(
        <PermissionGate 
          permission="parts:view"
          fallback={<div>Please login</div>}
        >
          <div>Protected Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('Please login')).toBeInTheDocument();
    });
  });

  describe('anyOf and allOf props', () => {
    beforeEach(() => {
      mockUseAuth.mockReturnValue({
        user: createMockUser({
          id: 3,
          email: 'manager@werco.com',
          first_name: 'Manager',
          last_name: 'User',
          role: 'manager',
          is_superuser: false,
        }),
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        logout: jest.fn(),
        refreshToken: jest.fn(),
      });
    });

    it('renders when any permission matches with anyOf', () => {
      render(
        <PermissionGate anyOf={['parts:view', 'admin:system']}>
          <div>Manager Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('Manager Content')).toBeInTheDocument();
    });

    it('renders when all permissions match with allOf', () => {
      render(
        <PermissionGate allOf={['parts:view', 'parts:create']}>
          <div>All Permissions Content</div>
        </PermissionGate>
      );
      expect(screen.getByText('All Permissions Content')).toBeInTheDocument();
    });
  });
});

describe('AdminOnly', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders children for admin user', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 1,
        email: 'admin@werco.com',
        first_name: 'Admin',
        last_name: 'User',
        role: 'admin',
        is_superuser: true,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <AdminOnly>
        <div>Admin Content</div>
      </AdminOnly>
    );
    expect(screen.getByText('Admin Content')).toBeInTheDocument();
  });

  it('does not render for non-admin user', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 2,
        email: 'user@werco.com',
        first_name: 'Regular',
        last_name: 'User',
        role: 'operator',
        is_superuser: false,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <AdminOnly>
        <div>Admin Content</div>
      </AdminOnly>
    );
    expect(screen.queryByText('Admin Content')).not.toBeInTheDocument();
  });
});

describe('CanCreate', () => {
  it('renders children when user can create resource', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 1,
        email: 'manager@werco.com',
        first_name: 'Manager',
        last_name: 'User',
        role: 'manager',
        is_superuser: false,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <CanCreate resource="parts">
        <button>Create Part</button>
      </CanCreate>
    );
    expect(screen.getByText('Create Part')).toBeInTheDocument();
  });
});

describe('CanEdit', () => {
  it('renders children when user can edit resource', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 1,
        email: 'manager@werco.com',
        first_name: 'Manager',
        last_name: 'User',
        role: 'manager',
        is_superuser: false,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <CanEdit resource="work_orders">
        <button>Edit Work Order</button>
      </CanEdit>
    );
    expect(screen.getByText('Edit Work Order')).toBeInTheDocument();
  });
});

describe('CanDelete', () => {
  it('renders children when user can delete resource', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 1,
        email: 'admin@werco.com',
        first_name: 'Admin',
        last_name: 'User',
        role: 'admin',
        is_superuser: true,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <CanDelete resource="parts">
        <button>Delete Part</button>
      </CanDelete>
    );
    expect(screen.getByText('Delete Part')).toBeInTheDocument();
  });

  it('does not render for operator (no delete permission)', () => {
    mockUseAuth.mockReturnValue({
      user: createMockUser({
        id: 2,
        email: 'operator@werco.com',
        first_name: 'Operator',
        last_name: 'User',
        role: 'operator',
        is_superuser: false,
      }),
      isAuthenticated: true,
      isLoading: false,
      login: jest.fn(),
      logout: jest.fn(),
      refreshToken: jest.fn(),
    });

    render(
      <CanDelete resource="parts">
        <button>Delete Part</button>
      </CanDelete>
    );
    expect(screen.queryByText('Delete Part')).not.toBeInTheDocument();
  });
});
