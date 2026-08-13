/**
 * Held-row fixtures shaped like the SERVER's actual payload.
 *
 * Mirrors `_held_job_row` and `HoldContext` in
 * `backend/app/api/endpoints/shop_floor.py` — `hold` is a nested block on the
 * row, carrying `held_at` / `held_by_user_id` / `held_by_name` and a nested
 * `blocker` that is null more often than not.
 *
 * This module exists because the first version of these tests asserted a FLAT
 * `item.blocker` the server never sends. Everything compiled, 66 tests passed,
 * and the reason display — the entire point of the feature — would have been
 * dead in production. Every held-row fixture now comes from here, so a future
 * contract change breaks ONE file instead of silently passing in six.
 */

import type { KioskCrewQueueItem, KioskQueueItem } from './kioskConstants';
import type { OperationHold } from '../../types';

/**
 * A hold that filed a blocker (note present, or a non-OTHER category), as an
 * IDENTIFIED SESSION sees it — the single-operator kiosk or the desktop. The
 * free text is present and `free_text_withheld` is false.
 */
export const HOLD_WITH_BLOCKER: OperationHold = {
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
    has_note: true,
    free_text_withheld: false,
    reported_at: '2026-08-11T19:14:00Z',
    reported_by_user_id: 12,
    reported_by_name: 'Dana R.',
  },
};

/**
 * The SAME hold as a CREW STATION receives it: `title` and `note` are ABSENT
 * (the keys are missing, not blanked), `free_text_withheld` is true and
 * `has_note` says a written reason exists.
 *
 * A station is an unattended, PIN-unlocked tablet with no operator identity and
 * no idle logout — the audience the wallboard already withholds NCR titles and
 * descriptions from. This fixture is the shape the crew station actually gets,
 * so the crew rows below use it; a crew test asserting `Z-axis alarm 4012` would
 * be asserting a payload the server does not send.
 */
export const HOLD_WITH_BLOCKER_STATION: OperationHold = {
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

/**
 * A BARE hold — no note, category OTHER. Files NO blocker; provenance comes from
 * the `operation_hold` event alone. This is the accidental fat-finger case the
 * whole feature exists for, so it is the fixture most worth asserting against.
 */
export const BARE_HOLD: OperationHold = {
  held_at: '2026-08-11T19:14:00Z',
  held_by_user_id: 12,
  held_by_name: 'Dana R.',
  blocker: null,
};

/**
 * Neither record exists (a hold placed before either was written). The server
 * reports what was recorded and never infers a holder, so this is a REAL state
 * that has to render sanely.
 */
export const UNRECORDED_HOLD: OperationHold = {
  held_at: null,
  held_by_user_id: null,
  held_by_name: null,
  blocker: null,
};

/** A startable row, as it appears on `queue`. */
export const QUEUE_ROW: KioskQueueItem = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-READY-0001',
  part_number: 'PN-1',
  part_name: 'Bracket',
  operation_number: '10',
  operation_name: 'Laser',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  quantity_scrapped: 0,
  priority: 5,
  due_date: null,
  run_order: 1,
};

/**
 * A held row, as it appears on `held`: the same job-card fields, plus the
 * server's explicit `startable: false` and the nested `hold` block. `run_order`
 * is null on held rows — the server does not rank work it will not dispatch.
 */
export const HELD_ROW: KioskQueueItem = {
  ...QUEUE_ROW,
  operation_id: 41,
  work_order_id: 11,
  work_order_number: 'WO-HELD-0001',
  operation_number: '20',
  operation_name: 'Deburr',
  status: 'on_hold',
  quantity_complete: 12,
  run_order: null,
  startable: false,
  hold: HOLD_WITH_BLOCKER,
};

/**
 * Crew rows carry the roster alongside everything above — and the STATION hold
 * shape, because that is what a station token is served (see
 * `HOLD_WITH_BLOCKER_STATION`).
 */
export const CREW_QUEUE_ROW: KioskCrewQueueItem = { ...QUEUE_ROW, roster: [] };
export const CREW_HELD_ROW: KioskCrewQueueItem = {
  ...HELD_ROW,
  hold: HOLD_WITH_BLOCKER_STATION,
  roster: [],
};

/** Swap in a different hold block (bare, unrecorded, custom) on a held row. */
export function heldRowWith(hold: OperationHold | null): KioskQueueItem {
  return { ...HELD_ROW, hold };
}

/** Crew twin of `heldRowWith`. */
export function crewHeldRowWith(hold: OperationHold | null): KioskCrewQueueItem {
  return { ...CREW_HELD_ROW, hold };
}
