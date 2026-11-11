import { hasPermission } from '../../../../utils/permissions';
import type { UserRole } from '../../../../types';
import { EXTRA_SOURCE_SPECS } from '../../sources/extraSources';
import { isPanelSize, type CoreSourceKey, type WidgetEligibilityContext } from '../../widgetTypes';
import { EXTRA_WIDGETS, EXTRA_WIDGET_RENDERERS } from './index';

jest.mock('../../../../services/api', () => ({ __esModule: true, default: {} }));

const CORE_SOURCE_KEYS: readonly CoreSourceKey[] = ['quality', 'equipment', 'stock', 'capacity'];

function contextFor(role: UserRole): WidgetEligibilityContext {
  return {
    can: permission => hasPermission(role, permission),
    hasRole: roles => roles.includes(role),
    isAdmin: role === 'admin',
  };
}

function eligibleIds(role: UserRole): string[] {
  const ctx = contextFor(role);
  return EXTRA_WIDGETS.filter(widget => !widget.isEligible || widget.isEligible(ctx)).map(widget => widget.id);
}

describe('EXTRA_WIDGETS catalog', () => {
  it('has the fourteen opt-in panels with unique ids the server will accept', () => {
    const ids = EXTRA_WIDGETS.map(widget => widget.id);
    expect(ids).toHaveLength(14);
    expect(new Set(ids).size).toBe(ids.length);
    ids.forEach(id => expect(id).toMatch(/^[a-z][a-z0-9_.]{0,63}$/));
  });

  it('pairs every definition with exactly one renderer', () => {
    EXTRA_WIDGETS.forEach(widget => expect(EXTRA_WIDGET_RENDERERS.get(widget.id)).toEqual(expect.any(Function)));
    expect(Array.from(EXTRA_WIDGET_RENDERERS.keys()).sort()).toEqual(EXTRA_WIDGETS.map(widget => widget.id).sort());
    expect(EXTRA_WIDGET_RENDERERS).toBeInstanceOf(Map);
  });

  it('declares only panels, with a valid default size and dialog copy', () => {
    EXTRA_WIDGETS.forEach(widget => {
      expect(widget.zone).toBe('panel');
      expect(isPanelSize(widget.defaultSize)).toBe(true);
      expect(widget.title.trim()).not.toBe('');
      expect(widget.description.trim()).not.toBe('');
      expect(widget.sources.length).toBeGreaterThan(0);
    });
  });

  it('declares only source keys the loader can fetch', () => {
    const known = new Set<string>([...Object.keys(EXTRA_SOURCE_SPECS), ...CORE_SOURCE_KEYS]);
    EXTRA_WIDGETS.forEach(widget => widget.sources.forEach(key => expect(known.has(key)).toBe(true)));
  });

  it('uses every extra source key at least once', () => {
    const used = new Set<string>(EXTRA_WIDGETS.flatMap(widget => widget.sources));
    Object.keys(EXTRA_SOURCE_SPECS).forEach(key => expect(used.has(key)).toBe(true));
  });

  it('shares one wallboard request across the four wallboard panels', () => {
    const wallboardPanels = EXTRA_WIDGETS.filter(widget => widget.sources.includes('wallboard')).map(
      widget => widget.id
    );
    expect(wallboardPanels.sort()).toEqual(['blocked_jobs', 'late_jobs', 'shipping_outlook', 'todays_output']);
  });

  it('cannot be looked up through Object.prototype names', () => {
    expect(EXTRA_WIDGET_RENDERERS.get('constructor')).toBeUndefined();
    expect(EXTRA_WIDGET_RENDERERS.has('toString')).toBe(false);
  });
});

describe('EXTRA_WIDGETS eligibility', () => {
  it('offers delivery KPIs and visitors only to the server require_role list', () => {
    const planners: UserRole[] = ['admin', 'manager', 'supervisor'];
    const others: UserRole[] = ['quality', 'shipping', 'viewer', 'operator'];
    for (const role of planners) {
      expect(eligibleIds(role)).toEqual(expect.arrayContaining(['delivery_kpis', 'visitors_on_site']));
    }
    for (const role of others) {
      expect(eligibleIds(role)).not.toContain('delivery_kpis');
      expect(eligibleIds(role)).not.toContain('visitors_on_site');
    }
  });

  it('gates visitors on the role list, not on visitor_logs:view (which login overrides can strip)', () => {
    const ctx: WidgetEligibilityContext = {
      can: () => false,
      hasRole: roles => roles.includes('admin'),
      isAdmin: true,
    };
    const visible = EXTRA_WIDGETS.filter(widget => !widget.isEligible || widget.isEligible(ctx)).map(
      widget => widget.id
    );
    expect(visible).toEqual(expect.arrayContaining(['visitors_on_site', 'delivery_kpis']));
  });

  it('offers the always-on panels to everyone who can open the dashboard', () => {
    const ctx: WidgetEligibilityContext = { can: () => false, hasRole: () => false, isAdmin: false };
    const visible = EXTRA_WIDGETS.filter(widget => !widget.isEligible || widget.isEligible(ctx)).map(
      widget => widget.id
    );
    expect(visible.sort()).toEqual(['action_inbox', 'notifications']);
  });

  it('follows the route permissions for the rest', () => {
    // shipping lacks quality:view and receiving:view but has shipping:view.
    expect(eligibleIds('shipping')).toEqual(expect.arrayContaining(['shipping_outlook', 'work_orders_overdue']));
    expect(eligibleIds('shipping')).not.toContain('quality_pulse');
    expect(eligibleIds('shipping')).not.toContain('inspection_queue');
    // quality has receiving:view and quality:view but not shipping:view.
    expect(eligibleIds('quality')).toEqual(expect.arrayContaining(['quality_pulse', 'inspection_queue']));
    expect(eligibleIds('quality')).not.toContain('shipping_outlook');
  });
});
