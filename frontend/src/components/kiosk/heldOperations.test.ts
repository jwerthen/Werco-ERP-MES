/**
 * heldOperations — the vocabulary behind the kiosk's held-job cards.
 *
 * Fixtures here mirror `_held_job_row` / `HoldContext` in
 * `backend/app/api/endpoints/shop_floor.py` field for field. An earlier version
 * of this suite asserted a FLAT `item.blocker`, which the server never sends —
 * it passed green against a payload that does not exist, and the reason display
 * would have been dead on arrival in production. Shape fidelity is the point.
 *
 * The rule under test throughout: reason (`hold.blocker`) and attribution
 * (`hold.held_by_name` / `hold.held_at`) are INDEPENDENT, because a bare hold —
 * the accidental fat-finger case — has the second without the first.
 */

import {
  formatHoldAttribution,
  hasHoldReason,
  holdIsUnexplained,
  holdReasonLabel,
  holdSeverityLabel,
  openBlockerLine,
  stillOpenBlockers,
} from './heldOperations';
import type { OperationHold, ResumeOpenBlocker } from '../../types';

/** A hold that filed a blocker: note or non-OTHER category. */
const HOLD_WITH_BLOCKER: OperationHold = {
  held_at: '2026-08-11T19:14:00Z',
  held_by_user_id: 12,
  held_by_name: 'Dana R.',
  blocker: {
    id: 5,
    category: 'machine_down',
    severity: 'high',
    status: 'open',
    title: 'Machine Down: Deburr',
    note: 'Z-axis alarm 4012',
    reported_at: '2026-08-11T19:14:00Z',
    reported_by_user_id: 12,
    reported_by_name: 'Dana R.',
  },
};

/**
 * A BARE hold — no note, category OTHER. Files no blocker; provenance comes
 * from the `operation_hold` event alone. This is the accidental case.
 */
const BARE_HOLD: OperationHold = {
  held_at: '2026-08-11T19:14:00Z',
  held_by_user_id: 12,
  held_by_name: 'Dana R.',
  blocker: null,
};

/** Neither record was written — a real state the server never guesses past. */
const UNRECORDED_HOLD: OperationHold = {
  held_at: null,
  held_by_user_id: null,
  held_by_name: null,
  blocker: null,
};

describe('holdReasonLabel', () => {
  it('reuses the kiosk hold vocabulary so the label matches the tile that placed it', () => {
    expect(holdReasonLabel('machine_down')).toBe('Machine down');
    expect(holdReasonLabel('quality_hold')).toBe('Quality hold');
  });

  it('humanises a category the kiosk does not know rather than dropping it', () => {
    expect(holdReasonLabel('customer_stop_work')).toBe('Customer stop work');
  });

  it('returns null for an absent category so callers can omit the line entirely', () => {
    expect(holdReasonLabel(null)).toBeNull();
    expect(holdReasonLabel(undefined)).toBeNull();
    expect(holdReasonLabel('   ')).toBeNull();
  });
});

describe('holdSeverityLabel', () => {
  it('capitalises the raw enum value', () => {
    expect(holdSeverityLabel('critical')).toBe('Critical');
  });

  it('returns null when unset', () => {
    expect(holdSeverityLabel(null)).toBeNull();
  });
});

