import { appendCsvHardwareImport, CSV_IMPORT_LIMIT, previewCsvHardwareImport, suggestCsvHardwareMapping } from './csvHardwareImportModel';
import type { CsvImportOptions } from './csvHardwareImportModel';
import { emptyPlan, newEvidence } from './types';
import type { QuoteFile, QuotePlan } from './types';

const headers = ['part_id', 'manufacturer', 'mpn', 'quantity_per_part', 'supplier', 'price', 'price_unit_quantity', 'minimum_price_quantity', 'currency', 'quoted_on', 'valid_until', 'moq', 'pack_quantity', 'order_multiple', 'freight'];
const values = ['A', 'Acme', '0000123', '2', 'Vendor', '0.123456789', '100', '100', 'USD', '2026-09-01', '2026-09-30', '100', '100', '100', ''];
const plan = (): QuotePlan => ({ ...emptyPlan(), parts: [{ id: 'A', name: 'Assembly', make_or_buy: 'make', costing_complete: true, purchase_unit_cost: null, evidence: newEvidence() }], source_reviews: [{ file_id: 7, sha256: 'a'.repeat(64), disposition: 'reviewed', note: 'Previous source review' }] });
const file = (data: string[][] = [values]): QuoteFile => ({ id: 7, file_name: 'hardware.csv', sha256: 'a'.repeat(64), analysis: { kind: 'csv', status: 'review_required', table: { rows: [headers, ...data].map((cells, index) => ({ row_number: index + 1, cells })) } } });
const options = (includeOffers = true): CsvImportOptions => ({ headerRow: 1, targetPartId: '', includeOffers, mapping: suggestCsvHardwareMapping(headers) });
const issues = (data: string[]) => previewCsvHardwareImport(file([data]), plan(), options()).rows[0].errors.join(' ');

test('mapped offer preserves exact MPN and decimals, units and unreviewed provenance', () => {
  const result = previewCsvHardwareImport(file(), plan(), options());
  expect(result.canImport).toBe(true);
  const hardware = result.rows[0].hardware!;
  expect(hardware.mpn).toBe('0000123');
  expect(hardware.offer).toMatchObject({ price_unit_quantity: '100', price_breaks: [{ minimum_quantity: '100', price: '0.123456789' }], freight: null, applicable: false, quoted_on: '2026-09-01', pack_quantity: 100 });
  expect(hardware.evidence.reviewed).toBe(false);
  expect(hardware.offer?.evidence.reviewed).toBe(false);
  expect(hardware.evidence.source).toContain('SHA-256 ' + 'a'.repeat(64));
  expect(hardware.evidence.source).toContain('CSV record 2');
  expect(result.rows[0].warnings.join()).toContain('Freight is unknown');
});

test('BOM-only import never invents supplier prices or valued inventory', () => {
  const source = file([values.map((value, index) => index >= 4 ? '' : value)]);
  const original = plan();
  const preview = previewCsvHardwareImport(source, original, options(false));
  expect(preview.canImport).toBe(true);
  const next = appendCsvHardwareImport(original, preview);
  expect(next.hardware[0]).toMatchObject({ offer: null, stock_available: 0, stock_unit_value: null });
  expect(next.parts[0].costing_complete).toBe(false);
  expect(next.source_reviews).toEqual([]);
  expect(original.parts[0].costing_complete).toBe(true);
  expect(original.hardware).toHaveLength(0);
  expect(() => appendCsvHardwareImport(next, preview)).toThrow('changed');
});

test('explicit column constants and target part selection work without guessing missing terms', () => {
  const settings = options();
  settings.targetPartId = 'A'; settings.mapping.part_id.column = null;
  settings.mapping.price_unit_quantity = { column: null, literal: '100' };
  settings.mapping.quoted_on = { column: null, literal: '' };
  const preview = previewCsvHardwareImport(file(), plan(), settings);
  expect(preview.canImport).toBe(true);
  expect(preview.rows[0].hardware?.offer?.quoted_on).toBeNull();
  settings.mapping.price_unit_quantity.literal = '';
  expect(previewCsvHardwareImport(file(), plan(), settings).canImport).toBe(false);
});

