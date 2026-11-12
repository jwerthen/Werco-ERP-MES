/**
 * Pure layout logic for the customizable dashboard: no React, no api.
 *
 * - `resolveLayout` turns a saved layout (or `null` = the stock default) into what the
 *   page renders for THIS viewer: unknown ids (a newer or older bundle), duplicates and
 *   widgets the viewer is not eligible for are dropped; banners keep registry order;
 *   KPIs and panels keep saved order; a panel size that is not a known PanelSize falls
 *   back to the widget's `defaultSize` (the widget is never dropped for its size).
 * - `buildSavePayload` turns the edit-mode draft (eligible widgets only) back into a full
 *   layout, CARRYING known-but-ineligible saved entries through unchanged so a temporary
 *   permission change never destroys a layout.
 * - `isSameLayout` compares two layouts the way the page would render them, so an
 *   untouched editor is never "dirty".
 * - The draft operations (`withZoneOrder`, `moveWidgetTo`, `removeWidget`, `addWidget`,
 *   `setPanelSize`, `setBannerVisible`) are what dashboard edit mode applies. Each is
 *   immutable and returns the SAME draft object when it changes nothing, so the editor's
 *   history records a step only for a real change (`next !== draft`).
 *
 * Every id lookup is a Map lookup (`registry.byId`): a stored `constructor` is unknown.
 */
import type { DashboardLayoutValues, DashboardWidgetPlacement } from '../../types/dashboardLayout';
import { MAX_DASHBOARD_WIDGETS } from '../../types/dashboardLayout';
import { DASHBOARD_REGISTRY, type WidgetRegistry } from './widgetRegistry';
import {
  isPanelSize,
  type DashboardSourceKey,
  type DashboardWidgetDefinition,
  type LayoutEntry,
  type PanelSize,
  type ResolvedLayout,
  type ResolvedPanel,
  type WidgetEligibilityContext,
} from './widgetTypes';

/** What dashboard edit mode edits: the viewer's ELIGIBLE widgets only. */
export interface LayoutDraft {
  /** Visible banner ids (render order is always registry order). */
  banners: string[];
  /** Visible KPI ids, in render order. */
  kpis: string[];
  /** Visible panels, in render order. */
  panels: { id: string; size: PanelSize }[];
}

type LayoutInput = DashboardLayoutValues | readonly LayoutEntry[] | null | undefined;

interface CanonicalLayout {
  banners: string[];
  kpis: string[];
  panels: { id: string; size: PanelSize }[];
}

const FALLBACK_PANEL_SIZE: PanelSize = 'half';

function entriesOf(layout: LayoutInput, registry: WidgetRegistry): readonly LayoutEntry[] {
  if (layout == null) return registry.classic;
  if (Array.isArray(layout)) return layout as readonly LayoutEntry[];
  const widgets = (layout as DashboardLayoutValues).widgets;
  return Array.isArray(widgets) ? widgets : [];
}

export function isWidgetEligible(definition: DashboardWidgetDefinition, ctx: WidgetEligibilityContext): boolean {
  return !definition.isEligible || definition.isEligible(ctx);
}

/** The size a panel renders at: the stored size when it is a known PanelSize, else the widget default. */
export function panelSizeFor(definition: DashboardWidgetDefinition, size: unknown): PanelSize {
  if (isPanelSize(size)) return size;
  return definition.defaultSize ?? FALLBACK_PANEL_SIZE;
}

/** Known, de-duplicated entries grouped by zone; banners in registry order; sizes normalized. */
function canonicalize(layout: LayoutInput, registry: WidgetRegistry): CanonicalLayout {
  const seen = new Set<string>();
  const bannerIds = new Set<string>();
  const kpis: string[] = [];
  const panels: { id: string; size: PanelSize }[] = [];
  for (const entry of entriesOf(layout, registry)) {
    if (!entry || typeof entry.id !== 'string') continue;
    const definition = registry.byId.get(entry.id);
    if (!definition || seen.has(entry.id)) continue;
    seen.add(entry.id);
    if (definition.zone === 'banner') bannerIds.add(entry.id);
    else if (definition.zone === 'kpi') kpis.push(entry.id);
    else panels.push({ id: entry.id, size: panelSizeFor(definition, entry.size) });
  }
  const banners = registry.widgets
    .filter(widget => widget.zone === 'banner' && bannerIds.has(widget.id))
    .map(widget => widget.id);
  return { banners, kpis, panels };
}

