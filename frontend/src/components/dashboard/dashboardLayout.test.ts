/**
 * Pure layout logic: what a saved layout renders for a viewer, what dashboard edit mode
 * saves back, and the draft operations edit mode applies.
 */
import type { DashboardLayoutValues } from '../../types/dashboardLayout';
import { MAX_DASHBOARD_WIDGETS } from '../../types/dashboardLayout';
import type { Permission } from '../../utils/permissions';
import {
  addWidget,
  buildSavePayload,
  countCarriedWidgets,
  countUnknownWidgets,
  draftFromLayout,
  isEmptyLayout,
  isSameLayout,
  moveWidgetTo,
  removeWidget,
  resolveLayout,
  setBannerVisible,
  setPanelSize,
  withZoneOrder,
  zoneIds,
  type LayoutDraft,
} from './dashboardLayout';
import { CLASSIC_LAYOUT, CORE_WIDGETS, DASHBOARD_REGISTRY, createWidgetRegistry } from './widgetRegistry';
import { CORE_WIDGET_RENDERERS } from './widgets/core';
import { EXTRA_WIDGET_RENDERERS } from './widgets/extra';
import type { DashboardWidgetDefinition, PanelSize, WidgetEligibilityContext } from './widgetTypes';

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

  it('round-trips: widgets a session cannot see survive its edits and return, in place and at their size, when access does', () => {
    const saved = layoutOf(
      { id: 'kpi.overdue' },
      { id: 'kpi.open_cars' },
      { id: 'kpi.due_today' },
      { id: 'capacity_overview', size: 'third' },
      { id: 'quality_pulse', size: 'full' },
      { id: 'presence', size: 'wide' }
    );
    // A session without quality:view (the shipping role) sees and edits only the rest.
    const draft = draftFromLayout(saved, SHIPPING);
    expect(draft.kpis).toEqual(['kpi.overdue', 'kpi.due_today']);
    expect(draft.panels).toEqual([
      { id: 'capacity_overview', size: 'third' },
      { id: 'presence', size: 'wide' },
    ]);
    const edited = buildSavePayload(
      {
        ...draft,
        kpis: ['kpi.due_today', 'kpi.overdue'],
        panels: [
          { id: 'presence', size: 'half' },
          { id: 'capacity_overview', size: 'third' },
          { id: 'shipping_outlook', size: 'third' },
        ],
      },
      saved,
      SHIPPING
    );
    // Each hidden widget follows the saved neighbour it followed before, with its own size.
    expect(edited.widgets).toEqual([
      { id: 'kpi.due_today' },
      { id: 'kpi.overdue' },
      { id: 'kpi.open_cars' },
      { id: 'presence', size: 'half' },
      { id: 'capacity_overview', size: 'third' },
      { id: 'quality_pulse', size: 'full' },
      { id: 'shipping_outlook', size: 'third' },
    ]);

    // Access returns: the hidden widgets render again where the viewer had them.
    const restored = resolveLayout(edited, ADMIN);
    expect(ids(restored.kpis)).toEqual(['kpi.due_today', 'kpi.overdue', 'kpi.open_cars']);
    expect(restored.panels.map(panel => [panel.definition.id, panel.size])).toEqual([
      ['presence', 'half'],
      ['capacity_overview', 'third'],
      ['quality_pulse', 'full'],
      ['shipping_outlook', 'third'],
    ]);
    expect(restored.neededSources).toEqual(['capacity', 'quality', 'wallboard']);
    // ...and an untouched dialog for that viewer saves nothing new.
    expect(buildSavePayload(draftFromLayout(edited, ADMIN), edited, ADMIN)).toEqual(edited);

    // Removing everything the restricted session can see still keeps what it cannot.
    const cleared = buildSavePayload({ banners: [], kpis: [], panels: [] }, edited, SHIPPING);
    expect(cleared.widgets).toEqual([{ id: 'kpi.open_cars' }, { id: 'quality_pulse', size: 'full' }]);
    expect(isEmptyLayout(resolveLayout(cleared, SHIPPING))).toBe(true);
    expect(ids(resolveLayout(cleared, ADMIN).kpis)).toEqual(['kpi.open_cars']);
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
  });

  it('caps the payload at MAX_DASHBOARD_WIDGETS, keeping the first ones', () => {
    // The real registry is smaller than the cap, so only a larger one can exercise it.
    const many: DashboardWidgetDefinition[] = Array.from({ length: MAX_DASHBOARD_WIDGETS + 6 }, (_, index) => ({
      id: `kpi.w${index}`,
      zone: 'kpi',
      title: `Widget ${index}`,
      description: '',
      category: 'Insights',
      sources: [],
    }));
    const registry = createWidgetRegistry(many, []);
    const allIds = many.map(definition => definition.id);
    const payload = buildSavePayload({ banners: [], kpis: allIds, panels: [] }, layoutOf(), MANAGER, registry);
    expect(payload.widgets).toHaveLength(MAX_DASHBOARD_WIDGETS);
    expect(payload.widgets.map(widget => widget.id)).toEqual(allIds.slice(0, MAX_DASHBOARD_WIDGETS));
  });

  it('counts as carried only widgets the viewer saved — never the stock default the layout started from', () => {
    // No saved layout: nothing of the viewer's is hidden, whatever the default contains.
    expect(countCarriedWidgets(null, MANAGER)).toBe(0);
    expect(countCarriedWidgets(null, SHIPPING)).toBe(0);
    // A non-admin's first save carries the admin-only setup nudge from the default; it was
    // never their choice, so it is never announced as "hidden from you right now".
    const firstSave = buildSavePayload({ ...draftFromLayout(null, MANAGER), kpis: ['kpi.overdue'] }, null, MANAGER);
    expect(firstSave.widgets.map(widget => widget.id)).toContain('setup_nudge');
    expect(countCarriedWidgets(firstSave, MANAGER)).toBe(0);
    // A widget the viewer did pick and can no longer see is still counted.
    expect(countCarriedWidgets(layoutOf({ id: 'setup_nudge' }, { id: 'kpi.open_cars' }), SHIPPING)).toBe(1);
  });

  it('counts the distinct saved widgets this bundle does not know (a save from here drops them)', () => {
    expect(countUnknownWidgets(null)).toBe(0);
    expect(countUnknownWidgets(layoutOf({ id: 'kpi.overdue' }, { id: 'presence' }))).toBe(0);
    const fromNewerBundle = layoutOf(
      { id: 'kpi.from_a_newer_bundle' },
      { id: 'kpi.overdue' },
      { id: 'kpi.from_a_newer_bundle' },
      { id: 'panel_from_a_newer_bundle', size: 'half' }
    );
    expect(countUnknownWidgets(fromNewerBundle)).toBe(2);
    // The documented policy stands: the payload built from this bundle leaves them out.
    const payload = buildSavePayload(draftFromLayout(fromNewerBundle, MANAGER), fromNewerBundle, MANAGER);
    expect(payload.widgets.map(widget => widget.id)).not.toContain('kpi.from_a_newer_bundle');
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

describe('draft operations (edit mode)', () => {
  const draft = (): LayoutDraft => ({
    banners: ['attention', 'alert_chips'],
    kpis: ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs'],
    panels: [
      { id: 'capacity_overview', size: 'wide' },
      { id: 'presence', size: 'narrow' },
      { id: 'recent_completions', size: 'full' },
    ],
  });
  /** A deep copy taken before an op, to prove the op never mutates its input. */
  const snapshot = (value: LayoutDraft) => JSON.parse(JSON.stringify(value)) as LayoutDraft;
  const definition = (id: string) => DASHBOARD_REGISTRY.byId.get(id)!;

  describe('zoneIds', () => {
    it('lists a zone in render order as a fresh array', () => {
      const base = draft();
      expect(zoneIds(base, 'kpi')).toEqual(['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs']);
      expect(zoneIds(base, 'kpi')).not.toBe(base.kpis);
      expect(zoneIds(base, 'panel')).toEqual(['capacity_overview', 'presence', 'recent_completions']);
    });
  });

  describe('withZoneOrder', () => {
    it('reorders one zone, keeps panel sizes and leaves the other zones untouched', () => {
      const base = draft();
      const before = snapshot(base);
      const next = withZoneOrder(base, 'panel', ['recent_completions', 'capacity_overview', 'presence']);
      expect(next.panels).toEqual([
        { id: 'recent_completions', size: 'full' },
        { id: 'capacity_overview', size: 'wide' },
        { id: 'presence', size: 'narrow' },
      ]);
      expect(next.kpis).toBe(base.kpis);
      expect(next.banners).toBe(base.banners);
      expect(base).toEqual(before);
      expect(withZoneOrder(base, 'kpi', ['kpi.open_ncrs', 'kpi.overdue', 'kpi.due_today']).kpis).toEqual([
        'kpi.open_ncrs',
        'kpi.overdue',
        'kpi.due_today',
      ]);
    });

    it('returns the same draft for the current order', () => {
      const base = draft();
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs'])).toBe(base);
      expect(withZoneOrder(base, 'panel', ['capacity_overview', 'presence', 'recent_completions'])).toBe(base);
    });

    it('rejects a membership change (added, missing, repeated or foreign ids) by returning the same draft', () => {
      const base = draft();
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.due_today'])).toBe(base);
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.due_today', 'kpi.open_ncrs', 'kpi.signed_in'])).toBe(base);
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.overdue', 'kpi.due_today'])).toBe(base);
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.due_today', 'kpi.signed_in'])).toBe(base);
      // A panel id is not a member of the KPI zone.
      expect(withZoneOrder(base, 'kpi', ['kpi.overdue', 'kpi.due_today', 'presence'])).toBe(base);
    });
  });

  describe('moveWidgetTo', () => {
    it('moves to a position, clamping to the first and last place', () => {
      const base = draft();
      const before = snapshot(base);
      expect(zoneIds(moveWidgetTo(base, 'kpi', 'kpi.open_ncrs', 0), 'kpi')).toEqual([
        'kpi.open_ncrs',
        'kpi.overdue',
        'kpi.due_today',
      ]);
      expect(zoneIds(moveWidgetTo(base, 'kpi', 'kpi.overdue', 1), 'kpi')).toEqual([
        'kpi.due_today',
        'kpi.overdue',
        'kpi.open_ncrs',
      ]);
      expect(zoneIds(moveWidgetTo(base, 'panel', 'capacity_overview', 99), 'panel')).toEqual([
        'presence',
        'recent_completions',
        'capacity_overview',
      ]);
      expect(zoneIds(moveWidgetTo(base, 'panel', 'recent_completions', -5), 'panel')).toEqual([
        'recent_completions',
        'capacity_overview',
        'presence',
      ]);
      expect(moveWidgetTo(base, 'panel', 'presence', 0).panels[0]).toEqual({ id: 'presence', size: 'narrow' });
      expect(base).toEqual(before);
    });

    it('returns the same draft when nothing moves', () => {
      const base = draft();
      expect(moveWidgetTo(base, 'kpi', 'kpi.overdue', 0)).toBe(base);
      expect(moveWidgetTo(base, 'kpi', 'kpi.overdue', -1)).toBe(base);
      expect(moveWidgetTo(base, 'kpi', 'kpi.open_ncrs', 2)).toBe(base);
      expect(moveWidgetTo(base, 'kpi', 'kpi.open_ncrs', 40)).toBe(base);
      expect(moveWidgetTo(base, 'kpi', 'kpi.overdue', Number.NaN)).toBe(base);
      // Not in that zone: a panel id against the KPI zone, and an unknown id.
      expect(moveWidgetTo(base, 'kpi', 'presence', 0)).toBe(base);
      expect(moveWidgetTo(base, 'panel', 'kpi.from_a_newer_bundle', 0)).toBe(base);
    });
  });

  describe('removeWidget', () => {
    it('removes from the named zone only, immutably', () => {
      const base = draft();
      const before = snapshot(base);
      const withoutTile = removeWidget(base, 'kpi', 'kpi.due_today');
      expect(withoutTile.kpis).toEqual(['kpi.overdue', 'kpi.open_ncrs']);
      expect(withoutTile.panels).toBe(base.panels);
      const withoutPanel = removeWidget(base, 'panel', 'presence');
      expect(zoneIds(withoutPanel, 'panel')).toEqual(['capacity_overview', 'recent_completions']);
      expect(withoutPanel.kpis).toBe(base.kpis);
      expect(base).toEqual(before);
    });

    it('returns the same draft when the widget is not in that zone', () => {
      const base = draft();
      expect(removeWidget(base, 'kpi', 'kpi.signed_in')).toBe(base);
      expect(removeWidget(base, 'kpi', 'presence')).toBe(base);
      expect(removeWidget(base, 'panel', 'kpi.overdue')).toBe(base);
    });
  });

  describe('addWidget', () => {
    it('appends a KPI to the end of the strip', () => {
      const base = draft();
      const before = snapshot(base);
      expect(addWidget(base, definition('kpi.signed_in')).kpis).toEqual([
        'kpi.overdue',
        'kpi.due_today',
        'kpi.open_ncrs',
        'kpi.signed_in',
      ]);
      expect(base).toEqual(before);
    });

    it("appends a panel at its default width, else 'half'", () => {
      const base = draft();
      const live = definition('live_activity');
      expect(live.defaultSize).toBe('narrow');
      expect(addWidget(base, live).panels.slice(-1)).toEqual([{ id: 'live_activity', size: 'narrow' }]);
      const sizeless: DashboardWidgetDefinition = { ...definition('presence'), id: 'sizeless', defaultSize: undefined };
      expect(addWidget(base, sizeless).panels.slice(-1)).toEqual([{ id: 'sizeless', size: 'half' }]);
    });

    it('returns the same draft for a widget already on the dashboard, or a banner', () => {
      const base = draft();
      expect(addWidget(base, definition('kpi.overdue'))).toBe(base);
      expect(addWidget(base, definition('presence'))).toBe(base);
      expect(addWidget(base, definition('morning_brief'))).toBe(base);
    });
  });

  describe('setPanelSize', () => {
    it('changes one panel width, immutably', () => {
      const base = draft();
      const before = snapshot(base);
      const next = setPanelSize(base, 'presence', 'third');
      expect(next.panels).toEqual([
        { id: 'capacity_overview', size: 'wide' },
        { id: 'presence', size: 'third' },
        { id: 'recent_completions', size: 'full' },
      ]);
      expect(next.panels[0]).toBe(base.panels[0]);
      expect(base).toEqual(before);
    });

    it('returns the same draft for an unchanged size, a missing panel or an unknown size', () => {
      const base = draft();
      expect(setPanelSize(base, 'presence', 'narrow')).toBe(base);
      expect(setPanelSize(base, 'live_activity', 'full')).toBe(base);
      expect(setPanelSize(base, 'kpi.overdue', 'full')).toBe(base);
      expect(setPanelSize(base, 'presence', 'enormous' as PanelSize)).toBe(base);
    });
  });

  describe('setBannerVisible', () => {
    it('switches banners on and off, and they still render and save in registry order', () => {
      const base = draft();
      const before = snapshot(base);
      const withBrief = setBannerVisible(base, 'morning_brief', true);
      expect(withBrief.banners).toEqual(['attention', 'alert_chips', 'morning_brief']);
      expect(base).toEqual(before);
      // Registry order wins over draft order, rendered and saved.
      expect(ids(resolveLayout(buildSavePayload(withBrief, null, MANAGER), MANAGER).banners)).toEqual([
        'attention',
        'morning_brief',
        'alert_chips',
      ]);
      expect(
        buildSavePayload(withBrief, null, MANAGER)
          .widgets.slice(0, 4)
          .map(widget => widget.id)
      ).toEqual(['attention', 'setup_nudge', 'morning_brief', 'alert_chips']);
      expect(setBannerVisible(base, 'attention', false).banners).toEqual(['alert_chips']);
    });

    it('returns the same draft when the banner is already in that state', () => {
      const base = draft();
      expect(setBannerVisible(base, 'attention', true)).toBe(base);
      expect(setBannerVisible(base, 'morning_brief', false)).toBe(base);
    });

    it('is clean after a banner is switched off and back on', () => {
      const saved = buildSavePayload(draftFromLayout(null, MANAGER), null, MANAGER);
      const untouched = draftFromLayout(saved, MANAGER);
      const toggled = setBannerVisible(setBannerVisible(untouched, 'attention', false), 'attention', true);
      expect(toggled.banners).not.toEqual(untouched.banners);
      expect(isSameLayout(buildSavePayload(toggled, saved, MANAGER), saved)).toBe(true);
    });
  });

  describe('with carried (ineligible) saved widgets', () => {
    // The shipping role cannot see the quality widgets: kpi.open_cars, kpi.pending_fai, quality_pulse.
    const saved = layoutOf(
      { id: 'attention' },
      { id: 'kpi.overdue' },
      { id: 'kpi.open_cars' },
      { id: 'kpi.due_today' },
      { id: 'kpi.pending_fai' },
      { id: 'capacity_overview', size: 'third' },
      { id: 'quality_pulse', size: 'full' },
      { id: 'presence', size: 'wide' }
    );
    const carriedIds = ['kpi.open_cars', 'kpi.pending_fai', 'quality_pulse'];
    const savedIdsOf = (draftValue: LayoutDraft) =>
      buildSavePayload(draftValue, saved, SHIPPING).widgets.map(widget => widget.id);

    it('the untouched draft is clean and every op keeps the carried entries anchored', () => {
      const untouched = draftFromLayout(saved, SHIPPING);
      expect(untouched.kpis).toEqual(['kpi.overdue', 'kpi.due_today']);
      expect(isSameLayout(buildSavePayload(untouched, saved, SHIPPING), saved)).toBe(true);

      const cases: [string, LayoutDraft, string[]][] = [
        [
          'withZoneOrder',
          withZoneOrder(untouched, 'kpi', ['kpi.due_today', 'kpi.overdue']),
          // Each carried tile follows the saved neighbour it followed before.
          [
            'attention',
            'kpi.due_today',
            'kpi.pending_fai',
            'kpi.overdue',
            'kpi.open_cars',
            'capacity_overview',
            'quality_pulse',
            'presence',
          ],
        ],
        [
          'moveWidgetTo',
          moveWidgetTo(untouched, 'panel', 'presence', 0),
          [
            'attention',
            'kpi.overdue',
            'kpi.open_cars',
            'kpi.due_today',
            'kpi.pending_fai',
            'presence',
            'capacity_overview',
            'quality_pulse',
          ],
        ],
        [
          'removeWidget',
          removeWidget(untouched, 'kpi', 'kpi.overdue'),
          [
            'attention',
            'kpi.open_cars',
            'kpi.due_today',
            'kpi.pending_fai',
            'capacity_overview',
            'quality_pulse',
            'presence',
          ],
        ],
        [
          'addWidget',
          addWidget(untouched, definition('kpi.signed_in')),
          [
            'attention',
            'kpi.overdue',
            'kpi.open_cars',
            'kpi.due_today',
            'kpi.pending_fai',
            'kpi.signed_in',
            'capacity_overview',
            'quality_pulse',
            'presence',
          ],
        ],
        [
          'setPanelSize',
          setPanelSize(untouched, 'capacity_overview', 'half'),
          [
            'attention',
            'kpi.overdue',
            'kpi.open_cars',
            'kpi.due_today',
            'kpi.pending_fai',
            'capacity_overview',
            'quality_pulse',
            'presence',
          ],
        ],
        [
          'setBannerVisible',
          setBannerVisible(untouched, 'alert_chips', true),
          [
            'attention',
            'alert_chips',
            'kpi.overdue',
            'kpi.open_cars',
            'kpi.due_today',
            'kpi.pending_fai',
            'capacity_overview',
            'quality_pulse',
            'presence',
          ],
        ],
      ];
      for (const [name, edited, expected] of cases) {
        expect([name, savedIdsOf(edited)]).toEqual([name, expected]);
        const payload = buildSavePayload(edited, saved, SHIPPING);
        carriedIds.forEach(id =>
          expect([name, payload.widgets.some(widget => widget.id === id)]).toEqual([name, true])
        );
        expect(payload.widgets.find(widget => widget.id === 'quality_pulse')).toEqual({
          id: 'quality_pulse',
          size: 'full',
        });
      }
      expect(
        buildSavePayload(setPanelSize(untouched, 'capacity_overview', 'half'), saved, SHIPPING).widgets
      ).toContainEqual({ id: 'capacity_overview', size: 'half' });
    });

    it('is clean again after a reorder is undone by the reverse reorder', () => {
      const untouched = draftFromLayout(saved, SHIPPING);
      const there = withZoneOrder(untouched, 'panel', ['presence', 'capacity_overview']);
      expect(isSameLayout(buildSavePayload(there, saved, SHIPPING), saved)).toBe(false);
      const back = withZoneOrder(there, 'panel', ['capacity_overview', 'presence']);
      expect(back).not.toBe(untouched);
      expect(isSameLayout(buildSavePayload(back, saved, SHIPPING), saved)).toBe(true);
      const moved = moveWidgetTo(moveWidgetTo(untouched, 'kpi', 'kpi.overdue', 1), 'kpi', 'kpi.overdue', 0);
      expect(isSameLayout(buildSavePayload(moved, saved, SHIPPING), saved)).toBe(true);
    });
  });
});
