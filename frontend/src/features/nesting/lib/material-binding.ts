import { z } from 'zod';
import type { NestingCatalogMaterial, NestingMaterialResolution, NestingPriceBasis } from '../../../types/quoteNesting';
import { mmToIn } from './units';

export type MaterialBinding = {
  companyId: number;
  catalog: NestingCatalogMaterial;
  priceBasis?: NestingPriceBasis;
  priceKey?: string | null;
  resolution?: NestingMaterialResolution;
  acknowledgement?: { contentHash: string; currency: 'USD'; reviewed: true };
};

const text = z.string().max(1000);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const decimal = z
  .string()
  .max(100)
  .refine(value => value.trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0);
const basis = z.enum(['per_lb', 'per_cubic_inch', 'per_square_foot']);
export const catalogMaterialSchema = z.object({
  id: z.number().int().positive(),
  name: z.string().min(1).max(255),
  category: z.string().min(1).max(80),
  source_updated_at: text.nullable(),
  catalog_hash: hash,
  density_lb_per_cubic_inch: decimal.nullable(),
  price_options: z
    .array(
      z.object({
        price_basis: basis,
        price_key: text.nullable(),
        source_field: text,
        unit_price: decimal.nullable(),
      })
    )
    .max(200),
  missing_metadata: z.array(text).max(100),
});
const resolutionSchema = z.object({
  schema_version: z.literal(1),
  company_id: z.number().int().positive(),
  catalog_material: catalogMaterialSchema,
  thickness_in: decimal,
  price_basis: basis,
  price_key: text.nullable(),
  currency: z.null(),
  status: z.enum(['review_required', 'unresolved']),
  calculable: z.boolean(),
  confirmed: z.literal(false),
  stocks: z
    .array(
      z.object({
        id: z.string().min(1).max(200),
        width_in: decimal,
        length_in: decimal,
        area_sq_in: decimal,
        area_sq_ft: decimal,
        volume_cu_in: decimal,
        weight_lb: decimal.nullable(),
        sheet_cost: decimal.nullable(),
      })
    )
    .max(20),
  issues: z.array(z.object({ code: text, field: text, message: text })).max(200),
  content_hash: hash,
});
const bindingSchema = z.object({
  companyId: z.number().int().positive(),
  catalog: catalogMaterialSchema,
  priceBasis: basis.optional(),
  priceKey: text.nullable().optional(),
  resolution: resolutionSchema.optional(),
  acknowledgement: z.object({ contentHash: hash, currency: z.literal('USD'), reviewed: z.literal(true) }).optional(),
});

export function validateMaterialBinding(input: unknown): asserts input is MaterialBinding {
  const parsed = bindingSchema.safeParse(input);
  if (!parsed.success) throw new Error('Invalid saved ERP material or pricing provenance.');
  const binding = parsed.data;
  if (
    binding.resolution &&
    (binding.resolution.company_id !== binding.companyId ||
      binding.resolution.catalog_material.id !== binding.catalog.id ||
      binding.resolution.catalog_material.catalog_hash !== binding.catalog.catalog_hash ||
      binding.resolution.price_basis !== binding.priceBasis ||
      binding.resolution.price_key !== (binding.priceKey ?? null))
  )
    throw new Error('ERP material resolution does not match its selected source.');
  if (binding.acknowledgement && binding.acknowledgement.contentHash !== binding.resolution?.content_hash)
    throw new Error('Pricing acknowledgment does not match its source snapshot.');
}

export function catalogFamily(category: string): string | undefined {
  return ({ steel: 'Carbon steel', stainless: 'Stainless steel', aluminum: 'Aluminum' } as Record<string, string>)[
    category
  ];
}

type PricingInputs = {
  thickness: number;
  materialBinding?: MaterialBinding;
  options: { id: string; width: number; height: number; price: number | null }[];
};

/** Length is the nest X axis; width is the nest Y axis, both sent in inches. */
export function resolutionMatchesInputs(input: PricingInputs): boolean {
  const binding = input.materialBinding,
    result = binding?.resolution;
  if (!binding || !result || !result.calculable || result.stocks.length !== input.options.length) return false;
  const matches = (value: string, mm: number) => Math.abs(Number(value) - mmToIn(mm)) < 1e-8;
  return (
    result.company_id === binding.companyId &&
    result.catalog_material.id === binding.catalog.id &&
    result.catalog_material.catalog_hash === binding.catalog.catalog_hash &&
    result.price_basis === binding.priceBasis &&
    result.price_key === (binding.priceKey ?? null) &&
    matches(result.thickness_in, input.thickness) &&
    new Set(result.stocks.map(stock => stock.id)).size === input.options.length &&
    input.options.every(option =>
      result.stocks.some(
        stock =>
          stock.id === option.id &&
          matches(stock.length_in, option.width) &&
          matches(stock.width_in, option.height) &&
          stock.sheet_cost !== null &&
          Number.isFinite(Number(stock.sheet_cost)) &&
          Number(stock.sheet_cost) > 0
      )
    )
  );
}

export function hasAcknowledgedCatalogPricing(input: PricingInputs): boolean {
  return (
    resolutionMatchesInputs(input) &&
    input.materialBinding?.acknowledgement?.currency === 'USD' &&
    input.materialBinding?.acknowledgement?.reviewed === true &&
    input.materialBinding?.acknowledgement?.contentHash === input.materialBinding?.resolution?.content_hash &&
    input.options.every(
      option =>
        option.price ===
        Number(input.materialBinding!.resolution!.stocks.find(stock => stock.id === option.id)?.sheet_cost)
    )
  );
}

export function clearCatalogPricing<T extends PricingInputs>(input: T): T {
  if (!input.materialBinding) return input;
  const { acknowledgement: _acknowledgement, ...binding } = input.materialBinding;
  void _acknowledgement;
  return { ...input, materialBinding: binding, options: input.options.map(option => ({ ...option, price: null })) };
}

export function materialGroupKey(input: {
  material: string;
  thickness: number;
  materialBinding?: MaterialBinding;
}): string {
  const identity = input.materialBinding
    ? ['catalog', input.materialBinding.companyId, input.materialBinding.catalog.id]
    : ['unassigned', input.material];
  return JSON.stringify([...identity, input.material, Math.round(input.thickness / 1e-6)]);
}

export const materialGroupLabel = (input: { material: string; materialBinding?: MaterialBinding }) =>
  input.materialBinding?.catalog.name ?? input.material;

/** Bound API decimal precision without changing displayed/imported physical geometry. */
export const decimalInches = (mm: number) =>
  mmToIn(mm)
    .toFixed(12)
    .replace(/\.?0+$/, '');
