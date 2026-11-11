/**
 * The opt-in dashboard panels (agent F2 owns this module).
 *
 * STUB — replaced by the real catalog. The page and the registry import these two names;
 * keep the names and types stable.
 */
import type { DashboardWidgetComponent, DashboardWidgetDefinition } from '../../widgetTypes';

/** Definitions for the opt-in panels, in the order the Customize dialog lists them. */
export const EXTRA_WIDGETS: readonly DashboardWidgetDefinition[] = [];

/** id → renderer for every entry in EXTRA_WIDGETS. A Map, never a plain object. */
export const EXTRA_WIDGET_RENDERERS: ReadonlyMap<string, DashboardWidgetComponent> = new Map();
