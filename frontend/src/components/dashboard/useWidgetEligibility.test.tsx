/**
 * Widget eligibility and the link gate come from the real role → permission map (and its
 * runtime overrides), the same `can` PrivateRoute uses — never a hand-made permission list.
 * These pin what each role is offered from the real registry.
 */
import { renderHook } from '@testing-library/react';
import { setCustomPermissions } from '../../utils/permissions';
import { isWidgetEligible } from './dashboardLayout';
import { useWidgetEligibility } from './useWidgetEligibility';
import { DASHBOARD_REGISTRY } from './widgetRegistry';

let mockUser: { id: number; role: string; is_superuser: boolean } | null = null;
jest.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, isAuthenticated: mockUser !== null, isLoading: false }),
}));
jest.mock('../../services/api', () => ({ __esModule: true, default: {} }));

const ALL_IDS = DASHBOARD_REGISTRY.widgets.map(widget => widget.id);
const QUALITY_GATED = ['kpi.open_cars', 'kpi.pending_fai', 'quality_pulse'];
const PLANNER_ONLY = ['delivery_kpis', 'visitors_on_site'];

function accessFor(role: string, isSuperuser = false) {
  mockUser = { id: 1, role, is_superuser: isSuperuser };
  return renderHook(() => useWidgetEligibility()).result.current;
}

function eligibleIds(role: string, isSuperuser = false): string[] {
  const { eligibility } = accessFor(role, isSuperuser);
  return DASHBOARD_REGISTRY.widgets.filter(widget => isWidgetEligible(widget, eligibility)).map(widget => widget.id);
}

const allExcept = (...excluded: string[]) => ALL_IDS.filter(id => !excluded.includes(id)).sort();

afterEach(() => {
  setCustomPermissions(null);
  mockUser = null;
});

describe('useWidgetEligibility', () => {
  it('offers a viewer every widget but the admin setup nudge and the planner-only panels', () => {
    expect(eligibleIds('viewer').sort()).toEqual(allExcept('setup_nudge', ...PLANNER_ONLY));
  });

  it('withholds the quality and receiving widgets from the shipping role', () => {
    expect(eligibleIds('shipping').sort()).toEqual(
      allExcept('setup_nudge', ...PLANNER_ONLY, ...QUALITY_GATED, 'inspection_queue')
    );
    expect(eligibleIds('shipping')).toContain('shipping_outlook');
  });

  it('offers the planner-only panels to a supervisor, and the setup nudge only to an admin', () => {
    expect(eligibleIds('supervisor').sort()).toEqual(allExcept('setup_nudge'));
    expect(eligibleIds('admin').sort()).toEqual([...ALL_IDS].sort());
  });

  it('passes a superuser and a platform admin through every role gate, as the backend require_role does', () => {
    expect(eligibleIds('viewer', true).sort()).toEqual([...ALL_IDS].sort());
    // Login stores `platform_admin: []` from the admin-only role-permissions endpoint; it must not hide anything.
    setCustomPermissions({ platform_admin: [] });
    expect(eligibleIds('platform_admin').sort()).toEqual(allExcept('setup_nudge'));
  });

  it('follows the runtime role-permission overrides the router follows', () => {
    setCustomPermissions({ manager: ['work_orders:view', 'inventory:view'] });
    const offered = eligibleIds('manager');
    QUALITY_GATED.forEach(id => expect(offered).not.toContain(id));
    expect(offered).toEqual(expect.arrayContaining(['work_orders_overdue', 'delivery_kpis']));
    const { canOpen } = accessFor('manager');
    expect(canOpen('/quality?tab=ncr')).toBe(false);
    expect(canOpen('/work-orders?scope=overdue&cots=1')).toBe(true);
  });

  it('gates links exactly as PrivateRoute does (query strings ignored, unguarded paths open)', () => {
    const shipping = accessFor('shipping').canOpen;
    expect(shipping('/quality')).toBe(false);
    expect(shipping('/calibration?filter=due_soon')).toBe(false);
    expect(shipping('/receiving')).toBe(false);
    expect(shipping('/warehouse?tab=inventory&filter=low_stock')).toBe(true);
    expect(shipping('/action-inbox')).toBe(true);

    const viewer = accessFor('viewer').canOpen;
    expect(viewer('/quality')).toBe(true);
    expect(viewer('/work-centers')).toBe(false);
    expect(accessFor('admin').canOpen('/work-centers')).toBe(true);
  });
});
