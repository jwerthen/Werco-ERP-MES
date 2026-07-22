/**
 * Foundry TV palette — the /wallboard board's own near-black surface set
 * (design handoff 2026-07-22). DELIBERATELY darker than the app shell's
 * --fd-* variables in index.css: the TV board is its own surface; keep this
 * palette wallboard-local and never fold it into the global CSS variables.
 * blockedOrange is a derived color (not a design-system token) that sits
 * between red and amber so BLOCKED reads distinctly from LATE at distance.
 */
export const FD = {
  /** page background */
  canvas: '#080a0f',
  /** panel surface */
  panel: '#0c1017',
  /** progress-bar track / recessed surfaces */
  sunken: '#070910',
  /** hairline */
  line: '#1a212c',
  /** primary text */
  ink: '#e6edf3',
  /** secondary text */
  body: '#9aa7b8',
  /** labels / de-emphasized text */
  mute: '#5b6677',
  /** faintest text, waiting card edge, dimmed-zero numerals */
  faint: '#3f4856',
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
