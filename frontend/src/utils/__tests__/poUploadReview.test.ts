import {
  partNumberKey,
  effectivePartNumber,
  newPartCoverageKeys,
  dedupePartsToCreate,
  buildLineItemsPayload,
} from '../poUploadReview';

describe('effectivePartNumber', () => {
  it('falls back to suggested_part_number when part_number is whitespace-only', () => {
    expect(effectivePartNumber({ part_number: '   ', suggested_part_number: 'WM-0042' })).toBe('WM-0042');
  });

  it('keeps the entered casing while trimming', () => {
    expect(effectivePartNumber({ part_number: '  AN960-10L ' })).toBe('AN960-10L');
  });

  it('returns empty string when both are blank', () => {
    expect(effectivePartNumber({ part_number: '  ', suggested_part_number: ' ' })).toBe('');
  });
});

describe('partNumberKey', () => {
  it('falls back to suggested_part_number when part_number is whitespace-only', () => {
    expect(partNumberKey({ part_number: '   ', suggested_part_number: 'WM-0042' })).toBe('wm-0042');
  });

  it('lowercases and trims the part number', () => {
    expect(partNumberKey({ part_number: '  AN960-10L  ' })).toBe('an960-10l');
  });

  it('normalizes case so matching is case-insensitive', () => {
    expect(partNumberKey({ part_number: 'an960-10L' })).toBe(partNumberKey({ part_number: 'AN960-10l' }));
  });

  it('falls back to suggested_part_number when part_number is empty', () => {
    expect(partNumberKey({ part_number: '', suggested_part_number: ' WM-0042 ' })).toBe('wm-0042');
  });

  it('falls back to suggested_part_number when part_number is null/undefined', () => {
    expect(partNumberKey({ part_number: null, suggested_part_number: 'WM-0042' })).toBe('wm-0042');
    expect(partNumberKey({ suggested_part_number: 'WM-0042' })).toBe('wm-0042');
  });

  it('prefers part_number over suggested_part_number when both are present', () => {
    expect(partNumberKey({ part_number: 'REAL-1', suggested_part_number: 'WM-0042' })).toBe('real-1');
  });

  it('returns an empty string when neither number is present', () => {
    expect(partNumberKey({})).toBe('');
    expect(partNumberKey({ part_number: null, suggested_part_number: null })).toBe('');
    expect(partNumberKey({ part_number: '   ' })).toBe('');
  });
});

describe('newPartCoverageKeys', () => {
  it('collects keys only from lines with create_new_part === true', () => {
    const keys = newPartCoverageKeys([
      { part_number: 'AN960-10L', create_new_part: true },
      { part_number: 'MS20470AD4', create_new_part: false },
      { part_number: 'NAS1149' },
    ]);
    expect(keys).toEqual(new Set(['an960-10l']));
  });

  it('normalizes keys (case + whitespace) and uses the suggested fallback', () => {
    const keys = newPartCoverageKeys([
      { part_number: '  AN960-10L ', create_new_part: true },
      { part_number: '', suggested_part_number: 'WM-0042', create_new_part: true },
    ]);
    expect(keys.has('an960-10l')).toBe(true);
    expect(keys.has('wm-0042')).toBe(true);
    expect(keys.size).toBe(2);
  });

  it('never includes empty keys', () => {
    const keys = newPartCoverageKeys([
      { part_number: '  ', create_new_part: true },
      { part_number: null, suggested_part_number: null, create_new_part: true },
    ]);
    expect(keys.size).toBe(0);
  });

  it('returns an empty set for an empty list', () => {
    expect(newPartCoverageKeys([]).size).toBe(0);
  });
});

