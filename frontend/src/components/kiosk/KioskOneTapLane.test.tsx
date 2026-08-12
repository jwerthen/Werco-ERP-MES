/**
 * `KioskOneTapLane` — the presentational half of the one-tap `+1 PIECE` control.
 *
 * Two things are pinned here, and both are safety properties rather than styling:
 *
 * 1. THE THREE STATES ARE TOLD APART WITHOUT READING A WORD. An operator has to
 *    know, at arm's length and through the glass of a shop iPad, whether the
 *    piece they just made is still theirs to take back (PENDING), gone to the
 *    ledger (RECORDED — the kiosk has no undo for a posted report), or refused
 *    (NOT SAVED). The states carry different chrome, and only the refusal
 *    interrupts a screen reader (`role="alert"`) with the server's own words.
 *
 * 2. FIXED GEOMETRY. `UNDO −1` is present in EVERY phase and merely goes dim
 *    when there is nothing to take back. This was found by browser testing: when
 *    UNDO rendered only while something was pending, `+1 PIECE` grew back to full
 *    width the instant the window closed, and a thumb already travelling toward
 *    UNDO landed on `+1` and RECORDED A PIECE instead of removing one — the
 *    precise accident the grace period exists to prevent. A test that only ever
 *    looks for UNDO while pending would not notice it coming back.
 *
 * 3. ORPHANED IS A DEAD END ON PURPOSE. When a delta outlives the (operator,
 *    operation) pair that made it, the lane holds it, NAMES that pair, and
 *    offers no way to send it anyway. Nobody standing at the kiosk can consent
 *    on the absent operator's behalf, so a "save anyway" control would be a
 *    one-tap route to crediting one person's pieces to another — the exact
 *    outcome the stamp exists to prevent. The absence of that control is
 *    therefore asserted, not assumed.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskOneTapLane from './KioskOneTapLane';
import { ONE_TAP_WINDOW_MS, type OneTapPieces } from './useOneTapPieces';

/**
 * A `OneTapPieces` in a chosen phase. Typed as the hook's own public contract,
 * so a stub that contradicts it is a compile error; `unbanked` is derived here
 * exactly as the hook derives it, so the two cannot drift.
 */
function oneTapState(over: Partial<OneTapPieces> = {}): OneTapPieces {
  const base = {
    phase: 'idle' as const,
    pending: 0,
    inFlight: 0,
    lastRecorded: 0,
    // Names the pair whenever anything is un-banked. `null` at rest, because
    // nothing is being held on anyone's behalf.
    pendingLabel: null as string | null,
    remainingMs: 0,
    windowMs: ONE_TAP_WINDOW_MS,
    error: null,
    tap: jest.fn(),
    undoOne: jest.fn(),
    flush: jest.fn(() => Promise.resolve()),
    retry: jest.fn(),
    discard: jest.fn(),
    ...over,
  };
  return { ...base, unbanked: base.pending + base.inFlight };
}

const renderLane = (props: Partial<React.ComponentProps<typeof KioskOneTapLane>> = {}) =>
  render(
    <KioskOneTapLane
      oneTap={oneTapState()}
      operatorName="Bob T"
      atCeiling={false}
      blocked={false}
      online
      {...props}
    />
  );

const lane = () => screen.getByTestId('kiosk-onetap');
const addButton = () => screen.getByTestId('kiosk-onetap-add');
const undoButton = () => screen.getByTestId('kiosk-onetap-undo');

