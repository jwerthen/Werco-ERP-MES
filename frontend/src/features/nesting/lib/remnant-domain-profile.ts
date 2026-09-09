import generated from './remnant-domain-profile.generated.json';
import type { GeometryProfileRef } from './geometry-profile';

function freeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

export const REMNANT_DOMAIN_PROFILE: Readonly<GeometryProfileRef> = freeze({ ...generated.identity });
export const REMNANT_DOMAIN_SPEC = freeze(generated.profile);
export const REMNANT_DOMAIN_RULES = freeze({
  ...generated.profile,
  numerics: Object.fromEntries(
    Object.entries(generated.profile.numerics).map(([key, value]) => [key, Number(value)])
  ) as {
    [K in keyof typeof generated.profile.numerics]: number;
  },
});

export function requireRemnantDomainProfile(ref: unknown): void {
  if (
    !ref ||
    typeof ref !== 'object' ||
    Array.isArray(ref) ||
    Object.keys(ref).sort().join(',') !== 'id,sha256' ||
    (ref as GeometryProfileRef).id !== REMNANT_DOMAIN_PROFILE.id ||
    (ref as GeometryProfileRef).sha256 !== REMNANT_DOMAIN_PROFILE.sha256
  )
    throw new Error('Unknown recorded-piece geometry profile. Review the saved planning inputs.');
}
