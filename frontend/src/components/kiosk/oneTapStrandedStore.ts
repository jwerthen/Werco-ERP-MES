import type { StrandedOneTapDelta } from './useOneTapPieces';

/**
 * Pieces the one-tap lane tapped but could never send — held across a reload so
 * they are not simply gone.
 *
 * A delta becomes stranded when the kiosk goes away while it is un-bankable: the
 * badge token expired and the operator never came back, the station was offline
 * at the moment the tab closed, or the last attempt failed ambiguously and no
 * human resolved it. Those pieces were MADE. Dropping them on unmount loses real
 * production silently, which is the one outcome this feature exists to prevent,
 * so the count and — critically — the pair that made it are written here and
 * surfaced on the next load.
 *
 * Two things about this store are deliberate:
 *
 * 1. IT IS NOT A RETRY QUEUE, and nothing here ever posts on its own. A stranded
 *    delta already failed under unknown circumstances (the endpoint is additive
 *    with no idempotency key, so its request may have landed), and the operator
 *    who made the pieces is by then usually gone. Resurrecting it automatically
 *    would either double-count or attribute one person's work to whoever is
 *    standing there. It is a NOTICE — a human reads it and decides.
 *
 * 2. IT LIVES IN sessionStorage, beside the station token, NOT localStorage.
 *    The record names an operator and a job, so it should die with the tab
 *    rather than sit on a shared shop tablet indefinitely; and a delta that has
 *    outlived the browser session is far too old to act on safely anyway.
 */

const STORAGE_KEY = 'kiosk_onetap_stranded';
/** A cap, so a station that strands repeatedly cannot grow this without bound. */
const MAX_RECORDS = 20;

/** Which kiosk stranded it — the two surfaces must not read each other's notices. */
export type OneTapSurface = 'crew' | 'operator';

export interface StrandedOneTapRecord extends StrandedOneTapDelta {
  surface: OneTapSurface;
  /** ISO-8601 UTC. Rendered through `centralTime` like every other timestamp. */
  at: string;
}

function readAll(): StrandedOneTapRecord[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (r): r is StrandedOneTapRecord =>
        typeof r === 'object' && r != null && typeof (r as StrandedOneTapRecord).pieces === 'number'
    );
  } catch {
    // sessionStorage unavailable, or a corrupt value: a missing notice must not
    // take the kiosk down with it.
    return [];
  }
}

function writeAll(records: StrandedOneTapRecord[]): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(records.slice(-MAX_RECORDS)));
  } catch {
    /* nothing useful to do — the in-page notice already fired */
  }
}

/** Notices for one surface, oldest first. */
export function readStranded(surface: OneTapSurface): StrandedOneTapRecord[] {
  return readAll().filter((r) => r.surface === surface);
}

/**
 * Record a delta that could not be sent. Deltas for the SAME pair accumulate
 * into one notice rather than stacking: they are the same operator's pieces on
 * the same operation, and two notices would read as two separate incidents.
 */
export function addStranded(surface: OneTapSurface, delta: StrandedOneTapDelta): void {
  if (delta.pieces <= 0) return;
  const all = readAll();
  const existing = all.find((r) => r.surface === surface && r.key === delta.key);
  if (existing) {
    existing.pieces += delta.pieces;
    existing.at = new Date().toISOString();
    writeAll(all);
    return;
  }
  all.push({ ...delta, surface, at: new Date().toISOString() });
  writeAll(all);
}

/**
 * Drop a notice — either because the pieces were finally banked under their own
 * pair, or because a human read what was being lost and dismissed it.
 */
export function clearStranded(surface: OneTapSurface, key: string): void {
  writeAll(readAll().filter((r) => !(r.surface === surface && r.key === key)));
}
