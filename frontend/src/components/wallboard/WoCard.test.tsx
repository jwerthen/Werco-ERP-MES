/**
 * WoCard — the gated customer-name row (row 3) on the Foundry TV board tile.
 *
 * The server decides whether a tile carries `customer_name` (executive
 * displays / privileged roles only). This component test pins the RENDER
 * contract that follows from that gate:
 *   - a non-blank customer_name renders in the dedicated `wo-card-customer`
 *     cell, uppercased, and takes the row over the op line;
 *   - a null / undefined / blank customer_name falls back to the op line
 *     (`OP n/total · NAME`, or `ALL OPS COMPLETE`) — the public-board default.
 *
 * WoCard is pure (no API/context), so it renders in isolation with no mocks.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import WoCard from './WoCard';
import type { WallboardJob } from '../../types/wallboard';

/** A RUNNING tile with a current op — so the op-line fallback is available and
 *  we can prove the customer cell takes precedence over it when present. */
function makeJob(overrides: Partial<WallboardJob> = {}): WallboardJob {
  return {
    wo_number: 'WO-2001',
    part_number: 'PN-88231',
    status: 'in_progress',
    qty_complete: 3,
    qty_ordered: 10,
    is_late: false,
    days_late: 0,
    blocked: false,
    down: false,
    running: true,
    ops_completed: 2,
    ops_total: 6,
    current_op: {
      sequence: 30,
      name: 'CNC Mill',
      work_center_code: 'MILL-1',
      work_center_name: 'Haas VF-4',
      status: 'in_progress',
      elapsed_minutes: 12,
      crew: [],
      crew_count: 0,
    },
    ...overrides,
  };
}

function renderCard(job: WallboardJob) {
  return render(<WoCard job={job} downtime={null} blockedInfo={null} extraMinutes={0} />);
}

describe('WoCard customer name (row 3)', () => {
  it('renders the customer name (uppercased) and hides the op line when set', () => {
    renderCard(makeJob({ customer_name: 'Globex Aerospace' }));

    const customer = screen.getByTestId('wo-card-customer');
    expect(customer).toHaveTextContent('GLOBEX AEROSPACE');
    // The op line is replaced by the customer, not shown alongside it.
    expect(screen.queryByText(/OP 3\/6 · CNC MILL/)).not.toBeInTheDocument();
  });

  it('falls back to the op line when customer_name is null (public board)', () => {
    renderCard(makeJob({ customer_name: null }));

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('falls back to the op line when customer_name is absent (undefined)', () => {
    renderCard(makeJob()); // no customer_name key at all

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('treats a blank / whitespace-only customer_name as absent (trim → op line)', () => {
    renderCard(makeJob({ customer_name: '   ' }));

    expect(screen.queryByTestId('wo-card-customer')).not.toBeInTheDocument();
    expect(screen.getByText('OP 3/6 · CNC MILL')).toBeInTheDocument();
  });

  it('shows the customer even when there is no current op (ALL OPS COMPLETE state)', () => {
    renderCard(makeJob({ customer_name: 'Initech', current_op: null }));

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByTestId('wo-card-customer')).toHaveTextContent('INITECH');
    // The customer takes the row, so the "ALL OPS COMPLETE" fallback is not shown.
    expect(card.queryByText('ALL OPS COMPLETE')).not.toBeInTheDocument();
  });
});