describe('formatHoldAttribution', () => {
  it('names who held it and when, in Central time', () => {
    const line = formatHoldAttribution(HOLD_WITH_BLOCKER);
    expect(line).toContain('Held by Dana R.');
    // 19:14Z is 2:14 PM Central — the shop clock, not the viewer's.
    expect(line).toContain('2:14');
  });

  it('works for a BARE hold, which has no blocker at all', () => {
    // The accidental case still says who stopped the job.
    expect(formatHoldAttribution(BARE_HOLD)).toContain('Held by Dana R.');
  });

  it('reads held_by_name / held_at, NOT the blocker reporter', () => {
    // The server already resolved most-recent-wins across the two records; a
    // fallback to the blocker's reporter here could only contradict it.
    const disagreeing: OperationHold = {
      ...HOLD_WITH_BLOCKER,
      held_by_name: 'Marco T.',
      blocker: { ...HOLD_WITH_BLOCKER.blocker!, reported_by_name: 'Dana R.' },
    };
    expect(formatHoldAttribution(disagreeing)).toContain('Held by Marco T.');
    expect(formatHoldAttribution(disagreeing)).not.toContain('Dana R.');
  });

  it('falls back to the half it has', () => {
    expect(formatHoldAttribution({ ...BARE_HOLD, held_at: null })).toBe('Held by Dana R.');
    expect(formatHoldAttribution({ ...BARE_HOLD, held_by_name: null })).toMatch(/^Held /);
  });

  it('returns null rather than printing "Held by —", which would read like an answer', () => {
    expect(formatHoldAttribution(null)).toBeNull();
    expect(formatHoldAttribution(undefined)).toBeNull();
    expect(formatHoldAttribution(UNRECORDED_HOLD)).toBeNull();
    expect(formatHoldAttribution({ ...UNRECORDED_HOLD, held_by_name: '  ', held_at: '' })).toBeNull();
  });
});

describe('hasHoldReason', () => {
  it('is true when a blocker carries reason text', () => {
    expect(hasHoldReason(HOLD_WITH_BLOCKER)).toBe(true);
  });

  it('is FALSE for a bare hold — no blocker means no reason text, however well attributed', () => {
    expect(hasHoldReason(BARE_HOLD)).toBe(false);
  });

  it('is false for an absent hold or an empty blocker', () => {
    expect(hasHoldReason(null)).toBe(false);
    expect(hasHoldReason(undefined)).toBe(false);
    expect(hasHoldReason(UNRECORDED_HOLD)).toBe(false);
    expect(
      hasHoldReason({
        ...BARE_HOLD,
        blocker: { ...HOLD_WITH_BLOCKER.blocker!, category: '', note: '  ', title: '' },
      })
    ).toBe(false);
  });
});

describe('holdIsUnexplained', () => {
  it('is true ONLY when there is neither a reason nor an attribution', () => {
    expect(holdIsUnexplained(UNRECORDED_HOLD)).toBe(true);
    expect(holdIsUnexplained(null)).toBe(true);
  });

  it('is false for a bare hold — "who" is still an explanation of sorts', () => {
    expect(holdIsUnexplained(BARE_HOLD)).toBe(false);
  });

  it('is false when a blocker explains it', () => {
    expect(holdIsUnexplained(HOLD_WITH_BLOCKER)).toBe(false);
  });
});

describe('stillOpenBlockers', () => {
  const blocker: ResumeOpenBlocker = {
    id: 5,
    title: 'Machine Down: OP20 Deburr',
    category: 'machine_down',
    severity: 'high',
    status: 'open',
  };

  it('returns the list the server sent', () => {
    expect(stillOpenBlockers({ open_blockers: [blocker] })).toEqual([blocker]);
  });

  it('reports none for an empty list — the caller then shows the plain success path', () => {
    expect(stillOpenBlockers({ open_blockers: [] })).toEqual([]);
  });

  it('tolerates a missing or null key rather than throwing on an older payload', () => {
    expect(stillOpenBlockers({})).toEqual([]);
    expect(stillOpenBlockers({ open_blockers: null })).toEqual([]);
    expect(stillOpenBlockers(null)).toEqual([]);
    expect(stillOpenBlockers(undefined)).toEqual([]);
  });
});

describe('openBlockerLine', () => {
  it("renders the server's own title VERBATIM — the kiosk never rewords a quality record", () => {
    expect(
      openBlockerLine({
        id: 1,
        title: 'Material Missing: OP10 Laser',
        category: 'material_missing',
        severity: 'high',
        status: 'open',
      })
    ).toBe('Material Missing: OP10 Laser');
  });

  it('falls back to the category label only when the title is blank', () => {
    expect(
      openBlockerLine({ id: 1, title: '  ', category: 'quality_hold', severity: 'high', status: 'open' })
    ).toBe('Quality hold');
  });

  it('never renders an empty line', () => {
    expect(openBlockerLine({ id: 1, title: '', category: '', severity: '', status: 'open' })).toBe('Open blocker');
  });
});
