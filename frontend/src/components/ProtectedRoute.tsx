/**
 * Protected Route Component
 * 
 * Wraps routes to enforce authentication and permission requirements.
 */

import React, { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { usePermissions } from '../hooks/usePermissions';
import { Permission } from '../utils/permissions';

interface ProtectedRouteProps {
  children: ReactNode;
  /** Permission required to access route */
  permission?: Permission;
  /** Any of these permissions required */
  anyOf?: Permission[];
  /** All of these permissions required */
  allOf?: Permission[];
  /** Require admin role */
  requireAdmin?: boolean;
  /** Redirect path if not authenticated */
  loginPath?: string;
  /** Redirect path if not authorized */
  unauthorizedPath?: string;
}

/**
 * Protect a route with authentication and optional permission requirements.
 * 
 * Usage in App.tsx:
 *   <Route 
 *     path="/admin" 
 *     element={
 *       <ProtectedRoute requireAdmin>
 *         <AdminPage />
 *       </ProtectedRoute>
 *     } 
 *   />
 *   
 *   <Route 
 *     path="/users" 
 *     element={
 *       <ProtectedRoute permission="users:view">
 *         <UsersPage />
 *       </ProtectedRoute>
 *     } 
 *   />
 */
export function ProtectedRoute({
  children,
  permission,
  anyOf,
  allOf,
  requireAdmin,
  loginPath = '/login',
  unauthorizedPath = '/unauthorized',
}: ProtectedRouteProps): JSX.Element {
  const { isAuthenticated, isLoading } = useAuth();
  const { can, canAny, canAll, isAdmin } = usePermissions();
  const location = useLocation();
  
  // Show loading state while checking auth
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div
          className="animate-spin rounded-full h-12 w-12 border-b-2 border-cyan-600"
          role="status"
          aria-label="Loading"
        ></div>
      </div>
    );
  }
  
  // Redirect to login if not authenticated
  if (!isAuthenticated) {
    return <Navigate to={loginPath} state={{ from: location }} replace />;
  }
  
  // Check permissions
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
  
  // Redirect to unauthorized if no access
  if (!hasAccess) {
    return <Navigate to={unauthorizedPath} state={{ from: location }} replace />;
  }
  
  return <>{children}</>;
}

/**
 * Route that requires authentication only (no specific permissions)
 */
export function AuthenticatedRoute({ children }: { children: ReactNode }): JSX.Element {
  return <ProtectedRoute>{children}</ProtectedRoute>;
}

/**
 * Route that requires admin access
 */
export function AdminRoute({ children }: { children: ReactNode }): JSX.Element {
  return <ProtectedRoute requireAdmin>{children}</ProtectedRoute>;
}

export default ProtectedRoute;
