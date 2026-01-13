/**
 * ProtectedRoute Component Tests
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { ProtectedRoute, AuthenticatedRoute, AdminRoute } from './ProtectedRoute';
import { useAuth } from '../context/AuthContext';
import { usePermissions } from '../hooks/usePermissions';

// Mock auth context and permissions hook
jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: jest.fn(),
}));

const mockUseAuth = useAuth as jest.MockedFunction<typeof useAuth>;
const mockUsePermissions = usePermissions as jest.MockedFunction<typeof usePermissions>;

// Helper to render with router
const renderWithRouter = (ui: React.ReactElement, { initialEntries = ['/protected'] } = {}) => {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/login" element={<div>Login Page</div>} />
        <Route path="/unauthorized" element={<div>Unauthorized Page</div>} />
        <Route path="/protected" element={ui} />
        <Route path="/custom-login" element={<div>Custom Login</div>} />
        <Route path="/custom-unauthorized" element={<div>Custom Unauthorized</div>} />
      </Routes>
    </MemoryRouter>
  );
};

// Default mock setup
const setupMocks = ({
  isAuthenticated = true,
  isLoading = false,
  isAdmin = false,
  can = () => true,
  canAny = () => true,
  canAll = () => true,
}: {
  isAuthenticated?: boolean;
  isLoading?: boolean;
  isAdmin?: boolean;
  can?: (permission: string) => boolean;
  canAny?: (permissions: string[]) => boolean;
  canAll?: (permissions: string[]) => boolean;
} = {}) => {
  mockUseAuth.mockReturnValue({
    user: isAuthenticated ? { id: 1, email: 'test@test.com', role: 'operator' } : null,
    isAuthenticated,
    isLoading,
    login: jest.fn(),
    logout: jest.fn(),
    refreshToken: jest.fn(),
  } as any);

  mockUsePermissions.mockReturnValue({
    can,
    canAny,
    canAll,
    isAdmin,
    isSuperuser: isAdmin,
    role: isAuthenticated ? 'operator' : undefined,
  } as any);
};

describe('ProtectedRoute', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('loading state', () => {
    it('shows loading spinner while auth is loading', () => {
      setupMocks({ isLoading: true });

      renderWithRouter(
        <ProtectedRoute>
          <div>Protected Content</div>
        </ProtectedRoute>
      );

      // Should show loading spinner (by checking for animate-spin class)
      const spinner = document.querySelector('.animate-spin');
      expect(spinner).toBeInTheDocument();
      expect(screen.queryByText('Protected Content')).not.toBeInTheDocument();
    });
  });

  describe('authentication', () => {
    it('redirects to login when not authenticated', () => {
      setupMocks({ isAuthenticated: false });

      renderWithRouter(
        <ProtectedRoute>
          <div>Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Login Page')).toBeInTheDocument();
      expect(screen.queryByText('Protected Content')).not.toBeInTheDocument();
    });

    it('renders children when authenticated', () => {
      setupMocks({ isAuthenticated: true });

      renderWithRouter(
        <ProtectedRoute>
          <div>Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Protected Content')).toBeInTheDocument();
    });

    it('uses custom login path', () => {
      setupMocks({ isAuthenticated: false });

      renderWithRouter(
        <ProtectedRoute loginPath="/custom-login">
          <div>Protected Content</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Custom Login')).toBeInTheDocument();
    });
  });

  describe('permission checks', () => {
    it('allows access when user has required permission', () => {
      setupMocks({
        isAuthenticated: true,
        can: (p) => p === 'parts:view',
      });

      renderWithRouter(
        <ProtectedRoute permission="parts:view">
          <div>Parts Page</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Parts Page')).toBeInTheDocument();
    });

    it('redirects to unauthorized when user lacks permission', () => {
      setupMocks({
        isAuthenticated: true,
        can: () => false,
      });

      renderWithRouter(
        <ProtectedRoute permission="admin:manage">
          <div>Admin Page</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Unauthorized Page')).toBeInTheDocument();
      expect(screen.queryByText('Admin Page')).not.toBeInTheDocument();
    });

    it('uses custom unauthorized path', () => {
      setupMocks({
        isAuthenticated: true,
        can: () => false,
      });

      renderWithRouter(
        <ProtectedRoute permission="admin:manage" unauthorizedPath="/custom-unauthorized">
          <div>Admin Page</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Custom Unauthorized')).toBeInTheDocument();
    });
  });

  describe('anyOf permissions', () => {
    it('allows access when user has at least one permission', () => {
      setupMocks({
        isAuthenticated: true,
        canAny: (perms) => perms.includes('parts:view'),
      });

      renderWithRouter(
        <ProtectedRoute anyOf={['parts:view', 'parts:edit']}>
          <div>Parts Page</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Parts Page')).toBeInTheDocument();
    });

    it('redirects when user has none of the permissions', () => {
      setupMocks({
        isAuthenticated: true,
        canAny: () => false,
      });

      renderWithRouter(
        <ProtectedRoute anyOf={['admin:view', 'admin:edit']}>
          <div>Admin Page</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Unauthorized Page')).toBeInTheDocument();
    });
  });

  describe('allOf permissions', () => {
    it('allows access when user has all permissions', () => {
      setupMocks({
        isAuthenticated: true,
        canAll: () => true,
      });

      renderWithRouter(
        <ProtectedRoute allOf={['parts:view', 'parts:edit']}>
          <div>Parts Editor</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Parts Editor')).toBeInTheDocument();
    });

    it('redirects when user lacks any required permission', () => {
      setupMocks({
        isAuthenticated: true,
        canAll: () => false,
      });

      renderWithRouter(
        <ProtectedRoute allOf={['parts:view', 'parts:delete']}>
          <div>Parts Manager</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Unauthorized Page')).toBeInTheDocument();
    });
  });

  describe('requireAdmin', () => {
    it('allows access for admin users', () => {
      setupMocks({
        isAuthenticated: true,
        isAdmin: true,
      });

      renderWithRouter(
        <ProtectedRoute requireAdmin>
          <div>Admin Dashboard</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Admin Dashboard')).toBeInTheDocument();
    });

    it('redirects non-admin users', () => {
      setupMocks({
        isAuthenticated: true,
        isAdmin: false,
      });

      renderWithRouter(
        <ProtectedRoute requireAdmin>
          <div>Admin Dashboard</div>
        </ProtectedRoute>
      );

      expect(screen.getByText('Unauthorized Page')).toBeInTheDocument();
    });
  });
});

describe('AuthenticatedRoute', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders children when authenticated', () => {
    setupMocks({ isAuthenticated: true });

    renderWithRouter(
      <AuthenticatedRoute>
        <div>Authenticated Content</div>
      </AuthenticatedRoute>
    );

    expect(screen.getByText('Authenticated Content')).toBeInTheDocument();
  });

  it('redirects to login when not authenticated', () => {
    setupMocks({ isAuthenticated: false });

    renderWithRouter(
      <AuthenticatedRoute>
        <div>Authenticated Content</div>
      </AuthenticatedRoute>
    );

    expect(screen.getByText('Login Page')).toBeInTheDocument();
  });
});

describe('AdminRoute', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('renders children for admin users', () => {
    setupMocks({ isAuthenticated: true, isAdmin: true });

    renderWithRouter(
      <AdminRoute>
        <div>Admin Only Content</div>
      </AdminRoute>
    );

    expect(screen.getByText('Admin Only Content')).toBeInTheDocument();
  });

  it('redirects non-admin users to unauthorized', () => {
    setupMocks({ isAuthenticated: true, isAdmin: false });

    renderWithRouter(
      <AdminRoute>
        <div>Admin Only Content</div>
      </AdminRoute>
    );

    expect(screen.getByText('Unauthorized Page')).toBeInTheDocument();
  });

  it('redirects unauthenticated users to login', () => {
    setupMocks({ isAuthenticated: false, isAdmin: false });

    renderWithRouter(
      <AdminRoute>
        <div>Admin Only Content</div>
      </AdminRoute>
    );

    expect(screen.getByText('Login Page')).toBeInTheDocument();
  });
});
