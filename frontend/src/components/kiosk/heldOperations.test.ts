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
  clearHoldToast,
  formatHoldAttribution,
  hasHoldReason,
  holdFreeTextWithheld,
  holdIsUnexplained,
  holdReasonLabel,
  holdSeverityLabel,
  namedOpenBlockers,
  offTheBoardSentence,
  openBlockerLine,
  openBlockerMeta,
  openBlockersFreeTextWithheld,
  resolveBlockerOutcome,
  resumeToast,
  stillHeldSentence,
  stillOpenBlockers,
} from './heldOperations';
import type { OperationHold, ResumeOpenBlocker } from '../../types';
import type { BlockerOperationOutcome, WorkOrderBlockerWriteResult } from '../../types/aiForward';

/** A still-open blocker as an IDENTIFIED session gets it back from resume. */
const TITLED_BLOCKER: ResumeOpenBlocker = {
  id: 5,
  title: 'Machine Down: OP20 Deburr',
  category: 'machine_down',
  severity: 'high',
  status: 'open',
  has_note: true,
  free_text_withheld: false,
};

/**
 * The SAME blocker as a CREW STATION gets it: `title` ABSENT (the key is
 * missing, not blanked), `free_text_withheld` true and `has_note` saying a
 * written reason exists.
 */
const STATION_BLOCKER: ResumeOpenBlocker = {
  id: 5,
  category: 'machine_down',
  severity: 'high',
  status: 'open',
  has_note: true,
  free_text_withheld: true,
};

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

describe('holdFreeTextWithheld', () => {
  /**
   * The crew-station shape: a station token is served the blocker WITHOUT
   * `title`/`note` (the keys are absent, not blanked) plus the two booleans that
   * describe what happened.
   */
  const STATION_HOLD: OperationHold = {
    ...HOLD_WITH_BLOCKER,
    blocker: {
      id: 5,
      category: 'machine_down',
      severity: 'high',
      status: 'open',
      has_note: true,
      free_text_withheld: true,
      reported_at: '2026-08-11T19:14:00Z',
      reported_by_user_id: 12,
      reported_by_name: 'Dana R.',
    },
  };

  it('is true when a note EXISTS but this response withheld it', () => {
    expect(holdFreeTextWithheld(STATION_HOLD)).toBe(true);
  });

  it('is false on an identified session, where the text is actually present', () => {
    expect(holdFreeTextWithheld(HOLD_WITH_BLOCKER)).toBe(false);
  });

  it('is false when the station withheld nothing because there was nothing to withhold', () => {
    // Both flags matter: announcing a note that does not exist would send an
    // operator chasing a supervisor over a categorized hold with no text.
    expect(
      holdFreeTextWithheld({
        ...STATION_HOLD,
        blocker: { ...STATION_HOLD.blocker!, has_note: false },
      })
    ).toBe(false);
  });

  it('is false for a bare hold and for an absent hold — there is no blocker at all', () => {
    expect(holdFreeTextWithheld(BARE_HOLD)).toBe(false);
    expect(holdFreeTextWithheld(UNRECORDED_HOLD)).toBe(false);
    expect(holdFreeTextWithheld(null)).toBe(false);
    expect(holdFreeTextWithheld(undefined)).toBe(false);
  });

  it('is false on a payload predating the flags rather than guessing', () => {
    expect(holdFreeTextWithheld({ ...HOLD_WITH_BLOCKER, blocker: { ...HOLD_WITH_BLOCKER.blocker! } })).toBe(false);
  });
});

