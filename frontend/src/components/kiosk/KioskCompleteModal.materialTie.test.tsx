/**
 * The kiosk COMPLETE screen's material-deduction notice.
 *
 * The arithmetic itself is unit-tested in `utils/materialTie.test.ts`; what is
 * pinned here is the WIRING and the COPY, because both are the parts that get a
 * shop floor into trouble:
 *  - an untied operation renders NOTHING (no placeholder, no nag);
 *  - the notice says material deducts when the WORK ORDER finishes, never that
 *    it is deducting now — a laser child WO has one operation per nest, so
 *    completing nest 1 of 3 moves no stock at all;
 *  - the GOOD keypad does not move the number (/complete asserts
 *    `quantity_complete = quantity_ordered`), while the SCRAP keypad does, and
 *    the UI says why;
 *  - a predicted shortage is informational: it never disables the Complete CTA.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskCompleteModal from './KioskCompleteModal';
import type { KioskMaterialTie } from './kioskConstants';
import type { ActiveJob } from '../../types';

const JOB: ActiveJob = {
  time_entry_id: 1,
  clock_in: '2026-07-25T12:00:00Z',
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 71,
  work_order_number: 'WO-2026-0142',
  operation_number: '10',
  operation_name: 'Laser Cut - nest-p001',
  quantity_ordered: 5,
  quantity_complete: 0,
  quantity_scrapped: 0,
};

const TIE: KioskMaterialTie = {
  allocation_id: 900,
  part_id: 55,
  part_number: 'SHT-.125-304',
  part_name: '.125 304 sheet',
  unit_of_measure: 'EA',
  qty_per_run: 1,
  qty_planned: 5,
  qty_consumed: 0,
  qty_remaining: 5,
  on_hand: 50,
  short_by: 0,
  pinned_lot_number: null,
};

const renderModal = (props: Partial<React.ComponentProps<typeof KioskCompleteModal>> = {}) =>
  render(
    <KioskCompleteModal
      job={JOB}
      nowMs={Date.parse('2026-07-25T13:00:00Z')}
      busy={false}
      online
      onCancel={jest.fn()}
      onConfirm={jest.fn()}
      {...props}
    />
  );

describe('KioskCompleteModal — material deduction notice', () => {
  it('renders nothing at all for an untied operation', () => {
    renderModal();
    expect(screen.queryByTestId('kiosk-complete-material')).not.toBeInTheDocument();

    renderModal({ materialTies: [] });
    expect(screen.queryByTestId('kiosk-complete-material')).not.toBeInTheDocument();
  });

  it('names the WORK ORDER whose completion deducts, and never claims it is deducting now', () => {
    renderModal({ materialTies: [TIE], operationScrapped: 0 });

    const notice = screen.getByTestId('kiosk-complete-material');
    expect(notice).toHaveTextContent('Material — deducts when WO-2026-0142 finishes');
    expect(notice).toHaveTextContent('5 EA · SHT-.125-304');
    expect(notice).toHaveTextContent('nothing leaves stock until the last operation on this work order completes');
    expect(notice.textContent).not.toMatch(/deducting now|will deduct now/i);
  });

  it('is computed from the ORDERED quantity — the good keypad does not move it', async () => {
    const user = userEvent.setup();
    renderModal({ materialTies: [TIE], operationScrapped: 0 });

    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('5 EA · SHT-.125-304');
    // Key a different good count; /complete still asserts quantity_ordered.
    await user.click(screen.getByTestId('kiosk-qty-good'));
    await user.click(screen.getByRole('button', { name: '2' }));
    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('5 EA · SHT-.125-304');
  });

  it('RAISES the prediction as scrap is keyed, and says why', async () => {
    const user = userEvent.setup();
    renderModal({ materialTies: [TIE], operationScrapped: 0 });

    await user.click(screen.getByTestId('kiosk-qty-scrap'));
    await user.click(screen.getByRole('button', { name: '2' }));

    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('7 EA · SHT-.125-304');
    expect(screen.getByTestId('kiosk-complete-material-scrap')).toHaveTextContent(
      'Includes +2 for the 2 scrap you entered — a scrapped run still used its material.'
    );
  });

  it('uses the OPERATION scrap total, not this session count', () => {
    // job.quantity_scrapped (this time entry's session count) is 0; the
    // operation already carries 3. Target = 5 ordered + 3 scrapped = 8.
    renderModal({ materialTies: [TIE], operationScrapped: 3 });
    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('8 EA · SHT-.125-304');
  });

  it('subtracts what a partially consumed tie already reported', () => {
    renderModal({ materialTies: [{ ...TIE, qty_consumed: 2 }], operationScrapped: 0 });
    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('3 EA · SHT-.125-304');
  });

  it('warns on a predicted shortage WITHOUT disabling the Complete button', () => {
    renderModal({ materialTies: [{ ...TIE, on_hand: 2 }], operationScrapped: 0 });

    expect(screen.getByTestId('kiosk-complete-material-short')).toHaveTextContent('never blocks the job');
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeEnabled();
  });
});
