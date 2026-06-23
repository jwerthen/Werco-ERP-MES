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
    expect(screen.getByText(/3 \/ 8 runs/)).toBeInTheDocument();
    expect(screen.getByText(/AL 6061/)).toBeInTheDocument();
    // A "PDF" chip flags that a reference drawing is attached.
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('omits the nest block for a non-laser operation', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={baseItem} />);
    expect(screen.queryByText(/CNC#/)).not.toBeInTheDocument();
  });
});
