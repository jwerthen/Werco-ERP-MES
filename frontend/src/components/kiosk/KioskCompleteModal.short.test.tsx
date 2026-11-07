/**
 * The kiosk COMPLETE / CLOCK OUT · KEEP OPEN overlay under owner ruling 1
 * (2026-11-05): a short count at operation completion is reported as scrap with a
 * reason, or the operation stays open — never closed short at the counted quantity.
 *
 * The server enforces it (`/complete` refuses 409 and writes nothing); what is pinned
 * here is that the overlay shows the same arithmetic BEFORE the tap:
 *  - FINISH pre-fills GOOD at what is still unaccounted for; KEEP OPEN pre-fills 0, and
 *    switching from finish resets an untouched prefill (a walk-away must never record
 *    phantom good);
 *  - SHORT n shows as a tile and a status panel, and COMPLETE is disabled while short;
 *  - "Report n as scrap" fills scrap and brings up the REQUIRED reason grid;
 *  - the leave-open verb is ALWAYS reachable, short or not;
 *  - a crewmate on the roster disables a SHORT finish and promotes leave-open;
 *  - a laser nest says runs; the received count follows the server's upstream short;
 *  - a clock-out refusal renders inline as an alert.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskCompleteModal from './KioskCompleteModal';
import type { KioskRosterEntry } from './kioskConstants';
import type { ActiveJob, LaserNestInfo } from '../../types';

const JOB: ActiveJob = {
  time_entry_id: 501,
  clock_in: '2026-11-07T12:00:00Z',
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_order_number: 'WO-2026-0142',
  operation_number: '20',
  operation_name: 'Deburr',
  quantity_ordered: 10,
  quantity_complete: 2,
  quantity_scrapped: 0,
};

const ME: KioskRosterEntry = {
  time_entry_id: 501,
  user_id: 3,
  operator_name: 'Rosa V',
  employee_id: 'E003',
  entry_type: 'run',
  clock_in: '2026-11-07T12:00:00Z',
};
const BOB: KioskRosterEntry = { ...ME, time_entry_id: 502, user_id: 11, operator_name: 'Bob T', employee_id: 'E011' };

const renderModal = (props: Partial<React.ComponentProps<typeof KioskCompleteModal>> = {}) =>
  render(
    <KioskCompleteModal
      job={JOB}
      nowMs={Date.parse('2026-11-07T13:00:00Z')}
      busy={false}
      online
      operationScrapped={0}
      currentUserId={3}
      roster={[ME]}
      onCancel={jest.fn()}
      onConfirm={jest.fn()}
      onLeaveOpen={jest.fn()}
      {...props}
    />
  );

const goodWell = () => screen.getByTestId('kiosk-qty-good');
const scrapWell = () => screen.getByTestId('kiosk-qty-scrap');
const complete = () => screen.getByTestId('kiosk-qty-confirm');

async function keyGood(user: ReturnType<typeof userEvent.setup>, digits: string) {
  await user.click(goodWell());
  await user.click(screen.getByRole('button', { name: 'Clear' }));
  for (const d of digits) await user.click(screen.getByRole('button', { name: d }));
}

describe('KioskCompleteModal — short count (owner ruling 1)', () => {
  describe('prefill', () => {
    it('FINISH pre-fills GOOD at what is still unaccounted for (received − good − scrap)', () => {
      renderModal({ operationScrapped: 1 });
      // 10 target, 2 good + 1 scrap recorded → 7.
      expect(within(goodWell()).getByText('7')).toBeInTheDocument();
      expect(screen.queryByTestId('kiosk-complete-short')).not.toBeInTheDocument();
      expect(complete()).toBeEnabled();
    });

    it('KEEP OPEN pre-fills 0 — only the pieces finished this session', () => {
      renderModal({ mode: 'leave_open' });
      expect(within(goodWell()).getByText('0')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'Clock out · keep open' })).toBeInTheDocument();
      expect(screen.getByTestId('kiosk-complete-leave-open-confirm')).toHaveTextContent('Clock out · leave Op 20 open');
    });

    it('switching FINISH → KEEP OPEN resets an untouched prefill to 0, with the hint', async () => {
      const user = userEvent.setup();
      renderModal();
      expect(within(goodWell()).getByText('8')).toBeInTheDocument();

      await user.click(screen.getByTestId('kiosk-complete-leave-open'));
      expect(within(goodWell()).getByText('0')).toBeInTheDocument();
      expect(screen.getByTestId('kiosk-complete-leave-open-hint')).toHaveTextContent(
        'Enter the pieces you finished this session'
      );

      // ...and back: the untouched prefill returns.
      await user.click(screen.getByTestId('kiosk-complete-switch-finish'));
      expect(within(goodWell()).getByText('8')).toBeInTheDocument();
    });

    it('keeps a count the operator keyed when switching modes', async () => {
      const user = userEvent.setup();
      const onLeaveOpen = jest.fn();
      renderModal({ onLeaveOpen });
      await keyGood(user, '3');

      await user.click(screen.getByTestId('kiosk-complete-leave-open'));
      expect(within(goodWell()).getByText('3')).toBeInTheDocument();
      await user.click(screen.getByTestId('kiosk-complete-leave-open-confirm'));
      expect(onLeaveOpen).toHaveBeenCalledWith(3, 0, null, null);
    });
  });

  describe('SHORT', () => {
    it('shows a SHORT tile and status panel, and disables COMPLETE while short', async () => {
      const user = userEvent.setup();
      const onConfirm = jest.fn();
      renderModal({ onConfirm });
      await keyGood(user, '6');

      // 2 + 6 of 10: 2 short.
      expect(screen.getByTestId('kiosk-complete-short-tile')).toHaveTextContent('2');
      const panel = screen.getByTestId('kiosk-complete-short');
      expect(panel).toHaveAttribute('role', 'status');
      expect(panel).toHaveTextContent(
        '2 of 10 not yet accounted for — record finished pieces, report lost pieces as scrap, or leave Op 20 open'
      );
      expect(complete()).toBeDisabled();
      await user.click(complete());
      expect(onConfirm).not.toHaveBeenCalled();
    });

    it('"Report n as scrap" fills scrap and requires a reason before COMPLETE', async () => {
      const user = userEvent.setup();
      const onConfirm = jest.fn();
      renderModal({ onConfirm });
      await keyGood(user, '6');

      await user.click(screen.getByTestId('kiosk-complete-report-scrap'));
      expect(within(scrapWell()).getByText('2')).toBeInTheDocument();
      expect(scrapWell()).toHaveAttribute('aria-pressed', 'true');
      expect(screen.queryByTestId('kiosk-complete-short')).not.toBeInTheDocument();
      // Covered — but scrap needs a reason first.
      expect(complete()).toBeDisabled();
      expect(screen.getByText('Choose a scrap reason to continue.')).toBeInTheDocument();

      await user.click(
        within(screen.getByRole('group', { name: 'Scrap reason' })).getByRole('button', { name: 'Material defect' })
      );
      expect(complete()).toBeEnabled();
      await user.click(complete());
      expect(onConfirm).toHaveBeenCalledWith(6, 2, 'Material defect', null);
    });

    it('keeps the leave-open verb visible at SHORT 0 as well as when short', async () => {
      const user = userEvent.setup();
      renderModal();
      // Full count prefilled: not short, leave-open still one tap away.
      expect(screen.queryByTestId('kiosk-complete-short')).not.toBeInTheDocument();
      expect(screen.getByTestId('kiosk-complete-leave-open')).toHaveTextContent('Clock out · leave Op 20 open');

      await keyGood(user, '1');
      expect(screen.getByTestId('kiosk-complete-short')).toBeInTheDocument();
      expect(screen.getByTestId('kiosk-complete-leave-open')).toBeEnabled();
    });

    it('works from the RECEIVED count the server reports, and says what never reached the op', async () => {
      const user = userEvent.setup();
      // 10 target, 2 closed short upstream → 8 received; 2 good already recorded.
      renderModal({ upstreamShort: 2, goodCeiling: 8 });
      expect(within(goodWell()).getByText('6')).toBeInTheDocument();
      expect(complete()).toBeEnabled();

      await keyGood(user, '5');
      const panel = screen.getByTestId('kiosk-complete-short');
      expect(panel).toHaveTextContent('1 of 8 not yet accounted for');
      expect(panel).toHaveTextContent('2 pcs never reached it — closed short upstream.');
    });

    it('says runs, not pieces, on a laser nest', async () => {
      const user = userEvent.setup();
      const nest: LaserNestInfo = {
        id: 5,
        nest_name: 'nest-p001',
        cnc_file_name: 'nest-1.cnc',
        planned_runs: 10,
        completed_runs: 2,
        remaining_runs: 8,
      };
      renderModal({ job: { ...JOB, laser_nest: nest }, upstreamShort: 1, goodCeiling: 9 });
      await keyGood(user, '5');
      expect(screen.getByTestId('kiosk-complete-short-tile')).toHaveTextContent(/2\s*Short · runs/);
      expect(screen.getByTestId('kiosk-complete-short')).toHaveTextContent('1 run never reached it');
    });
  });

  describe('roster', () => {
    it('a crewmate still clocked in disables a SHORT finish and promotes leave-open', async () => {
      const user = userEvent.setup();
      renderModal({ roster: [ME, BOB] });
      await keyGood(user, '6');
      await user.click(screen.getByTestId('kiosk-complete-report-scrap'));
      await user.click(
        within(screen.getByRole('group', { name: 'Scrap reason' })).getByRole('button', { name: 'Material defect' })
      );

      // Covered by scrap, but it is a SHORT close — refused while Bob is clocked in.
      expect(screen.getByTestId('kiosk-complete-others')).toHaveTextContent(
        'Bob T still clocked in — they must clock out with their count first'
      );
      expect(complete()).toBeDisabled();
      expect(screen.getByTestId('kiosk-complete-leave-open')).toHaveClass('bg-fd-blue/15');
    });

    it('a crewmate does not block a FULL-count finish (nothing is declared short)', () => {
      renderModal({ roster: [ME, BOB] });
      expect(screen.queryByTestId('kiosk-complete-others')).not.toBeInTheDocument();
      expect(complete()).toBeEnabled();
    });

    it('never counts the operator themself as "someone else"', async () => {
      const user = userEvent.setup();
      renderModal({ roster: [ME] });
      await keyGood(user, '6');
      await user.click(screen.getByTestId('kiosk-complete-report-scrap'));
      await user.click(
        within(screen.getByRole('group', { name: 'Scrap reason' })).getByRole('button', { name: 'Material defect' })
      );
      expect(screen.queryByTestId('kiosk-complete-others')).not.toBeInTheDocument();
      expect(complete()).toBeEnabled();
    });
  });

  it('notes on KEEP OPEN that a full count completes the operation on clock-out', async () => {
    const user = userEvent.setup();
    renderModal({ mode: 'leave_open' });
    expect(screen.queryByTestId('kiosk-complete-leave-open-full')).not.toBeInTheDocument();
    await keyGood(user, '8');
    expect(screen.getByTestId('kiosk-complete-leave-open-full')).toHaveTextContent(
      'That is the full count — clocking out completes Op 20.'
    );
  });

  it('renders a clock-out refusal INLINE as an alert, entry intact', () => {
    renderModal({
      error: 'Op 20 is already complete; clock out with 0 pieces and tell a supervisor about any extra pieces.',
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Op 20 is already complete; clock out with 0 pieces and tell a supervisor about any extra pieces.'
    );
    expect(within(goodWell()).getByText('8')).toBeInTheDocument();
  });
});
