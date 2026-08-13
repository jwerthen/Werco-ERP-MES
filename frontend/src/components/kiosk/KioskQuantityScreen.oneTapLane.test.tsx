/**
 * The two REPORT PRODUCTION quantity surfaces — `KioskQuantityScreen` (crew
 * station) and `KioskReportModal` (single-operator kiosk) — and the two props
 * the one-tap `+1 PIECE` lane hands them.
 *
 * Both props exist to stop the same accident, from opposite ends:
 *
 * `oneTapLane` — passing it DROPS `+1` from the quick-add row below. The two
 *   controls commit differently: the lane's `+1` posts itself after a short undo
 *   window, while a row `+1` only fills the GOOD field for a later confirm. Two
 *   controls reading `+1` side by side, meaning those two different things, is
 *   exactly what leaves an operator unsure whether their part was counted. The
 *   invariant `quantityQuickAdds.ts` protects is SAME APPEARANCE ⇒ SAME
 *   BEHAVIOUR, so the row gives the label up rather than keep old chrome over
 *   new semantics.
 *
 * `confirmLockedLabel` — set while the lane still holds un-banked pieces, it
 *   disables confirm and says why. The lane always commits first, so exactly ONE
 *   mechanism owns the operator's count at any moment and a keyed confirm can
 *   never race a pending auto-post into two reports for one run of parts.
 *
 * Neither prop may change anything else: a caller that passes no lane must get
 * the screen exactly as it was, `+1` included.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskQuantityScreen from './KioskQuantityScreen';
import KioskReportModal from './KioskReportModal';

/** The quick-add ROW's labels, in render order. The lane's own "+1 piece" tap
 *  does not match — which is the point: it is a different-looking control. */
const quickAddLabels = () =>
  screen
    .getAllByRole('button')
    .map((b) => b.textContent || '')
    .filter((text) => /^\+\d+$|^Full nest /.test(text));

const confirmButton = () => screen.getByTestId('kiosk-qty-confirm');

/** A stand-in for the real lane — these tests are about the HOST's wiring. */
const laneStub = <div data-testid="onetap-lane-stub">one-tap lane</div>;

// ---------------------------------------------------------------------------
// KioskQuantityScreen (crew station)
// ---------------------------------------------------------------------------

type ScreenProps = React.ComponentProps<typeof KioskQuantityScreen>;

const screenProps = (over: Partial<ScreenProps> = {}): ScreenProps => ({
  title: 'Report production — Bob T',
  jobLabel: 'WO-2026-0142 · Op 20 Weld',
  confirmLabel: 'Record',
  requireTotalPositive: true,
  quickAddCeiling: 13,
  busy: false,
  onConfirm: jest.fn(),
  onCancel: jest.fn(),
  ...over,
});

describe('KioskQuantityScreen — the one-tap lane props', () => {
  describe('oneTapLane', () => {
    it('renders the lane and drops +1 from the quick-add row, so +1 means one thing', () => {
      render(<KioskQuantityScreen {...screenProps({ oneTapLane: laneStub, fullNestQuantity: 40 })} />);

      expect(screen.getByTestId('onetap-lane-stub')).toBeInTheDocument();
      expect(quickAddLabels()).toEqual(['+5', '+25', 'Full nest 40']);
      expect(screen.queryByRole('button', { name: 'Add +1 to good' })).not.toBeInTheDocument();
      // The rest of the row is untouched and still targets GOOD only.
      expect(screen.getByRole('button', { name: 'Add +5 to good' })).toBeInTheDocument();
    });

    it('leaves the row exactly as it is when no lane is passed', () => {
      render(<KioskQuantityScreen {...screenProps({ fullNestQuantity: 40 })} />);

      expect(screen.queryByTestId('onetap-lane-stub')).not.toBeInTheDocument();
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25', 'Full nest 40']);
      expect(screen.getByRole('button', { name: 'Add +1 to good' })).toBeInTheDocument();
    });

    it('still fills the GOOD field from the row it kept, unchanged', async () => {
      const user = userEvent.setup();
      render(<KioskQuantityScreen {...screenProps({ oneTapLane: laneStub })} />);

      await user.click(screen.getByRole('button', { name: 'Add +5 to good' }));
      expect(within(screen.getByTestId('kiosk-qty-good')).getByText('5')).toBeInTheDocument();
    });
  });

  describe('confirmLockedLabel', () => {
    it('disables confirm and says why while pieces are still un-banked', async () => {
      const user = userEvent.setup();
      const onConfirm = jest.fn();
      const { rerender } = render(
        <KioskQuantityScreen {...screenProps({ oneTapLane: laneStub, onConfirm })} />
      );

      // A keyed entry that WOULD confirm — so the lock below is doing the work,
      // not an empty field.
      await user.click(screen.getByRole('button', { name: 'Add +5 to good' }));
      expect(confirmButton()).toBeEnabled();
      expect(confirmButton()).toHaveTextContent('Record');

      rerender(
        <KioskQuantityScreen
          {...screenProps({ oneTapLane: laneStub, onConfirm, confirmLockedLabel: 'Recording 3 pcs…' })}
        />
      );

      expect(confirmButton()).toBeDisabled();
      expect(confirmButton()).toHaveTextContent('Recording 3 pcs…');
      expect(confirmButton()).not.toHaveTextContent('Record production');

      await user.click(confirmButton());
      expect(onConfirm).not.toHaveBeenCalled();
    });

    it('outranks the busy label — the operator needs to read why confirm will not fire', () => {
      render(
        <KioskQuantityScreen
          {...screenProps({ oneTapLane: laneStub, busy: true, confirmLockedLabel: 'Recording 2 pcs…' })}
        />
      );

      expect(confirmButton()).toHaveTextContent('Recording 2 pcs…');
      expect(confirmButton()).not.toHaveTextContent('Saving…');
    });

    it('leaves confirm alone when null', async () => {
      const user = userEvent.setup();
      const onConfirm = jest.fn();
      render(<KioskQuantityScreen {...screenProps({ oneTapLane: laneStub, onConfirm, confirmLockedLabel: null })} />);

      await user.click(screen.getByRole('button', { name: 'Add +5 to good' }));
      expect(confirmButton()).toBeEnabled();
      await user.click(confirmButton());
      expect(onConfirm).toHaveBeenCalledWith(5, 0, null, null);
    });
  });
});

