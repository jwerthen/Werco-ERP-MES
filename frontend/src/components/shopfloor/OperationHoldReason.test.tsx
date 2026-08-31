/**
 * `<OperationHoldReason>` — the shared "why is this held?" disclosure.
 *
 * Three desk surfaces render it (Time Clock's held section, Shop Floor
 * Operations' held card, and its mobile Next-Job strip), and each of them puts a
 * Clear Hold button directly underneath. So this component is the only thing
 * standing between an operator and lifting somebody else's quality stop without
 * ever seeing that there was one — which is the defect the whole release exists
 * to close. Pinned directly, rather than only through the two pages, because the
 * pages exercise one payload shape each and the interesting cases are the ones
 * neither of them happens to send.
 *
 * The rules below are correctness, not copy:
 *
 * 1. **Reason and attribution are INDEPENDENT.** A BARE hold (no note, category
 *    OTHER — the accidental fat-finger case) files no blocker, so it has a
 *    holder and no reason; a hold placed before either record existed has
 *    neither. Gating one on the other renders the accident as anonymous AND
 *    reasonless, the single case that most needs to read as an accident.
 * 2. **"No reason recorded" is only said when NOTHING is known.** Saying it over
 *    a hold that has a holder is a false statement about a quality record.
 * 3. **A WITHHELD note is stated, never silently dropped.** No payload reaching
 *    this component today sets `free_text_withheld` (both desk endpoints serve
 *    identified user sessions), so this branch is unreachable from either page's
 *    fixtures — which is exactly why it is pinned here. Silence where a note
 *    exists reads as "no reason given", the one way withholding could actively
 *    mislead.
 * 4. **An absent `hold` renders NOTHING.** The SPA and the API deploy
 *    independently; a build carrying this component can be live against a
 *    backend that does not send the block yet, and an empty amber box would
 *    invent a hold with no reason.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import OperationHoldReason from './OperationHoldReason';
import type { OperationHold } from '../../types';

const FULL_HOLD: OperationHold = {
  held_at: '2026-08-11T19:14:00Z',
  held_by_user_id: 12,
  held_by_name: 'Dana R.',
  blocker: {
    id: 55,
    category: 'machine_down',
    severity: 'critical',
    status: 'open',
    title: 'Machine Down: OP20 Laser Cut',
    note: 'spindle bearing failed — do not run',
    has_note: true,
    free_text_withheld: false,
    reported_at: '2026-08-11T19:14:00Z',
    reported_by_user_id: 12,
    reported_by_name: 'Dana R.',
  },
};

describe('OperationHoldReason', () => {
  it('renders the category, the severity, the verbatim note and who held it', () => {
    render(<OperationHoldReason hold={FULL_HOLD} />);

    expect(screen.getByTestId('operation-hold-reason-category')).toHaveTextContent('Machine down');
    expect(screen.getByTestId('operation-hold-reason-severity')).toHaveTextContent('Critical');
    // Verbatim — the UI never rewords what an operator wrote on a quality record.
    expect(screen.getByTestId('operation-hold-reason-note')).toHaveTextContent(
      'spindle bearing failed — do not run'
    );
    expect(screen.getByTestId('operation-hold-reason-attribution')).toHaveTextContent(/^Held by Dana R\. · /);
    // Nothing claims the reason is missing when it plainly is not.
    expect(screen.queryByTestId('operation-hold-reason-unrecorded')).not.toBeInTheDocument();
    expect(screen.queryByTestId('operation-hold-reason-no-blocker')).not.toBeInTheDocument();
  });

  it('names the holder of a BARE hold and says no reason was given — not that nothing is known', () => {
    // No blocker at all: the mis-tap. Attribution survives on its own.
    render(<OperationHoldReason hold={{ ...FULL_HOLD, blocker: null }} />);

    expect(screen.getByTestId('operation-hold-reason-no-blocker')).toHaveTextContent('No reason given');
    expect(screen.getByTestId('operation-hold-reason-attribution')).toHaveTextContent('Held by Dana R.');
    expect(screen.queryByTestId('operation-hold-reason-unrecorded')).not.toBeInTheDocument();
  });

  it('says the reason was not recorded ONLY when neither half is known', () => {
    render(
      <OperationHoldReason hold={{ held_at: null, held_by_user_id: null, held_by_name: null, blocker: null }} />
    );

    expect(screen.getByTestId('operation-hold-reason-unrecorded')).toHaveTextContent('No hold reason recorded');
    expect(screen.queryByTestId('operation-hold-reason-attribution')).not.toBeInTheDocument();
    // "No reason given" would be a second, redundant claim on the same panel.
    expect(screen.queryByTestId('operation-hold-reason-no-blocker')).not.toBeInTheDocument();
  });

  it('renders the WHEN alone when the server recorded a time but no name', () => {
    render(
      <OperationHoldReason
        hold={{ held_at: '2026-08-11T19:14:00Z', held_by_user_id: null, held_by_name: null, blocker: null }}
      />
    );

    const attribution = screen.getByTestId('operation-hold-reason-attribution');
    // Never "Held by —", which reads like an answer.
    expect(attribution.textContent).not.toMatch(/Held by/);
    expect(attribution.textContent).toMatch(/^Held /);
  });

  it('states that a note exists but was withheld, rather than going quiet', () => {
    render(
      <OperationHoldReason
        hold={{
          ...FULL_HOLD,
          blocker: {
            ...FULL_HOLD.blocker!,
            title: null,
            note: null,
            has_note: true,
            free_text_withheld: true,
          },
        }}
      />
    );

    expect(screen.getByTestId('operation-hold-reason-note-withheld')).toHaveTextContent(
      /written note was recorded but is not shown here/i
    );
    // The category still renders — that is what tells a deliberate stop from a mis-tap.
    expect(screen.getByTestId('operation-hold-reason-category')).toHaveTextContent('Machine down');
    expect(screen.queryByTestId('operation-hold-reason-no-blocker')).not.toBeInTheDocument();
  });

  it('does not claim a note was withheld when none was ever written', () => {
    render(
      <OperationHoldReason
        hold={{
          ...FULL_HOLD,
          blocker: { ...FULL_HOLD.blocker!, note: null, has_note: false, free_text_withheld: true },
        }}
      />
    );

    expect(screen.queryByTestId('operation-hold-reason-note-withheld')).not.toBeInTheDocument();
  });

  it('renders nothing at all when the payload carries no hold block', () => {
    // A backend that predates the field. An empty amber panel would invent a hold.
    const { container } = render(<OperationHoldReason hold={undefined} />);
    expect(container).toBeEmptyDOMElement();

    const { container: nullContainer } = render(<OperationHoldReason hold={null} />);
    expect(nullContainer).toBeEmptyDOMElement();
  });

  it('names an unrecognized category instead of dropping it', () => {
    // A category the kiosk vocabulary does not know is still information.
    render(
      <OperationHoldReason
        hold={{ ...FULL_HOLD, blocker: { ...FULL_HOLD.blocker!, category: 'fixture_missing' } }}
      />
    );

    expect(screen.getByTestId('operation-hold-reason-category')).toHaveTextContent('Fixture missing');
  });

  it('renders a blocker title that carries the only written reason', () => {
    // An office-created blocker routinely puts its free text in the TITLE with an
    // empty note (see `_blocker_free_text_recorded` on the backend). Dropping it
    // rendered a bare category over a hold that HAD a written reason — and
    // `hasHoldReason` counts a title, so "No reason given" never fired either.
    render(
      <OperationHoldReason
        hold={{
          ...FULL_HOLD,
          blocker: { ...FULL_HOLD.blocker!, title: 'NCR-1042 cracked welds, ACME rejected lot', note: null },
        }}
      />
    );

    expect(screen.getByTestId('operation-hold-reason-title')).toHaveTextContent(
      'NCR-1042 cracked welds, ACME rejected lot'
    );
    expect(screen.queryByTestId('operation-hold-reason-note')).not.toBeInTheDocument();
  });

  it('drops a title that only restates the category chip above it', () => {
    render(
      <OperationHoldReason
        hold={{ ...FULL_HOLD, blocker: { ...FULL_HOLD.blocker!, title: 'Machine down', note: null } }}
      />
    );

    // The stutter reads as a rendering bug on the one panel that has to be
    // believed; the category chip already says it.
    expect(screen.queryByTestId('operation-hold-reason-title')).not.toBeInTheDocument();
    expect(screen.getByTestId('operation-hold-reason-category')).toHaveTextContent('Machine down');
  });

  it('honours the caller-supplied test id so a page can name its own instance', () => {
    render(<OperationHoldReason hold={FULL_HOLD} testId="shop-floor-hold-reason-202" />);

    expect(screen.getByTestId('shop-floor-hold-reason-202')).toBeInTheDocument();
    expect(screen.getByTestId('shop-floor-hold-reason-202-note')).toBeInTheDocument();
  });
});
