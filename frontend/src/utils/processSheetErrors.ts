/**
 * Structured-detail parsing for the Process Sheets capture endpoints.
 *
 * The shop-floor step endpoints refuse with OBJECT `detail` payloads for two
 * cases the UI must render richly (never as JSON.stringify soup):
 *  - 409 {code:"OUT_OF_TOLERANCE", detail, measured, lsl, usl} — NO record was
 *    written; the kiosk shows the danger strip with measured vs limits.
 *  - 409 {code:"STEPS_INCOMPLETE", detail, missing:[{step_id,label,serials}]}
 *    from BOTH complete endpoints (shop-floor and office work-orders) — the
 *    caller surfaces exactly which steps/serials are outstanding.
 *
 * These helpers accept BOTH error shapes in the app: axios errors
 * (`err.response.data.detail`) from services/api.ts and KioskApiError
 * (`err.detail` / `err.status`) from services/kioskStationClient.ts.
 */

import type { MissingStepInfo, StepsBypassedInfo } from '../types/processSheet';

/** Pull the JSON `detail` from an axios error or a KioskApiError. */
export function extractApiErrorDetail(err: unknown): unknown {
  if (!err || typeof err !== 'object') return null;
  const direct = (err as { detail?: unknown }).detail;
  if (direct != null) return direct;
  const nested = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  return nested ?? null;
}

/** HTTP status from an axios error or a KioskApiError, when known. */
export function extractErrorStatus(err: unknown): number | null {
  if (!err || typeof err !== 'object') return null;
  const direct = (err as { status?: unknown }).status;
  if (typeof direct === 'number') return direct;
  const nested = (err as { response?: { status?: unknown } }).response?.status;
  return typeof nested === 'number' ? nested : null;
}

export interface OutOfToleranceInfo {
  measured: number;
  lsl: number;
  usl: number;
  /** The server's human sentence (inner `detail`), verbatim. */
  message: string;
}

/** Parse a 409 OUT_OF_TOLERANCE refusal; null for any other error. */
export function extractOutOfTolerance(err: unknown): OutOfToleranceInfo | null {
  const detail = extractApiErrorDetail(err);
  if (!detail || typeof detail !== 'object') return null;
  const d = detail as { code?: unknown; detail?: unknown; measured?: unknown; lsl?: unknown; usl?: unknown };
  if (d.code !== 'OUT_OF_TOLERANCE') return null;
  const measured = Number(d.measured);
  const lsl = Number(d.lsl);
  const usl = Number(d.usl);
  const message =
    typeof d.detail === 'string' && d.detail.trim()
      ? d.detail
      : `Measured ${measured} is outside tolerance (${lsl} to ${usl})`;
  return { measured, lsl, usl, message };
}

/** Parse one STEPS_INCOMPLETE payload ({code, missing}) into its missing-step list. */
function parseStepsIncompleteDetail(detail: unknown): MissingStepInfo[] | null {
  if (!detail || typeof detail !== 'object') return null;
  const d = detail as { code?: unknown; missing?: unknown };
  if (d.code !== 'STEPS_INCOMPLETE' || !Array.isArray(d.missing)) return null;
  return (d.missing as Array<{ step_id?: unknown; label?: unknown; serials?: unknown }>).map((m) => ({
    step_id: Number(m.step_id),
    label: typeof m.label === 'string' && m.label ? m.label : `Step ${m.step_id}`,
    serials: Array.isArray(m.serials) ? (m.serials as unknown[]).map(String) : [],
  }));
}

/** Parse a 409 STEPS_INCOMPLETE refusal into its missing-step list; null otherwise. */
export function extractStepsIncomplete(err: unknown): MissingStepInfo[] | null {
  return parseStepsIncompleteDetail(extractApiErrorDetail(err));
}

/**
 * Parse the backward-compatible `steps_incomplete` field a SUCCESSFUL clock-out
 * (TimeEntryResponse) may now carry: the clock-out quantity reached target but
 * required step records are missing, so the operation DELIBERATELY stays
 * IN_PROGRESS. This is NOT an error — the TimeEntry closed normally and the
 * labor was recorded fine; callers must surface it as info, never as a failure.
 */
export function extractClockOutStepsIncomplete(response: unknown): MissingStepInfo[] | null {
  if (!response || typeof response !== 'object') return null;
  return parseStepsIncompleteDetail((response as { steps_incomplete?: unknown }).steps_incomplete);
}

/** Info line (never an error tone) for a clock-out that leaves required step records outstanding. */
export function clockedOutStepsMessage(missing: MissingStepInfo[]): string {
  // Count outstanding record SLOTS (one per missing serial; one for a
  // non-serialized step) — "records still needed", not step definitions.
  const slots = missing.reduce((n, m) => n + Math.max(1, m.serials.length), 0);
  return `Clocked out — ${slots} step record${slots === 1 ? '' : 's'} still needed before this operation can complete.`;
}

/** Human toast line for a STEPS_INCOMPLETE refusal (labels + outstanding serials). */
export function stepsIncompleteMessage(missing: MissingStepInfo[]): string {
  const items = missing.map((m) => (m.serials.length > 0 ? `${m.label} (${m.serials.join(', ')})` : m.label));
  return `Required process steps are missing records: ${items.join('; ')}`;
}

/**
 * Parse the `steps_bypassed` summary a SUCCESSFUL WO-level complete
 * (POST /work-orders/{id}/complete) may carry: an authorized force-complete
 * bypassed required step records — a deliberate, audited override. NOT an
 * error (the action succeeded by design); null when nothing was bypassed.
 */
export function extractStepsBypassed(response: unknown): StepsBypassedInfo | null {
  if (!response || typeof response !== 'object') return null;
  const raw = (response as { steps_bypassed?: unknown }).steps_bypassed;
  if (!raw || typeof raw !== 'object') return null;
  const d = raw as { count?: unknown; steps?: unknown; truncated?: unknown };
  const count = Number(d.count);
  if (!Number.isFinite(count) || count <= 0) return null;
  const steps = Array.isArray(d.steps)
    ? (d.steps as Array<{ operation?: unknown; step_id?: unknown; label?: unknown; serials?: unknown }>).map((s) => ({
        operation: typeof s.operation === 'string' ? s.operation : '',
        step_id: Number(s.step_id),
        label: typeof s.label === 'string' && s.label ? s.label : `Step ${s.step_id}`,
        serials: Array.isArray(s.serials) ? (s.serials as unknown[]).map(String) : [],
      }))
    : [];
  return { count, steps, truncated: Boolean(d.truncated) };
}

/** Notice line (info/warning tone, never error) for a force-complete that bypassed step records. */
export function stepsBypassedMessage(info: StepsBypassedInfo): string {
  const labels = Array.from(new Set(info.steps.map((s) => s.label)));
  const suffix = labels.length > 0 ? `: ${labels.join(', ')}${info.truncated ? ', …' : ''}` : '';
  return `Completed with ${info.count} step record${info.count === 1 ? '' : 's'} bypassed${suffix}`;
}
