/**
 * Telling a keyboard-wedge badge scanner from a person at the keys.
 *
 * A wedge scanner "types" a badge as a burst — every character a few milliseconds
 * apart, usually followed by Enter. The floor-PIN pad needs to know: while it is up
 * the PIN field has focus and the page's badge capture is paused, so the next badge
 * scanned at the station would land in the field and its Enter would submit it as a
 * PIN — a wrong PIN charged against the operator waiting at the pad, and five of
 * those lock them out. The pad uses this to discard such a burst instead.
 *
 * Pure: takes keystroke times in milliseconds (any monotonic clock), oldest first,
 * and says whether they arrived faster than a person types.
 */

/** Fewest keystrokes that can count as a scan (badges are at least 4 characters). */
export const SCAN_BURST_MIN_KEYS = 4;
/** That many keystrokes inside this window is a scanner: ~15 ms a key, well beyond human typing. */
export const SCAN_BURST_WINDOW_MS = 60;
/** A whole run averaging this or less between keys is a scanner too (a person manages ~100 ms+). */
export const SCAN_MAX_AVG_INTERVAL_MS = 30;
/** After a discarded burst, keys that keep arriving closer together than this are the same scan. */
export const SCAN_TRAILING_QUIET_MS = 150;

/**
 * True when `keyTimes` look like a scanner: any `SCAN_BURST_MIN_KEYS` consecutive
 * keystrokes within `SCAN_BURST_WINDOW_MS`, or the whole run (at least
 * `SCAN_BURST_MIN_KEYS` long) averaging `SCAN_MAX_AVG_INTERVAL_MS` or less per key.
 * The window rule catches a scan that lands after a person has already typed a digit
 * or two; the average rule catches a slower scanner on a longer badge.
 */
export function isScanBurst(keyTimes: readonly number[]): boolean {
  const n = keyTimes.length;
  if (n < SCAN_BURST_MIN_KEYS) return false;
  for (let i = SCAN_BURST_MIN_KEYS - 1; i < n; i += 1) {
    if (keyTimes[i] - keyTimes[i - SCAN_BURST_MIN_KEYS + 1] <= SCAN_BURST_WINDOW_MS) return true;
  }
  return (keyTimes[n - 1] - keyTimes[0]) / (n - 1) <= SCAN_MAX_AVG_INTERVAL_MS;
}