export function resolveLayout(
  saved: LayoutInput,
  ctx: WidgetEligibilityContext,
  registry: WidgetRegistry = DASHBOARD_REGISTRY
): ResolvedLayout {
  const canonical = canonicalize(saved, registry);
  const eligible = (id: string) => {
    const definition = registry.byId.get(id);
    return Boolean(definition && isWidgetEligible(definition, ctx));
  };
  const banners = canonical.banners.filter(eligible).map(id => registry.byId.get(id)!);
  const kpis = canonical.kpis.filter(eligible).map(id => registry.byId.get(id)!);
  const panels: ResolvedPanel[] = canonical.panels
    .filter(panel => eligible(panel.id))
    .map(panel => ({ definition: registry.byId.get(panel.id)!, size: panel.size }));
  const visible = [...banners, ...kpis, ...panels.map(panel => panel.definition)];
  const sources = new Set<DashboardSourceKey>();
  visible.forEach(definition => definition.sources.forEach(source => sources.add(source)));
  return {
    banners,
    kpis,
    panels,
    visibleIds: new Set(visible.map(definition => definition.id)),
    neededSources: Array.from(sources).sort(),
  };
}

/** The editor's starting draft: exactly what the page renders for this viewer. */
export function draftFromLayout(
  saved: LayoutInput,
  ctx: WidgetEligibilityContext,
  registry: WidgetRegistry = DASHBOARD_REGISTRY
): LayoutDraft {
  const resolved = resolveLayout(saved, ctx, registry);
  return {
    banners: resolved.banners.map(definition => definition.id),
    kpis: resolved.kpis.map(definition => definition.id),
    panels: resolved.panels.map(panel => ({ id: panel.definition.id, size: panel.size })),
  };
}

/**
 * Put carried (known-but-ineligible) entries back where they were: each one follows the
 * nearest earlier saved entry the draft still shows, or leads the zone when none did.
 * Relative order among carried entries is the saved order. An untouched draft therefore
 * rebuilds the saved layout exactly (no phantom "unsaved changes").
 */
function mergeCarried<T extends { id: string }>(draft: T[], base: T[], isCarried: (id: string) => boolean): T[] {
  const draftIds = new Set(draft.map(item => item.id));
  const anchored = new Map<string | null, T[]>();
  let anchor: string | null = null;
  for (const item of base) {
    if (draftIds.has(item.id)) {
      anchor = item.id;
    } else if (isCarried(item.id)) {
      const group = anchored.get(anchor) ?? [];
      group.push(item);
      anchored.set(anchor, group);
    }
  }
  const merged: T[] = [...(anchored.get(null) ?? [])];
  for (const item of draft) {
    merged.push(item);
    merged.push(...(anchored.get(item.id) ?? []));
  }
  return merged;
}

