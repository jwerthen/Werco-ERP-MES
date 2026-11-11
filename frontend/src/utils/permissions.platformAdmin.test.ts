/**
 * platform_admin passes every frontend permission check, whatever per-company role
 * overrides are loaded.
 *
 * Login loads `GET /admin/settings/role-permissions`, which answers `platform_admin: []`
 * (the backend's DEFAULT_ROLE_PERMISSIONS has no row for that role, and `require_role`
 * waves it through every gate instead). Read as an override, that empty list made every
 * `can()` false for a password-login platform admin until the page reloaded — the nav
 * emptied and every route guard sent them to /unauthorized. The dashboard's link gating
 * and widget eligibility both run through this one `can`, so it has to agree with the
 * backend.
 */
import { canAccessPath } from './routeAccess';
import {
  getPermissionsForRole,
  hasAllPermissions,
  hasAnyPermission,
  hasPermission,
  setCustomPermissions,
} from './permissions';

afterEach(() => setCustomPermissions(null));

describe('platform_admin and per-company role overrides', () => {
  it('ignores the empty platform_admin override the role-permissions endpoint returns', () => {
    setCustomPermissions({ platform_admin: [], manager: ['work_orders:view'] });

    expect(hasPermission('platform_admin', 'work_orders:view')).toBe(true);
    expect(hasPermission('platform_admin', 'admin:settings')).toBe(true);
    expect(hasPermission('platform_admin', 'visitor_logs:view')).toBe(true);
    expect(hasAnyPermission('platform_admin', ['quality:calibration'])).toBe(true);
    expect(hasAllPermissions('platform_admin', ['quality:view', 'purchasing:create'])).toBe(true);
    expect(getPermissionsForRole('platform_admin')).toContain('admin:settings');
  });

  it('ignores a narrowing platform_admin override too (the backend role gate never reads one)', () => {
    setCustomPermissions({ platform_admin: ['work_orders:view'] });
    expect(hasPermission('platform_admin', 'users:roles')).toBe(true);
  });

  it('still applies overrides to tenant roles', () => {
    setCustomPermissions({ platform_admin: [], manager: ['work_orders:view'] });
    expect(hasPermission('manager', 'work_orders:view')).toBe(true);
    expect(hasPermission('manager', 'quality:view')).toBe(false);
  });

  it('opens every route PrivateRoute guards for a platform_admin under an empty override', () => {
    setCustomPermissions({ platform_admin: [] });
    const can = (permission: Parameters<typeof hasPermission>[1]) => hasPermission('platform_admin', permission);
    for (const path of ['/work-centers', '/calibration', '/quality', '/setup', '/visitor-log', '/hank-engineering']) {
      expect(canAccessPath(path, can)).toBe(true);
    }
  });
});
