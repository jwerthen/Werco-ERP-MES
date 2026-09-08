import type { NestingCatalogMaterial, NestingMaterialResolution } from '../types/quoteNesting';
import { createBlankQuote, type Quote } from '../features/nesting/lib/quoting';
import { mmToIn } from '../features/nesting/lib/units';
import type { MaterialBinding } from '../features/nesting/lib/material-binding';

export function catalogFixture(id = 11): NestingCatalogMaterial {
  return {
    id,
    name: 'A36 sheet',
    category: 'steel',
    source_updated_at: null,
    catalog_hash: 'a'.repeat(64),
    density_lb_per_cubic_inch: '0.284',
    price_options: [
      { price_basis: 'per_lb', price_key: null, source_field: 'stock_price_per_pound', unit_price: '0.9' },
    ],
    missing_metadata: ['currency', 'grade', 'inventory_mapping', 'approved_revision'],
  };
}

export function catalogQuoteFixture(acknowledged = false) {
  const quote: Quote = createBlankQuote();
  const catalog = catalogFixture();
  const resolution: NestingMaterialResolution = {
    schema_version: 1,
    company_id: 2,
    catalog_material: catalog,
    thickness_in: '0.125',
    price_basis: 'per_lb',
    price_key: null,
    currency: null,
    status: 'review_required',
    calculable: true,
    confirmed: false,
    content_hash: 'b'.repeat(64),
    issues: [{ code: 'missing_currency', field: 'currency', message: 'Currency is not recorded.' }],
    stocks: quote.options.map(option => {
      const width = mmToIn(option.height),
        length = mmToIn(option.width),
        area = width * length;
      const volume = area * 0.125,
        weight = volume * 0.284;
      return {
        id: option.id,
        width_in: String(width),
        length_in: String(length),
        area_sq_in: String(area),
        area_sq_ft: String(area / 144),
        volume_cu_in: String(volume),
        weight_lb: String(weight),
        sheet_cost: (weight * 0.9).toFixed(2),
      };
    }),
  };
  const binding: MaterialBinding = {
    companyId: 2,
    catalog,
    priceBasis: 'per_lb',
    resolution,
    ...(acknowledged
      ? { acknowledgement: { contentHash: resolution.content_hash, currency: 'USD' as const, reviewed: true as const } }
      : {}),
  };
  quote.materialBinding = binding;
  if (acknowledged)
    quote.options = quote.options.map(option => ({
      ...option,
      price: Number(resolution.stocks.find(stock => stock.id === option.id)!.sheet_cost),
    }));
  return { quote, binding, resolution, catalog };
}
