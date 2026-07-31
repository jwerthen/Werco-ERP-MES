/**
 * The CREW STATION complete-confirm dialog's material-deduction notice.
 *
 * The single-operator screen's notice is covered in
 * `KioskCompleteModal.materialTie.test.tsx`; this is the other half of the same
 * copy, and it had none. Both screens render the SAME builders from
 * `utils/materialTie`, which is exactly why it needs its own pin — a wiring
 * mistake here (feeding `pendingGood` instead of `quantityOrdered`, dropping the
 * timing note) would sail past that file while shipping a different number to a
 * different set of operators.
 *
 * What is pinned:
 *  - an untied operation renders NOTHING (no placeholder, no nag);
 *  - the notice says the deduction happens when THIS OPERATION completes — the
 *    badge scan below is what fires it. It may not slip back to "when the work
 *    order finishes" (understates: a laser child WO carries one operation per
 *    nest and draws nest by nest) nor drift to per-run (over-states: production
 *    reported on the previous screen posts nothing on its own, because an
 *    in-progress operation is still reducible and consumption never reverses);
 *  - the good count entered on the previous screen does NOT move the number
 *    (/complete asserts `quantity_complete = quantity_ordered`) while the scrap
 *    count DOES, and the UI says why;
 *  - a predicted shortage is informational — it never gates the badge signature.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskCompleteConfirmModal from './KioskCompleteConfirmModal';
import type { KioskMaterialTie } from './kioskConstants';

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

const renderModal = (props: Partial<React.ComponentProps<typeof KioskCompleteConfirmModal>> = {}) =>
  render(
    <KioskCompleteConfirmModal
      open
      jobLabel="WO-2026-0142 · Op 10 Laser Cut - nest-p001"
      roster={[]}
      nowMs={Date.parse('2026-07-25T13:00:00Z')}
      pendingGood={0}
      pendingScrap={0}
      quantityOrdered={5}
      workOrderNumber="WO-2026-0142"
      busy={false}
      error={null}
      onCancel={jest.fn()}
      onBadge={jest.fn()}
      {...props}
    />
  );

const notice = () => screen.getByTestId('kiosk-crew-complete-material');

describe('KioskCompleteConfirmModal — material deduction notice', () => {
  it('renders nothing at all for an untied operation', () => {
    const { unmount } = renderModal();
    expect(screen.queryByTestId('kiosk-crew-complete-material')).not.toBeInTheDocument();
    unmount();

    renderModal({ materialTies: [] });
    expect(screen.queryByTestId('kiosk-crew-complete-material')).not.toBeInTheDocument();
  });

  it('says THIS operation completing is what deducts, and names the job as context', () => {
    renderModal({ materialTies: [TIE], operationScrapped: 0 });

    expect(notice()).toHaveTextContent('Material — deducts when you complete this operation on WO-2026-0142');
    expect(notice()).toHaveTextContent('5 EA · SHT-.125-304');
    expect(notice()).toHaveTextContent('this leaves stock when the operation completes, not as each run is reported');
  });

  it('neither defers the deduction to the work order nor promises it per run', () => {
    renderModal({ materialTies: [TIE], operationScrapped: 0 });

    const text = notice().textContent || '';
    expect(text).not.toMatch(/finishes/i);
    expect(text).not.toMatch(/per run|each run completes|deducting now/i);
  });

  it('scales by the ORDERED quantity, not the good count from the previous screen', () => {
    // The crew station reports production first and then asserts completion at
    // the target, so `pendingGood` never moves this number.
    renderModal({ materialTies: [TIE], operationScrapped: 0, pendingGood: 2, quantityOrdered: 5 });
    expect(notice()).toHaveTextContent('5 EA · SHT-.125-304');
  });

  it('RAISES the prediction for the scrap being reported, and says why', () => {
    renderModal({ materialTies: [TIE], operationScrapped: 0, pendingGood: 3, pendingScrap: 2 });

    expect(notice()).toHaveTextContent('7 EA · SHT-.125-304');
    expect(screen.getByTestId('kiosk-crew-complete-material-scrap')).toHaveTextContent(
      'Includes +2 for the 2 scrap you entered — a scrapped run still used its material.'
    );
  });

  it('uses the OPERATION scrap total and subtracts what a partial tie already reported', () => {
    renderModal({ materialTies: [{ ...TIE, qty_consumed: 2 }], operationScrapped: 3 });
    // target = 1/run x (5 ordered + 3 scrapped) = 8, less 2 already consumed.
    expect(notice()).toHaveTextContent('6 EA · SHT-.125-304');
  });

  it('warns on a predicted shortage WITHOUT gating the badge signature', async () => {
    const user = userEvent.setup();
    const onBadge = jest.fn();
    renderModal({ materialTies: [{ ...TIE, on_hand: 2 }], operationScrapped: 0, onBadge });

    expect(screen.getByTestId('kiosk-crew-complete-material-short')).toHaveTextContent('never blocks the job');

    // A shortage is advisory: the completion still signs. (It drives the lot
    // negative and writes ALLOCATION_SHORTAGE — production truth outranks the
    // inventory figure.)
    await user.click(screen.getByRole('button', { name: '7' }));
    await user.click(screen.getByRole('button', { name: 'Complete' }));
    expect(onBadge).toHaveBeenCalledWith('7');
  });
});
