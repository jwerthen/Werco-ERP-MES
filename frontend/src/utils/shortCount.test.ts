import {
  SHORT_EPSILON,
  clockOutOperationStatus,
  closingShortAfterEntry,
  formatShortQuantity,
  goodCeiling,
  operationReceived,
  operationUnaccounted,
  shortAfterEntry,
  shortUnitLabel,
} from './shortCount';

describe('operationReceived', () => {
  it('is the target less what never reached the operation', () => {
    expect(operationReceived({ target: 10, upstreamShort: 2 })).toBe(8);
  });

  it('reads a payload without the key as nothing lost upstream (every pre-ruling row)', () => {
    expect(operationReceived({ target: 10 })).toBe(10);
    expect(operationReceived({ target: 10, upstreamShort: null })).toBe(10);
  });

  it('never goes negative, and garbage reads as zero', () => {
    expect(operationReceived({ target: 2, upstreamShort: 5 })).toBe(0);
    expect(operationReceived({ target: Number.NaN, upstreamShort: 1 })).toBe(0);
    expect(operationReceived({ target: undefined })).toBe(0);
  });
});

describe('operationUnaccounted / shortAfterEntry', () => {
  it('counts both good and scrap as accounted for', () => {
    expect(operationUnaccounted({ target: 10, complete: 6, scrapped: 1 })).toBe(3);
    expect(shortAfterEntry({ target: 10, complete: 6, scrapped: 1, good: 2, scrap: 1 })).toBe(0);
  });

  it('works from the received count, not the target', () => {
    // Op 10 closed 8 + 2 (declared 2): op 20 is accounted at 8 good.
    expect(shortAfterEntry({ target: 10, upstreamShort: 2, complete: 0, good: 8 })).toBe(0);
    expect(shortAfterEntry({ target: 10, upstreamShort: 2, complete: 0, good: 7 })).toBe(1);
  });

  it('floors at zero (an over-report is never a negative short)', () => {
    expect(shortAfterEntry({ target: 5, complete: 5, good: 3 })).toBe(0);
  });

  it('trims float residue below the server epsilon', () => {
    expect(shortAfterEntry({ target: 0.3, complete: 0.1, good: 0.2 })).toBe(0);
    expect(SHORT_EPSILON).toBe(1e-9);
  });
});

describe('closingShortAfterEntry', () => {
  it('is what the close would declare short — scrap covers it but does not erase it', () => {
    expect(closingShortAfterEntry({ target: 10, complete: 6, good: 2, scrap: 2 })).toBe(2);
    expect(closingShortAfterEntry({ target: 10, complete: 6, good: 4 })).toBe(0);
  });
});

describe('goodCeiling', () => {
  it("takes the server's ceiling when the payload carries one", () => {
    expect(goodCeiling(10, 8)).toBe(8);
    expect(goodCeiling(10, 0)).toBe(0);
  });

  it('falls back to the target without one', () => {
    expect(goodCeiling(10, null)).toBe(10);
    expect(goodCeiling(10)).toBe(10);
  });
});

describe('shortUnitLabel / formatShortQuantity', () => {
  it('says runs on a nest and pieces elsewhere, singular at 1', () => {
    expect(shortUnitLabel(true, 1)).toBe('run');
    expect(shortUnitLabel(true, 2)).toBe('runs');
    expect(shortUnitLabel(false, 1)).toBe('pc');
    expect(shortUnitLabel(false, 3)).toBe('pcs');
  });

  it('prints whole numbers whole and trims residue', () => {
    expect(formatShortQuantity(2)).toBe('2');
    expect(formatShortQuantity(2.0000000001)).toBe('2');
    expect(formatShortQuantity(1.5)).toBe('1.5');
  });
});

describe('clockOutOperationStatus', () => {
  it("reads the clock-out response's operation_status", () => {
    expect(clockOutOperationStatus({ operation_status: 'complete' })).toBe('complete');
    expect(clockOutOperationStatus({ operation_status: 'IN_PROGRESS' })).toBe('in_progress');
  });

  it('is null for an older backend that does not send it', () => {
    expect(clockOutOperationStatus({})).toBeNull();
    expect(clockOutOperationStatus(null)).toBeNull();
    expect(clockOutOperationStatus(undefined)).toBeNull();
    expect(clockOutOperationStatus('complete')).toBeNull();
    expect(clockOutOperationStatus({ operation_status: '' })).toBeNull();
  });
});
