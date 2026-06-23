/**
 * Laser-nest manage gating.
 *
 * The WorkOrderDetail "Add nest manually" / edit / attach-PDF / delete controls
 * are gated by `hasPermission(user.role, 'routings:create')`, which must mirror
 * the backend RBAC for the manual-nest write endpoints
 * (require_role([ADMIN, MANAGER, SUPERVISOR]); platform_admin has everything).
 */
import { hasPermission } from '../../utils/permissions';
import { UserRole } from '../../types';

const NEST_MANAGE_PERMISSION = 'routings:create' as const;

describe('laser-nest manage gating (routings:create)', () => {
  it.each<UserRole>(['admin', 'manager', 'supervisor', 'platform_admin'])(
    'allows nest management for %s',
    (role) => {
      expect(hasPermission(role, NEST_MANAGE_PERMISSION)).toBe(true);
    }
  );

  it.each<UserRole>(['operator', 'viewer', 'quality', 'shipping'])(
    'hides nest management for %s',
    (role) => {
      expect(hasPermission(role, NEST_MANAGE_PERMISSION)).toBe(false);
    }
  );

  it('hides nest management when the role is undefined', () => {
    expect(hasPermission(undefined, NEST_MANAGE_PERMISSION)).toBe(false);
  });
});
