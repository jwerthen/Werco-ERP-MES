/**
 * KioskQuantityScreen's quantity QUICK-ADD row — the crew station's copy.
 *
 * The crew station (`/kiosk?kiosk=1&station=<id>`) renders THIS screen, not the
 * single-operator kiosk's overlays, which is why an owner on an iPad saw a bare
 * number pad after the row shipped to the other two screens. What is pinned here
 * is the part that gets a shop floor into trouble:
 *
 * 1. Both quantity fields are on screen at once and only GOOD may ever receive a
 *    quick add — including while the shared keypad is bound to scrap, in which
 *    case the keypad follows the operator to good so the next digit cannot land
 *    in the field they just steered away from. There is no scrap quick-add at
 *    all: scrap takes a reason and a deliberate entry.
 *
 * 2. The row is bounded by the SERVER, not by taste. Both writers behind this
 *    screen refuse an over-target good quantity before any mutation —
 *    `POST /shop-floor/operations/{id}/production` with 400 "Quantity (N) cannot
 *    exceed quantity ordered (T)" and `POST /shop-floor/clock-out/{id}` with 400
 *    "Quantity produced exceeds quantity ordered" — so a tap must never key a
 *    state the server would refuse (the repo's non-optimistic convention).
 *
 * 3. It is OPT-IN, and the opt-in is the ceiling itself: a caller that cannot
 *    work out what the server will take gets no row rather than an unbounded
 *    one, and the screen renders exactly as it did before.
 *
 * 4. It is the SAME row the single-operator kiosk shows, off `quantityQuickAdds.ts`
 *    — same amounts, order and labels. An operator learns it once.
 */

import React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskQuantityScreen from './KioskQuantityScreen';
import KioskReportModal from './KioskReportModal';

