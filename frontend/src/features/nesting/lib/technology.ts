export const recipeId = (material: string, thickness: number) => `${material}|${Number(thickness.toFixed(8))}`;
export const recipeFields = [
  ['feed', 'Cut speed', 'in/min'],
  ['kerf', 'Kerf', 'in'],
  ['pressure', 'Gas pressure', 'psi'],
  ['nozzle', 'Nozzle diameter', 'in'],
  ['focus', 'Focus offset', 'in'],
  ['height', 'Cut height', 'in'],
  ['pierce', 'Pierce time', 's'],
  ['power', 'Power', 'W'],
] as const;
export type Recipe = {
  material: string;
  thickness: number;
  source: string;
  gas: string;
  feed: string;
  kerf: string;
  pressure: string;
  nozzle: string;
  focus: string;
  height: string;
  pierce: string;
  power: string;
};
export function validateRecipes(data: unknown): Record<string, Recipe> {
  if (!Array.isArray(data) || data.length > 200) throw new Error('Expected an array of at most 200 recipe records.');
  const next: Record<string, Recipe> = {};
  for (const r of data) {
    if (
      !r ||
      !['Carbon steel', 'Stainless steel', 'Aluminum'].includes(r.material) ||
      !Number.isFinite(r.thickness) ||
      r.thickness <= 0 ||
      r.thickness > 100 ||
      typeof r.source !== 'string' ||
      r.source.length > 1000 ||
      !['Unspecified', 'Oxygen', 'Nitrogen', 'Air', 'Argon'].includes(r.gas)
    )
      throw new Error('Invalid recipe material, thickness, source, or gas.');
    for (const [key, label] of recipeFields) {
      if (
        typeof r[key] !== 'string' ||
        (r[key] !== '' &&
          (r[key].trim() === '' || !Number.isFinite(Number(r[key])) || (key !== 'focus' && Number(r[key]) < 0)))
      )
        throw new Error(`${label} must be a valid number or left blank.`);
    }
    if (Number(r.power) > 6000) throw new Error('Power exceeds 6,000 W.');
    if (r.feed !== '' && Number(r.feed) <= 0) throw new Error('Cut speed must be positive or left blank.');
    const id = recipeId(r.material, r.thickness);
    if (next[id]) throw new Error(`Duplicate recipe for ${r.material} / ${r.thickness / 25.4} in.`);
    next[id] = r;
  }
  return next;
}
