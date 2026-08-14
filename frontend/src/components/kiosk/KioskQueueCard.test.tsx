import React from 'react';
import { render, screen } from '@testing-library/react';
import KioskQueueCard from './KioskQueueCard';
import { KioskQueueItem } from './kioskConstants';

const baseItem: KioskQueueItem = {
  operation_id: 1,
  work_order_id: 10,
  work_order_number: 'WO-1001',
  part_number: 'P-500',
  part_name: 'Bracket',
  operation_number: 20,
  operation_name: 'Laser cut',
  work_center_id: 3,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 1,
  due_date: null,
};

describe('KioskQueueCard laser-nest surfacing', () => {
  it('renders the CNC number and runs progress when a nest is present', () => {
    render(
      <KioskQueueCard
        onSelect={jest.fn()}
        item={{
          ...baseItem,
          laser_nest: {
            id: 7,
            nest_name: 'Nest 7',
            cnc_number: '4455',
            cnc_file_name: null,
            planned_runs: 8,
            completed_runs: 3,
            remaining_runs: 5,
            material: 'AL 6061',
            thickness: '0.090"',
            sheet_size: null,
            has_document: true,
          },
        }}
      />
    );

    expect(screen.getByText(/CNC# 4455/)).toBeInTheDocument();
    expect(screen.getByText(/3\/8 runs/)).toBeInTheDocument();
    expect(screen.getByText(/AL 6061/)).toBeInTheDocument();
    // A "PDF" chip flags that a reference drawing is attached.
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('omits the nest block for a non-laser operation', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={baseItem} />);
    expect(screen.queryByText(/CNC#/)).not.toBeInTheDocument();
  });
});

/**
 * The card's ACCESSIBLE NAME, against the permanently mixed spellings of
 * `operation_number` (legacy rows hold `Op 20`, new rows hold `20`; the mint was
 * fixed forward with no backfill).
 *
 * The visible line has run through `formatOperationLabel` since PR #227. The
 * aria-label interpolated the raw column, so a screen reader announced
 * "operation Op 20" for a legacy row and "operation 20" for its twin — the same
 * operation named two ways on the same card, for the operator least able to
 * cross-check it against the visible text.
 */
describe('KioskQueueCard accessible name', () => {
  const nameOf = (item: Partial<KioskQueueItem>) => {
    const { unmount } = render(<KioskQueueCard onSelect={jest.fn()} item={{ ...baseItem, ...item } as KioskQueueItem} />);
    const label = screen.getByRole('button').getAttribute('aria-label') || '';
    unmount();
    return label;
  };

  it('announces the same operation identically for both stored spellings', () => {
    // `operation_name` is absent, so the number is what names the operation.
    const legacy = nameOf({ operation_number: 'Op 20', operation_name: '' });
    const current = nameOf({ operation_number: '20', operation_name: '' });

    expect(legacy).toBe('Work order WO-1001, operation 20');
    expect(legacy).toBe(current);
    expect(legacy).not.toMatch(/Op\s*20/);
  });

  it('still prefers the operation NAME when the server sends one', () => {
    expect(nameOf({ operation_number: 'Op 20', operation_name: 'Laser cut' })).toBe(
      'Work order WO-1001, operation Laser cut'
    );
  });

  it('announces a laser nest as a nest, matching the visible label', () => {
    // `laser_nest_service` writes `Nest {index}` into this same column.
    expect(nameOf({ operation_number: 'Nest 3', operation_name: '' })).toBe(
      'Work order WO-1001, operation Nest 3'
    );
  });

  it('renders a nest label without an "Op" in front of it', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={{ ...baseItem, operation_number: 'Nest 3' }} />);
    const card = screen.getByRole('button');
    expect(card).toHaveTextContent('Nest 3');
    expect(card).not.toHaveTextContent(/Op\s*Nest/);
  });
});