describe('KioskOneTapLane', () => {
  describe('the three states an operator must never confuse', () => {
    it('PENDING says the pieces are not yet recorded, and how long is left to undo', () => {
      renderLane({
        oneTap: oneTapState({ phase: 'pending', pending: 3, remainingMs: 2400 }),
      });

      expect(lane()).toHaveAttribute('data-phase', 'pending');
      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveTextContent('3');
      expect(status).toHaveTextContent(/not yet recorded/i);
      // The countdown is what tells the operator the way out is still open.
      expect(screen.getByTestId('kiosk-onetap-countdown')).toHaveTextContent('recording in 3s');
      // Not an alert — nothing has gone wrong.
      expect(status).toHaveAttribute('role', 'status');
    });

    it('RECORDED says recorded, and offers no undo anywhere — a posted report has none', () => {
      renderLane({
        oneTap: oneTapState({ phase: 'recorded', lastRecorded: 4 }),
      });

      expect(lane()).toHaveAttribute('data-phase', 'recorded');
      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveTextContent('4');
      expect(status).toHaveTextContent(/recorded/i);
      expect(status).not.toHaveTextContent(/not yet recorded/i);
      expect(screen.queryByTestId('kiosk-onetap-countdown')).not.toBeInTheDocument();
      // The correction screen is the only path after a post; the control that
      // implies otherwise is disabled, not merely absent (see geometry below).
      expect(undoButton()).toBeDisabled();
    });

    it('NOT SAVED interrupts with role="alert" and the server\'s words verbatim', () => {
      // Verbatim: an operator reading "Quantity (14) cannot exceed quantity
      // ordered (13)" can act on it; "Could not save" sends them to a supervisor.
      const detail = 'Quantity (14) cannot exceed quantity ordered (13)';
      renderLane({
        oneTap: oneTapState({ phase: 'failed', pending: 2, error: detail }),
      });

      expect(lane()).toHaveAttribute('data-phase', 'failed');
      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveAttribute('role', 'alert');
      expect(status).toHaveTextContent(detail);
      expect(status).toHaveTextContent(/not saved/i);
      // The count is still on screen and still theirs.
      expect(status).toHaveTextContent('2');
      // RETRY lives inside the status block, deliberately out of the button row.
      expect(screen.getByTestId('kiosk-onetap-retry')).toBeEnabled();
    });

    it('IDLE invites the first tap and names the window', () => {
      renderLane();

      expect(lane()).toHaveAttribute('data-phase', 'idle');
      expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(/tap once per finished piece · 5s to undo/i);
    });

    it('SAVING distinguishes what is on the wire from what is still queued behind it', () => {
      renderLane({
        oneTap: oneTapState({ phase: 'saving', inFlight: 3, pending: 2 }),
      });

      expect(lane()).toHaveAttribute('data-phase', 'saving');
      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveTextContent('3');
      expect(status).toHaveTextContent(/recording/i);
      expect(status).toHaveTextContent(/\+2 more pending/i);
    });
  });

  describe('ORPHANED — the delta outlived the pair that made it', () => {
    /**
     * The compliance case, rendered. These pieces are a claim that a SPECIFIC
     * operator made parts on a SPECIFIC operation; the station has since moved
     * on. The lane's job here is to hold the count, say whose it is, and offer
     * nobody a way to launder it into the current pair's name.
     */
    const ORPHAN_LABEL = 'Alice Reed · WO-2026-0142 · Op 20 Weld';
    const orphaned = () =>
      oneTapState({ phase: 'orphaned', pending: 2, pendingLabel: ORPHAN_LABEL });

    it('holds the count on screen — the pieces were made, so they are never hidden', () => {
      renderLane({ oneTap: orphaned() });

      expect(lane()).toHaveAttribute('data-phase', 'orphaned');
      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveTextContent('2');
      expect(status).toHaveTextContent(/not saved/i);
    });

    it('names the pair the pieces belong to, not just the count', () => {
      // A bare "2 pcs held" is unactionable — the whole recovery is "that
      // operator scans back onto that job", so both halves have to be legible.
      renderLane({ oneTap: orphaned() });

      const status = screen.getByTestId('kiosk-onetap-status');
      expect(status).toHaveTextContent(ORPHAN_LABEL);
      expect(status).toHaveTextContent(/another operator/i);
    });

    it('falls back to a neutral phrase rather than an empty name when the label is gone', () => {
      renderLane({ oneTap: oneTapState({ phase: 'orphaned', pending: 2, pendingLabel: null }) });

      expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(/tapped by another operator/i);
    });

    it('refuses another tap — merging two operators into one report is the hazard', () => {
      renderLane({ oneTap: orphaned() });

      expect(addButton()).toBeDisabled();
    });

    it('refuses UNDO — nobody here may edit down an absent operator\'s count', () => {
      renderLane({ oneTap: orphaned() });

      expect(undoButton()).toBeInTheDocument();
      expect(undoButton()).toBeDisabled();
    });

    it('offers NO "save anyway" — the lane holds exactly two controls, both dark', async () => {
      // The property, stated as a count rather than as an absence of one string:
      // any newly-added escape hatch (SAVE ANYWAY / RECORD / POST / DISCARD)
      // fails this, whatever it ends up being called.
      const user = userEvent.setup();
      const state = orphaned();
      renderLane({ oneTap: state });

      const controls = within(lane()).getAllByRole('button');
      expect(controls).toHaveLength(2);
      expect(controls).toEqual([addButton(), undoButton()]);
      expect(screen.queryByTestId('kiosk-onetap-retry')).not.toBeInTheDocument();

      // …and the two that ARE there cannot be coaxed into sending it.
      await user.click(addButton());
      await user.click(undoButton());
      expect(state.tap).not.toHaveBeenCalled();
      expect(state.undoOne).not.toHaveBeenCalled();
      expect(state.flush).not.toHaveBeenCalled();
      expect(state.retry).not.toHaveBeenCalled();
      expect(state.discard).not.toHaveBeenCalled();
    });
  });

  describe('who the lane is recording as', () => {
    // The heading says it too, but the heading is not where a thumb is looking.
    // The quiet failure is one person scanning, walking off, and the next person
    // tapping twenty parts under the first name — with every tap resetting the
    // idle timer, so inactivity never bounds it either.
    it.each([
      ['idle', oneTapState()],
      ['pending', oneTapState({ phase: 'pending', pending: 1, remainingMs: 5000 })],
      ['saving', oneTapState({ phase: 'saving', inFlight: 2 })],
      ['recorded', oneTapState({ phase: 'recorded', lastRecorded: 2 })],
      ['failed', oneTapState({ phase: 'failed', pending: 2, error: 'Operation is on hold' })],
      ['orphaned', oneTapState({ phase: 'orphaned', pending: 2, pendingLabel: 'Alice Reed · Op 20' })],
    ] as const)('names the operator in the %s phase', (_phase, state) => {
      renderLane({ oneTap: state, operatorName: 'Bob T' });

      expect(screen.getByTestId('kiosk-onetap-operator')).toHaveTextContent(/recording as\s*Bob T/i);
    });

    it('names the pair holding the pieces SEPARATELY from who is standing there', () => {
      // Both names on screen at once, and they disagree — that disagreement is
      // the notice. Collapsing them to one line is how a held count starts
      // reading as the current operator's.
      renderLane({
        oneTap: oneTapState({ phase: 'orphaned', pending: 2, pendingLabel: 'Alice Reed · Op 20' }),
        operatorName: 'Bob T',
      });

      expect(screen.getByTestId('kiosk-onetap-operator')).toHaveTextContent(/Bob T/);
      expect(screen.getByTestId('kiosk-onetap-operator')).not.toHaveTextContent(/Alice Reed/);
      expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(/Alice Reed/);
    });
  });

  describe('fixed geometry — UNDO never moves and never vanishes', () => {
    // The regression this pins: a vanishing UNDO let `+1 PIECE` grow back under
    // a thumb already in flight toward it, recording a piece instead of removing
    // one. Both controls are therefore always present, at the same size, in
    // every phase.
    it.each([
      ['idle', oneTapState()],
      ['recorded', oneTapState({ phase: 'recorded', lastRecorded: 2 })],
      ['saving', oneTapState({ phase: 'saving', inFlight: 2 })],
    ] as const)('renders UNDO in the %s phase, disabled rather than removed', (_phase, state) => {
      renderLane({ oneTap: state });

      expect(undoButton()).toBeInTheDocument();
      expect(undoButton()).toBeDisabled();
      // …and `+1` is still right there beside it, same row.
      expect(addButton()).toBeInTheDocument();
    });

    it('enables UNDO exactly while there is a tap to take back', () => {
      renderLane({ oneTap: oneTapState({ phase: 'pending', pending: 1, remainingMs: 5000 }) });

      expect(undoButton()).toBeEnabled();
      expect(addButton()).toBeInTheDocument();
    });

    it('carries an accessible name, since the label alone reads as punctuation', () => {
      renderLane({ oneTap: oneTapState({ phase: 'pending', pending: 1 }) });
      expect(screen.getByRole('button', { name: /undo one piece/i })).toBe(undoButton());
    });

    it('takes back one tap per press, mirroring the button it undoes', async () => {
      const user = userEvent.setup();
      const state = oneTapState({ phase: 'pending', pending: 2, remainingMs: 5000 });
      renderLane({ oneTap: state });

      await user.click(undoButton());
      expect(state.undoOne).toHaveBeenCalledTimes(1);
      expect(state.tap).not.toHaveBeenCalled();
    });
  });

  describe('when a tap cannot land', () => {
    it('goes dark at the ceiling, and says why, rather than keying a guaranteed refusal', () => {
      // The server refuses an over-target report before any mutation, so per the
      // repo's non-optimistic convention the tap goes away instead.
      renderLane({ atCeiling: true });

      expect(addButton()).toBeDisabled();
      expect(screen.getByTestId('kiosk-onetap-status')).toHaveTextContent(/already at its target/i);
    });

    it('goes dark offline, and points at the offline banner for the reason', () => {
      // Every other mutation control on this kiosk goes dark offline; a tap that
      // cannot reach the server must not look like one that did.
      renderLane({ online: false, offlineHintId: 'kiosk-offline-hint' });

      expect(addButton()).toBeDisabled();
      expect(addButton()).toHaveAttribute('aria-describedby', 'kiosk-offline-hint');
    });

    it('drops the offline pointer once the station is back online', () => {
      renderLane({ online: true, offlineHintId: 'kiosk-offline-hint' });

      expect(addButton()).toBeEnabled();
      expect(addButton()).not.toHaveAttribute('aria-describedby');
    });

    it('refuses RETRY offline too, and points at the same banner', () => {
      renderLane({
        oneTap: oneTapState({ phase: 'failed', pending: 1, error: 'Network unreachable' }),
        online: false,
        offlineHintId: 'kiosk-offline-hint',
      });

      const retry = screen.getByTestId('kiosk-onetap-retry');
      expect(retry).toBeDisabled();
      expect(retry).toHaveAttribute('aria-describedby', 'kiosk-offline-hint');
    });

    it('goes dark while the host blocks mutations', () => {
      renderLane({ blocked: true });
      expect(addButton()).toBeDisabled();
    });

    it('still counts a tap while a previous batch is on the wire', async () => {
      // A run of parts must keep counting while the last batch is still in
      // flight — `busy` is deliberately not a reason to refuse a tap.
      const user = userEvent.setup();
      const state = oneTapState({ phase: 'saving', inFlight: 3 });
      renderLane({ oneTap: state, blocked: false });

      expect(addButton()).toBeEnabled();
      await user.click(addButton());
      expect(state.tap).toHaveBeenCalledTimes(1);
    });
  });
});