const renderScreen = (props: Partial<React.ComponentProps<typeof KioskQuantityScreen>> = {}) =>
  render(
    <KioskQuantityScreen
      title="Report production"
      jobLabel="WO-2026-0142 · Op 20 Weld"
      confirmLabel="Continue"
      requireTotalPositive
      busy={false}
      onConfirm={jest.fn()}
      onCancel={jest.fn()}
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
const caption = () => screen.getByTestId('kiosk-qty-quickadd-label');

describe('KioskQuantityScreen — quantity quick adds', () => {
  describe('opt-in', () => {
    it('renders no row at all when the caller passes no ceiling', () => {
      // The bare screen an owner on the crew station saw: keypad only. A caller
      // that cannot compute the server's ceiling — LEAVE for a job outside this
      // station's queue — lands here, and that is the intended fallback.
      renderScreen();

      expect(screen.queryByTestId('kiosk-qty-quickadds')).not.toBeInTheDocument();
      expect(quickAddLabels()).toEqual([]);
      // …and the keypad is untouched.
      expect(screen.getByTestId('kiosk-key-7')).toBeInTheDocument();
    });

    it('renders +1 / +5 / +25 once a ceiling is given', () => {
      renderScreen({ quickAddCeiling: 13 });

      expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
      // Each button names the field it targets — two quantity fields are visible.
      expect(quickAdd('+1')).toBeInTheDocument();
      expect(caption()).toHaveTextContent('Quick add to good · max 13');
    });
  });

  it('counts up from zero on the REPORT path, where the row has real room', async () => {
    const user = userEvent.setup();
    renderScreen({ quickAddCeiling: 13 });

    await user.click(quickAdd('+5'));
    await user.click(quickAdd('+5'));
    await user.click(quickAdd('+1'));

    expect(within(goodWell()).getByText('11')).toBeInTheDocument();
    // Never scrap.
    expect(within(scrapWell()).getByText('0')).toBeInTheDocument();
  });

  describe('the server ceiling', () => {
    it('tops out at the ceiling instead of overshooting it', async () => {
      const user = userEvent.setup();
      renderScreen({ quickAddCeiling: 13 });

      await user.click(quickAdd('+25'));

      expect(within(goodWell()).getByText('13')).toBeInTheDocument();
      // At the ceiling the row goes disabled rather than keying a refusal.
      expect(quickAdd('+1')).toBeDisabled();
      expect(quickAdd('+25')).toBeDisabled();
    });

    it('clamps a keyed count up to the ceiling, never past it', async () => {
      const user = userEvent.setup();
      renderScreen({ quickAddCeiling: 13 });

      // Key 9 on the good field, then reach for +25.
      await user.click(screen.getByRole('button', { name: '9' }));
      await user.click(quickAdd('+25'));

      expect(within(goodWell()).getByText('13')).toBeInTheDocument();
    });

    it('says so plainly, and offers nothing, when the operation is already at its target', () => {
      // Every piece was reported in-shift, so any good quantity at all is a 400.
      renderScreen({ quickAddCeiling: 0 });

      expect(caption()).toHaveTextContent('Quick add to good · operation is already at its target');
      expect(quickAdd('+1')).toBeDisabled();
    });

    it('arrives disabled on the COMPLETE path, where good pre-fills at the ceiling', () => {
      // 50 ordered, 37 recorded → final-entry good pre-fills 13, which IS the
      // ceiling. The row is there for rebuilding a cleared count, not for
      // pushing past the target.
      renderScreen({ initialGood: 13, quickAddCeiling: 13, requireTotalPositive: false });

      expect(within(goodWell()).getByText('13')).toBeInTheDocument();
      expect(quickAdd('+1')).toBeDisabled();
    });
  });

  it('re-points an open SCRAP keypad at good, so the next digit cannot land in scrap', async () => {
    const user = userEvent.setup();
    renderScreen({ quickAddCeiling: 50 });

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

  it('sends the quick-added count as the good quantity, changing nothing else', async () => {
    const user = userEvent.setup();
    const onConfirm = jest.fn();
    renderScreen({ quickAddCeiling: 50, onConfirm });

    await user.click(quickAdd('+25'));
    await user.click(screen.getByTestId('kiosk-qty-confirm'));

    // Unchanged reporting semantics: good only, scrap 0, no reason, no code.
    expect(onConfirm).toHaveBeenCalledWith(25, 0, null, null);
  });

  it('still requires a scrap reason — a quick add cannot unlock a scrap entry', async () => {
    const user = userEvent.setup();
    const onConfirm = jest.fn();
    renderScreen({ quickAddCeiling: 50, onConfirm });

    await user.click(scrapWell());
    await user.click(screen.getByRole('button', { name: '3' }));
    await user.click(quickAdd('+5'));

    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(screen.getByText(/choose a scrap reason to continue/i)).toBeInTheDocument();
  });

  it('is disabled while a mutation is in flight', () => {
    renderScreen({ quickAddCeiling: 50, busy: true });
    expect(quickAdd('+1')).toBeDisabled();
  });

  describe('full-nest tap', () => {
    it('appends Full nest {n} when the operation carries a per-item target', async () => {
      const user = userEvent.setup();
      renderScreen({ quickAddCeiling: 40, fullNestQuantity: 40 });

      expect(quickAddLabels()).toEqual(['+1', '+5', '+25', 'Full nest 40']);

      await user.click(quickAdd('Full nest 40'));
      expect(within(goodWell()).getByText('40')).toBeInTheDocument();
    });

    it('is omitted when there is no per-item target, or it is 1', () => {
      renderScreen({ quickAddCeiling: 40, fullNestQuantity: null });
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
      cleanup();

      renderScreen({ quickAddCeiling: 40, fullNestQuantity: 1 });
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25']);
    });

    it('is clamped like every other tap when the crew is part-way through the nest', async () => {
      const user = userEvent.setup();
      // 40-piece nest, 34 already recorded → 6 left, and "Full nest 40" may not
      // key 40 of them.
      renderScreen({ quickAddCeiling: 6, fullNestQuantity: 40 });

      await user.click(quickAdd('Full nest 40'));
      expect(within(goodWell()).getByText('6')).toBeInTheDocument();
    });
  });

  it('offers the same row as the single-operator kiosk, in the same order', async () => {
    // The drift guard: both rows come from quantityQuickAdds.ts, and an operator
    // moving between the crew station and the single-operator kiosk must not
    // have to relearn the row.
    renderScreen({ quickAddCeiling: 25, fullNestQuantity: 40 });
    const crewRow = quickAddLabels();
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

    expect(crewRow).toEqual(quickAddLabels());

    // …and one tap still means +1 there, unchanged.
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '+1' }));
    expect(screen.getByTestId('kiosk-report-qty')).toHaveTextContent('1');
  });
});
