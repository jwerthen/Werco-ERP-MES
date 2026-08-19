/**
 * KioskQueueCard — the UNIT badge on an operator's queue card.
 *
 * The kiosk is the surface the whole 083 story starts from: a welder at the crew
 * station could not find the "Unit #" the office had typed into the work order's Notes,
 * because finding it meant reading a paragraph. The badge is the fix, and this card is
 * where the operator meets it first — before clocking in, while deciding which card is
 * the job in front of them.
 *
 * The card is pure (no API/context), so it renders with no mocks. Two things are pinned:
 * the badge shows the unit when there is one, and the card is untouched when there is
 * not — `unit_number` is absent on most queue rows, so a card that grew an empty chip
 * or a stray gap would change every card in the shop to fix a few.
 *
 * The blank-value contract itself belongs to `UnitBadge` and is proven in
 * UnitBadge.test.tsx; what is proven here is that this card routes the field through it
 * rather than rendering its own chip.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import KioskQueueCard from './KioskQueueCard';
import { KioskQueueItem } from './kioskConstants';

const baseItem: KioskQueueItem = {
  operation_id: 1,
  work_order_id: 10,
  work_order_number: 'WO-1001',
  part_number: 'P-500',
  part_name: 'Weldment, frame',
  operation_number: 20,
  operation_name: 'Weld out',
  work_center_id: 3,
  status: 'ready',
  quantity_ordered: 1,
  quantity_complete: 0,
  priority: 1,
  due_date: null,
};

function renderCard(overrides: Partial<KioskQueueItem> = {}) {
  return render(<KioskQueueCard onSelect={jest.fn()} item={{ ...baseItem, ...overrides } as KioskQueueItem} />);
}

describe('KioskQueueCard unit number', () => {
  it('shows the unit badge beside the work-order number', () => {
    renderCard({ unit_number: '2410048' });

    const badge = screen.getByTestId('unit-badge');
    expect(badge).toHaveTextContent('Unit');
    expect(badge).toHaveTextContent('2410048');
    // The WO number is still the card's primary identifier — the unit is an addition,
    // not a replacement, because the office and the traveler both key off WO number.
    expect(screen.getByText('WO-1001')).toBeInTheDocument();
  });

  it.each([
    ['null', null],
    ['absent from the payload', undefined],
    ['blank', ''],
    ['whitespace only', '   '],
  ])('renders no badge at all when the unit number is %s', (_label, value) => {
    renderCard({ unit_number: value as string | null | undefined });

    expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
    // The rest of the card is unchanged — this is most of the cards on most stations.
    expect(screen.getByText('WO-1001')).toBeInTheDocument();
    expect(screen.getByText('P-500')).toBeInTheDocument();
  });
});
