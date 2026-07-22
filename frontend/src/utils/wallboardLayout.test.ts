/**
 * Pure helpers for the /wallboard Foundry TV board: the strict job-state
 * precedence, the duration formatting behind the card time values and the
 * BLOCKED/DOWN rail, and the label helpers.
 */

import type { WallboardJob } from '../types/wallboard';
import { blockerLabel, classifyJob, formatAgeHours, formatDownDuration, titleCaseDept } from './wallboardLayout';

describe('classifyJob (work-order card state class)', () => {
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

describe('duration formatting (card time values, rail magnitude columns)', () => {
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

  it('titleCaseDept renders the scope line title-cased, never the raw param', () => {
    expect(titleCaseDept('machining')).toBe('Machining');
    expect(titleCaseDept('cnc_milling')).toBe('Cnc Milling');
    expect(titleCaseDept('WELDING')).toBe('Welding');
  });
});
