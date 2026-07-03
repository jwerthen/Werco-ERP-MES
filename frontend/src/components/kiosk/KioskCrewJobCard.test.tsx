import React from 'react';
import { render, screen } from '@testing-library/react';
import KioskCrewJobCard from './KioskCrewJobCard';
import { KioskCrewQueueItem } from './kioskConstants';

const CLOCK_IN_BOB = '2026-07-02T15:00:00Z';
const CLOCK_IN_CHARLIE = '2026-07-02T16:22:00Z';
// "Now": Bob has been on 2h10m33s, Charlie 48m33s.
const NOW_MS = Date.parse('2026-07-02T17:10:33Z');

const ITEM: KioskCrewQueueItem = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 37,
  quantity_scrapped: 2,
  priority: 5,
  due_date: null,
  roster: [
    { time_entry_id: 501, user_id: 11, operator_name: 'Bob T', employee_id: 'E011', entry_type: 'run', clock_in: CLOCK_IN_BOB },
    { time_entry_id: 502, user_id: 12, operator_name: 'Charlie M', employee_id: 'E012', entry_type: 'setup', clock_in: CLOCK_IN_CHARLIE },
  ],
};

describe('KioskCrewJobCard', () => {
  it('shows the operation-level crew tally including scrap (the double-count guard)', () => {
    render(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS} onSelect={jest.fn()} />);
    expect(screen.getByTestId('kiosk-crew-tally')).toHaveTextContent('37 of 50 · 2 scrap');
  });

  it('omits the scrap segment when nothing has been scrapped', () => {
    render(
      <KioskCrewJobCard item={{ ...ITEM, quantity_scrapped: 0 }} nowMs={NOW_MS} onSelect={jest.fn()} />
    );
    expect(screen.getByTestId('kiosk-crew-tally')).toHaveTextContent('37 of 50');
    expect(screen.getByTestId('kiosk-crew-tally')).not.toHaveTextContent('scrap');
  });

  it('renders a NON-interactive roster chip per open entry with a live per-person timer', () => {
    render(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS} onSelect={jest.fn()} />);

    const roster = screen.getByRole('list', { name: /crew clocked in/i });
    const chips = screen.getAllByRole('listitem');
    expect(roster).toBeInTheDocument();
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent('Bob T');
    expect(chips[0]).toHaveTextContent('02:10:33');
    expect(chips[1]).toHaveTextContent('Charlie M');
    expect(chips[1]).toHaveTextContent('00:48:33');
    // Chips are informational, not buttons — joining/leaving goes through the badge scan.
    expect(chips[0].querySelector('button')).toBeNull();
  });

  it('ticks the timers as nowMs advances (parent drives the 1s ticker)', () => {
    jest.useFakeTimers();
    try {
      const { rerender } = render(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS} onSelect={jest.fn()} />);
      expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('02:10:33');

      // One minute later on the (skew-corrected) clock.
      jest.advanceTimersByTime(60_000);
      rerender(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS + 60_000} onSelect={jest.fn()} />);
      expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('02:11:33');
      expect(screen.getAllByRole('listitem')[1]).toHaveTextContent('00:49:33');
    } finally {
      jest.useRealTimers();
    }
  });

  it('tags SETUP entries so run vs setup labor is visible at a glance', () => {
    render(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS} onSelect={jest.fn()} />);
    const chips = screen.getAllByRole('listitem');
    expect(chips[1]).toHaveTextContent(/setup/i);
    expect(chips[0]).not.toHaveTextContent(/setup/i);
  });

  it('preserves the laser-nest summary block', () => {
    render(
      <KioskCrewJobCard
        item={{
          ...ITEM,
          laser_nest: {
            id: 88,
            nest_name: 'NEST-88',
            cnc_number: 'CNC-1042',
            planned_runs: 4,
            completed_runs: 1,
            remaining_runs: 3,
            material: '304 SS',
            thickness: '11GA',
            has_document: true,
          },
        }}
        nowMs={NOW_MS}
        onSelect={jest.fn()}
      />
    );
    expect(screen.getByText('CNC# CNC-1042')).toBeInTheDocument();
    expect(screen.getByText('1 / 4 runs')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('is one giant tap target that reports the crew count to screen readers', () => {
    const onSelect = jest.fn();
    render(<KioskCrewJobCard item={ITEM} nowMs={NOW_MS} onSelect={onSelect} />);
    const card = screen.getByRole('button', { name: /WO-2026-0142.*2 clocked in/i });
    card.click();
    expect(onSelect).toHaveBeenCalledWith(ITEM);
  });
});
