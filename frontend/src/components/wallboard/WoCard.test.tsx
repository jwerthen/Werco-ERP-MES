/**
 * WoCard — the gated customer-name row (row 3) and the HELD state on the
 * Foundry TV board tile.
 *
 * The server decides whether a tile carries `customer_name` (executive
 * displays / privileged roles only). This component test pins the RENDER
 * contract that follows from that gate:
 *   - a non-blank customer_name renders in the dedicated `wo-card-customer`
 *     cell, uppercased, and takes the row over the op line;
 *   - a null / undefined / blank customer_name falls back to the op line
 *     (`OP n/total · NAME`, or `ALL OPS COMPLETE`) — the public-board default.
 *
 * HELD (2026-08-19) is the second contract pinned here. ON_HOLD work orders
 * joined the wall population, they carry the hold on the EXISTING `status`
 * field (no new wire key), and three things about the tile are decisions rather
 * than styling:
 *   - HELD LEADS the precedence — a held WO that is also down, blocked, late or
 *     running still reads HELD. It is deliberately stopped and somebody already
 *     knows, so naming any other condition on it would be misleading;
 *   - it renders GREY and de-emphasized, identically to WAITING, and must NEVER
 *     pulse or take the DOWN red wash. Leading the precedence is exactly what
 *     guarantees that, which is why the precedence test and the no-pulse test
 *     are the same claim from two directions;
 *   - its stop-reason cell shows the bare words ON HOLD and NOTHING ELSE. The Z3
 *     ON HOLD panel is counts-and-ages only precisely because hold reasons and
 *     NCR titles can name customers and suppliers, so putting held work on the
 *     grid stays a POPULATION change, not a disclosure-category change.
 *
 * WoCard is pure (no API/context), so it renders in isolation with no mocks.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import WoCard from './WoCard';
import { FD } from './wallboardTokens';
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

describe('WoCard HELD state (ON_HOLD on the wall, 2026-08-19)', () => {
  /** A held tile. The hold rides on `status` — there is no `held` boolean. */
  const heldJob = (overrides: Partial<WallboardJob> = {}) =>
    makeJob({ status: 'on_hold', running: false, ...overrides });

  it('reads HELD, with the bare words ON HOLD as its stop reason', () => {
    renderCard(heldJob());

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByText('HELD')).toBeInTheDocument();
    expect(card.getByText('ON HOLD')).toBeInTheDocument();
    // Not the WAITING chip/reason it would otherwise fall through to — without
    // its own chipWord case the switch default silently reads WAITING.
    expect(card.queryByText('WAITING')).not.toBeInTheDocument();
    expect(card.queryByText('IN QUEUE')).not.toBeInTheDocument();
  });

  it('wins the precedence outright — DOWN, BLOCKED, LATE and RUNNING all at once still read HELD', () => {
    renderCard(heldJob({ down: true, blocked: true, is_late: true, days_late: 9, running: true }));

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByText('HELD')).toBeInTheDocument();
    expect(card.queryByText('DOWN')).not.toBeInTheDocument();
    expect(card.queryByText('BLOCKED')).not.toBeInTheDocument();
    expect(card.queryByText(/^LATE /)).not.toBeInTheDocument();
    expect(card.queryByText('RUNNING')).not.toBeInTheDocument();
    // No time value either: a deliberate stop has no clock worth reading at 5m,
    // and the running elapsed (12m) would contradict the word HELD beside it.
    expect(card.queryByText('12M')).not.toBeInTheDocument();
  });

  it('does not pulse and never takes the DOWN red wash, even when the job IS down', () => {
    renderCard(heldJob({ down: true }));

    const card = screen.getByTestId('wo-card-WO-2001');
    // fdPulse is the board's entire motion budget and it is reserved for DOWN.
    expect(card.querySelectorAll('[style*="fdPulse"]')).toHaveLength(0);
    // The red wash (background gradient + border) is keyed on the DOWN state.
    expect(card.getAttribute('style') ?? '').not.toContain('240,68,56');
    // Grey, not an alarm colour: the left edge is WAITING's faint token.
    expect(card).toHaveStyle({ borderLeftColor: FD.faint });
    expect(screen.getByText('HELD')).toHaveStyle({ color: FD.waiting });
  });

  it('renders grey and de-emphasized EXACTLY like WAITING', () => {
    // Same job, only the status differs — so any style difference between the
    // two cards is attributable to the HELD spec and nothing else.
    const { unmount } = renderCard(makeJob({ status: 'released', running: false }));
    const waitingCardStyle = screen.getByTestId('wo-card-WO-2001').getAttribute('style');
    const waitingChipStyle = screen.getByText('WAITING').getAttribute('style');
    unmount();

    renderCard(heldJob());
    expect(screen.getByTestId('wo-card-WO-2001').getAttribute('style')).toBe(waitingCardStyle);
    expect(screen.getByText('HELD').getAttribute('style')).toBe(waitingChipStyle);
  });

  it('never borrows a stop reason from the joins — no hold reason, NCR title or free text', () => {
    // Both joins hit, and both are ignored: the card's stop-reason cell is the
    // one place a hold reason could leak onto an unattended public screen.
    render(
      <WoCard
        job={heldJob({ down: true, blocked: true })}
        downtime={{ category: 'maintenance', since: '2026-07-22T12:00:00Z', minutes: 134 }}
        blockedInfo={{ wo_number: 'WO-2001', category: 'waiting_inspect', age_hours: 22 }}
        extraMinutes={0}
      />
    );

    const card = screen.getByTestId('wo-card-WO-2001');
    expect(within(card).getByText('ON HOLD')).toBeInTheDocument();
    expect(within(card).queryByText('MAINTENANCE')).not.toBeInTheDocument();
    expect(within(card).queryByText('WAITING INSPECT')).not.toBeInTheDocument();
    expect(within(card).queryByText('2H14M')).not.toBeInTheDocument();
    expect(within(card).queryByText('22H')).not.toBeInTheDocument();
  });
});