describe('resumeToast', () => {
  it('claims success only when the operation actually rejoined the queue', () => {
    expect(resumeToast({ status: 'ready' }, 'WO-1001')).toEqual({
      type: 'success',
      message: 'WO-1001 resumed',
    });
    expect(resumeToast({ status: 'in_progress' }, 'WO-1001').type).toBe('success');
  });

  it('does NOT claim success when the resume landed back on PENDING', () => {
    // Resume restores, it never releases: a hold lifted off a PENDING operation
    // (or one whose WO is still DRAFT, or whose predecessor is incomplete)
    // stays off the board. A green "resumed" would send the operator looking
    // for a card that is not going to appear.
    const toast = resumeToast({ status: 'pending' }, 'WO-1001');
    expect(toast.type).toBe('info');
    expect(toast.message).toMatch(/hold lifted/i);
    expect(toast.message).not.toMatch(/^WO-1001 resumed$/);
  });

  it('never renders a blank subject line', () => {
    expect(resumeToast({ status: 'ready' }, '  ').message).toBe('Operation resumed');
    expect(resumeToast(null, null).message).toBe('Operation resumed');
    expect(resumeToast(undefined, undefined).type).toBe('success');
  });
});

describe('clearHoldToast (the desk screens: ShopFloor + ShopFloorSimple)', () => {
  it('claims success only when the hold came off cleanly AND left nothing open', () => {
    expect(clearHoldToast({ status: 'ready', open_blockers: [] }, 'WO-1001')).toEqual({
      type: 'success',
      message: 'WO-1001 hold cleared',
    });
    expect(clearHoldToast({ status: 'in_progress', open_blockers: [] }, 'WO-1001').type).toBe('success');
  });

  it('warns — never succeeds — when the resume landed back on PENDING', () => {
    // The write worked, but the job did NOT come back to the board. Green here
    // sends the operator looking for a card that is not going to appear.
    const toast = clearHoldToast({ status: 'pending', open_blockers: [] }, 'WO-1001');
    expect(toast.type).toBe('warning');
    expect(toast.message).toMatch(/did not return to the queue/i);
  });

  it('warns and NAMES the blockers the resume did not resolve', () => {
    const toast = clearHoldToast({ status: 'ready', open_blockers: [TITLED_BLOCKER] }, 'WO-1001');
    expect(toast.type).toBe('warning');
    expect(toast.message).toContain('1 blocker still open');
    // The server's own title, verbatim — the UI never rewords a quality record.
    expect(toast.message).toContain('Machine Down: OP20 Deburr');
  });

  it('falls back to the category when the title is withheld, rather than going silent', () => {
    const toast = clearHoldToast({ status: 'ready', open_blockers: [STATION_BLOCKER] }, 'WO-1001');
    expect(toast.type).toBe('warning');
    expect(toast.message).toContain('Machine down');
  });

  it('composes ONE warning when the job stayed off the board AND a blocker is open', () => {
    // Two toasts for one tap read as two things happening, and the second would
    // push the first off the stack before it was read.
    const toast = clearHoldToast({ status: 'pending', open_blockers: [TITLED_BLOCKER] }, 'WO-1001');
    expect(toast.type).toBe('warning');
    expect(toast.message).toMatch(/did not return to the queue/i);
    expect(toast.message).toContain('Machine Down: OP20 Deburr');
    expect(toast.message.startsWith('WO-1001 hold cleared')).toBe(true);
  });

  it('pluralizes the blocker count', () => {
    const two = clearHoldToast(
      { status: 'ready', open_blockers: [TITLED_BLOCKER, { ...TITLED_BLOCKER, id: 6, title: 'Second stop' }] },
      'WO-1001'
    );
    expect(two.message).toContain('2 blockers still open');
    expect(two.message).toContain('Second stop');
  });

  it('tolerates an older payload with no open_blockers key', () => {
    expect(clearHoldToast({ status: 'ready' }, 'WO-1001').type).toBe('success');
    expect(clearHoldToast({}, 'WO-1001').type).toBe('success');
  });

  it('never renders a blank subject line', () => {
    expect(clearHoldToast({ status: 'ready' }, '  ').message).toBe('Operation hold cleared');
    expect(clearHoldToast(null, null).message).toBe('Operation hold cleared');
    expect(clearHoldToast(undefined, undefined).type).toBe('success');
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
    expect(openBlockerLine({ id: 1, title: '  ', category: 'quality_hold', severity: 'high', status: 'open' })).toBe(
      'Quality hold'
    );
  });

  it('never renders an empty line', () => {
    expect(openBlockerLine({ id: 1, title: '', category: '', severity: '', status: 'open' })).toBe('Open blocker');
  });

  it('falls back to the category when the STATION response omits the title key entirely', () => {
    // The crew-station shape: `title` is absent, not blank — the server does not
    // send the key at all, so a devtools console on the tablet has nothing to read.
    expect(openBlockerLine(STATION_BLOCKER)).toBe('Machine down');
  });
});

