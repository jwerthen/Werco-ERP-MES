/**
 * Foundry TV palette — the /wallboard board's own near-black surface set
 * (design handoff 2026-07-22). DELIBERATELY darker than the app shell's
 * --fd-* variables in index.css: the TV board is its own surface; keep this
 * palette wallboard-local and never fold it into the global CSS variables.
 * blockedOrange is a derived color (not a design-system token) that sits
 * between red and amber so BLOCKED reads distinctly from LATE at distance.
 *
 * TEXT CONTRAST (2026-07-23): the surfaces stay near-black (that dark canvas is
 * the intentional look), but the text/label/hairline tokens were brightened so
 * the board is legible on a wall TV under office lighting — the original grays
 * (mute #5b6677 ≈ 2.7:1, faint #3f4856 ≈ 1.5:1 on panel) washed out against
 * ambient glare. Do NOT re-darken these below AA-ish on #0c1017 without a
 * readability check on real hardware.
 */
export const FD = {
  /** page background */
  canvas: '#080a0f',
  /** panel surface */
  panel: '#0c1017',
  /** progress-bar track / recessed surfaces */
  sunken: '#070910',
  /** hairline (brightened for panel separation under glare) */
  line: '#2a323f',
  /** primary text */
  ink: '#eef3f8',
  /** secondary text (brightened) */
  body: '#c3ccd8',
  /** labels / de-emphasized text (brightened — was too dark to read on a TV) */
  mute: '#94a1b2',
  /** faintest text, waiting card edge, dimmed-zero numerals (brightened) */
  faint: '#6b7686',
  /** DOWN */
  red: '#f04438',
  /** LATE */
  amber: '#d29922',
  /** RUNNING / on-track */
  green: '#3fb950',
  /** brand accent (SHIP TODAY rule, TODAY label) — never a status */
  blue: '#2f81f7',
  /** WAITING chip / bar fill */
  waiting: '#8b98a5',
  /** BLOCKED (derived, between red and amber) */
  blockedOrange: '#ea7d2c',
} as const;
