/**
 * ANDON WALL color tokens — one palette, one meaning, every zone (spec §6).
 * Hairlines are STRUCTURE only; state is carried by large filled color fields
 * or ≥3rem colored numerals. Navy/brand-red are brand accents, never status.
 */
export const WB = {
  bg: '#070a0f',
  panel: '#141b26',
  panelDim: '#10151d',
  hairline: '#243042',
  text: '#f0f4f9',
  muted: '#8b98a9',
  /** running / on-time / complete / all-clear */
  green: '#3fb950',
  /** idle / off-shift / no-data — dim, informational, never alarmed */
  slate: '#5b6878',
  /** schedule attention: late, queue ≥5, quality >0, SHIP behind, offline stage 1 */
  amber: '#d29922',
  /** BLOCKED, everywhere */
  orange: '#f0883e',
  /** DOWN / hard failure only; SHIP past-noon escalation; offline stage 2 */
  red: '#f04438',
  /** brand accent only (SHIP header rule, wordmark contexts) — never a status */
  navy: '#1B4D9C',
  /** wordmark dot only */
  brandRed: '#C8352B',
} as const;
