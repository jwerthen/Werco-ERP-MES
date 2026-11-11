/**
 * Pure layout logic: what a saved layout renders for a viewer, and what the Customize
 * dialog saves back.
 */
import type { DashboardLayoutValues } from '../../types/dashboardLayout';
import type { Permission } from '../../utils/permissions';
import {
  buildSavePayload,
  countCarriedWidgets,
  draftFromLayout,
  isEmptyLayout,
  isSameLayout,
  resolveLayout,
} from './dashboardLayout';
import { CLASSIC_LAYOUT, CORE_WIDGETS, DASHBOARD_REGISTRY, createWidgetRegistry } from './widgetRegistry';
import { CORE_WIDGET_RENDERERS } from './widgets/core';
import { EXTRA_WIDGET_RENDERERS } from './widgets/extra';
import type { DashboardWidgetDefinition, WidgetEligibilityContext } from './widgetTypes';

const ctxFor = (permissions: Permission[] | 'all', isAdmin = false): WidgetEligibilityContext => ({
  can: permission => permissions === 'all' || permissions.includes(permission),
  hasRole: () => isAdmin,
  isAdmin,
});

const MANAGER = ctxFor(['work_orders:view', 'quality:view', 'inventory:view', 'quality:calibration']);
const ADMIN = ctxFor('all', true);
/** The shipping role: no quality:view. */
const SHIPPING = ctxFor(['work_orders:view', 'inventory:view', 'shipping:view']);

const layoutOf = (...widgets: { id: string; size?: string | null }[]): DashboardLayoutValues => ({
  format_version: 1,
  widgets,
});

const ids = (definitions: readonly DashboardWidgetDefinition[]) => definitions.map(definition => definition.id);

describe('the registry', () => {
  it('has unique ids that match the server pattern, and a renderer for every widget', () => {
    const seen = new Set<string>();
    for (const widget of DASHBOARD_REGISTRY.widgets) {
      expect(widget.id).toMatch(/^[a-z][a-z0-9_.]{0,63}$/);
      expect(seen.has(widget.id)).toBe(false);
      seen.add(widget.id);
      expect(CORE_WIDGET_RENDERERS.get(widget.id) ?? EXTRA_WIDGET_RENDERERS.get(widget.id)).toBeDefined();
      if (widget.zone === 'panel') expect(widget.defaultSize).toBeDefined();
    }
  });

  it('builds the classic layout from known core widgets only', () => {
    const coreIds = new Set(ids(CORE_WIDGETS));
    CLASSIC_LAYOUT.forEach(entry => expect(coreIds.has(entry.id)).toBe(true));
  });

  it('keeps the first definition when an id is registered twice', () => {
    const first: DashboardWidgetDefinition = {
      id: 'dup',
      zone: 'panel',
      title: 'First',
      description: '',
      category: 'Insights',
      sources: [],
      defaultSize: 'half',
    };
    const registry = createWidgetRegistry([first, { ...first, title: 'Second' }], []);
    expect(registry.widgets).toHaveLength(1);
    expect(registry.byId.get('dup')?.title).toBe('First');
  });
});

describe('resolveLayout', () => {
  it('renders the classic cockpit for a null (default) layout, byte-identical sizes', () => {
    const resolved = resolveLayout(null, ADMIN);
    expect(ids(resolved.banners)).toEqual(['attention', 'setup_nudge', 'morning_brief', 'alert_chips']);
    expect(ids(resolved.kpis)).toEqual([
      'kpi.overdue',
      'kpi.idle_operators',
      'kpi.active_work_orders',
      'kpi.signed_in',
      'kpi.checked_in',
      'kpi.due_today',
      'kpi.calibration_due',
      'kpi.low_stock',
      'kpi.completed_today',
      'kpi.open_ncrs',
    ]);
    expect(resolved.panels.map(panel => [panel.definition.id, panel.size])).toEqual([
      ['capacity_overview', 'wide'],
      ['live_activity', 'narrow'],
      ['work_center_status', 'wide'],
      ['presence', 'narrow'],
      ['recent_completions', 'full'],
    ]);
    expect(resolved.neededSources).toEqual(['capacity', 'equipment', 'quality', 'stock']);
  });

  it('drops the admin-only setup nudge for a non-admin', () => {
    expect(ids(resolveLayout(null, MANAGER).banners)).toEqual(['attention', 'morning_brief', 'alert_chips']);
  });

  it('drops unknown ids — including Object.prototype keys — and duplicates', () => {
    const resolved = resolveLayout(
      layoutOf(
        { id: 'constructor' },
        { id: 'toString' },
        { id: 'hasOwnProperty' },
        { id: 'kpi.overdue' },
        { id: 'kpi.from_a_newer_bundle' },
        { id: 'kpi.overdue' },
        { id: 'presence', size: 'full' },
        { id: 'presence', size: 'third' }
      ),
      MANAGER
    );
    expect(ids(resolved.kpis)).toEqual(['kpi.overdue']);
    expect(resolved.panels.map(panel => [panel.definition.id, panel.size])).toEqual([['presence', 'full']]);
    expect(Array.from(resolved.visibleIds).sort()).toEqual(['kpi.overdue', 'presence']);
    expect(resolved.neededSources).toEqual([]);
  });

  it('renders banners in registry order whatever the saved order; KPIs and panels in saved order', () => {
    const resolved = resolveLayout(
      layoutOf(
        { id: 'alert_chips' },
        { id: 'kpi.open_ncrs' },
        { id: 'attention' },
        { id: 'kpi.overdue' },
        { id: 'recent_completions' },
        { id: 'capacity_overview' }
      ),
      MANAGER
    );
    expect(ids(resolved.banners)).toEqual(['attention', 'alert_chips']);
    expect(ids(resolved.kpis)).toEqual(['kpi.open_ncrs', 'kpi.overdue']);
    expect(resolved.panels.map(panel => panel.definition.id)).toEqual(['recent_completions', 'capacity_overview']);
  });

  it('coerces an unknown or missing panel size to the widget default instead of dropping the widget', () => {
    const resolved = resolveLayout(
      layoutOf(
        { id: 'capacity_overview', size: 'enormous' },
        { id: 'presence', size: null },
        { id: 'live_activity', size: 'third' }
      ),
      MANAGER
    );
    expect(resolved.panels.map(panel => [panel.definition.id, panel.size])).toEqual([
      ['capacity_overview', 'wide'],
      ['presence', 'narrow'],
      ['live_activity', 'third'],
    ]);
  });

  it('hides widgets the viewer is not eligible for and does not fetch their sources', () => {
    const saved = layoutOf({ id: 'kpi.open_cars' }, { id: 'kpi.pending_fai' }, { id: 'kpi.overdue' });
    expect(ids(resolveLayout(saved, SHIPPING).kpis)).toEqual(['kpi.overdue']);
    expect(resolveLayout(saved, SHIPPING).neededSources).toEqual([]);
    expect(ids(resolveLayout(saved, MANAGER).kpis)).toEqual(['kpi.open_cars', 'kpi.pending_fai', 'kpi.overdue']);
    expect(resolveLayout(saved, MANAGER).neededSources).toEqual(['quality']);
  });

  it('treats a stored empty list as a deliberately empty dashboard', () => {
    expect(isEmptyLayout(resolveLayout(layoutOf(), ADMIN))).toBe(true);
    expect(isEmptyLayout(resolveLayout(null, ADMIN))).toBe(false);
  });

  it('survives a malformed stored value without throwing', () => {
    const malformed = {
      format_version: 1,
      widgets: [null, 7, { id: 42 }, { id: 'kpi.overdue' }],
    } as unknown as DashboardLayoutValues;
    expect(ids(resolveLayout(malformed, MANAGER).kpis)).toEqual(['kpi.overdue']);
    expect(isEmptyLayout(resolveLayout({ format_version: 1 } as unknown as DashboardLayoutValues, MANAGER))).toBe(true);
  });
});

