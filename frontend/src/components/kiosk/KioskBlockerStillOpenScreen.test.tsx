/**
 * KioskBlockerStillOpenScreen — after a resume that left blockers OPEN.
 *
 * This is the compliance-facing half of the resume flow. The backend returns
 * `open_blockers` precisely so operation status and blocker status cannot
 * silently diverge; if the kiosk swallowed that list, a live quality stop would
 * read as cleared to the next person who walked up to the machine. So the
 * warning is a screen with an explicit exit, not a toast that ages out under
 * the 15s queue poll, and the server's own blocker text is rendered verbatim.
 */

import React from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import KioskBlockerStillOpenScreen from './KioskBlockerStillOpenScreen';
import type { ResumeOpenBlocker } from '../../types';

const MACHINE: ResumeOpenBlocker = {
  id: 5,
  title: 'Machine Down: OP20 Deburr',
  category: 'machine_down',
  severity: 'high',
  status: 'open',
};

const MATERIAL: ResumeOpenBlocker = {
  id: 6,
  title: 'Material Missing: OP20 Deburr',
  category: 'material_missing',
  severity: 'critical',
  status: 'acknowledged',
};

describe('KioskBlockerStillOpenScreen', () => {
  it('confirms the job resumed while naming the problem that did NOT close', () => {
    render(
      <KioskBlockerStillOpenScreen
        blockers={[MACHINE]}
        jobLabel="WO-HELD-0001 · Op 20 Deburr"
        doneLabel="Back to queue"
        onDone={jest.fn()}
      />
    );

    expect(screen.getByText(/job resumed/i)).toBeInTheDocument();
    expect(screen.getByText('WO-HELD-0001 · Op 20 Deburr')).toBeInTheDocument();
    expect(screen.getByText(/hold still open/i)).toBeInTheDocument();
  });

  it("renders the server's blocker title VERBATIM", () => {
    render(
      <KioskBlockerStillOpenScreen
        blockers={[MACHINE]}
        jobLabel="job"
        doneLabel="Back to queue"
        onDone={jest.fn()}
      />
    );

    const list = screen.getByTestId('kiosk-open-blockers');
    expect(within(list).getByText('Machine Down: OP20 Deburr')).toBeInTheDocument();
    // Category + severity ride alongside, they do not replace the title.
    expect(list).toHaveTextContent('Machine down');
    expect(list).toHaveTextContent('High');
  });

  it('lists every still-open blocker and pluralises the heading', () => {
    render(
      <KioskBlockerStillOpenScreen
        blockers={[MACHINE, MATERIAL]}
        jobLabel="job"
        doneLabel="Back to board"
        onDone={jest.fn()}
      />
    );

    expect(screen.getByText(/2 holds still open/i)).toBeInTheDocument();
    const items = within(screen.getByTestId('kiosk-open-blockers')).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Machine Down: OP20 Deburr');
    expect(items[1]).toHaveTextContent('Material Missing: OP20 Deburr');
  });

  it('tells the operator the record does not clear itself', () => {
    render(
      <KioskBlockerStillOpenScreen
        blockers={[MACHINE]}
        jobLabel="job"
        doneLabel="Back to queue"
        onDone={jest.fn()}
      />
    );
    // The kiosk cannot resolve a blocker, so the honest instruction is to hand
    // it to somebody who can — otherwise a mis-tap leaves a phantom behind.
    expect(screen.getByTestId('kiosk-blocker-open-followup')).toHaveTextContent(/tell a supervisor/i);
  });

  it('needs an explicit tap to leave, so a queue refresh cannot yank it away', async () => {
    const onDone = jest.fn();
    render(
      <KioskBlockerStillOpenScreen
        blockers={[MACHINE]}
        jobLabel="job"
        doneLabel="Back to board"
        onDone={onDone}
      />
    );

    const done = screen.getByTestId('kiosk-blocker-open-done');
    expect(done).toHaveTextContent('Back to board');
    await userEvent.click(done);
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
