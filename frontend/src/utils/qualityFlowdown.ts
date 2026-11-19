/**
 * Small pure helpers for the purchase-order quality flow-down (AS9100D 8.4.3).
 * See types/poFlowdown.ts for the wire shapes.
 */
import type { POQualityClause } from '../types/poFlowdown';

/** Catalog order: sort_order, then code — the order the server prints clauses in. */
export function sortQualityClauses<T extends Pick<POQualityClause, 'code' | 'sort_order'>>(clauses: T[]): T[] {
  return [...clauses].sort((a, b) => a.sort_order - b.sort_order || a.code.localeCompare(b.code));
}

/** Codes of the active, default-selected clauses, in catalog order. */
export function defaultQualityClauseCodes(clauses: POQualityClause[]): string[] {
  return sortQualityClauses(clauses.filter(clause => clause.is_active && clause.default_selected)).map(
    clause => clause.code
  );
}

/** The same set of codes, ignoring order and duplicates. */
export function sameQualityClauseCodes(a: readonly string[], b: readonly string[]): boolean {
  const left = new Set(a);
  const right = new Set(b);
  if (left.size !== right.size) return false;
  return Array.from(left).every(code => right.has(code));
}

/** A FastAPI `detail` as display text (the client interceptor already flattens 422 arrays). */
export function flowdownErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}

/** The HTTP status of an Axios-shaped error, if any. */
export function flowdownErrorStatus(err: unknown): number | undefined {
  const status = (err as { response?: { status?: unknown } })?.response?.status;
  return typeof status === 'number' ? status : undefined;
}