export function buildSavePayload(
  draft: LayoutDraft,
  saved: LayoutInput,
  ctx: WidgetEligibilityContext,
  registry: WidgetRegistry = DASHBOARD_REGISTRY
): DashboardLayoutValues {
  const base = canonicalize(saved, registry);
  const definitionOf = (id: string) => registry.byId.get(id);
  const isCarried = (id: string) => {
    const definition = definitionOf(id);
    return Boolean(definition && !isWidgetEligible(definition, ctx));
  };
  // The draft is eligible-only by construction; filter defensively anyway.
  const seen = new Set<string>();
  const keep = (id: string, zone: DashboardWidgetDefinition['zone']) => {
    const definition = definitionOf(id);
    if (!definition || definition.zone !== zone || seen.has(id) || !isWidgetEligible(definition, ctx)) return false;
    seen.add(id);
    return true;
  };
  const draftBanners = draft.banners.filter(id => keep(id, 'banner'));
  const draftKpis = draft.kpis.filter(id => keep(id, 'kpi')).map(id => ({ id }));
  const draftPanels = draft.panels
    .filter(panel => keep(panel.id, 'panel'))
    .map(panel => ({ id: panel.id, size: panelSizeFor(definitionOf(panel.id)!, panel.size) }));

  const bannerIds = new Set([...draftBanners, ...base.banners.filter(isCarried)]);
  const banners = registry.widgets.filter(widget => widget.zone === 'banner' && bannerIds.has(widget.id));
  const kpis = mergeCarried(
    draftKpis,
    base.kpis.map(id => ({ id })),
    isCarried
  );
  const panels = mergeCarried(draftPanels, base.panels, isCarried);

  const widgets: DashboardWidgetPlacement[] = [
    ...banners.map(widget => ({ id: widget.id })),
    ...kpis.map(kpi => ({ id: kpi.id })),
    ...panels.map(panel => ({ id: panel.id, size: panel.size })),
  ].slice(0, MAX_DASHBOARD_WIDGETS);
  return { format_version: 1, widgets };
}

/** Same rendered result: same banners, same KPI order, same panel order and sizes (null size ≡ default). */
export function isSameLayout(a: LayoutInput, b: LayoutInput, registry: WidgetRegistry = DASHBOARD_REGISTRY): boolean {
  return JSON.stringify(canonicalize(a, registry)) === JSON.stringify(canonicalize(b, registry));
}

/**
 * How many widgets the viewer put in their SAVED layout but cannot currently see (they are
 * kept on save, and the edit bar says so).
 *
 * Only a saved layout is counted: `null` (no saved layout — the stock default) is 0. And a
 * stock-default widget the viewer is not eligible for is never counted, saved or not: it
 * is in the layout because the default put it there (`buildSavePayload` carries it through
 * a non-admin's first save so an untouched editor stays clean), not because the viewer
 * chose it — today that is the admin-only setup nudge, gated by role rather than by a
 * permission that may come back.
 */
export function countCarriedWidgets(
  saved: LayoutInput,
  ctx: WidgetEligibilityContext,
  registry: WidgetRegistry = DASHBOARD_REGISTRY
): number {
  if (saved == null) return 0;
  const stockDefault = new Set(registry.classic.map(entry => entry.id));
  const canonical = canonicalize(saved, registry);
  return [...canonical.banners, ...canonical.kpis, ...canonical.panels.map(panel => panel.id)].filter(id => {
    const definition = registry.byId.get(id);
    return Boolean(definition && !isWidgetEligible(definition, ctx) && !stockDefault.has(id));
  }).length;
}

/**
 * How many distinct widgets the SAVED layout names that this bundle does not know (a layout
 * saved by a newer SPA, e.g. from a fresh tab after a deploy). They are never drafted or
 * carried, so a save from this bundle drops them (docs/DASHBOARD.md); the edit bar says so
 * before Done. `null` (no saved layout) is 0.
 */
export function countUnknownWidgets(saved: LayoutInput, registry: WidgetRegistry = DASHBOARD_REGISTRY): number {
  if (saved == null) return 0;
  const unknown = new Set<string>();
  for (const entry of entriesOf(saved, registry)) {
    if (entry && typeof entry.id === 'string' && !registry.byId.has(entry.id)) unknown.add(entry.id);
  }
  return unknown.size;
}

/** True when no widget at all would render. */
export function isEmptyLayout(resolved: ResolvedLayout): boolean {
  return resolved.banners.length === 0 && resolved.kpis.length === 0 && resolved.panels.length === 0;
}

// ── Draft operations (dashboard edit mode) ─────────────────────────────────────
// Pure and immutable. Each returns the SAME draft object when nothing changes (an unknown
// id, an out-of-zone id, an unchanged position, size or visibility), which is what lets the
// editor's history skip no-op steps by reference.

/** The two zones a user reorders. Banners are only switched on and off, in registry order. */
export type EditableZone = 'kpi' | 'panel';

