/**
 * The floor renders the COMPONENT's live number, and never a stale snapshot.
 *
 * On a BOM-exploded assembly job the card's `part_number` is the ASSEMBLY's —
 * not what the operator is holding. The component's number also appears inside
 * `operation_name` as a baked-in prefix ("OLD-123 - Deburr"), but that is a
 * snapshot from when the work order was raised, and renumbering a part
 * deliberately does not rewrite it (an operation name on a released work order is
 * part of the released quality plan).
 *
 * So these fixtures deliberately hold a STALE prefix alongside a FRESH
 * `component_part_number` — the exact state after a renumber — and assert the
 * card shows the live one. Asserting only "the component number appears" would
 * pass against a card that showed nothing but the prefix.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import KioskQueueCard from './KioskQueueCard';
import KioskCrewJobCard from './KioskCrewJobCard';

const BASE = {
  operation_id: 1,
  work_order_id: 10,
  work_order_number: 'WO-1001',
  part_number: 'ASSY-900',
  part_name: 'Weldment',
  operation_number: '10',
  // The STALE prefix: the number this component had when the WO was raised.
  operation_name: 'OLD-123 - Deburr',
  component_part_number: 'NEW-456',
  component_part_name: 'Bracket',
  work_center_id: 5,
  status: 'ready',
  quantity_ordered: 10,
  quantity_complete: 0,
  priority: 3,
  due_date: null,
  steps_total: 0,
  steps_recorded: 0,
  roster: [],
} as any;

describe('kiosk cards show the live component number', () => {
  it('KioskQueueCard renders the component number, and leaves the stale prefix alone', () => {
    render(<KioskQueueCard item={BASE} onSelect={jest.fn()} />);

    expect(screen.getByText('NEW-456')).toBeInTheDocument();
    // The assembly's number is still shown — the component line adds to it.
    expect(screen.getByText(/ASSY-900/)).toBeInTheDocument();
  });

  it('KioskQueueCard renders no component line when there is no component', () => {
    render(
      <KioskQueueCard
        item={{ ...BASE, component_part_number: null, component_part_name: null }}
        onSelect={jest.fn()}
      />
    );
    expect(screen.queryByText(/Component/)).not.toBeInTheDocument();
  });

  it('KioskCrewJobCard renders the component number and name', () => {
    render(<KioskCrewJobCard item={BASE} onSelect={jest.fn()} nowMs={Date.now()} />);

    expect(screen.getByText('NEW-456')).toBeInTheDocument();
    expect(screen.getByText(/Bracket/)).toBeInTheDocument();
    // The released operation name is rendered UNCHANGED — the card must not
    // "helpfully" rewrite the prefix it displays.
    expect(screen.getByText(/OLD-123 - Deburr/)).toBeInTheDocument();
  });

  it('KioskCrewJobCard renders no component line when there is no component', () => {
    render(
      <KioskCrewJobCard
        item={{ ...BASE, component_part_number: null, component_part_name: null }}
        onSelect={jest.fn()}
        nowMs={Date.now()}
      />
    );
    expect(screen.queryByText(/Component/)).not.toBeInTheDocument();
  });
});
