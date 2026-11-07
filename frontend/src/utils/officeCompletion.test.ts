import {
  isOpenEntriesRefusal,
  isOverrideReasonRefusal,
  isReopenRequiredRefusal,
  joinOperatorNames,
  operationMayHaveLabor,
  operationUnaccountedChip,
} from './officeCompletion';

// The server's texts (backend/app/services/operation_action_gates.py), verbatim.
const OFFICE_OPEN_ENTRIES =
  'Dana R. and Lee K. are still clocked in to Op 10 — have them clock out with their count first, or leave the operation open. Resubmit with acknowledge_open_entries to clock them out; pieces they have not reported will not be counted.';
const FLOOR_OPEN_ENTRIES =
  'Dana R. is still clocked in to Op 10 — have them clock out with their count first, or leave the operation open.';
const REDUCE_SHORT =
  'Lowering Op 10 by 3 would leave it 3 short, and a completed operation cannot be short. Resubmit with reopen_operation to reopen it (it returns to in progress).';
const REOPEN_IS_OFFICE_ONLY = 'reopen_operation is an office correction';
const OFFICE_OVERRIDE =
  'quantity_override_reason is required to record more good than the 8 counted on the floor for Op 10';
const FORCE_OVERRIDE =
  'quantity_complete (10) exceeds the 8 the floor recorded at Op 20; record the pieces there, or give quantity_override_reason';

describe('refusal detectors', () => {
  it('offers the acknowledgement only for the OFFICE still-clocked-in refusal', () => {
    expect(isOpenEntriesRefusal(OFFICE_OPEN_ENTRIES)).toBe(true);
    expect(isOpenEntriesRefusal(FLOOR_OPEN_ENTRIES)).toBe(false);
    expect(isOpenEntriesRefusal({ detail: OFFICE_OPEN_ENTRIES })).toBe(false);
    expect(isOpenEntriesRefusal(null)).toBe(false);
  });

  it('offers the reopen only for the would-leave-it-short correction refusal', () => {
    expect(isReopenRequiredRefusal(REDUCE_SHORT)).toBe(true);
    expect(isReopenRequiredRefusal(REOPEN_IS_OFFICE_ONLY)).toBe(false);
    expect(isReopenRequiredRefusal('Op 20 is already complete with these units; correct Op 20 first.')).toBe(false);
  });

  it('recognizes both override-reason refusals', () => {
    expect(isOverrideReasonRefusal(OFFICE_OVERRIDE)).toBe(true);
    expect(isOverrideReasonRefusal(FORCE_OVERRIDE)).toBe(true);
    expect(isOverrideReasonRefusal('cannot complete work order: 2 of 10 unaccounted for')).toBe(false);
  });
});

describe('operationMayHaveLabor', () => {
  const never = { status: 'ready' as const, actual_setup_hours: 0, actual_run_hours: 0 };

  it('is false only for an operation nobody ever started', () => {
    expect(operationMayHaveLabor(never)).toBe(false);
    expect(operationMayHaveLabor({ ...never, status: 'pending' })).toBe(false);
  });

  it('is true for anything a clock-in (or a desk save) could have started', () => {
    expect(operationMayHaveLabor({ ...never, actual_start: '2026-11-05T14:00:00Z' })).toBe(true);
    expect(operationMayHaveLabor({ ...never, started_by: 7 })).toBe(true);
    expect(operationMayHaveLabor({ ...never, status: 'in_progress' })).toBe(true);
    expect(operationMayHaveLabor({ ...never, actual_run_hours: 0.5 })).toBe(true);
  });

  it("prefers the server's own has_labor_evidence whenever the payload carries it", () => {
    // A desk save started it, but nobody ever clocked in: no reason is required.
    expect(operationMayHaveLabor({ ...never, status: 'in_progress', has_labor_evidence: false })).toBe(false);
    expect(operationMayHaveLabor({ ...never, has_labor_evidence: true })).toBe(true);
  });
});

describe('operationUnaccountedChip', () => {
  const op = {
    status: 'in_progress' as const,
    cancelled_nest_id: null,
    quantity_complete: 8,
    quantity_scrapped: 0,
  };

  it("prefers the server's figure (it alone knows the chain credit)", () => {
    expect(operationUnaccountedChip({ ...op, quantity_unaccounted: 1, upstream_quantity_short: 1 }, 10)).toBe(1);
  });

  it('falls back to the per-operation arithmetic on an older payload', () => {
    expect(operationUnaccountedChip(op, 10)).toBe(2);
    expect(operationUnaccountedChip({ ...op, quantity_scrapped: 1, upstream_quantity_short: 1 }, 10)).toBe(0);
  });

  it('shows nothing on a fresh, complete, or cancelled-nest row', () => {
    expect(operationUnaccountedChip({ ...op, quantity_complete: 0 }, 10)).toBe(0);
    expect(operationUnaccountedChip({ ...op, status: 'complete' }, 10)).toBe(0);
    expect(operationUnaccountedChip({ ...op, cancelled_nest_id: 3 }, 10)).toBe(0);
  });

  it('counts scrap-only progress and drops float residue', () => {
    expect(operationUnaccountedChip({ ...op, quantity_complete: 0, quantity_scrapped: 1 }, 10)).toBe(9);
    expect(operationUnaccountedChip({ ...op, quantity_unaccounted: 1e-12 }, 10)).toBe(0);
  });
});

describe('joinOperatorNames', () => {
  it('reads like a sentence and skips unnamed rows', () => {
    expect(joinOperatorNames([])).toBe('');
    expect(joinOperatorNames(['Dana R.'])).toBe('Dana R.');
    expect(joinOperatorNames(['Dana R.', null, 'Lee K.'])).toBe('Dana R. and Lee K.');
    expect(joinOperatorNames(['Dana R.', 'Lee K.', ' Sam P. '])).toBe('Dana R., Lee K. and Sam P.');
  });
});
