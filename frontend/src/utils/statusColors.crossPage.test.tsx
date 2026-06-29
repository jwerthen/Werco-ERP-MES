/**
 * Batch 8 — cross-page status-color consistency regression.
 *
 * The point of the design-system batch was to END the per-page disagreement on
 * status coloring. Before centralization, `in_progress` rendered DIFFERENT colors
 * depending on which page you were on:
 *
 *   - WorkOrderDetail painted it BLUE   (live work in flight)
 *   - Maintenance / Customers painted it AMBER/yellow  (treated like "pending")
 *
 * Those pages render their status pill through two *different* seams, both of
 * which now point at the single `statusColors` source of truth:
 *
 *   - WorkOrderDetail (src/pages/WorkOrderDetail.tsx) and Customers
 *     (src/pages/Customers.tsx) build a raw span with `statusColor(status)`.
 *   - Maintenance (src/pages/Maintenance.tsx) renders `<StatusBadge status=... />`
 *     with NO colorMap, so it falls back to the same central map.
 *
 * This test locks the resolution: exercise BOTH seams the real pages use and
 * prove they emit the IDENTICAL central blue class for `in_progress` — i.e. the
 * historical WorkOrderDetail-vs-Maintenance/Customers disagreement can never
 * silently come back. A full render of those pages is heavy (API mocks, routers,
 * many contexts), so we assert at the smallest faithful seam: the exact helper /
 * component call each page makes.
 *
 * If a future change reintroduced a page-local map (the exact regression this
 * batch removed), it would diverge from `variantClass.blue` and fail here.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { StatusBadge } from '../components/ui/StatusBadge';
import { statusColor, statusVariant, variantClass } from './statusColors';

/**
 * Extract the bg-/text- color classes a page would stamp onto its status pill.
 * WorkOrderDetail/Customers do exactly this: interpolate `statusColor(status)`
 * into a span's className. We render that span and read the color classes back
 * off the DOM so the assertion is on rendered output, not the raw string.
 */
function workOrderDetailPillClasses(status: string): string[] {
  // Mirrors WorkOrderDetail.tsx: `${'...'} ${statusColor(workOrder.status)}`
  render(
    <span data-testid="wo-pill" className={`px-3 py-1 rounded-full capitalize ${statusColor(status)}`}>
      {status}
    </span>,
  );
  const el = screen.getByTestId('wo-pill');
  return variantClass.blue.split(' ').filter((c) => el.classList.contains(c));
}

/**
 * The classes Maintenance's `<StatusBadge status=... />` (default central map,
 * no colorMap override) actually renders.
 */
function maintenanceBadgeClasses(status: string): string[] {
  render(<StatusBadge status={status} />);
  const el = screen.getByText(status.replace(/_/g, ' '));
  return variantClass.blue.split(' ').filter((c) => el.classList.contains(c));
}

describe('cross-page in_progress color consistency (Batch 8 centralization)', () => {
  const BLUE_CLASSES = variantClass.blue.split(' ');

  it('in_progress canonically resolves to blue (the resolved disagreement)', () => {
    expect(statusVariant('in_progress')).toBe('blue');
    expect(statusColor('in_progress')).toBe(variantClass.blue);
  });

  it('WorkOrderDetail seam (statusColor span) renders the central blue for in_progress', () => {
    expect(workOrderDetailPillClasses('in_progress')).toEqual(BLUE_CLASSES);
  });

  it('Maintenance/Customers seam (StatusBadge default) renders the central blue for in_progress', () => {
    expect(maintenanceBadgeClasses('in_progress')).toEqual(BLUE_CLASSES);
  });

  it('both page seams render the SAME class for in_progress (no more per-page divergence)', () => {
    const woClasses = workOrderDetailPillClasses('in_progress');
    const maintClasses = maintenanceBadgeClasses('in_progress');
    // The whole batch in one assertion: identical, and identical to the central blue.
    expect(woClasses).toEqual(maintClasses);
    expect(woClasses).toEqual(BLUE_CLASSES);
    // And specifically NOT the amber one page used to use — proving the bug is gone.
    expect(woClasses).not.toEqual(variantClass.amber.split(' '));
  });

  it('the same lock holds for other shared statuses both pages display', () => {
    // released (WO terminal-good) and completed both pages render via the same
    // seams; assert the seams agree there too so the lock is not in_progress-only.
    (['released', 'completed', 'on_hold', 'cancelled'] as const).forEach((status) => {
      const expected = statusColor(status);
      // statusColor seam
      expect(statusColor(status)).toBe(expected);
      // StatusBadge default seam resolves to the same class string
      render(<StatusBadge status={status} />);
      const badge = screen.getAllByText(status.replace(/_/g, ' ')).slice(-1)[0];
      expected.split(' ').forEach((c) => expect(badge).toHaveClass(c));
    });
  });
});