test('ambiguous headers require estimator mapping and never choose the first matching column', () => {
  expect(suggestCsvHardwareMapping(['mpn', 'manufacturer_part_number']).mpn.column).toBeNull();
  expect(suggestCsvHardwareMapping([' Unit Price ']).price.column).toBe(0);
});

test('duplicate CSV identities and tier rows block the entire import', () => {
  const second = [...values]; second[1] = ' acme '; second[5] = '0.10';
  const preview = previewCsvHardwareImport(file([values, second]), plan(), options());
  expect(preview.canImport).toBe(false);
  expect(preview.rows[1].errors.join()).toContain('multi-row price tiers');
  expect(() => appendCsvHardwareImport(plan(), preview)).toThrow('Resolve');
});

test('existing identity on another part blocks silent duplication or conflicting stock pools', () => {
  const p = plan();
  const hardware = previewCsvHardwareImport(file(), p, options()).rows[0].hardware!;
  p.hardware.push({ ...hardware, part_id: 'different-part', manufacturer: 'ACME' });
  const preview = previewCsvHardwareImport(file(), p, options());
  expect(preview.canImport).toBe(false);
  expect(preview.rows[0].errors.join()).toContain('shared supply');
});

test.each([
  [1, '', 'Manufacturer is required'], [2, '', 'Exact manufacturer part number'],
  [3, '', 'Quantity per part'], [3, '0', 'Quantity per part'], [3, '1e3', 'Quantity per part'],
  [5, '', 'blank price is unknown'], [5, '-1', 'Offer price'], [5, '0.1234567890', 'Offer price'],
  [5, '1000000000000.000000001', 'Offer price'], [5, 'NaN', 'Offer price'],
  [6, '', 'each units'], [7, '', 'order quantity'], [8, 'EUR', 'currency'], [8, 'usd', 'currency'],
  [9, '2026-02-30', 'real YYYY-MM-DD'], [9, '09/01/2026', 'real YYYY-MM-DD'],
  [10, '2026-08-01', 'precedes'], [11, '', 'Minimum order quantity'], [12, '0', 'Pack quantity'],
  [13, '1.5', 'Order multiple'], [14, '$10', 'Freight'],
] as const)('invalid mapped field %s=%s blocks import', (index, value, expected) => {
  const changed = [...values]; changed[index] = value;
  expect(issues(changed)).toContain(expected);
});

test('explicit zero price and freight are preserved instead of treated as blanks', () => {
  const changed = [...values]; changed[5] = '0'; changed[14] = '0';
  const preview = previewCsvHardwareImport(file([changed]), plan(), options());
  expect(preview.canImport).toBe(true);
  expect(preview.rows[0].hardware?.offer?.price_breaks[0].price).toBe('0');
  expect(preview.rows[0].hardware?.offer?.freight).toBe('0');
});

test('purchase boundary changes invalidate previews and cannot hide imported hardware cost', () => {
  const p = plan(); const preview = previewCsvHardwareImport(file(), p, options());
  p.parts[0].make_or_buy = 'buy';
  expect(() => appendCsvHardwareImport(p, preview)).toThrow('changed');
  expect(previewCsvHardwareImport(file(), p, options()).rows[0].errors.join()).toContain('made part');
});

test('partial extraction, mismatched cells and excessive row counts cannot become a partial import', () => {
  const partial = file(); partial.analysis!.status = 'partial';
  expect(previewCsvHardwareImport(partial, plan(), options()).canImport).toBe(false);
  const short = file([values.slice(0, -1)]);
  expect(previewCsvHardwareImport(short, plan(), options()).rows[0].errors.join()).toContain('Cell count');
  const large = file(Array.from({ length: CSV_IMPORT_LIMIT + 1 }, (_, index) => values.map((value, column) => column === 2 ? `MPN${index}` : value)));
  const preview = previewCsvHardwareImport(large, plan(), options());
  expect(preview.rows).toHaveLength(CSV_IMPORT_LIMIT);
  expect(preview.canImport).toBe(false);
  expect(preview.errors.join()).toContain('at most 500');
});
