/**
 * useDispatchNotice — which "the planner changed this machine's order" notices a
 * floor screen shows, and the acknowledgement that hides them (SPEC §7).
 *
 * The rules pinned here are the ones a floor would notice breaking: whose "Got
 * it" hides what (device vs operator), the 15-minute first-sighting rule measured
 * against the SERVER's clock, a storage failure that must not take the page down,
 * the honest "changed N times" count, and an older backend that sends nothing.
 */

import { act, renderHook } from '@testing-library/react';
import type { DispatchNotice } from '../types';
import { DISPATCH_NOTICE_STORAGE_PREFIX, dispatchNoticeAckKey, useDispatchNotice } from './useDispatchNotice';
import type { DispatchNoticeAckScope, DispatchNoticeSource, UseDispatchNoticeOptions } from './useDispatchNotice';

/** Frozen notice shaped exactly like CONTRACT §7.2's example. */
function makeNotice(overrides: Partial<DispatchNotice> = {}): DispatchNotice {
  return {
    id: 90231,
    occurred_at: '2026-11-04T16:34:12.929689Z',
    work_center_id: 12,
    work_center_code: 'BRAKE-1',
    change_kind: 'machine_order',
    actor_display: 'Jon W.',
    next_up_before: {
      operation_id: 3102,
      work_order_number: 'WO-20261103-004',
      unit_number: '7',
      operation_number: '20',
      operation_name: 'Brake form gusset',
      part_number: 'BRKT-1180',
    },
    next_up_after: {
      operation_id: 3101,
      work_order_number: 'WO-20261103-004',
      unit_number: '7',
      operation_number: '10',
      operation_name: 'Brake form flange',
      part_number: 'BRKT-1180',
    },
    moved_up: [3101],
    updates_in_window: 3,
    ...overrides,
  };
}

/** 7 minutes after the notice — inside the first-sighting window. */
const SERVER_FRESH = '2026-11-04T16:41:07.957738Z';
/** 20 minutes after the notice — outside it. */
const SERVER_STALE = '2026-11-04T16:54:30.000000Z';

const OPERATOR_A: DispatchNoticeAckScope = { kind: 'user', userId: 3 };
const OPERATOR_B: DispatchNoticeAckScope = { kind: 'user', userId: 4 };
const DEVICE: DispatchNoticeAckScope = { kind: 'device' };

function source(
  notice: DispatchNotice | null | undefined,
  serverTime: string | null = SERVER_FRESH
): DispatchNoticeSource {
  return { notices: [notice], serverTime };
}

function renderNotice(initialProps: UseDispatchNoticeOptions) {
  return renderHook((props: UseDispatchNoticeOptions) => useDispatchNotice(props), { initialProps });
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
});

describe('useDispatchNotice — what is shown', () => {
  it('shows a fresh notice with the next-up change flagged and its moved-up ids', () => {
    const { result } = renderNotice({ sources: [source(makeNotice())], scope: OPERATOR_A });

    expect(result.current.active).toHaveLength(1);
    expect(result.current.active[0].notice.id).toBe(90231);
    expect(result.current.active[0].nextUpChanged).toBe(true);
    expect(result.current.active[0].observedChanges).toBe(1);
    expect(Array.from(result.current.movedUpOperationIds)).toEqual([3101]);
  });

  it('flags an order-only change (same next-up operation) as NOT a next-up change', () => {
    const quiet = makeNotice({ next_up_after: makeNotice().next_up_before, moved_up: [3150] });
    const { result } = renderNotice({ sources: [source(quiet)], scope: OPERATOR_A });
    expect(result.current.active[0].nextUpChanged).toBe(false);
  });

  it('keeps only the newest notice per work center and dedupes the same notice across payloads', () => {
    const older = makeNotice({ id: 90100 });
    const newest = makeNotice({ id: 90231 });
    const otherMachine = makeNotice({ id: 90200, work_center_id: 14, work_center_code: 'WELD-2', moved_up: [3200] });
    const { result } = renderNotice({
      // The kiosk's queue payload and my-active-job both carry BRAKE-1's notice.
      sources: [source(newest), { notices: [older, newest, otherMachine], serverTime: SERVER_FRESH }],
      scope: OPERATOR_A,
    });

    expect(result.current.active.map(entry => entry.notice.id)).toEqual([90231, 90200]);
    expect(new Set(result.current.movedUpOperationIds)).toEqual(new Set([3101, 3200]));
  });

  it('shows nothing without an acknowledgement scope (no identity to key it by)', () => {
    const { result } = renderNotice({ sources: [source(makeNotice())], scope: null });
    expect(result.current.active).toEqual([]);
    expect(result.current.movedUpOperationIds.size).toBe(0);
  });
});