// ---------------------------------------------------------------------------
// KioskReportModal (single-operator kiosk)
// ---------------------------------------------------------------------------

type ModalProps = React.ComponentProps<typeof KioskReportModal>;

const modalProps = (over: Partial<ModalProps> = {}): ModalProps => ({
  workOrderNumber: 'WO-2026-0142',
  operationNumber: '20',
  reportedGood: 37,
  quantityOrdered: 50,
  busy: false,
  online: true,
  initialTab: 'good',
  onCancel: jest.fn(),
  onConfirmGood: jest.fn(),
  onConfirmScrap: jest.fn(),
  ...over,
});

describe('KioskReportModal — the one-tap lane props', () => {
  describe('oneTapLane', () => {
    it('renders the lane and drops +1 from the quick-add row, so +1 means one thing', () => {
      render(<KioskReportModal {...modalProps({ oneTapLane: laneStub, fullNestQuantity: 40 })} />);

      expect(screen.getByTestId('onetap-lane-stub')).toBeInTheDocument();
      expect(quickAddLabels()).toEqual(['+5', '+25', 'Full nest 40']);
    });

    it('leaves the row exactly as it is when no lane is passed', () => {
      render(<KioskReportModal {...modalProps({ fullNestQuantity: 40 })} />);

      expect(screen.queryByTestId('onetap-lane-stub')).not.toBeInTheDocument();
      expect(quickAddLabels()).toEqual(['+1', '+5', '+25', 'Full nest 40']);
    });

    it('keeps the lane out of the SCRAP tab — scrap takes a reason and a deliberate entry', async () => {
      const user = userEvent.setup();
      render(<KioskReportModal {...modalProps({ oneTapLane: laneStub })} />);

      expect(screen.getByTestId('onetap-lane-stub')).toBeInTheDocument();
      await user.click(screen.getByTestId('kiosk-report-tab-scrap'));

      // A control that commits on a timer has no business on the scrap tab.
      expect(screen.queryByTestId('onetap-lane-stub')).not.toBeInTheDocument();
    });
  });

  describe('confirmLockedLabel', () => {
    it('disables confirm and says why while pieces are still un-banked', async () => {
      const user = userEvent.setup();
      const onConfirmGood = jest.fn();
      const { rerender } = render(
        <KioskReportModal {...modalProps({ oneTapLane: laneStub, onConfirmGood })} />
      );

      await user.click(screen.getByRole('button', { name: '+5' }));
      expect(confirmButton()).toBeEnabled();
      expect(confirmButton()).toHaveTextContent('Confirm +5 good');

      rerender(
        <KioskReportModal
          {...modalProps({ oneTapLane: laneStub, onConfirmGood, confirmLockedLabel: 'Recording 3 pcs…' })}
        />
      );

      expect(confirmButton()).toBeDisabled();
      expect(confirmButton()).toHaveTextContent('Recording 3 pcs…');

      await user.click(confirmButton());
      expect(onConfirmGood).not.toHaveBeenCalled();
    });

    it('locks the SCRAP tab too — it writes the same operation row the lane is posting to', async () => {
      const user = userEvent.setup();
      const onConfirmScrap = jest.fn();
      render(
        <KioskReportModal
          {...modalProps({ oneTapLane: laneStub, onConfirmScrap, confirmLockedLabel: 'Recording 3 pcs…' })}
        />
      );

      await user.click(screen.getByTestId('kiosk-report-tab-scrap'));
      await user.click(screen.getByTestId('kiosk-key-2'));
      await user.click(screen.getByRole('button', { name: 'Material defect' }));

      expect(confirmButton()).toBeDisabled();
      expect(confirmButton()).toHaveTextContent('Recording 3 pcs…');
      await user.click(confirmButton());
      expect(onConfirmScrap).not.toHaveBeenCalled();
    });

    it('outranks OFFLINE and SAVING — the lock is what the operator needs to read', () => {
      render(
        <KioskReportModal
          {...modalProps({
            oneTapLane: laneStub,
            busy: true,
            online: false,
            confirmLockedLabel: 'Recording 2 pcs…',
          })}
        />
      );

      expect(confirmButton()).toHaveTextContent('Recording 2 pcs…');
      expect(confirmButton()).not.toHaveTextContent('Offline');
      expect(confirmButton()).not.toHaveTextContent('Saving…');
    });

    it('leaves confirm alone when null', async () => {
      const user = userEvent.setup();
      const onConfirmGood = jest.fn();
      render(<KioskReportModal {...modalProps({ oneTapLane: laneStub, onConfirmGood, confirmLockedLabel: null })} />);

      await user.click(screen.getByRole('button', { name: '+5' }));
      await user.click(confirmButton());
      expect(onConfirmGood).toHaveBeenCalledWith(5);
    });
  });
});
