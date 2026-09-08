import { inToMm, mmToIn } from './units';

export type PolicyMaterial = 'Carbon steel' | 'Stainless steel' | 'Aluminum';
export type SpacingBand = {
  id: string;
  material: PolicyMaterial;
  thickness_min_in: string;
  thickness_max_in: string;
  minimum_gap_in: string;
  gap_thickness_multiplier: string;
  minimum_margin_in: string;
  margin_thickness_multiplier: string;
};
export type SpacingPolicyContent = { schema_version: 1; units: 'in'; name: string; bands: SpacingBand[] };
export type SpacingPolicySnapshot = {
  schema_version: 1;
  company_id: number;
  policy_id: number;
  publication_id: number;
  revision_id: number;
  revision_number: number;
  content_sha256: string;
  band: SpacingBand;
  thickness_in: string;
  gap_in: string;
  margin_in: string;
  resolved_at: string;
};
export type SpacingOverride = { schema_version: 1; reason: string; changed_at: string };

const SCALE = BigInt('1000000000');
const ZERO = BigInt(0);
const ONE = BigInt(1);
const TWO = BigInt(2);
const MATERIALS = ['Carbon steel', 'Stainless steel', 'Aluminum'];
const BAND_KEYS = [
  'id',
  'material',
  'thickness_min_in',
  'thickness_max_in',
  'minimum_gap_in',
  'gap_thickness_multiplier',
  'minimum_margin_in',
  'margin_thickness_multiplier',
];

function requireValue(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function exactKeys(value: unknown, keys: string[], message: string): asserts value is Record<string, unknown> {
  requireValue(value && typeof value === 'object' && !Array.isArray(value), message);
  const actual = Object.keys(value);
  requireValue(actual.length === keys.length && actual.every(key => keys.includes(key)), message);
}
function isUtcTimestamp(value: unknown): value is string {
  return typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
}
function decimalText(value: bigint): string {
  const whole = value / SCALE;
  const fraction = (value % SCALE).toString().padStart(9, '0').replace(/0+$/, '');
  return whole.toString() + (fraction ? '.' + fraction : '');
}
function decimalUnits(value: unknown): bigint {
  requireValue(
    typeof value === 'string' && value.length <= 24 && /^(0|[1-9]\d*)(\.\d{1,9})?$/.test(value),
    'Policy values must be decimal inches with at most nine decimal places.'
  );
  const [whole, fraction = ''] = value.split('.');
  const units = BigInt(whole) * SCALE + BigInt(fraction.padEnd(9, '0'));
  requireValue(decimalText(units) === value, 'Policy decimals must omit unnecessary zeros.');
  return units;
}

/** Normalize form text; stored policy values always use canonical decimal strings. */
export function normalizePolicyDecimal(value: string): string {
  const input = value.trim();
  requireValue(input.length <= 24 && /^\d+(\.\d{1,9})?$/.test(input), 'Enter a decimal with up to nine places.');
  const [whole, fraction = ''] = input.split('.');
  return decimalText(BigInt(whole) * SCALE + BigInt(fraction.padEnd(9, '0')));
}

/** Half-up rounding of a finite number's decimal representation, including exponent notation. */
function roundedPolicyValue(value: number, maximum: number): string {
  requireValue(
    Number.isFinite(value) && value > 0 && value <= maximum,
    'Policy dimension is outside its supported range.'
  );
  const [mantissa, exponentText = '0'] = String(value).toLowerCase().split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  const digits = BigInt(whole + fraction);
  const places = 9 + Number(exponentText) - fraction.length;
  const power = BigInt('1' + '0'.repeat(Math.abs(places)));
  const result = places >= 0 ? digits * power : digits / power + ((digits % power) * TWO >= power ? ONE : ZERO);
  requireValue(result > ZERO, 'Thickness is below the policy resolution of 0.000000001 inch.');
  return decimalText(result);
}

export function policyThicknessIn(value: number): string {
  return roundedPolicyValue(value, 4);
}

export function validateSpacingBand(value: unknown): SpacingBand {
  exactKeys(value, BAND_KEYS, 'Invalid spacing policy band.');
  requireValue(typeof value.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value.id), 'Invalid policy band ID.');
  requireValue(
    typeof value.material === 'string' && MATERIALS.includes(value.material),
    'Invalid policy material family.'
  );
  const lo = decimalUnits(value.thickness_min_in);
  const hi = decimalUnits(value.thickness_max_in);
  requireValue(lo < hi && hi <= BigInt(4) * SCALE, 'Thickness bands must increase and remain within four inches.');
  for (const [minimumKey, multiplierKey] of [
    ['minimum_gap_in', 'gap_thickness_multiplier'],
    ['minimum_margin_in', 'margin_thickness_multiplier'],
  ]) {
    const minimum = decimalUnits(value[minimumKey]);
    const multiplier = decimalUnits(value[multiplierKey]);
    requireValue(
      minimum <= BigInt(100) * SCALE && multiplier <= BigInt(100) * SCALE,
      'Policy constants must be no greater than 100.'
    );
    requireValue(minimum > ZERO || multiplier > ZERO, 'Each spacing formula must yield a positive allowance.');
  }
  return value as SpacingBand;
}