describe('useDispatchNotice — older backend / missing fields', () => {
  it('shows nothing when the payload omits the notice fields entirely', () => {
    const { result } = renderNotice({
      sources: [{ notices: undefined, serverTime: undefined }, { notices: [undefined, null] }, {}],
      scope: OPERATOR_A,
    });
    expect(result.current.active).toEqual([]);
    expect(window.localStorage.length).toBe(0);
  });

  it('ignores a malformed notice rather than crashing', () => {
    const malformed = { occurred_at: SERVER_FRESH } as unknown as DispatchNotice;
    const { result } = renderNotice({ sources: [source(malformed)], scope: OPERATOR_A });
    expect(result.current.active).toEqual([]);
  });

  it('treats a missing moved_up list as "nobody moved up"', () => {
    const noMovedUp = { ...makeNotice() } as Partial<DispatchNotice>;
    delete noMovedUp.moved_up;
    const { result } = renderNotice({ sources: [source(noMovedUp as DispatchNotice)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);
    expect(result.current.movedUpOperationIds.size).toBe(0);
  });
});

describe('useDispatchNotice — first sighting is aged by server_time, never the device clock', () => {
  it('counts a notice older than 15 minutes by server_time as already seen', () => {
    // The DEVICE clock says the notice is brand new; the server says 20 minutes.
    jest.useFakeTimers({ now: new Date('2026-11-04T16:34:20Z') });
    const { result } = renderNotice({ sources: [source(makeNotice(), SERVER_STALE)], scope: OPERATOR_A });

    expect(result.current.active).toEqual([]);
    expect(result.current.movedUpOperationIds.size).toBe(0);
  });

  it('shows a notice fresh by server_time even when the device clock is hours ahead', () => {
    jest.useFakeTimers({ now: new Date('2026-11-05T09:00:00Z') });
    const { result } = renderNotice({ sources: [source(makeNotice(), SERVER_FRESH)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);
  });

  it('applies the 15-minute rule only to the FIRST sighting — a later, newer notice still shows', () => {
    const { result, rerender } = renderNotice({
      sources: [source(makeNotice(), SERVER_STALE)],
      scope: OPERATOR_A,
    });
    expect(result.current.active).toEqual([]);

    // A newer change lands. Even read late (old by server_time), this scope has
    // seen the machine before, so it is news.
    rerender({
      sources: [source(makeNotice({ id: 90300, occurred_at: '2026-11-04T16:35:00Z' }), SERVER_STALE)],
      scope: OPERATOR_A,
    });
    expect(result.current.active.map(entry => entry.notice.id)).toEqual([90300]);
  });
});

describe('useDispatchNotice — acknowledgement scope', () => {
  it('"Got it" hides the notice and its MOVED UP ids for that scope', () => {
    const notice = makeNotice();
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);

    act(() => result.current.acknowledge(notice));
    expect(result.current.active).toEqual([]);
    expect(result.current.movedUpOperationIds.size).toBe(0);

    // The next poll brings the same notice back: still acknowledged.
    rerender({ sources: [source({ ...notice })], scope: OPERATOR_A });
    expect(result.current.active).toEqual([]);
  });

  it('a newer notice after the acknowledgement shows again', () => {
    const notice = makeNotice();
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    act(() => result.current.acknowledge(notice));

    rerender({ sources: [source(makeNotice({ id: 90400 }))], scope: OPERATOR_A });
    expect(result.current.active.map(entry => entry.notice.id)).toEqual([90400]);
  });

  it('is per (user, work center): a second operator on the same tablet still sees what the first dismissed', () => {
    const notice = makeNotice();
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    act(() => result.current.acknowledge(notice));
    expect(result.current.active).toEqual([]);

    // Operator A's session ends; operator B badges in on the same shared kiosk.
    rerender({ sources: [source(notice)], scope: OPERATOR_B });
    expect(result.current.active.map(entry => entry.notice.id)).toEqual([90231]);

    // ...and A, signing back in, still has it acknowledged.
    rerender({ sources: [source(notice)], scope: OPERATOR_A });
    expect(result.current.active).toEqual([]);
  });

  it('acknowledging one machine leaves another machine’s notice up', () => {
    const brake = makeNotice();
    const weld = makeNotice({ id: 90232, work_center_id: 14, work_center_code: 'WELD-2' });
    const { result } = renderNotice({
      sources: [{ notices: [brake, weld], serverTime: SERVER_FRESH }],
      scope: OPERATOR_A,
    });

    act(() => result.current.acknowledge(brake));
    expect(result.current.active.map(entry => entry.notice.work_center_code)).toEqual(['WELD-2']);
  });

  it('the device scope (crew station) is keyed apart from any operator', () => {
    const notice = makeNotice();
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: DEVICE });
    act(() => result.current.acknowledge(notice));
    expect(window.localStorage.getItem(dispatchNoticeAckKey(DEVICE, 12))).toContain('"ackedId":90231');
    expect(window.localStorage.getItem(dispatchNoticeAckKey(OPERATOR_A, 12))).toBeNull();

    // A personal screen on the same browser profile is unaffected by the station's ack.
    rerender({ sources: [source(notice)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);
  });

  it('survives a remount — the acknowledgement is persisted per scope', () => {
    const notice = makeNotice();
    const first = renderNotice({ sources: [source(notice)], scope: DEVICE });
    act(() => first.result.current.acknowledge(notice));
    first.unmount();

    const second = renderNotice({ sources: [source(notice)], scope: DEVICE });
    expect(second.result.current.active).toEqual([]);
  });

  it('re-reads when another tab acknowledges (storage event)', () => {
    const notice = makeNotice();
    const { result } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);

    const key = dispatchNoticeAckKey(OPERATOR_A, 12);
    window.localStorage.setItem(key, JSON.stringify({ ackedId: 90231, seen: [] }));
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key }));
    });
    expect(result.current.active).toEqual([]);
  });
});

