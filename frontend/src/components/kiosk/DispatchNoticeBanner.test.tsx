/**
 * DispatchNoticeBanner — the strip's words and its one control (SPEC §7).
 *
 * Wording is the product here: the operator reads it from arm's length, so the
 * next-up case leads with what to RUN, the order-only case says plainly that
 * their next job did not change, and "changed N times" appears only when this
 * device actually saw more than one change.
 */

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import DispatchNoticeBanner, { describeNextUp, dispatchNoticeText } from './DispatchNoticeBanner';
import type { DispatchNotice } from '../../types';
import type { ActiveDispatchNotice } from '../../hooks/useDispatchNotice';

function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    // 16:34 UTC on 2026-11-04 is 10:34 AM Central (CST, after the Nov 1 change).
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 12,
    work_center_code: 'BRAKE-1',
    change_kind: 'machine_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 3102,
      work_order_number: 'WO-1233',
      unit_number: '7',
      operation_number: '30',
      operation_name: 'Weld',
      part_number: 'BRKT-1180',
    },
    next_up_after: {
      operation_id: 3101,
      work_order_number: 'WO-1234',
      unit_number: '7',
      operation_number: '20',
      operation_name: 'Bend',
      part_number: 'BRKT-1180',
    },
    moved_up: [3101],
    updates_in_window: 3,
    ...overrides,
  };
}

function entry(notice: DispatchNotice, observedChanges = 1): ActiveDispatchNotice {
  return {
    notice,
    nextUpChanged: (notice.next_up_before?.operation_id ?? null) !== (notice.next_up_after?.operation_id ?? null),
    observedChanges,
  };
}

/** The visible sentence, whitespace-normalized. */
function stripText(): string {
  return (screen.getByTestId('dispatch-notice').querySelector('p')?.textContent || '').replace(/\s+/g, ' ').trim();
}

