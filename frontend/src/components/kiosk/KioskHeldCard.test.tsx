/**
 * KioskHeldCard — a held operation stays VISIBLE, says why it stopped, and is
 * never startable.
 *
 * Fixtures come from heldOperationFixtures, which mirrors the server's actual
 * `_held_job_row` output — `hold` is a NESTED block, and `hold.blocker` inside
 * it. An earlier version of this suite asserted a flat `item.blocker`; it passed
 * against a payload the server never sends, which is precisely how a dead reason
 * display would have shipped. The nesting is pinned explicitly below.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskHeldCard from './KioskHeldCard';
import {
  BARE_HOLD,
  HELD_ROW,
  HOLD_WITH_BLOCKER_STATION,
  UNRECORDED_HOLD,
  heldRowWith,
} from './heldOperationFixtures';

describe('KioskHeldCard', () => {
  it('marks the job as on hold and shows its progress', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} />);

    expect(screen.getByTestId('kiosk-held-badge')).toHaveTextContent(/on hold/i);
    expect(screen.getByText('WO-HELD-0001')).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-held-card')).toHaveTextContent('12');
  });

  it('reads the reason out of the NESTED hold.blocker the server actually sends', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} />);

    // If this ever regresses to a flat item.blocker, these all go blank.
    expect(screen.getByTestId('kiosk-held-reason')).toHaveTextContent('Machine down');
    expect(screen.getByTestId('kiosk-held-note')).toHaveTextContent('Z-axis alarm 4012');
    expect(screen.getByTestId('kiosk-held-severity')).toHaveTextContent('High');
  });

  it('reads the attribution out of hold.held_by_name / hold.held_at', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} />);

    const attribution = screen.getByTestId('kiosk-held-attribution');
    expect(attribution).toHaveTextContent('Held by Dana R.');
    // Central, not the viewer's timezone: 19:14Z is 2:14 PM.
    expect(attribution).toHaveTextContent('2:14');
  });

  it('renders the operator note VERBATIM', () => {
    const item = heldRowWith({
      ...HELD_ROW.hold!,
      blocker: { ...HELD_ROW.hold!.blocker!, note: 'Coolant low <check> line 3' },
    });
    render(<KioskHeldCard item={item} onResume={jest.fn()} />);
    expect(screen.getByTestId('kiosk-held-note')).toHaveTextContent('Coolant low <check> line 3');
  });

  it('still names WHO stopped a BARE hold, which files no blocker at all', () => {
    // The accidental fat-finger case. Gating attribution on the blocker would
    // leave exactly this card anonymous AND reasonless — the one case that most
    // needs to read as an accident.
    render(<KioskHeldCard item={heldRowWith(BARE_HOLD)} onResume={jest.fn()} />);

    expect(screen.getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
    expect(screen.getByTestId('kiosk-held-no-blocker')).toHaveTextContent(/no reason given/i);
    // Not the "nothing recorded at all" copy — we do know who.
    expect(screen.queryByTestId('kiosk-held-no-reason')).not.toBeInTheDocument();
  });

  it('says nothing was recorded only when BOTH reason and attribution are absent', () => {
    render(<KioskHeldCard item={heldRowWith(UNRECORDED_HOLD)} onResume={jest.fn()} />);

    expect(screen.getByTestId('kiosk-held-no-reason')).toHaveTextContent(/no hold reason recorded/i);
    expect(screen.queryByTestId('kiosk-held-attribution')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kiosk-held-note')).not.toBeInTheDocument();
    // Still resumable — an unrecorded hold is exactly what needs lifting.
    expect(screen.getByTestId('kiosk-held-resume')).toBeEnabled();
  });

  it('renders sanely when the hold block is missing entirely', () => {
    render(<KioskHeldCard item={heldRowWith(null)} onResume={jest.fn()} />);
    expect(screen.getByTestId('kiosk-held-no-reason')).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-held-resume')).toBeEnabled();
  });

  it('is NOT startable: the only interactive element is Resume', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} />);

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAttribute('data-testid', 'kiosk-held-resume');
    // The card body itself must not be a tap target the way KioskQueueCard is.
    expect(screen.getByTestId('kiosk-held-card')).not.toHaveAttribute('role', 'button');
  });

  it('hands the item back on Resume — it never resumes on its own', async () => {
    const onResume = jest.fn();
    render(<KioskHeldCard item={HELD_ROW} onResume={onResume} />);

    await userEvent.click(screen.getByTestId('kiosk-held-resume'));

    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onResume).toHaveBeenCalledWith(HELD_ROW);
  });

  it('disables Resume while a mutation is in flight or the station is offline', async () => {
    const onResume = jest.fn();
    render(<KioskHeldCard item={HELD_ROW} onResume={onResume} disabled offlineHintId="hint" />);

    const resume = screen.getByTestId('kiosk-held-resume');
    expect(resume).toBeDisabled();
    expect(resume).toHaveAttribute('aria-describedby', 'hint');
    await userEvent.click(resume);
    expect(onResume).not.toHaveBeenCalled();
  });

  it('names the job in the Resume accessible label, so the verb is never ambiguous', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} />);
    expect(screen.getByRole('button', { name: /Resume work order WO-HELD-0001/i })).toBeInTheDocument();
  });

  it('renders the same information at crew-station density', () => {
    render(<KioskHeldCard item={HELD_ROW} onResume={jest.fn()} size="crew" />);

    expect(screen.getByTestId('kiosk-held-badge')).toHaveTextContent(/on hold/i);
    expect(screen.getByTestId('kiosk-held-note')).toHaveTextContent('Z-axis alarm 4012');
    expect(screen.getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  describe('when the server withheld the blocker free text (station payload)', () => {
    const stationRow = heldRowWith(HOLD_WITH_BLOCKER_STATION);

    it('renders no note, because the payload carries none to render', () => {
      render(<KioskHeldCard item={stationRow} onResume={jest.fn()} size="crew" />);

      expect(screen.queryByTestId('kiosk-held-note')).not.toBeInTheDocument();
      expect(screen.getByTestId('kiosk-held-card')).not.toHaveTextContent('Z-axis alarm 4012');
      expect(screen.getByTestId('kiosk-held-card')).not.toHaveTextContent('Machine Down: Deburr');
    });

    it('says a note EXISTS rather than letting silence read as "no reason given"', () => {
      render(<KioskHeldCard item={stationRow} onResume={jest.fn()} size="crew" />);

      expect(screen.getByTestId('kiosk-held-note-withheld')).toHaveTextContent(/written note was recorded/i);
      expect(screen.queryByTestId('kiosk-held-no-blocker')).not.toBeInTheDocument();
      expect(screen.queryByTestId('kiosk-held-no-reason')).not.toBeInTheDocument();
    });

    it('keeps category, severity and attribution — what tells a real hold from a mis-tap', () => {
      render(<KioskHeldCard item={stationRow} onResume={jest.fn()} size="crew" />);

      expect(screen.getByTestId('kiosk-held-reason')).toHaveTextContent('Machine down');
      expect(screen.getByTestId('kiosk-held-severity')).toHaveTextContent('High');
      expect(screen.getByTestId('kiosk-held-attribution')).toHaveTextContent('Held by Dana R.');
    });

    it('stays silent when there was no free text to withhold in the first place', () => {
      const noText = heldRowWith({
        ...HOLD_WITH_BLOCKER_STATION,
        blocker: { ...HOLD_WITH_BLOCKER_STATION.blocker!, has_note: false },
      });
      render(<KioskHeldCard item={noText} onResume={jest.fn()} size="crew" />);

      expect(screen.queryByTestId('kiosk-held-note-withheld')).not.toBeInTheDocument();
    });
  });
});

/**
 * The Resume button's ACCESSIBLE NAME. Same defect as the queue card's: the
 * visible label ran through `formatOperationLabel` while the aria-label
 * interpolated the raw column, so a legacy row (`Op 20`) and a new row (`20`)
 * announced the same operation two different ways.
 */
describe('KioskHeldCard accessible name', () => {
  const resumeNameFor = (operationNumber: string, operationName = '') => {
    const { unmount } = render(
      <KioskHeldCard
        item={{ ...HELD_ROW, operation_number: operationNumber, operation_name: operationName }}
        onResume={jest.fn()}
      />
    );
    const label = screen.getByTestId('kiosk-held-resume').getAttribute('aria-label') || '';
    unmount();
    return label;
  };

  it('announces the same operation identically for both stored spellings', () => {
    const legacy = resumeNameFor('Op 20');
    const current = resumeNameFor('20');

    expect(legacy).toBe('Resume work order WO-HELD-0001, operation 20');
    expect(legacy).toBe(current);
    expect(legacy).not.toMatch(/Op\s*20/);
  });

  it('still prefers the operation NAME when the server sends one', () => {
    expect(resumeNameFor('Op 20', 'Deburr')).toBe('Resume work order WO-HELD-0001, operation Deburr');
  });
});