describe('useDispatchNotice — "changed N times" counts distinct ids THIS device observed', () => {
  it('stays at 1 across polls that keep returning the same notice (updates_in_window is NOT used)', () => {
    const notice = makeNotice({ updates_in_window: 7 });
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    rerender({ sources: [source({ ...notice })], scope: OPERATOR_A });
    rerender({ sources: [source({ ...notice })], scope: OPERATOR_A });
    expect(result.current.active[0].observedChanges).toBe(1);
  });

  it('counts each distinct new id observed since the last acknowledgement', () => {
    const { result, rerender } = renderNotice({ sources: [source(makeNotice({ id: 100 }))], scope: OPERATOR_A });
    rerender({ sources: [source(makeNotice({ id: 101 }))], scope: OPERATOR_A });
    rerender({ sources: [source(makeNotice({ id: 102 }))], scope: OPERATOR_A });
    expect(result.current.active.map(entry => [entry.notice.id, entry.observedChanges])).toEqual([[102, 3]]);

    // "Got it" resets the count; the next change starts again at 1.
    act(() => result.current.acknowledge(result.current.active[0].notice));
    rerender({ sources: [source(makeNotice({ id: 103 }))], scope: OPERATOR_A });
    expect(result.current.active[0].observedChanges).toBe(1);
  });
});

describe('useDispatchNotice — blocked storage never breaks the page', () => {
  it('works from memory when localStorage throws on every call', () => {
    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError');
    });

    const notice = makeNotice();
    const { result, rerender } = renderNotice({ sources: [source(notice)], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);

    act(() => result.current.acknowledge(notice));
    expect(result.current.active).toEqual([]);
    // The next poll does not resurrect it while the page stays mounted.
    rerender({ sources: [source({ ...notice })], scope: OPERATOR_A });
    expect(result.current.active).toEqual([]);
  });

  it('works when touching window.localStorage itself throws', () => {
    jest.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    const notice = makeNotice();
    const { result } = renderNotice({ sources: [source(notice)], scope: DEVICE });
    expect(result.current.active).toHaveLength(1);
    act(() => result.current.acknowledge(notice));
    expect(result.current.active).toEqual([]);
  });

  it('a corrupt stored record is treated as no record, not a crash', () => {
    window.localStorage.setItem(dispatchNoticeAckKey(OPERATOR_A, 12), '{not json');
    const { result } = renderNotice({ sources: [source(makeNotice())], scope: OPERATOR_A });
    expect(result.current.active).toHaveLength(1);
    expect(window.localStorage.getItem(dispatchNoticeAckKey(OPERATOR_A, 12))?.startsWith('{"ackedId"')).toBe(true);
  });
});

it('stores under a versioned prefix', () => {
  renderNotice({ sources: [source(makeNotice())], scope: OPERATOR_A });
  const keys = Array.from({ length: window.localStorage.length }, (_, index) => window.localStorage.key(index));
  expect(keys).toHaveLength(1);
  expect(keys.every(key => key?.startsWith(DISPATCH_NOTICE_STORAGE_PREFIX))).toBe(true);
});