describe('openBlockerMeta', () => {
  it('carries category · severity under a titled line — they say different things', () => {
    expect(openBlockerMeta(TITLED_BLOCKER)).toBe('Machine down · High');
  });

  it('DROPS the category when the withheld title left it doing duty as the headline', () => {
    // Without this the station renders "Machine down" over "Machine down · High"
    // — a stutter that reads as a rendering bug on a screen that has to be believed.
    expect(openBlockerLine(STATION_BLOCKER)).toBe('Machine down');
    expect(openBlockerMeta(STATION_BLOCKER)).toBe('High');
  });

  it('is null when nothing is left to say', () => {
    expect(openBlockerMeta({ id: 1, category: 'machine_down', severity: '', status: 'open' })).toBeNull();
  });
});

describe('openBlockersFreeTextWithheld', () => {
  it('is true when a station response withheld text somebody actually wrote', () => {
    expect(openBlockersFreeTextWithheld([STATION_BLOCKER])).toBe(true);
  });

  it('is false on an identified session — the text is present and gets rendered', () => {
    expect(openBlockersFreeTextWithheld([TITLED_BLOCKER])).toBe(false);
  });

  it('is false when the station withheld nothing anybody wrote', () => {
    // free_text_withheld is true on EVERY station row; `has_note` is what says a
    // human wrote something. Announcing a withheld note that does not exist would
    // send an operator chasing a supervisor for a server-composed title.
    expect(openBlockersFreeTextWithheld([{ ...STATION_BLOCKER, has_note: false }])).toBe(false);
  });

  it('is true when ANY row in the list withheld one', () => {
    expect(openBlockersFreeTextWithheld([{ ...STATION_BLOCKER, has_note: false }, STATION_BLOCKER])).toBe(true);
  });

  it('tolerates an older backend that sends neither flag', () => {
    expect(
      openBlockersFreeTextWithheld([{ id: 9, title: 'x', category: 'other', severity: 'low', status: 'open' }])
    ).toBe(false);
  });
});

