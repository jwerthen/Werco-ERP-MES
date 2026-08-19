/**
 * UnitBadge — "UNIT 2410048", the build identity of a one-unit-per-work-order job.
 *
 * ONE implementation renders on ten surfaces (the WO list row and mobile card, the WO
 * detail hero, the kiosk queue / clock-in / running-job screens, the crew station, the
 * held card, the dispatch board), so its contract is worth pinning here once rather
 * than re-proving it on each caller.
 *
 * The property that is BEHAVIOR rather than styling — and the only one whose regression
 * would be visible on every screen in the shop at once — is **never an empty
 * container**. Most work orders do not track a unit, and the field is `null` on all of
 * them. A badge that rendered its "UNIT" label with no number, or an empty bordered
 * chip, or even just its flex gap, would put a visual artifact on every card in the
 * building. `null`, `undefined`, `''` and whitespace-only must all render NOTHING, so a
 * work order without a unit looks exactly as it did before migration 083.
 *
 * The `size` variants are asserted only as "the class actually changes", not by
 * restating the Tailwind strings: those are styling and are meant to be tunable without
 * a test edit. What is asserted is that a caller asking for `lg` (the kiosk running-job
 * hero, read at arm's length) does not silently get `md`.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { UnitBadge } from './UnitBadge';

describe('UnitBadge', () => {
  it('renders the unit number with its UNIT label', () => {
    render(<UnitBadge unitNumber="2410048" />);

    const badge = screen.getByTestId('unit-badge');
    expect(badge).toHaveTextContent('Unit');
    expect(badge).toHaveTextContent('2410048');
  });

  describe('renders nothing at all when there is no unit to show', () => {
    // The whole point: a work order that does not track a unit must look untouched by
    // 083 — no border, no label, no gap. Each of these four is a real value the server
    // or the caller can produce: `null` from the column, `undefined` from an absent key
    // on a hand-built kiosk payload, and the two blanks from a planner clearing the
    // inline editor or typing spaces into it.
    it.each([
      ['null (the column on a work order with no unit)', null],
      ['undefined (an absent key on a kiosk payload)', undefined],
      ['an empty string', ''],
      ['whitespace only', '   '],
    ])('%s', (_label, value) => {
      const { container } = render(<UnitBadge unitNumber={value as string | null | undefined} />);

      expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
      // Not merely "no testid" — nothing is rendered, so no stray wrapper survives to
      // take up a flex gap on a dense row.
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('renders nothing for a non-string value', () => {
    // `typeof unitNumber !== 'string'` is the first guard, and it is what keeps a
    // NUMBER-shaped payload (JSON has no string/number discipline for a digits-only
    // field) from rendering `[object Object]`-class output or crashing on `.trim()`.
    const { container } = render(<UnitBadge unitNumber={2410048 as unknown as string} />);

    expect(screen.queryByTestId('unit-badge')).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it('trims surrounding whitespace off the displayed value', () => {
    // Free text from an office field. A leading space would show up as a gap between
    // the label and the number at 22px on the kiosk hero.
    render(<UnitBadge unitNumber="  2410048  " />);

    expect(screen.getByText('2410048')).toBeInTheDocument();
  });

  it('renders a unit number that is not purely numeric', () => {
    // Nothing about the column is numeric — it is a customer's numbering scheme, and
    // the backend stores whatever the planner types (String(50), no validator).
    render(<UnitBadge unitNumber="24-100-48/B" />);

    expect(screen.getByTestId('unit-badge')).toHaveTextContent('24-100-48/B');
  });

  describe('size', () => {
    it('defaults to md and applies a DIFFERENT class set for sm and lg', () => {
      // Asserted as "the variants are distinct", not by restating Tailwind strings:
      // the exact classes are styling, but a caller asking for the arm's-length `lg`
      // (the kiosk running-job hero) silently getting `md` is a legibility bug.
      const classesFor = (props: Partial<React.ComponentProps<typeof UnitBadge>>) => {
        const { unmount } = render(<UnitBadge unitNumber="2410048" {...props} />);
        const className = screen.getByTestId('unit-badge').className;
        unmount();
        return className;
      };

      const dflt = classesFor({});
      const md = classesFor({ size: 'md' });
      const sm = classesFor({ size: 'sm' });
      const lg = classesFor({ size: 'lg' });

      expect(dflt).toBe(md);
      expect(new Set([sm, md, lg]).size).toBe(3);
    });

    it('appends a caller className without dropping its own', () => {
      render(<UnitBadge unitNumber="2410048" className="mt-2" />);

      const badge = screen.getByTestId('unit-badge');
      expect(badge).toHaveClass('mt-2');
      // The identity colour is not a status colour (statusColors.ts owns
      // green/blue/amber/red/slate), which is what stops "UNIT" reading as an alert
      // beside a real status chip. A caller class must not displace it.
      expect(badge).toHaveClass('text-fd-cyan');
    });
  });
});
