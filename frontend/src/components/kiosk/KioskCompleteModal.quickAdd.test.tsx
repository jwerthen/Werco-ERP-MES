/**
 * The kiosk COMPLETE screen's quantity QUICK-ADD row.
 *
 * What is pinned here is the part that gets a shop floor into trouble.
 *
 * 1. TWO quantity fields are on screen and only GOOD may ever receive a quick
 *    add — including while the keypad is bound to scrap, in which case the
 *    keypad follows the operator to good so the next digit can't land in the
 *    field they just steered away from.
 *
 * 2. The row is bounded by the SERVER, not by taste. Clock-out refuses 400
 *    "Quantity produced exceeds quantity ordered" once
 *    `operation.quantity_complete + produced` clears the operation target
 *    (backend/app/api/endpoints/shop_floor.py), and this modal pre-fills good at
 *    exactly `remaining` — the ceiling itself. So the row arrives DISABLED and a
 *    tap can never push the field into a state the server would refuse (the
 *    repo's non-optimistic convention for server-gated actions). Deleting the
 *    ceiling turns the owner's `+1` into a guaranteed refusal that aborts the
 *    whole completion — the operator stays clocked in with nothing recorded.
 *
 * 3. It is data entry only: the material deduction notice is computed from the
 *    ORDERED quantity, and a quick add must not move it, exactly as the good
 *    keypad must not (see KioskCompleteModal.materialTie.test.tsx).
 *
 * 4. It is the SAME row the REPORT modal shows — same amounts, order and labels,
 *    off `quantityQuickAdds.ts`. An operator learns it once.
 */

import React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskCompleteModal from './KioskCompleteModal';
import KioskReportModal from './KioskReportModal';
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
  quantity_ordered: 25,
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
  qty_planned: 25,
  qty_consumed: 0,
  qty_remaining: 25,
  on_hand: 500,
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

/** The quick-add row's visible labels, in render order, from anywhere on screen. */
const quickAddLabels = () =>
  screen
    .getAllByRole('button')
    .map((b) => b.textContent || '')
    .filter((text) => /^\+\d+$|^Full nest /.test(text));

const goodWell = () => screen.getByTestId('kiosk-qty-good');
const scrapWell = () => screen.getByTestId('kiosk-qty-scrap');
const quickAdd = (label: string) => screen.getByRole('button', { name: `Add ${label} to good` });

/**
 * Good pre-fills at the ceiling, so the only way to a live quick-add row is the
 * one an operator uses: open the good keypad and clear the count.
 */
const clearGood = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(goodWell());
  await user.click(screen.getByRole('button', { name: 'Clear' }));
};