describe('resolveBlockerOutcome (closing a blocker — did the job actually come off hold?)', () => {
  /** A resolved blocker row, with whatever operation account the server sent. */
  function resolved(outcome: BlockerOperationOutcome | null): WorkOrderBlockerWriteResult {
    return {
      id: 1,
      company_id: 1,
      work_order_id: 42,
      category: 'material_missing',
      severity: 'high',
      status: 'resolved',
      title: 'Material missing',
      reported_at: '2026-01-01T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      operation_outcome: outcome,
    };
  }

  it('reports "not attempted" — never a shortfall — when no outcome rides the response', () => {
    // An acknowledge never puts the operation in play. Warning here would attach
    // a "still held" notice to a call that was never about the hold.
    const verdict = resolveBlockerOutcome(resolved(null));
    expect(verdict).toEqual({
      attempted: false,
      stillHeld: false,
      landedPending: false,
      openBlockers: [],
      withheldReason: null,
      fellShort: false,
    });
  });

  it('tolerates a null/absent result the same way', () => {
    expect(resolveBlockerOutcome(null).fellShort).toBe(false);
    expect(resolveBlockerOutcome(undefined).attempted).toBe(false);
  });

  it('flags the reported defect: the operation is still on hold', () => {
    const verdict = resolveBlockerOutcome(
      resolved({
        operation_id: 9,
        operation_status: 'on_hold',
        operation_resumed: false,
        resume_withheld_reason: 'other_blockers_open',
        operation_still_held: true,
        open_blockers: [TITLED_BLOCKER],
      })
    );
    expect(verdict.stillHeld).toBe(true);
    expect(verdict.landedPending).toBe(false);
    expect(verdict.openBlockers).toEqual([TITLED_BLOCKER]);
    expect(verdict.fellShort).toBe(true);
  });

  it('flags a resume that landed off the board', () => {
    const verdict = resolveBlockerOutcome(
      resolved({
        operation_id: 9,
        operation_status: 'pending',
        operation_resumed: true,
        resume_withheld_reason: null,
        operation_still_held: false,
        open_blockers: [],
      })
    );
    expect(verdict.landedPending).toBe(true);
    expect(verdict.stillHeld).toBe(false);
    expect(verdict.fellShort).toBe(true);
  });

  it('does NOT flag a withheld reason that never held anything', () => {
    // `no_operation` / `operation_not_held` / `operation_missing` all mean there
    // was nothing to resume. Keying the warning on "a reason is present" would
    // put a hold notice on a blocker that never touched an operation.
    for (const reason of ['no_operation', 'operation_not_held', 'operation_missing'] as const) {
      const verdict = resolveBlockerOutcome(
        resolved({
          operation_id: null,
          operation_status: null,
          operation_resumed: false,
          resume_withheld_reason: reason,
          operation_still_held: false,
          open_blockers: [],
        })
      );
      expect(verdict.attempted).toBe(true);
      expect(verdict.fellShort).toBe(false);
    }
  });

  it('stays green on a resume that reached READY', () => {
    const verdict = resolveBlockerOutcome(
      resolved({
        operation_id: 9,
        operation_status: 'ready',
        operation_resumed: true,
        resume_withheld_reason: null,
        operation_still_held: false,
        open_blockers: [],
      })
    );
    expect(verdict.fellShort).toBe(false);
  });

  it('does not read PENDING as a shortfall when nothing resumed', () => {
    // A held operation that was already PENDING-adjacent must not be reported as
    // "the hold cleared but stayed off the board" — nothing cleared.
    const verdict = resolveBlockerOutcome(
      resolved({
        operation_id: 9,
        operation_status: 'pending',
        operation_resumed: false,
        resume_withheld_reason: 'other_blockers_open',
        operation_still_held: false,
        open_blockers: [TITLED_BLOCKER],
      })
    );
    expect(verdict.landedPending).toBe(false);
    expect(verdict.fellShort).toBe(false);
  });
});

describe('namedOpenBlockers (how the three toasts name what is still in the way)', () => {
  it('prints the server line per blocker, joined the one way', () => {
    expect(namedOpenBlockers([TITLED_BLOCKER, { ...TITLED_BLOCKER, id: 6, title: 'Awaiting MTR' }])).toBe(
      'Machine Down: OP20 Deburr; Awaiting MTR'
    );
  });

  it('falls back to the category label for a title-less row, exactly as openBlockerLine does', () => {
    expect(namedOpenBlockers([STATION_BLOCKER])).toBe('Machine down');
  });

  it('is empty for an empty list — every caller checks length before printing anything', () => {
    expect(namedOpenBlockers([])).toBe('');
  });
});

describe('offTheBoardSentence (ONE explanation of a resume floored at PENDING)', () => {
  /**
   * The point of this suite is not the prose; it is that BOTH verbs on the Work
   * Order page — Clear Hold and Resolve-a-blocker — get the identical
   * explanation, because it describes one piece of server behavior. The subject
   * is the only thing a call site may vary.
   */
  it('explains what PENDING means for the floor, not just that it is PENDING', () => {
    const sentence = offTheBoardSentence();
    expect(sentence).toContain('did NOT go back on the board');
    expect(sentence).toContain('dispatch board or at the kiosk');
    expect(sentence).toContain('released');
  });

  it('varies ONLY the subject', () => {
    const asClearHold = offTheBoardSentence();
    const asResolve = offTheBoardSentence('The hold cleared, but the job');
    expect(asClearHold.startsWith('It ')).toBe(true);
    expect(asResolve.startsWith('The hold cleared, but the job ')).toBe(true);
    // Everything after the subject is the same sentence, character for character.
    expect(asResolve.slice('The hold cleared, but the job'.length)).toBe(asClearHold.slice('It'.length));
  });
});

