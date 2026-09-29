import { findProductionSpikes, isSevereTargetMiss } from './analyticsSignals';

test('severe miss threshold promotes measured OTD misses without treating unavailable or zero targets as misses', () => {
  expect(isSevereTargetMiss(44.4, 95)).toBe(true);
  expect(isSevereTargetMiss(74.9, 100)).toBe(true);
  expect(isSevereTargetMiss(75, 100)).toBe(false);
  expect(isSevereTargetMiss(null, 95)).toBe(false);
  expect(isSevereTargetMiss(0, 0)).toBe(false);
});

test('flags discontinuous production jumps, preserves data, and ignores small or gradual changes', () => {
  const points = [{ date: '2026-09-01', units_produced: 0 }, { date: '2026-09-02', units_produced: 800 }, { date: '2026-09-03', units_produced: 900 }];
  expect(findProductionSpikes(points)).toEqual([{ date: '2026-09-02', previous: 0, value: 800 }]);
  expect(points[1].units_produced).toBe(800);
  expect(findProductionSpikes([{ date: '1', units_produced: 1 }, { date: '2', units_produced: 6 }])).toEqual([]);
});
