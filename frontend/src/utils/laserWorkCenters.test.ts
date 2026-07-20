/**
 * Laser-dispatch work-center ordering (owner decision, 2026-07-20): nest
 * packages default onto the Ermaksan fiber laser and must NEVER default onto
 * the HSG tube laser. Tiers: ermaksan/fiber → other laser-ish → rest → tube
 * last; deterministic (name, then id) within a tier.
 */

import { WorkCenter } from '../types';
import {
  defaultLaserWorkCenter,
  laserDispatchTier,
  sortWorkCentersForLaserDispatch,
} from './laserWorkCenters';

let nextId = 1;
const wc = (overrides: Partial<WorkCenter>): WorkCenter => ({
  id: nextId++,
  version: 1,
  code: `WC-${nextId}`,
  name: 'Work Center',
  work_center_type: 'fabrication',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  is_active: true,
  current_status: 'available',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

const ermaksan = wc({ id: 5, name: 'Ermaksan Fiber Laser', code: 'LSR-1', work_center_type: 'laser_cutting' });
const tube = wc({ id: 6, name: 'HSG Tube Laser', code: 'LSR-2', work_center_type: 'laser_cutting' });
const otherLaser = wc({ id: 7, name: 'Backup Laser', code: 'LSR-3', work_center_type: 'laser_cutting' });
const brake = wc({ id: 8, name: 'Brake Press', code: 'BRK-1', work_center_type: 'forming' });

describe('laserDispatchTier', () => {
  it('ranks ermaksan/fiber first, laser-ish next, rest after, tube dead last', () => {
    expect(laserDispatchTier(ermaksan)).toBe(0);
    expect(laserDispatchTier(otherLaser)).toBe(1);
    expect(laserDispatchTier(brake)).toBe(2);
    expect(laserDispatchTier(tube)).toBe(3);
  });

  it('matches "fiber" on the code even without "ermaksan" anywhere', () => {
    expect(laserDispatchTier(wc({ name: 'Bystronic', code: 'FIBER-2', work_center_type: 'cutting' }))).toBe(0);
  });

  it('demotes tube entries even when they also say fiber/laser', () => {
    expect(laserDispatchTier(wc({ name: 'Fiber Tube Laser', code: 'LSR-9' }))).toBe(3);
  });
});

describe('sortWorkCentersForLaserDispatch', () => {
  it('orders by tier and never mutates the input', () => {
    const input = [tube, brake, otherLaser, ermaksan];
    const sorted = sortWorkCentersForLaserDispatch(input);
    expect(sorted.map((w) => w.id)).toEqual([5, 7, 8, 6]);
    expect(input.map((w) => w.id)).toEqual([6, 8, 7, 5]); // untouched
  });

  it('is deterministic within a tier (name, then id)', () => {
    const laserB = wc({ id: 21, name: 'Laser B', work_center_type: 'laser_cutting' });
    const laserA = wc({ id: 22, name: 'Laser A', work_center_type: 'laser_cutting' });
    const laserA2 = wc({ id: 20, name: 'Laser A', work_center_type: 'laser_cutting' });
    expect(sortWorkCentersForLaserDispatch([laserB, laserA, laserA2]).map((w) => w.id)).toEqual([20, 22, 21]);
  });
});

describe('defaultLaserWorkCenter', () => {
  it('defaults to the Ermaksan fiber laser when present', () => {
    expect(defaultLaserWorkCenter([tube, brake, otherLaser, ermaksan])?.id).toBe(5);
  });

  it('falls back to another laser when no ermaksan/fiber entry exists', () => {
    expect(defaultLaserWorkCenter([brake, tube, otherLaser])?.id).toBe(7);
  });

  it('NEVER defaults to a tube laser — undefined when tube is the only laser', () => {
    expect(defaultLaserWorkCenter([tube, brake])).toBeUndefined();
    expect(defaultLaserWorkCenter([])).toBeUndefined();
  });
});