describe('dedupePartsToCreate', () => {
  it('maps create_new_part lines to trimmed create_parts entries', () => {
    expect(
      dedupePartsToCreate([
        {
          part_number: '  AN960-10L ',
          description: 'Washer',
          new_part_type: 'hardware',
          create_new_part: true,
        },
      ])
    ).toEqual([{ part_number: 'AN960-10L', description: 'Washer', part_type: 'hardware' }]);
  });

  it('uses the suggested part number when part_number is blank', () => {
    expect(
      dedupePartsToCreate([
        {
          part_number: '',
          suggested_part_number: ' WM-0042 ',
          description: 'Bracket',
          new_part_type: 'purchased',
          create_new_part: true,
        },
      ])
    ).toEqual([{ part_number: 'WM-0042', description: 'Bracket', part_type: 'purchased' }]);
  });

  it('dedupes case/whitespace-insensitively with the first occurrence winning', () => {
    expect(
      dedupePartsToCreate([
        {
          part_number: 'AN960-10L',
          description: 'First occurrence',
          new_part_type: 'hardware',
          create_new_part: true,
        },
        {
          part_number: '  an960-10l ',
          description: 'Second occurrence',
          new_part_type: 'purchased',
          create_new_part: true,
        },
      ])
    ).toEqual([{ part_number: 'AN960-10L', description: 'First occurrence', part_type: 'hardware' }]);
  });

  it('drops entries without any part number and skips non-create lines', () => {
    expect(
      dedupePartsToCreate([
        { part_number: '  ', description: 'No number', new_part_type: 'purchased', create_new_part: true },
        { part_number: 'SKIP-1', description: 'Not flagged', new_part_type: 'purchased', create_new_part: false },
        { part_number: 'KEEP-1', description: 'Kept', new_part_type: 'raw_material', create_new_part: true },
      ])
    ).toEqual([{ part_number: 'KEEP-1', description: 'Kept', part_type: 'raw_material' }]);
  });

  it('returns an empty array when no lines are flagged', () => {
    expect(dedupePartsToCreate([])).toEqual([]);
    expect(
      dedupePartsToCreate([
        { part_number: 'P-1', description: 'x', new_part_type: 'purchased', create_new_part: false },
      ])
    ).toEqual([]);
  });
});

describe('buildLineItemsPayload', () => {
  it('builds the create-from-upload line payload', () => {
    expect(
      buildLineItemsPayload([
        {
          part_number: ' AN960-10L ',
          description: 'Washer',
          qty_ordered: 100,
          unit_price: 0.12,
          line_total: 12,
          selected_part_id: 42,
        },
      ])
    ).toEqual([
      {
        part_id: 42,
        part_number: 'AN960-10L',
        description: 'Washer',
        quantity_ordered: 100,
        unit_price: 0.12,
        line_total: 12,
      },
    ]);
  });

  it('sends part_id 0 for lines without a selected part', () => {
    const [line] = buildLineItemsPayload([
      {
        part_number: 'NEW-1',
        description: 'New part line',
        qty_ordered: 5,
        unit_price: 2,
        line_total: 10,
        selected_part_id: null,
      },
    ]);
    expect(line.part_id).toBe(0);
  });

  it('falls back to the suggested part number when part_number is blank', () => {
    const [line] = buildLineItemsPayload([
      {
        part_number: '',
        suggested_part_number: ' WM-0042 ',
        description: 'Bracket',
        qty_ordered: 1,
        unit_price: 9.5,
        line_total: 9.5,
        selected_part_id: null,
      },
    ]);
    expect(line.part_number).toBe('WM-0042');
  });

  it('preserves line order and emits one entry per line', () => {
    const payload = buildLineItemsPayload([
      {
        part_number: 'A-1',
        description: 'a',
        qty_ordered: 1,
        unit_price: 1,
        line_total: 1,
        selected_part_id: 7,
      },
      {
        part_number: 'A-1',
        description: 'duplicate part, second line',
        qty_ordered: 2,
        unit_price: 1,
        line_total: 2,
        selected_part_id: null,
      },
    ]);
    expect(payload).toHaveLength(2);
    expect(payload.map((l) => l.part_number)).toEqual(['A-1', 'A-1']);
    expect(payload.map((l) => l.part_id)).toEqual([7, 0]);
  });
});