describe('DispatchNoticeBanner wording', () => {
  it('next-up changed → amber alert: "RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved up by Jon W. 10:34 AM"', () => {
    render(<DispatchNoticeBanner notices={[entry(makeNotice())]} onAcknowledge={jest.fn()} variant="kiosk" />);

    const alert = screen.getByRole('alert');
    expect(alert).toBeInTheDocument();
    expect(stripText()).toBe('RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved up by Jon W. 10:34 AM');
    expect(screen.getByTestId('dispatch-notice')).toHaveAttribute('data-kind', 'next-up');
    // Not a status strip as well.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('omits the unit when the work order has none', () => {
    const notice = makeNotice();
    notice.next_up_after = { ...notice.next_up_after!, unit_number: null };
    render(<DispatchNoticeBanner notices={[entry(notice)]} onAcknowledge={jest.fn()} />);
    expect(stripText()).toBe('RUN NEXT: WO-1234 · Op 20 Bend — moved up by Jon W. 10:34 AM');
    expect(stripText()).not.toMatch(/Unit/);
  });

  it('next-up unchanged → calmer status strip: "Queue order updated by … — your next job is unchanged"', () => {
    const notice = makeNotice({ next_up_after: makeNotice().next_up_before });
    render(<DispatchNoticeBanner notices={[entry(notice)]} onAcknowledge={jest.fn()} />);

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(stripText()).toBe('Queue order updated by Jon W. 10:34 AM — your next job is unchanged');
    expect(screen.getByTestId('dispatch-notice')).toHaveAttribute('data-kind', 'order');
  });

  it('says where a job arrived from a machine move', () => {
    render(
      <DispatchNoticeBanner notices={[entry(makeNotice({ change_kind: 'moved_in' }))]} onAcknowledge={jest.fn()} />
    );
    expect(stripText()).toBe('RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved to this machine by Jon W. 10:34 AM');
  });

  it('handles a queue that is now empty without inventing a job', () => {
    render(<DispatchNoticeBanner notices={[entry(makeNotice({ next_up_after: null }))]} onAcknowledge={jest.fn()} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(stripText()).toBe('RUN NEXT: nothing queued here now — changed by Jon W. 10:34 AM');
  });

  it('falls back to "the planner" when the actor is unknown', () => {
    const text = dispatchNoticeText(entry(makeNotice({ actor_display: null })));
    expect(`${text.lead}${text.subject}${text.tail}`).toBe(
      'RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved up by the planner 10:34 AM'
    );
  });

  it('renders the operation through the shared label helper (no "Op Op 20" from a legacy row)', () => {
    expect(
      describeNextUp({
        operation_id: 1,
        work_order_number: 'WO-9',
        unit_number: null,
        operation_number: 'Op 20',
        operation_name: 'Bend',
        part_number: null,
      })
    ).toBe('WO-9 · Op 20 Bend');
    expect(
      describeNextUp({
        operation_id: 2,
        work_order_number: 'WO-9',
        unit_number: '  ',
        operation_number: 'Nest 3',
        operation_name: 'Laser Cut - CNC0412',
        part_number: null,
      })
    ).toBe('WO-9 · Nest 3 Laser Cut - CNC0412');
  });
});

describe('DispatchNoticeBanner "changed N times"', () => {
  it('appears only when this device observed more than one distinct change', () => {
    const { rerender } = render(
      <DispatchNoticeBanner notices={[entry(makeNotice({ updates_in_window: 9 }), 1)]} onAcknowledge={jest.fn()} />
    );
    // updates_in_window is a 12-hour total — never shown as "since you checked".
    expect(screen.queryByTestId('dispatch-notice-change-count')).not.toBeInTheDocument();
    expect(screen.queryByText(/9/)).not.toBeInTheDocument();

    rerender(<DispatchNoticeBanner notices={[entry(makeNotice(), 3)]} onAcknowledge={jest.fn()} />);
    expect(screen.getByTestId('dispatch-notice-change-count')).toHaveTextContent(
      'Changed 3 times since you last checked'
    );
  });
});

describe('DispatchNoticeBanner control', () => {
  it('"Got it" is a real button that acknowledges exactly the shown notice', () => {
    const onAcknowledge = jest.fn();
    const notice = makeNotice();
    render(<DispatchNoticeBanner notices={[entry(notice)]} onAcknowledge={onAcknowledge} variant="kiosk" />);

    const button = screen.getByRole('button', { name: /got it/i });
    expect(button.tagName).toBe('BUTTON');
    expect(button).toHaveAttribute('type', 'button');
    // Gloved-tap sizing on the kiosk.
    expect(button.className).toMatch(/min-h-16/);
    fireEvent.click(button);
    expect(onAcknowledge).toHaveBeenCalledTimes(1);
    expect(onAcknowledge).toHaveBeenCalledWith(notice);
  });

  it('renders nothing when there is nothing to show', () => {
    const { container } = render(<DispatchNoticeBanner notices={[]} onAcknowledge={jest.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names the machine on each strip when several are up, with a distinct button name each', () => {
    const brake = makeNotice();
    const weld = makeNotice({ id: 90300, work_center_id: 14, work_center_code: 'WELD-2' });
    render(<DispatchNoticeBanner notices={[entry(weld), entry(brake)]} onAcknowledge={jest.fn()} />);

    const strips = screen.getAllByTestId('dispatch-notice');
    expect(strips[0]).toHaveTextContent('WELD-2');
    expect(strips[1]).toHaveTextContent('BRAKE-1');
    expect(screen.getByRole('button', { name: /got it.*WELD-2/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /got it.*BRAKE-1/i })).toBeInTheDocument();
  });

  it('names the machine on a single strip only when asked (desk screens with several stations)', () => {
    const { rerender } = render(<DispatchNoticeBanner notices={[entry(makeNotice())]} onAcknowledge={jest.fn()} />);
    expect(stripText()).not.toMatch(/BRAKE-1/);
    rerender(<DispatchNoticeBanner notices={[entry(makeNotice())]} onAcknowledge={jest.fn()} showWorkCenter />);
    expect(stripText()).toMatch(/^BRAKE-1 RUN NEXT:/);
  });
});
