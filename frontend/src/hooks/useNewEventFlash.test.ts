/**
 * useNewEventFlash — the wallboard's only flashing. Locks the diff contract:
 * stable-id keys (never list index), suppression on first paint AND on
 * ?dept= change (resetKey), a fresh baseline after the reset so the next
 * genuinely-new event still flashes, and the payload-identity guard so
 * unrelated re-renders (1s clock ticks) never re-diff.
 */

import { renderHook } from '@testing-library/react';
import type { WallboardResponse, WallboardWorkCenter } from '../types/wallboard';
import { collectEventKeys, useNewEventFlash } from './useNewEventFlash';

function wc(id: number, overrides: Partial<WallboardWorkCenter> = {}): WallboardWorkCenter {
  return {
    id,
    code: `WC-${id}`,
    name: `WC ${id}`,
    status: null,
    active_jobs: [],
    queued_count: 0,
    blocked_count: 0,
    down: null,
    ...overrides,
  };
}

function makePayload(overrides: Partial<WallboardResponse> = {}): WallboardResponse {
  return {
    work_centers: [wc(1)],
    late_wos: [],
    blocked_wos: [],
    generated_at: '2026-06-10T13:00:00Z',
    ...overrides,
  };
}

const downPayload = (generatedAt: string): WallboardResponse =>
  makePayload({
    work_centers: [wc(1, { down: { category: 'mechanical', since: null, minutes: 2 } })],
    generated_at: generatedAt,
  });

describe('collectEventKeys', () => {
  it('keys events by stable ids: wc down/blocked, late WO number, blocked WO number', () => {
    const payload = makePayload({
      work_centers: [
        wc(1, { down: { category: 'mechanical', since: null, minutes: 5 } }),
        wc(2, { blocked_count: 2 }),
      ],
      late_wos: [{ wo_number: 'WO-9', part_number: null, due_date: null, days_late: 1, status: null }],
      blocked_wos: [{ wo_number: 'WO-8', category: 'material_missing', age_hours: 3 }],
    });
    expect(collectEventKeys(payload)).toEqual(
      new Set(['wc-down:1', 'down:1:mechanical', 'wc-blocked:2', 'late:WO-9', 'blocked:WO-8'])
    );
  });
});

describe('useNewEventFlash', () => {
  it('suppresses on first paint, flashes only newly-added keys, then settles', () => {
    const first = downPayload('2026-06-10T13:00:00Z');
    const { result, rerender } = renderHook(({ payload, dept }) => useNewEventFlash(payload, dept), {
      initialProps: { payload: first as WallboardResponse | null, dept: '' },
    });
    // First paint: the down center is baseline, nothing flashes.
    expect(result.current.size).toBe(0);

    // Next poll: WC-2 newly blocked → only ITS keys flash, the pre-existing
    // down center stays steady (diff is by stable id, not list position).
    const second = makePayload({
      work_centers: [
        wc(1, { down: { category: 'mechanical', since: null, minutes: 32 } }),
        wc(2, { blocked_count: 1 }),
      ],
      generated_at: '2026-06-10T13:00:30Z',
    });
    rerender({ payload: second, dept: '' });
    expect(result.current).toEqual(new Set(['wc-blocked:2']));

    // Third poll, same events → steady again.
    rerender({ payload: { ...second, generated_at: '2026-06-10T13:01:00Z' }, dept: '' });
    expect(result.current.size).toBe(0);
  });

  it('re-renders with the SAME payload identity never re-diff (clock ticks)', () => {
    const first = makePayload();
    const { result, rerender } = renderHook(({ payload, dept }) => useNewEventFlash(payload, dept), {
      initialProps: { payload: first as WallboardResponse | null, dept: '' },
    });
    const second = downPayload('2026-06-10T13:00:30Z');
    rerender({ payload: second, dept: '' });
    const flashed = result.current;
    expect(flashed).toEqual(new Set(['wc-down:1', 'down:1:mechanical']));

    // 1s clock tick re-render with the identical payload object: the returned
    // set is the same instance — no re-diff happened.
    rerender({ payload: second, dept: '' });
    expect(result.current).toBe(flashed);
  });

  it('suppresses on ?dept= change (resetKey) and rebuilds a fresh baseline', () => {
    const plant = makePayload();
    const { result, rerender } = renderHook(({ payload, dept }) => useNewEventFlash(payload, dept), {
      initialProps: { payload: plant as WallboardResponse | null, dept: '' },
    });
    expect(result.current.size).toBe(0);

    // Dept switch: the machining board has events the plant baseline lacked —
    // it must NOT light up the whole board.
    const machining = downPayload('2026-06-10T13:00:30Z');
    rerender({ payload: machining, dept: 'machining' });
    expect(result.current.size).toBe(0);

    // The post-reset payload became the fresh baseline: the next poll only
    // flashes what is genuinely new relative to IT.
    const machiningWithBlock = makePayload({
      work_centers: [wc(1, { down: { category: 'mechanical', since: null, minutes: 3 } }), wc(2, { blocked_count: 1 })],
      generated_at: '2026-06-10T13:01:00Z',
    });
    rerender({ payload: machiningWithBlock, dept: 'machining' });
    expect(result.current).toEqual(new Set(['wc-blocked:2']));
  });

  it('returns an empty set while the payload is still null', () => {
    const { result } = renderHook(() => useNewEventFlash(null, ''));
    expect(result.current.size).toBe(0);
  });
});
