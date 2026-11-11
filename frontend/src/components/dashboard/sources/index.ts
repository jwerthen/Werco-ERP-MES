/**
 * Every data source the dashboard's keyed loader can fetch: the core specs (sections the
 * dashboard showed before customization) merged with the opt-in widgets' specs.
 * `GET /shop-floor/dashboard` is not a source — it is the page heartbeat.
 */
import type { DashboardSourceKey, SourceSpec } from '../widgetTypes';
import { CORE_SOURCE_SPECS } from './coreSources';
import { EXTRA_SOURCE_SPECS } from './extraSources';

export const SOURCE_SPECS: Record<DashboardSourceKey, SourceSpec> = { ...CORE_SOURCE_SPECS, ...EXTRA_SOURCE_SPECS };

/** Spec lookup that cannot hit `Object.prototype` (keys arrive from widget definitions). */
export function getSourceSpec(
  key: string,
  specs: Partial<Record<string, SourceSpec>> = SOURCE_SPECS
): SourceSpec | undefined {
  return Object.prototype.hasOwnProperty.call(specs, key) ? specs[key] : undefined;
}