/** The widget ids of one editable zone, in render order (a fresh array). */
export function zoneIds(draft: LayoutDraft, zone: EditableZone): string[] {
  return zone === 'kpi' ? [...draft.kpis] : draft.panels.map(panel => panel.id);
}

function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

/**
 * Put one zone in `order`. Only a REORDER is accepted: when `order` does not name exactly
 * the zone's current widgets (an added, missing or repeated id), the draft is returned
 * unchanged. Panels keep their sizes.
 */
export function withZoneOrder(draft: LayoutDraft, zone: EditableZone, order: readonly string[]): LayoutDraft {
  const current = zoneIds(draft, zone);
  if (sameIds(current, order)) return draft;
  if (order.length !== current.length || new Set(order).size !== order.length) return draft;
  const members = new Set(current);
  if (!order.every(id => members.has(id))) return draft;
  if (zone === 'kpi') return { ...draft, kpis: [...order] };
  const byId = new Map(draft.panels.map(panel => [panel.id, panel] as const));
  return { ...draft, panels: order.map(id => byId.get(id)!) };
}

/**
 * Move `id` to position `index` of its zone (0-based; clamped to the first and last place).
 * Unchanged when `id` is not in the zone or is already there.
 */
export function moveWidgetTo(draft: LayoutDraft, zone: EditableZone, id: string, index: number): LayoutDraft {
  const order = zoneIds(draft, zone);
  const from = order.indexOf(id);
  if (from < 0 || Number.isNaN(index)) return draft;
  const to = Math.max(0, Math.min(order.length - 1, Math.trunc(index)));
  if (to === from) return draft;
  order.splice(from, 1);
  order.splice(to, 0, id);
  return withZoneOrder(draft, zone, order);
}

/** Take `id` off its zone. Unchanged when it is not there. */
export function removeWidget(draft: LayoutDraft, zone: EditableZone, id: string): LayoutDraft {
  if (zone === 'kpi') {
    return draft.kpis.includes(id) ? { ...draft, kpis: draft.kpis.filter(kpi => kpi !== id) } : draft;
  }
  return draft.panels.some(panel => panel.id === id)
    ? { ...draft, panels: draft.panels.filter(panel => panel.id !== id) }
    : draft;
}

/**
 * Append a KPI or panel to the END of its zone; a panel takes its `defaultSize`, else
 * `'half'` (the rule the Customize dialog had). Unchanged when the widget is already on the
 * dashboard, or is a banner (banners are switched on with `setBannerVisible`).
 */
export function addWidget(draft: LayoutDraft, definition: DashboardWidgetDefinition): LayoutDraft {
  if (definition.zone === 'kpi') {
    return draft.kpis.includes(definition.id) ? draft : { ...draft, kpis: [...draft.kpis, definition.id] };
  }
  if (definition.zone !== 'panel' || draft.panels.some(panel => panel.id === definition.id)) return draft;
  const size = isPanelSize(definition.defaultSize) ? definition.defaultSize : FALLBACK_PANEL_SIZE;
  return { ...draft, panels: [...draft.panels, { id: definition.id, size }] };
}

/** Change a panel's width. Unchanged when the panel is not on the dashboard or already has that size. */
export function setPanelSize(draft: LayoutDraft, id: string, size: PanelSize): LayoutDraft {
  const index = draft.panels.findIndex(panel => panel.id === id);
  if (index < 0 || draft.panels[index].size === size || !isPanelSize(size)) return draft;
  return {
    ...draft,
    panels: draft.panels.map((panel, position) => (position === index ? { ...panel, size } : panel)),
  };
}

/**
 * Switch a banner on or off (the former Customize dialog's semantics: on appends, off filters).
 * Banners always RENDER and SAVE in registry order whatever the draft order is
 * (`resolveLayout` / `buildSavePayload`). Unchanged when the banner is already in that state.
 */
export function setBannerVisible(draft: LayoutDraft, id: string, visible: boolean): LayoutDraft {
  const shown = draft.banners.includes(id);
  if (shown === visible) return draft;
  return {
    ...draft,
    banners: visible ? [...draft.banners, id] : draft.banners.filter(banner => banner !== id),
  };
}