describe('buildSavePayload / isSameLayout', () => {
  it('rebuilds the default exactly from an untouched draft (no phantom unsaved changes)', () => {
    const draft = draftFromLayout(null, MANAGER);
    const payload = buildSavePayload(draft, null, MANAGER);
    expect(payload.format_version).toBe(1);
    // The admin-only setup nudge is carried for a non-admin rather than silently removed.
    expect(payload.widgets.map(widget => widget.id)).toContain('setup_nudge');
    expect(isSameLayout(payload, null)).toBe(true);
  });

  it('carries saved widgets the viewer cannot see through a save, in place', () => {
    const saved = layoutOf(
      { id: 'kpi.overdue' },
      { id: 'kpi.open_cars' },
      { id: 'kpi.due_today' },
      { id: 'kpi.pending_fai' },
      { id: 'presence', size: 'half' }
    );
    const untouched = draftFromLayout(saved, SHIPPING);
    expect(untouched.kpis).toEqual(['kpi.overdue', 'kpi.due_today']);
    const unchanged = buildSavePayload(untouched, saved, SHIPPING);
    expect(unchanged.widgets).toEqual(saved.widgets);
    expect(isSameLayout(unchanged, saved)).toBe(true);

    // Reorder and remove what they can see; the hidden tiles keep their anchors.
    const edited = buildSavePayload({ ...untouched, kpis: ['kpi.due_today'] }, saved, SHIPPING);
    expect(edited.widgets.map(widget => widget.id)).toEqual([
      'kpi.open_cars',
      'kpi.due_today',
      'kpi.pending_fai',
      'presence',
    ]);
    expect(countCarriedWidgets(saved, SHIPPING)).toBe(2);
    expect(countCarriedWidgets(saved, MANAGER)).toBe(0);
  });

  it('drops unknown ids, emits sizes only for panels and caps the list', () => {
    const saved = layoutOf({ id: 'kpi.from_a_newer_bundle' }, { id: 'kpi.overdue', size: 'wide' });
    const payload = buildSavePayload(
      {
        banners: ['attention', 'constructor'],
        kpis: ['kpi.overdue', 'kpi.overdue', 'presence'],
        panels: [{ id: 'presence', size: 'third' }],
      },
      saved,
      MANAGER
    );
    expect(payload).toEqual({
      format_version: 1,
      widgets: [{ id: 'attention' }, { id: 'kpi.overdue' }, { id: 'presence', size: 'third' }],
    });
    expect(payload.widgets.length).toBeLessThanOrEqual(64);
  });

  it('treats a null or unknown size as the widget default', () => {
    expect(isSameLayout(layoutOf({ id: 'presence', size: null }), layoutOf({ id: 'presence', size: 'narrow' }))).toBe(
      true
    );
    expect(isSameLayout(layoutOf({ id: 'presence', size: 'bogus' }), layoutOf({ id: 'presence' }))).toBe(true);
    expect(isSameLayout(layoutOf({ id: 'presence', size: 'full' }), layoutOf({ id: 'presence' }))).toBe(false);
    expect(
      isSameLayout(
        layoutOf({ id: 'kpi.overdue' }, { id: 'kpi.due_today' }),
        layoutOf({ id: 'kpi.due_today' }, { id: 'kpi.overdue' })
      )
    ).toBe(false);
    // Banner order never matters: banners always render in registry order.
    expect(
      isSameLayout(
        layoutOf({ id: 'alert_chips' }, { id: 'attention' }),
        layoutOf({ id: 'attention' }, { id: 'alert_chips' })
      )
    ).toBe(true);
  });
});
