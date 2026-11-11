/**
 * What a widget's eligibility predicate may consult, and the dashboard's link gate.
 *
 * Both come from the ONE `usePermissions().can` that PrivateRoute and the sidebar use
 * (platform_admin passes every permission — utils/permissions.ts — and a superuser
 * passes everything in the hook), so the dashboard never shows a widget or a link the
 * router would refuse, and never hides one it would allow.
 *
 * Eligibility is a UX proxy: every endpoint enforces its own authorization, and a widget
 * still renders a 403 as "Not available for your role".
 */
import { useCallback, useMemo } from 'react';
import { usePermissions } from '../../hooks/usePermissions';
import type { UserRole } from '../../types';
import { canAccessPath } from '../../utils/routeAccess';
import type { WidgetEligibilityContext } from './widgetTypes';

export interface DashboardAccess {
  eligibility: WidgetEligibilityContext;
  /** The exact predicate PrivateRoute enforces for `href`. */
  canOpen: (href: string) => boolean;
}

export function useWidgetEligibility(): DashboardAccess {
  const permissions = usePermissions();
  const { can, isAdmin, role, isSuperuser } = permissions;

  // Mirrors backend `require_role([...])`, which always admits superusers and platform admins.
  const hasRole = useCallback(
    (roles: UserRole[]) => isSuperuser || role === 'platform_admin' || (role !== undefined && roles.includes(role)),
    [isSuperuser, role]
  );

  const eligibility = useMemo<WidgetEligibilityContext>(() => ({ can, hasRole, isAdmin }), [can, hasRole, isAdmin]);
  const canOpen = useCallback((href: string) => canAccessPath(href, can), [can]);

  return useMemo(() => ({ eligibility, canOpen }), [eligibility, canOpen]);
}
