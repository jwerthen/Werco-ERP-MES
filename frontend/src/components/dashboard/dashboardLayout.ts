/**
 * Pure layout logic for the customizable dashboard: no React, no api.
 *
 * - `resolveLayout` turns a saved layout (or `null` = the stock default) into what the
 *   page renders for THIS viewer: unknown ids (a newer or older bundle), duplicates and
 *   widgets the viewer is not eligible for are dropped; banners keep registry order;
 *   KPIs and panels keep saved order; a panel size that is not a known PanelSize falls
 *   back to the widget's `defaultSize` (the widget is never dropped for its size).
 * - `buildSavePayload` turns the Customize dialog's draft (eligible widgets only) back
 *   into a full layout, CARRYING known-but-ineligible saved entries through unchanged so
 *   a temporary permission change never destroys a layout.
 * - `isSameLayout` compares two layouts the way the page would render them, so an
 *   untouched dialog is never "dirty".
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

/** What the Customize dialog edits: the viewer's ELIGIBLE widgets only. */
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

/** The dialog's starting draft: exactly what the page renders for this viewer. */
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
 * kept on save, and the dialog says so).
 *
 * Only a saved layout is counted: `null` (no saved layout — the stock default) is 0. And a
 * stock-default widget the viewer is not eligible for is never counted, saved or not: it
 * is in the layout because the default put it there (`buildSavePayload` carries it through
 * a non-admin's first save so an untouched dialog stays clean), not because the viewer
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

/** True when no widget at all would render. */
export function isEmptyLayout(resolved: ResolvedLayout): boolean {
  return resolved.banners.length === 0 && resolved.kpis.length === 0 && resolved.panels.length === 0;
}
