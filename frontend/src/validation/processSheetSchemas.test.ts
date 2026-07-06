/**
 * Process-sheet step schema — client-side mirror of the backend's
 * process_sheet_service._validate_step_definition rules (measurement limit
 * ordering, list options, instruction never-required) plus the payload builder
 * that maps validated form data onto the API contract.
 */

import { processSheetStepSchema, parseListOptions } from './schemas';
import { buildStepPayload } from '../components/processSheets/ProcessSheetStepModal';

const base = {
  sequence: 10,
  label: 'Bore diameter',
  instruction_text: '',
  step_type: 'measurement' as const,
  is_required: true,
  requires_gauge: false,
  spc_characteristic_id: 0,
  nominal: '10',
  lsl: '9.9',
  usl: '10.1',
  unit: 'mm',
  decimals: '',
  options_text: '',
  hint: '',
};

function errorPaths(input: unknown): string[] {
  const result = processSheetStepSchema.safeParse(input);
  if (result.success) return [];
  return result.error.issues.map((issue) => issue.path.join('.'));
}

describe('processSheetStepSchema — measurement rules', () => {
  it('accepts a valid measurement definition', () => {
    const result = processSheetStepSchema.safeParse(base);
    expect(result.success).toBe(true);
  });

  it('rejects lsl > usl', () => {
    expect(errorPaths({ ...base, lsl: '10', nominal: '10', usl: '5' })).toContain('usl');
  });

  it('rejects nominal outside [lsl, usl]', () => {
    expect(errorPaths({ ...base, lsl: '9', nominal: '12', usl: '11' })).toContain('nominal');
  });

  it('rejects lsl == usl (a zero-width tolerance band)', () => {
    expect(errorPaths({ ...base, lsl: '10', nominal: '10', usl: '10' })).toContain('usl');
  });

  it('rejects non-numeric limits', () => {
    expect(errorPaths({ ...base, lsl: 'abc' })).toContain('lsl');
  });

  it('rejects empty limits (an empty string is NOT silently zero)', () => {
    const paths = errorPaths({ ...base, nominal: '' });
    expect(paths).toContain('nominal');
  });

  it('requires a unit', () => {
    expect(errorPaths({ ...base, unit: '' })).toContain('unit');
  });

  it('rejects fractional or out-of-range decimals', () => {
    expect(errorPaths({ ...base, decimals: '1.5' })).toContain('decimals');
    expect(errorPaths({ ...base, decimals: '9' })).toContain('decimals');
  });

  it('ignores measurement config entirely for non-measurement types', () => {
    const result = processSheetStepSchema.safeParse({
      ...base,
      step_type: 'checkbox',
      nominal: '',
      lsl: '',
      usl: '',
      unit: '',
    });
    expect(result.success).toBe(true);
  });
});

describe('processSheetStepSchema — list rules', () => {
  it('requires at least one non-empty option', () => {
    expect(errorPaths({ ...base, step_type: 'list', options_text: '' })).toContain('options_text');
    expect(errorPaths({ ...base, step_type: 'list', options_text: '  \n \n' })).toContain('options_text');
  });

  it('accepts one option per line and trims blanks', () => {
    const result = processSheetStepSchema.safeParse({
      ...base,
      step_type: 'list',
      options_text: ' Pass \n\nFail\n',
    });
    expect(result.success).toBe(true);
    expect(parseListOptions(' Pass \n\nFail\n')).toEqual(['Pass', 'Fail']);
  });
});

describe('buildStepPayload — API contract mapping', () => {
  it('builds the measurement config with optional decimals', () => {
    const parsed = processSheetStepSchema.parse({ ...base, decimals: '3', requires_gauge: true });
    const payload = buildStepPayload(parsed);
    expect(payload).toEqual({
      sequence: 10,
      label: 'Bore diameter',
      instruction_text: null,
      step_type: 'measurement',
      is_required: true,
      config: { nominal: 10, lsl: 9.9, usl: 10.1, unit: 'mm', decimals: 3 },
      requires_gauge: true,
      spc_characteristic_id: null,
    });
  });

  it('forces INSTRUCTION steps to non-required and strips measurement-only flags', () => {
    const parsed = processSheetStepSchema.parse({
      ...base,
      step_type: 'instruction',
      is_required: true,
      requires_gauge: true,
      spc_characteristic_id: 4,
      instruction_text: 'Read the drawing notes first.',
    });
    const payload = buildStepPayload(parsed);
    expect(payload.is_required).toBe(false);
    expect(payload.requires_gauge).toBe(false);
    expect(payload.spc_characteristic_id).toBeNull();
    expect(payload.config).toBeNull();
    expect(payload.instruction_text).toBe('Read the drawing notes first.');
  });

  it('maps list options and photo/file hints into config', () => {
    const listPayload = buildStepPayload(
      processSheetStepSchema.parse({ ...base, step_type: 'list', options_text: 'Pass\nFail' })
    );
    expect(listPayload.config).toEqual({ options: ['Pass', 'Fail'] });

    const photoPayload = buildStepPayload(
      processSheetStepSchema.parse({ ...base, step_type: 'photo', hint: 'Photograph the weld seam' })
    );
    expect(photoPayload.config).toEqual({ hint: 'Photograph the weld seam' });

    const filePayload = buildStepPayload(processSheetStepSchema.parse({ ...base, step_type: 'file', hint: '' }));
    expect(filePayload.config).toBeNull();
  });

  it('carries a selected SPC characteristic only on measurement steps', () => {
    const parsed = processSheetStepSchema.parse({ ...base, spc_characteristic_id: 12 });
    expect(buildStepPayload(parsed).spc_characteristic_id).toBe(12);

    const nonMeasurement = processSheetStepSchema.parse({
      ...base,
      step_type: 'value',
      spc_characteristic_id: 12,
    });
    expect(buildStepPayload(nonMeasurement).spc_characteristic_id).toBeNull();
  });
});
