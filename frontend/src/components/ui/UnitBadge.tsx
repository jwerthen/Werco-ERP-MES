import React from 'react';

/**
 * "UNIT 2410048" — the build identity of a one-unit-per-work-order job.
 *
 * ONE implementation for every surface that shows a work order (the WO list row
 * and mobile card, the WO detail hero, the kiosk queue / clock-in / running-job
 * screens, the crew station, the held card, the dispatch board). Same reasoning as
 * `KioskJobNotes` and `KioskRunOrderChip`: an operator who reads "UNIT 2410048" on
 * the kiosk and again on the TV must be looking at the same badge, not two
 * hand-rolled chips that drifted apart.
 *
 * Two properties are behavior, not styling:
 *
 *  1. **Never an empty container.** A blank / whitespace-only / absent value renders
 *     `null` — no border, no label, no gap. Most work orders do not track a unit,
 *     and those must look exactly as they did before migration 083.
 *  2. **Cyan, not a status color.** `statusColors.ts` owns green/blue/amber/red/slate
 *     for status meaning. Identity is not a status, so the badge takes the one
 *     remaining Foundry accent — which is also what stops "UNIT" reading as an alert
 *     on a card that already carries a real status chip beside it.
 *
 * The `fd-*` tokens resolve CSS variables, so this renders correctly both in the
 * app and inside a `.fd-scope-kiosk` subtree without a second variant.
 */

const SIZE_CLASSES = {
  /** Dense table rows, mobile cards, dispatch cards. */
  sm: 'gap-1 rounded-[3px] px-1.5 py-0.5 text-[10px]',
  /** Kiosk queue cards, crew board cards, the WO detail hero. */
  md: 'gap-1.5 rounded-[3px] px-2 py-1 text-[13px]',
  /** Read at arm's length: the clock-in confirm and the running-job hero. */
  lg: 'gap-2 rounded-[4px] px-3 py-1.5 text-[22px]',
} as const;

const LABEL_CLASSES = {
  sm: 'text-[9px]',
  md: 'text-[10px]',
  lg: 'text-[13px]',
} as const;

export type UnitBadgeSize = keyof typeof SIZE_CLASSES;

export interface UnitBadgeProps {
  /** `WorkOrder.unit_number` — null/undefined/blank all render nothing. */
  unitNumber?: string | null;
  size?: UnitBadgeSize;
  className?: string;
}

export function UnitBadge({ unitNumber, size = 'md', className = '' }: UnitBadgeProps) {
  if (typeof unitNumber !== 'string') return null;
  const value = unitNumber.trim();
  if (!value) return null;

  return (
    <span
      data-testid="unit-badge"
      className={`inline-flex items-center border border-fd-cyan/45 bg-fd-cyan/10 font-mono font-bold leading-none text-fd-cyan ${SIZE_CLASSES[size]} ${className}`}
    >
      <span className={`font-semibold uppercase tracking-[0.12em] opacity-75 ${LABEL_CLASSES[size]}`}>Unit</span>
      <span className="tabular-nums">{value}</span>
    </span>
  );
}

export default UnitBadge;
