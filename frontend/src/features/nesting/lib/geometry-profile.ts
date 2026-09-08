import generated from './geometry-profile.generated.json';

/** Generated from the single normative backend JSON; verified by both build gates. */
export type GeometryProfileRef = { id: string; sha256: string };
export const CURRENT_GEOMETRY_PROFILE: Readonly<GeometryProfileRef> = Object.freeze({ ...generated.identity });

function deepFreeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}
export const GEOMETRY_PROFILE_SPEC = deepFreeze(generated.profile);
export const COMPENSATED_GEOMETRY_RULES = deepFreeze({
  ...generated.profile,
  numerics: Object.fromEntries(
    Object.entries(generated.profile.numerics).map(([key, value]) => [key, Number(value)])
  ) as {
    [K in keyof typeof generated.profile.numerics]: number;
  },
});

/** Absence is readable historical input; it never silently selects today's rules. */
export function resolveGeometryProfile(ref: unknown): typeof COMPENSATED_GEOMETRY_RULES | undefined {
  if (ref === undefined) return undefined;
  if (
    !ref ||
    typeof ref !== 'object' ||
    Array.isArray(ref) ||
    Object.keys(ref).sort().join(',') !== 'id,sha256' ||
    (ref as GeometryProfileRef).id !== CURRENT_GEOMETRY_PROFILE.id ||
    (ref as GeometryProfileRef).sha256 !== CURRENT_GEOMETRY_PROFILE.sha256
  )
    throw new Error(
      'Unknown or invalid geometry profile. Open a supported estimate or explicitly update its clearance rules.'
    );
  return COMPENSATED_GEOMETRY_RULES;
}

export function requireCurrentGeometryProfile(ref: unknown): void {
  if (!resolveGeometryProfile(ref))
    throw new Error(
      'This estimate uses earlier clearance rules. Use current clearance rules, then calculate a new nest. Saved server calculations require a new saved revision.'
    );
}

export function geometryProfileLabel(ref: unknown): string {
  return resolveGeometryProfile(ref) ? 'Compensated clearance envelopes' : 'Earlier nominal clearance rules';
}
