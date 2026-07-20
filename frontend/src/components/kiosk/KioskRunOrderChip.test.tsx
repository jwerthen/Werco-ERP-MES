/**
 * KioskRunOrderChip — the manager-dictated run order shown to operators.
 *
 * One shared chip lights up BOTH shop-floor surfaces: the single-operator kiosk
 * queue card and the crew-station job card. It only DISPLAYS the server-assigned
 * rank (advisory — any job can still be started) and renders nothing when the
 * operation is unranked.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import KioskQueueCard from './KioskQueueCard';
import KioskCrewJobCard from './KioskCrewJobCard';
import { KioskCrewQueueItem, KioskQueueItem } from './kioskConstants';

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

const crewItem: KioskCrewQueueItem = { ...baseItem, roster: [] };

describe('KioskRunOrderChip on the operator kiosk queue card', () => {
  it('shows the rank when the operation is ranked', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={{ ...baseItem, run_order: 3 }} />);

    const chip = screen.getByTestId('kiosk-run-order-chip');
    expect(chip).toHaveTextContent('Run');
    expect(chip).toHaveTextContent('3');
    expect(chip).toHaveAttribute('aria-label', 'Run order 3');
  });

  it('renders nothing when run_order is null', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={{ ...baseItem, run_order: null }} />);
    expect(screen.queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });

  it('renders nothing when the server omits run_order entirely', () => {
    render(<KioskQueueCard onSelect={jest.fn()} item={baseItem} />);
    expect(screen.queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });
});

describe('KioskRunOrderChip on the crew-station job card', () => {
  it('shows the rank when the operation is ranked', () => {
    render(<KioskCrewJobCard onSelect={jest.fn()} nowMs={Date.now()} item={{ ...crewItem, run_order: 1 }} />);

    const chip = screen.getByTestId('kiosk-run-order-chip');
    expect(chip).toHaveTextContent('1');
    expect(chip).toHaveAttribute('aria-label', 'Run order 1');
  });

  it('renders nothing when run_order is null', () => {
    render(<KioskCrewJobCard onSelect={jest.fn()} nowMs={Date.now()} item={{ ...crewItem, run_order: null }} />);
    expect(screen.queryByTestId('kiosk-run-order-chip')).not.toBeInTheDocument();
  });
});
