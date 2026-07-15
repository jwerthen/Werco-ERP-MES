/**
 * Pure layout math for the ANDON WALL floor grid: deterministic grid shape,
 * density tiers with 2-poll hysteresis, alarm-first partition/sort, the 5ch
 * magnitude formatting, and the title-cased dept chip.
 */

import type { WallboardJob, WallboardWorkCenter } from '../types/wallboard';
import {
  blockerLabel,
  classifyJob,
  classifyWorkCenter,
  computeGridShape,
  formatAgeHours,
  formatDownDuration,
  isIdleWorkCenter,
  nextTierState,
  partitionWorkCenters,
  tierForCount,
  titleCaseDept,
} from './wallboardLayout';

function wc(overrides: Partial<WallboardWorkCenter> & { id: number }): WallboardWorkCenter {
  return {
    code: `WC-${overrides.id}`,
    name: `WC ${overrides.id}`,
    status: null,
    active_jobs: [],
    queued_count: 0,
    blocked_count: 0,
    down: null,
    ...overrides,
  };
}

describe('computeGridShape', () => {
  it.each([
    // [nActive, rows, cols, tier] — the spec's verified cases.
    [1, 1, 1, 'roomy'],
    [3, 1, 3, 'roomy'],
    [8, 2, 4, 'standard'],
    [9, 2, 5, 'standard'],
    [12, 3, 4, 'standard'],
    [14, 3, 5, 'dense'],
    [20, 4, 5, 'dense'],
  ])('N=%i → %i×%i (%s)', (n, rows, cols, tier) => {
    expect(computeGridShape(n as number)).toEqual({ rows, cols, tier });
  });

  it('always allocates at least N cells with trailing empties < cols', () => {
    for (let n = 1; n <= 20; n += 1) {
      const { rows, cols } = computeGridShape(n);
      expect(rows * cols).toBeGreaterThanOrEqual(n);
      expect(rows * cols - n).toBeLessThan(cols);
    }
  });

  it('treats N=0 as a single cell (the caller renders empty/idle states instead)', () => {
    expect(computeGridShape(0)).toEqual({ rows: 1, cols: 1, tier: 'roomy' });
  });
});

describe('tierForCount boundaries', () => {
  it.each([
    [1, 'roomy'],
    [6, 'roomy'],
    [7, 'standard'],
    [12, 'standard'],
    [13, 'dense'],
    [20, 'dense'],
  ])('N=%i → %s', (n, tier) => {
    expect(tierForCount(n as number)).toBe(tier);
  });
});

describe('nextTierState (2-poll hysteresis)', () => {
  it('applies the tier immediately on first paint', () => {
    expect(nextTierState(null, 14)).toEqual({ tier: 'dense', pendingTier: null });
  });

  it('holds the old tier for one poll, then commits on the second consecutive poll', () => {
    let state = nextTierState(null, 6); // roomy
    state = nextTierState(state, 7);
    expect(state).toEqual({ tier: 'roomy', pendingTier: 'standard' });
    state = nextTierState(state, 7);
    expect(state).toEqual({ tier: 'standard', pendingTier: null });
  });

  it('a one-poll flap across the boundary never switches the tier', () => {
    let state = nextTierState(null, 6); // roomy
    state = nextTierState(state, 7); // pending standard
    state = nextTierState(state, 6); // flapped back
    expect(state).toEqual({ tier: 'roomy', pendingTier: null });
    state = nextTierState(state, 6);
    expect(state.tier).toBe('roomy');
  });

  it('a changed candidate restarts the 2-poll clock', () => {
    let state = nextTierState(null, 12); // standard
    state = nextTierState(state, 13); // pending dense
    state = nextTierState(state, 5); // now pending roomy instead
    expect(state).toEqual({ tier: 'standard', pendingTier: 'roomy' });
    state = nextTierState(state, 5);
    expect(state).toEqual({ tier: 'roomy', pendingTier: null });
  });
});