describe('stillHeldSentence (why the job is STILL stopped, and the two ways off it)', () => {
  it('names how many and which, and both remedies', () => {
    const sentence = stillHeldSentence('other_blockers_open', [TITLED_BLOCKER]);
    expect(sentence).toContain('STILL on hold');
    expect(sentence).toContain('another blocker is');
    expect(sentence).toContain('Machine Down: OP20 Deburr');
    expect(sentence).toContain('Resolve that one too');
    expect(sentence).toContain('Clear Hold');
  });

  it('counts off the LIST, so the number and the names can never disagree', () => {
    const sentence = stillHeldSentence('other_blockers_open', [
      TITLED_BLOCKER,
      { ...TITLED_BLOCKER, id: 6, title: 'Awaiting MTR' },
    ]);
    expect(sentence).toContain('2 other blockers are');
    expect(sentence).toContain('Machine Down: OP20 Deburr; Awaiting MTR');
    expect(sentence).toContain('Resolve those too');
  });

  it('still says something true when the reason arrives without its list', () => {
    const sentence = stillHeldSentence('other_blockers_open', []);
    expect(sentence).toContain('STILL on hold');
    expect(sentence).not.toContain('()');
    expect(sentence).toContain('Clear Hold');
  });

  it('is defensive about the nothing-to-resume reasons rather than silent', () => {
    // The caller only reaches this function when the SERVER says the operation is
    // still on hold, so these pairings should not occur — but a vague true
    // sentence beats an empty toast if one ever does.
    for (const reason of ['no_operation', 'operation_not_held', 'operation_missing'] as const) {
      const sentence = stillHeldSentence(reason, []);
      expect(sentence).toContain('STILL on hold');
      expect(sentence).toContain('Clear Hold');
    }
    expect(stillHeldSentence(null, [])).toContain('STILL on hold');
    expect(stillHeldSentence(undefined, [])).toContain('STILL on hold');
  });
});

describe('resolveBlockerOutcome carries the reason through', () => {
  it('hands the withheld reason to the wording layer without judging on it', () => {
    const verdict = resolveBlockerOutcome({
      id: 1,
      company_id: 1,
      work_order_id: 42,
      category: 'material_missing',
      severity: 'high',
      status: 'resolved',
      title: 'Material missing',
      reported_at: '2026-01-01T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      operation_outcome: {
        operation_id: 9,
        operation_status: 'on_hold',
        operation_resumed: false,
        resume_withheld_reason: 'other_blockers_open',
        operation_still_held: true,
        open_blockers: [TITLED_BLOCKER],
      },
    });
    expect(verdict.withheldReason).toBe('other_blockers_open');
    // The WARNING still comes off the operation's status, never off the reason.
    expect(verdict.stillHeld).toBe(true);
  });

  it('reports no reason when the resume happened', () => {
    const verdict = resolveBlockerOutcome({
      id: 1,
      company_id: 1,
      work_order_id: 42,
      category: 'material_missing',
      severity: 'high',
      status: 'resolved',
      title: 'Material missing',
      reported_at: '2026-01-01T00:00:00Z',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      operation_outcome: {
        operation_id: 9,
        operation_status: 'ready',
        operation_resumed: true,
        resume_withheld_reason: null,
        operation_still_held: false,
        open_blockers: [],
      },
    });
    expect(verdict.withheldReason).toBeNull();
  });
});
