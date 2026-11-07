/**
 * The phone "My current work" card under owner ruling 1 (2026-11-05): a short count is
 * reported as scrap with a reason, or the operation stays open.
 *
 * "Remaining" is what is still UNACCOUNTED FOR — received (target less any upstream
 * short) less good less the OPERATION's scrap — so an operation whose short is covered
 * by recorded scrap offers Complete, `+1` stops at the server's good ceiling, and a
 * COMPLETE operation offers no Complete at all.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import MobileCurrentWork from './MobileCurrentWork';
import type { ActiveJob } from '../../types';

const JOB: ActiveJob = {
  time_entry_id: 501,
  clock_in: '2026-11-07T14:00:00Z',
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 101,
  work_order_number: 'WO-2026-0042',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_name: 'Deburr 1',
  quantity_ordered: 10,
  quantity_complete: 6,
  operation_quantity_scrapped: 0,
};

const noop = () => undefined;

function renderCard(job: Partial<ActiveJob> = {}, props: Partial<React.ComponentProps<typeof MobileCurrentWork>> = {}) {
  const selected = { ...JOB, ...job };
  return render(
    <MobileCurrentWork
      jobs={[selected]}
      selected={selected}
      onSelect={noop}
      onQueue={noop}
      onReport={noop}
      onAddOne={noop}
      onDrawing={noop}
      onNest={noop}
      onInstructions={noop}
      onHold={noop}
      onCheckOut={noop}
      onComplete={noop}
      disabled={false}
      operationAvailable
      elapsed="1m"
      operationStatus="in_progress"
      {...props}
    />
  );
}

describe('MobileCurrentWork — short count (owner ruling 1)', () => {
  it('shows +1 while pieces are still unaccounted for', () => {
    renderCard();
    expect(screen.getByText('4 remaining')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+1 Complete' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Complete operation' })).not.toBeInTheDocument();
  });

  it('counts the OPERATION scrap total as accounted for, and offers Complete once it covers the short', () => {
    // 8 good + 2 scrapped of 10: nothing left to record — closing it is a covered short.
    renderCard({ quantity_complete: 8, operation_quantity_scrapped: 2, quantity_scrapped: 0 });
    expect(screen.getByText('0 remaining')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Complete operation' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '+1 Complete' })).not.toBeInTheDocument();
  });

  it('ignores this session scrap count — only the operation total is accounted for', () => {
    // `quantity_scrapped` on my-active-job is the SESSION count; it must not close the gap.
    renderCard({ quantity_complete: 8, operation_quantity_scrapped: 0, quantity_scrapped: 2 });
    expect(screen.getByText('2 remaining')).toBeInTheDocument();
  });

  it('works from the received count when an upstream close declared a short', () => {
    // Target 10, 2 never reached this op: 8 received, 8 recorded → done.
    renderCard({ quantity_complete: 8, upstream_quantity_short: 2, quantity_good_ceiling: 8 });
    expect(screen.getByText('0 remaining')).toBeInTheDocument();
    expect(screen.getByText('/ 8')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Complete operation' })).toBeEnabled();
  });

  it("disables +1 at the server's good ceiling, even when the snapshot still shows pieces remaining", () => {
    // The ceiling is the server's figure and wins: here it already says 8 (an upstream
    // close landed between two polled reads), so /production would refuse a 9th piece.
    renderCard({ quantity_complete: 8, quantity_good_ceiling: 8 });
    expect(screen.getByText('2 remaining')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+1 Complete' })).toBeDisabled();
  });

  it('offers no Complete on an operation that is already COMPLETE', () => {
    renderCard({ quantity_complete: 10 }, { operationStatus: 'complete' });
    expect(screen.queryByRole('button', { name: 'Complete operation' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+1 Complete' })).not.toBeInTheDocument();
  });
});