describe('classification, idle partition, and alarm-first sort', () => {
  const running = wc({
    id: 1,
    name: 'Mill 1',
    active_jobs: [
      {
        wo_number: 'WO-1',
        part_number: 'P-1',
        op_name: 'Op',
        operator_name: null,
        elapsed_minutes: 5,
        qty_done: 0,
        qty_target: 1,
      },
    ],
  });
  const late = wc({
    id: 2,
    name: 'Lathe 1',
    active_jobs: [
      {
        wo_number: 'WO-2',
        part_number: 'P-2',
        op_name: 'Op',
        operator_name: null,
        elapsed_minutes: 5,
        qty_done: 0,
        qty_target: 1,
        is_late: true,
      },
    ],
  });
  const blocked = wc({ id: 3, name: 'Saw 1', blocked_count: 2 });
  const down = wc({
    id: 4,
    name: 'Weld 1',
    down: { category: 'maintenance', since: null, minutes: 47 },
  });
  const idle = wc({ id: 5, name: 'Deburr' });

  it('classifies DOWN > BLOCKED > LATE > RUNNING', () => {
    expect(classifyWorkCenter(down)).toBe('down');
    expect(classifyWorkCenter(blocked)).toBe('blocked');
    expect(classifyWorkCenter(late)).toBe('late');
    expect(classifyWorkCenter(running)).toBe('running');
  });

  it('old payloads without is_late classify as plain running', () => {
    expect(classifyWorkCenter(running)).toBe('running');
  });

  it('idle = no jobs, no downtime, nothing blocked', () => {
    expect(isIdleWorkCenter(idle)).toBe(true);
    expect(isIdleWorkCenter(blocked)).toBe(false);
    expect(isIdleWorkCenter(down)).toBe(false);
    expect(isIdleWorkCenter(running)).toBe(false);
  });

  it('partitions idle out and sorts active alarm-first, alphabetical within class', () => {
    const downB = wc({
      id: 6,
      name: 'Anodize',
      down: { category: 'mechanical', since: null, minutes: 5 },
    });
    const { active, idle: idleOut } = partitionWorkCenters([running, idle, blocked, down, late, downB]);
    expect(idleOut.map(c => c.name)).toEqual(['Deburr']);
    expect(active.map(c => c.name)).toEqual([
      'Anodize', // down, alpha first
      'Weld 1', // down
      'Saw 1', // blocked
      'Lathe 1', // running-late
      'Mill 1', // running
    ]);
  });
});

describe('classifyJob (job wall state class)', () => {
  const base: WallboardJob = { wo_number: 'WO-1' };

  it('applies strict precedence DOWN > BLOCKED > LATE > RUNNING > WAITING', () => {
    expect(classifyJob({ ...base, down: true, blocked: true, is_late: true, running: true })).toBe('down');
    expect(classifyJob({ ...base, blocked: true, is_late: true, running: true })).toBe('blocked');
    expect(classifyJob({ ...base, is_late: true, running: true })).toBe('late');
    expect(classifyJob({ ...base, running: true })).toBe('running');
    expect(classifyJob(base)).toBe('waiting');
  });

  it('a sparse job (all flags absent) classifies as waiting', () => {
    expect(classifyJob({ wo_number: 'WO-SPARSE' })).toBe('waiting');
  });
});

describe('magnitude formatting (5ch column)', () => {
  it('formats downtime minutes as 47m / 2h14m / 38h', () => {
    expect(formatDownDuration(47)).toBe('47m');
    expect(formatDownDuration(134)).toBe('2h14m');
    expect(formatDownDuration(38 * 60)).toBe('38h');
    expect(formatDownDuration(0)).toBe('0m');
  });

  it('formats blocked age hours as 45m / 38h / 6d', () => {
    expect(formatAgeHours(0.75)).toBe('45m');
    expect(formatAgeHours(38)).toBe('38h');
    expect(formatAgeHours(144)).toBe('6d');
  });
});

describe('labels', () => {
  it('blockerLabel replaces underscores', () => {
    expect(blockerLabel('material_missing')).toBe('material missing');
  });

  it('titleCaseDept renders the chip title-cased, never the raw param', () => {
    expect(titleCaseDept('machining')).toBe('Machining');
    expect(titleCaseDept('cnc_milling')).toBe('Cnc Milling');
    expect(titleCaseDept('WELDING')).toBe('Welding');
  });
});
