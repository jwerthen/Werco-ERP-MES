/**
 * WoCard row 2 — the unit number takes the large slot, and the card is otherwise
 * unchanged for the work orders that do not track one.
 *
 * The TV wall is the reason `unit_number` became a column at all: the number used to
 * live in `work_orders.notes`, which is unbounded free text and therefore cannot go on
 * an unattended screen. So this is the surface the feature exists for, and it carries
 * two claims worth pinning.
 *
 * 1. **When a job tracks a unit, THAT is the number somebody reads off the wall.** The
 *    part number on these weld assemblies is a long string that truncates and is
 *    unreadable at distance anyway, so the unit takes row 2's large type and the part
 *    number steps down beneath it. The part number must still RENDER — demoting it is
 *    the design, deleting it is a regression.
 * 2. **With no unit, row 2 is byte-identical to its pre-083 self.** Most tiles on the
 *    board have no unit number, and a layout that shifted for all of them (an empty
 *    stacked wrapper, a smaller part number, a stray gap) would be a whole-wall
 *    regression shipped to fix a handful of tiles.
 *
 * Unlike every other surface, this card does NOT use the shared `UnitBadge` — the TV
 * board has its own scale and its own `FD` colour tokens — so `UnitBadge`'s own
 * blank-handling contract does not cover it, and the blank/whitespace cases are
 * re-asserted here against this component's own `job.unit_number?.trim() || null`.
 *
 * WoCard is pure (no API/context), so it renders in isolation with no mocks.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import WoCard from './WoCard';
import type { WallboardJob } from '../../types/wallboard';

/** The large-type class row 2's headline element carries. */
const HEADLINE_CLASS = 'text-[1.9375rem]';

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

describe('WoCard unit number (row 2)', () => {
  it('renders the unit in the large slot and steps the part number down beneath it', () => {
    renderCard(makeJob({ unit_number: '2410048' }));

    const unit = screen.getByTestId('wo-card-unit');
    expect(unit).toHaveTextContent('UNIT');
    expect(unit).toHaveTextContent('2410048');
    // The unit is what carries row 2's headline type — the point of the whole change.
    expect(unit).toHaveClass(HEADLINE_CLASS);

    // The part number is DEMOTED, not deleted: an operator still has to be able to
    // tell what the unit is made of.
    const partNumber = screen.getByText('PN-88231');
    expect(partNumber).toBeInTheDocument();
    expect(partNumber).not.toHaveClass(HEADLINE_CLASS);
  });

  it('leaves the card unchanged when the job tracks no unit', () => {
    renderCard(makeJob({ unit_number: null }));

    expect(screen.queryByTestId('wo-card-unit')).not.toBeInTheDocument();
    // Row 2 is exactly what it was before 083: the part number in the headline slot.
    expect(screen.getByText('PN-88231')).toHaveClass(HEADLINE_CLASS);
  });

  it('leaves the card unchanged when unit_number is absent from the payload entirely', () => {
    // An older server, or a tile built before the field existed. `undefined` must read
    // the same as `null` — the board polls a live API and cannot assume its shape.
    renderCard(makeJob());

    expect(screen.queryByTestId('wo-card-unit')).not.toBeInTheDocument();
    expect(screen.getByText('PN-88231')).toHaveClass(HEADLINE_CLASS);
  });

  it.each([
    ['an empty string', ''],
    ['whitespace only', '   '],
  ])('treats %s as no unit rather than rendering an empty UNIT label', (_label, value) => {
    renderCard(makeJob({ unit_number: value }));

    expect(screen.queryByTestId('wo-card-unit')).not.toBeInTheDocument();
    expect(screen.getByText('PN-88231')).toHaveClass(HEADLINE_CLASS);
  });

  it('shows the unit alongside a gated customer name without either displacing the other', () => {
    // `unit_number` is UNGATED and `customer_name` is gated to executive displays, so
    // an exec TV is the one board that renders both. They live on different rows and
    // must not compete: this is the render-side half of the server contract asserted in
    // backend tests/api/test_unit_number_shop_floor_surfaces.py.
    renderCard(makeJob({ unit_number: '2410048', customer_name: 'Globex Aerospace' }));

    const card = within(screen.getByTestId('wo-card-WO-2001'));
    expect(card.getByTestId('wo-card-unit')).toHaveTextContent('2410048');
    expect(card.getByTestId('wo-card-customer')).toHaveTextContent('GLOBEX AEROSPACE');
  });

  it('renders the unit on a tile with no part number at all (standalone laser nest WO)', () => {
    // A part-less work order is a real shape (migration 067). The unit must not depend
    // on the part number's presence to render.
    renderCard(makeJob({ unit_number: '2410048', part_number: null }));

    expect(screen.getByTestId('wo-card-unit')).toHaveTextContent('2410048');
  });
});