describe('KioskCompleteModal — quantity quick adds', () => {
  it('offers +1 / +5 / +25 against the GOOD field', () => {
    renderModal();

    expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
    // Each button names the field it targets — two quantity fields are on screen.
    expect(quickAdd('+1')).toBeInTheDocument();
    expect(quickAdd('+25')).toBeInTheDocument();
  });

  it('adds to good, and never to scrap', async () => {
    const user = userEvent.setup();
    renderModal();
    await clearGood(user);

    await user.click(quickAdd('+5'));
    await user.click(quickAdd('+1'));

    expect(within(goodWell()).getByText('6')).toBeInTheDocument();
    expect(within(scrapWell()).getByText('0')).toBeInTheDocument();
  });

  describe('the server ceiling', () => {
    it('arrives disabled, because good pre-fills at the most clock-out will take', () => {
      renderModal();

      // 25 ordered, 0 complete → good pre-fills 25, and 26 is a 400.
      expect(within(goodWell()).getByText('25')).toBeInTheDocument();
      expect(quickAdd('+1')).toBeDisabled();
      expect(screen.getByTestId('kiosk-complete-quickadd-label')).toHaveTextContent('Quick add to good · max 25');
    });

    it('tops out at the ceiling instead of overshooting it', async () => {
      const user = userEvent.setup();
      renderModal();
      await clearGood(user);

      await user.click(quickAdd('+25'));
      expect(within(goodWell()).getByText('25')).toBeInTheDocument();

      // At the ceiling the row goes disabled rather than keying a refusal.
      expect(quickAdd('+25')).toBeDisabled();
      expect(quickAdd('+1')).toBeDisabled();
    });

    it('clamps a partial count up to the ceiling, never past it', async () => {
      const user = userEvent.setup();
      // 25 ordered, 20 already complete → the ceiling for this entry is 5.
      renderModal({ job: { ...JOB, quantity_complete: 20 } });
      await clearGood(user);

      expect(screen.getByTestId('kiosk-complete-quickadd-label')).toHaveTextContent('max 5');
      await user.click(quickAdd('+25'));
      expect(within(goodWell()).getByText('5')).toBeInTheDocument();
    });

    it('says so plainly when the operation is already at its target', () => {
      // The laser flow: every run was reported in-shift, so COMPLETE has no
      // pieces left to record and any good quantity at all would be refused.
      renderModal({ job: { ...JOB, quantity_complete: 25 } });

      expect(screen.getByTestId('kiosk-complete-quickadd-label')).toHaveTextContent(
        'Quick add to good · operation is already at its target'
      );
      expect(quickAdd('+1')).toBeDisabled();
    });
  });

  it('re-points an open SCRAP keypad at good, so the next digit cannot land in scrap', async () => {
    const user = userEvent.setup();
    renderModal();
    await clearGood(user);

    await user.click(scrapWell());
    await user.click(screen.getByRole('button', { name: '2' }));
    expect(within(scrapWell()).getByText('2')).toBeInTheDocument();

    await user.click(quickAdd('+1'));
    expect(scrapWell()).toHaveAttribute('aria-pressed', 'false');
    expect(goodWell()).toHaveAttribute('aria-pressed', 'true');

    // The next keypad digit appends to GOOD (1 → 17), not to the scrap count.
    await user.click(screen.getByRole('button', { name: '7' }));
    expect(within(goodWell()).getByText('17')).toBeInTheDocument();
    expect(within(scrapWell()).getByText('2')).toBeInTheDocument();
  });

  it('sends the quick-added count as the good quantity on confirm', async () => {
    const user = userEvent.setup();
    const onConfirm = jest.fn();
    renderModal({ onConfirm });
    await clearGood(user);

    await user.click(quickAdd('+5'));
    await user.click(screen.getByTestId('kiosk-qty-confirm'));

    // Unchanged completion semantics: good rides the clock-out, scrap is 0 and
    // carries no reason. The host still asserts completion at the target qty.
    expect(onConfirm).toHaveBeenCalledWith(5, 0, null, null);
  });

  it('is disabled while a mutation is in flight', async () => {
    const user = userEvent.setup();
    const { rerender } = renderModal();
    await clearGood(user);
    expect(quickAdd('+1')).toBeEnabled();

    rerender(
      <KioskCompleteModal
        job={JOB}
        nowMs={Date.parse('2026-07-25T13:00:00Z')}
        busy
        online
        onCancel={jest.fn()}
        onConfirm={jest.fn()}
      />
    );
    expect(quickAdd('+1')).toBeDisabled();
  });

  it('does not move the material deduction notice — quick adds are data entry only', async () => {
    const user = userEvent.setup();
    renderModal({ materialTies: [TIE], operationScrapped: 0 });
    await clearGood(user);

    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('25 EA · SHT-.125-304');
    await user.click(quickAdd('+5'));
    expect(screen.getByTestId('kiosk-complete-material')).toHaveTextContent('25 EA · SHT-.125-304');
  });

  describe('full-nest tap', () => {
    it('appends Full nest {n} when the operation carries a per-item target', async () => {
      const user = userEvent.setup();
      renderModal({ job: { ...JOB, quantity_ordered: 40, component_quantity: 40 } });

      expect(quickAddLabels()).toEqual(['+1', '+5', '+25', 'Full nest 40']);

      await clearGood(user);
      await user.click(quickAdd('Full nest 40'));
      expect(within(goodWell()).getByText('40')).toBeInTheDocument();
    });

    it('is omitted when there is no per-item target, or it is 1', () => {
      renderModal({ job: { ...JOB, component_quantity: null } });
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
      cleanup();

      renderModal({ job: { ...JOB, component_quantity: 1 } });
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
    });
  });

  it('offers the same row as the REPORT modal, in the same order', async () => {
    // The drift guard: both rows come from quantityQuickAdds.ts, and an operator
    // moving between the two screens must not have to relearn the row.
    renderModal({ job: { ...JOB, component_quantity: 40 } });
    const completeRow = quickAddLabels();
    cleanup();

    render(
      <KioskReportModal
        workOrderNumber="WO-2026-0142"
        operationNumber="10"
        reportedGood={0}
        quantityOrdered={25}
        fullNestQuantity={40}
        busy={false}
        online
        initialTab="good"
        onCancel={jest.fn()}
        onConfirmGood={jest.fn()}
        onConfirmScrap={jest.fn()}
      />
    );

    expect(completeRow).toEqual(quickAddLabels());

    // …and one tap still means +1 there, unchanged by the refactor.
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '+1' }));
    expect(screen.getByTestId('kiosk-report-qty')).toHaveTextContent('1');
  });
});