export function validateSpacingContent(value: unknown): SpacingPolicyContent {
  exactKeys(value, ['schema_version', 'units', 'name', 'bands'], 'Invalid spacing policy content.');
  requireValue(value.schema_version === 1 && value.units === 'in', 'Spacing policy must declare inches.');
  requireValue(
    typeof value.name === 'string' && value.name.trim().length > 0 && value.name.length <= 199,
    'Enter a policy name.'
  );
  requireValue(
    Array.isArray(value.bands) && value.bands.length > 0 && value.bands.length <= 128,
    'A policy must contain 1–128 thickness bands.'
  );
  const bands = value.bands.map(validateSpacingBand);
  requireValue(new Set(bands.map(band => band.id)).size === bands.length, 'Policy band IDs must be unique.');
  for (let i = 0; i < bands.length; i++) {
    for (let j = i + 1; j < bands.length; j++) {
      const a = bands[i],
        b = bands[j];
      if (a.material === b.material)
        requireValue(
          decimalUnits(a.thickness_max_in) <= decimalUnits(b.thickness_min_in) ||
            decimalUnits(b.thickness_max_in) <= decimalUnits(a.thickness_min_in),
          'Thickness bands for the same material cannot overlap.'
        );
    }
  }
  return value as SpacingPolicyContent;
}

export function resolveSpacingBand(band: SpacingBand, material: string, thicknessIn: number) {
  validateSpacingBand(band);
  const thickness_in = policyThicknessIn(thicknessIn);
  const thickness = decimalUnits(thickness_in);
  requireValue(
    band.material === material &&
      thickness >= decimalUnits(band.thickness_min_in) &&
      thickness < decimalUnits(band.thickness_max_in),
    'This policy band does not match the material and thickness.'
  );
  function allowance(minimum: string, multiplier: string) {
    const product = thickness * decimalUnits(multiplier);
    const upward = product / SCALE + (product % SCALE > ZERO ? ONE : ZERO);
    return decimalText(upward > decimalUnits(minimum) ? upward : decimalUnits(minimum));
  }
  return {
    thickness_in,
    gap_in: allowance(band.minimum_gap_in, band.gap_thickness_multiplier),
    margin_in: allowance(band.minimum_margin_in, band.margin_thickness_multiplier),
  };
}

/** Local consistency only. The API verifies publication authority and current eligibility. */
export function validatePolicySnapshot(
  value: unknown,
  material: string,
  thicknessMm: number,
  gapMm: number,
  marginMm: number
): SpacingPolicySnapshot {
  exactKeys(
    value,
    [
      'schema_version',
      'company_id',
      'policy_id',
      'publication_id',
      'revision_id',
      'revision_number',
      'content_sha256',
      'band',
      'thickness_in',
      'gap_in',
      'margin_in',
      'resolved_at',
    ],
    'Invalid saved spacing policy.'
  );
  requireValue(value.schema_version === 1, 'Unsupported spacing policy snapshot.');
  for (const key of ['company_id', 'policy_id', 'publication_id', 'revision_id', 'revision_number'])
    requireValue(Number.isSafeInteger(value[key]) && Number(value[key]) > 0, 'Invalid spacing policy identity.');
  requireValue(
    typeof value.content_sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.content_sha256),
    'Invalid policy hash.'
  );
  requireValue(
    isUtcTimestamp(value.resolved_at),
    'Policy resolution must record a UTC timestamp.'
  );
  const band = validateSpacingBand(value.band);
  const resolved = resolveSpacingBand(band, material, mmToIn(thicknessMm));
  for (const key of ['thickness_in', 'gap_in', 'margin_in'] as const)
    requireValue(value[key] === resolved[key], 'Saved spacing does not match its policy formula.');
  requireValue(
    gapMm === inToMm(Number(resolved.gap_in)) && marginMm === inToMm(Number(resolved.margin_in)),
    'Current spacing differs from its applied policy.'
  );
  return value as SpacingPolicySnapshot;
}

export function validateSpacingOverride(value: unknown): SpacingOverride {
  exactKeys(value, ['schema_version', 'reason', 'changed_at'], 'Invalid custom-spacing reason.');
  requireValue(
    value.schema_version === 1 &&
      typeof value.reason === 'string' &&
      value.reason.trim().length > 0 &&
      value.reason.length <= 1000,
    'Enter a reason for custom spacing.'
  );
  requireValue(
    isUtcTimestamp(value.changed_at),
    'Custom spacing must record a UTC timestamp.'
  );
  return value as SpacingOverride;
}
