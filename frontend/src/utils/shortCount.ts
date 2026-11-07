/**
 * Short count at operation completion — the client's per-operation ARITHMETIC twin.
 *
 * Owner ruling 1 (2026-11-05): a short count at completion must be reported as scrap
 * with a required reason, or the operation stays open. It is never closed short at the
 * counted quantity. The server is the arbiter: `POST /shop-floor/operations/{id}/complete`
 * refuses 409 while any received unit is still unaccounted for, and writes nothing.
 *
 * This module mirrors ONE thing: the per-operation properties of the server's
 * `OperationAccounting` (`backend/app/services/work_order_state_service.py`), so a floor
 * screen can say "2 SHORT" before the operator taps, instead of discovering the 409.
 * `shortCount.parity.test.ts` holds the two together — change both, never one.
 *
 * What it deliberately does NOT do is the CHAIN logic (which upstream closes declared a
 * short, component feed, sequential-only main-chain credit). That stays server-only, one
 * implementation, and arrives on every floor payload already resolved:
 *
 * - `quantity_ordered`        — the operation TARGET (`operation_target_quantity`), unchanged;
 * - `upstream_quantity_short` — target units that never reached this operation;
 * - `quantity_good_ceiling`   — the most good the server will accept here.
 *
 * So `received = quantity_ordered − upstream_quantity_short`, and everything below is
 * arithmetic on that. A payload without the keys (an older backend, a stale fixture)
 * reads as "nothing lost upstream", which is exactly what every pre-ruling row is.
 */

/** The server's `QTY_EPSILON`. Float residue below this is zero. */
export const SHORT_EPSILON = 1e-9;

function finite(value: unknown): number {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/** The figures one operation's accounting is computed from. */
export interface ShortCountInput {
  /** The operation target (`quantity_ordered` on every floor payload). */
  target: number | null | undefined;
  /** `upstream_quantity_short` — target units that never reached this operation. */
  upstreamShort?: number | null;
  /** Good already recorded on the operation. */
  complete?: number | null;
  /** Scrap already recorded on the OPERATION (never a session count). */
  scrapped?: number | null;
}

/** A proposed entry on top of the recorded counts (good / scrap about to be sent). */
export interface ShortCountEntry extends ShortCountInput {
  good?: number | null;
  scrap?: number | null;
}

/** Units that reached the operation: `max(0, target − upstream short)`. */
export function operationReceived({
  target,
  upstreamShort,
}: Pick<ShortCountInput, 'target' | 'upstreamShort'>): number {
  return Math.max(0, finite(target) - Math.max(0, finite(upstreamShort)));
}

function clampResidue(value: number): number {
  return value > SHORT_EPSILON ? value : 0;
}

/** Received units recorded neither good nor scrapped — what a close must still account for. */
export function operationUnaccounted(input: ShortCountInput): number {
  return clampResidue(
    Math.max(0, operationReceived(input) - Math.max(0, finite(input.complete)) - Math.max(0, finite(input.scrapped)))
  );
}

/**
 * Still unaccounted AFTER this entry lands: received − (complete + good) − (scrapped + scrap).
 * > 0 means a Complete right now would be refused with the server's short-count 409.
 */
export function shortAfterEntry(input: ShortCountEntry): number {
  return operationUnaccounted({
    ...input,
    complete: Math.max(0, finite(input.complete)) + Math.max(0, finite(input.good)),
    scrapped: Math.max(0, finite(input.scrapped)) + Math.max(0, finite(input.scrap)),
  });
}

/**
 * What a close right now would DECLARE short: received − good (scrap covers it, or the
 * close is refused). > 0 means the close is a SHORT close, which the server refuses while
 * another operator is still clocked in, and for a standing API token.
 */
export function closingShortAfterEntry(input: ShortCountEntry): number {
  return clampResidue(
    Math.max(0, operationReceived(input) - Math.max(0, finite(input.complete)) - Math.max(0, finite(input.good)))
  );
}

/**
 * The most good the server will accept here. The payload's `quantity_good_ceiling` when
 * it carries one, else the target (no upstream credit is the pre-ruling reading).
 */
export function goodCeiling(target: number | null | undefined, ceiling?: number | null): number {
  if (ceiling != null && Number.isFinite(Number(ceiling))) return Math.max(0, Number(ceiling));
  return Math.max(0, finite(target));
}

/** "run" / "runs" on a laser nest (its target is sheet runs), "pc" / "pcs" everywhere else. */
export function shortUnitLabel(isNest: boolean, count: number): string {
  const one = Math.abs(count - 1) <= SHORT_EPSILON;
  if (isNest) return one ? 'run' : 'runs';
  return one ? 'pc' : 'pcs';
}

/** A figure as the floor reads it: whole numbers stay whole, residue is trimmed. */
export function formatShortQuantity(value: number): string {
  const rounded = Math.round(value * 1e6) / 1e6;
  return String(rounded);
}

/**
 * The operation's status after a clock-out, from the clock-out response's
 * `operation_status` ("complete" when that clock-out completed the operation on its own,
 * a full good count). Null when the response does not carry it (an older backend).
 */
export function clockOutOperationStatus(res: unknown): string | null {
  if (res == null || typeof res !== 'object') return null;
  const status = (res as { operation_status?: unknown }).operation_status;
  return typeof status === 'string' && status ? status.toLowerCase() : null;
}
