import { currencies, newEvidence } from './types';
import type { HardwareLine, QuoteFile, QuotePlan, SupplierOffer } from './types';

export const CSV_IMPORT_LIMIT = 500;
export const csvHardwareFields = [
  { key: 'part_id', label: 'Target part ID', offer: false, aliases: ['part_id', 'target_part_id', 'assembly_id'] },
  { key: 'manufacturer', label: 'Manufacturer', offer: false, aliases: ['manufacturer', 'mfr', 'brand'] },
  { key: 'mpn', label: 'Manufacturer part number', offer: false, aliases: ['mpn', 'manufacturer_part_number', 'manufacturer_part_no'] },
  { key: 'quantity_per_part', label: 'Quantity per part', offer: false, aliases: ['quantity_per_part', 'qty_per_part', 'quantity', 'qty'] },
  { key: 'supplier', label: 'Supplier', offer: true, aliases: ['supplier', 'vendor'] },
  { key: 'price', label: 'Quoted price', offer: true, aliases: ['price', 'quoted_price', 'unit_price'] },
  { key: 'price_unit_quantity', label: 'Each units covered by price', offer: true, aliases: ['price_unit_quantity', 'price_unit_qty'] },
  { key: 'minimum_price_quantity', label: 'Price applies from order quantity', offer: true, aliases: ['minimum_price_quantity', 'price_break_minimum', 'minimum_quantity'] },
  { key: 'currency', label: 'Currency', offer: true, aliases: ['currency', 'currency_code'] },
  { key: 'quoted_on', label: 'Quoted date (YYYY-MM-DD)', offer: true, aliases: ['quoted_on', 'quote_date', 'quoted_date'] },
  { key: 'valid_until', label: 'Valid until (YYYY-MM-DD)', offer: true, aliases: ['valid_until', 'expiry_date', 'expiration_date'] },
  { key: 'minimum_order_quantity', label: 'Minimum order quantity', offer: true, aliases: ['minimum_order_quantity', 'moq'] },
  { key: 'pack_quantity', label: 'Pack quantity', offer: true, aliases: ['pack_quantity', 'pack_qty', 'pack_size'] },
  { key: 'order_multiple', label: 'Order multiple', offer: true, aliases: ['order_multiple', 'order_increment'] },
  { key: 'freight', label: 'Freight for this offer', offer: true, aliases: ['freight', 'shipping_cost'] },
] as const;
export type CsvHardwareField = typeof csvHardwareFields[number]['key'];
export interface CsvColumnMapping { column: number | null; literal: string }
export type CsvHardwareMapping = Record<CsvHardwareField, CsvColumnMapping>;
export interface CsvRow { row_number: number; cells: string[] }
export interface CsvImportOptions { headerRow: number; targetPartId: string; includeOffers: boolean; mapping: CsvHardwareMapping }
export interface CsvHardwarePreviewRow { row: number; partId: string; manufacturer: string; mpn: string; quantity: string; supplier: string; price: string; currency: string; errors: string[]; warnings: string[]; hardware: HardwareLine | null }
export interface CsvHardwarePreview { fileId: number; planSignature: string; rows: CsvHardwarePreviewRow[]; errors: string[]; canImport: boolean }

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const headerKey = (value: string): string => value.trim().toLowerCase().replace(/[\s-]+/g, '_');
const identity = (manufacturer: string, mpn: string): string => JSON.stringify([manufacturer.trim().toLowerCase(), mpn.trim().toLowerCase()]);

export function csvRows(file: QuoteFile): CsvRow[] {
  const rows = record(record(file.analysis).table).rows;
  if (!Array.isArray(rows)) return [];
  return rows.filter((row): row is CsvRow => {
    const value = record(row);
    return Number.isInteger(value.row_number) && Number(value.row_number) > 0 && Array.isArray(value.cells) && value.cells.every(cell => typeof cell === 'string');
  });
}

export function suggestCsvHardwareMapping(headers: string[]): CsvHardwareMapping {
  return Object.fromEntries(csvHardwareFields.map(field => {
    const matches = headers.map((header, index) => (field.aliases as readonly string[]).includes(headerKey(header)) ? index : -1).filter(index => index >= 0);
    return [field.key, { column: matches.length === 1 ? matches[0] : null, literal: '' }];
  })) as CsvHardwareMapping;
}

export function csvImportPlanSignature(plan: QuotePlan): string {
  return JSON.stringify({ currency: plan.currency, parts: plan.parts, hardware: plan.hardware, source_reviews: plan.source_reviews });
}

function decimal(value: string, positive = false): boolean {
  if (value.length > 64 || !/^\d+(?:\.\d{1,9})?$/.test(value)) return false;
  const [rawInteger, fraction = ''] = value.split('.');
  const integer = rawInteger.replace(/^0+/, '') || '0';
  const maximum = '1000000000000';
  if (integer.length > maximum.length || (integer.length === maximum.length && integer > maximum)) return false;
  if (integer === maximum && /[1-9]/.test(fraction)) return false;
  return !positive || /[1-9]/.test(value);
}

function whole(value: string): boolean {
  return /^\d{1,12}$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= 1 && Number(value) <= 100000000;
}

function validDate(value: string): boolean {
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value) || value.startsWith('0000')) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

export function previewCsvHardwareImport(file: QuoteFile, plan: QuotePlan, options: CsvImportOptions): CsvHardwarePreview {
  const allRows = csvRows(file);
  const header = allRows.find(row => row.row_number === options.headerRow);
  const data = allRows.filter(row => row.row_number > options.headerRow && row.cells.some(cell => cell.trim()));
  const errors: string[] = [];
  const analysis = record(file.analysis);
  if (analysis.kind !== 'csv' || !header) errors.push('Select a header record from extracted CSV evidence.');
  if (analysis.status === 'partial' || analysis.status === 'error') errors.push('The CSV extraction is incomplete. Correct or split the source before importing.');
  if (!/^[a-f0-9]{64}$/.test(file.sha256)) errors.push('The CSV must have a recorded source SHA-256.');
  if (!data.length) errors.push('There are no nonempty records after the selected header.');
  if (data.length > CSV_IMPORT_LIMIT) errors.push(`Import at most ${CSV_IMPORT_LIMIT} records at a time. Split the CSV into complete smaller files.`);
  if (plan.hardware.length + data.length > 10000) errors.push('This import exceeds the quote limit of 10,000 hardware lines.');
  const seen = new Map<string, number>();
  const existing = new Set(plan.hardware.map(row => identity(row.manufacturer, row.mpn)));
  const rows = data.slice(0, CSV_IMPORT_LIMIT).map(row => {
    const get = (field: CsvHardwareField): string => {
      const mapped = options.mapping[field];
      return (mapped.column === null ? mapped.literal : row.cells[mapped.column] ?? '').trim();
    };
    const rowErrors: string[] = [];
    const warnings: string[] = [];
    if (row.cells.length !== header?.cells.length) rowErrors.push('Cell count differs from the header; correct the CSV before importing.');
    const partId = get('part_id') || options.targetPartId;
    const part = plan.parts.find(item => item.id === partId);
    if (!part || part.make_or_buy !== 'make') rowErrors.push('Choose a defined made part; purchased parts do not accrue internal hardware cost.');
    const manufacturer = get('manufacturer'); const mpn = get('mpn'); const quantity = get('quantity_per_part');
    if (!manufacturer || manufacturer.length > 120) rowErrors.push('Manufacturer is required and must be at most 120 characters.');
    if (!mpn || mpn.length > 120) rowErrors.push('Exact manufacturer part number is required and must be at most 120 characters.');
    if (!decimal(quantity, true)) rowErrors.push('Quantity per part requires a positive decimal, up to 9 decimal places; totals and pack quantities are different fields.');
    const key = identity(manufacturer, mpn);
    if (manufacturer && mpn) {
      if (seen.has(key)) rowErrors.push(`Identity repeats record ${seen.get(key)}. Merge demand explicitly; multi-row price tiers are not supported.`);
      else seen.set(key, row.row_number);
      if (existing.has(key)) rowErrors.push('This identity already exists in the quote. Reconcile demand and shared supply in Hardware before importing it again.');
    }
    const source = `${file.file_name}; SHA-256 ${file.sha256}; CSV record ${row.row_number}`;
    let offer: SupplierOffer | null = null;
    if (options.includeOffers) {
      const supplier = get('supplier'); const price = get('price'); const priceUnit = get('price_unit_quantity'); const minimum = get('minimum_price_quantity'); const currency = get('currency');
      const quotedOn = get('quoted_on'); const validUntil = get('valid_until'); const freight = get('freight');
      if (!supplier || supplier.length > 120) rowErrors.push('Supplier is required for an offer (maximum 120 characters).');
      if (!decimal(price)) rowErrors.push('Offer price requires an explicit nonnegative decimal; a blank price is unknown.');
      if (!decimal(priceUnit, true)) rowErrors.push('Enter the positive number of each units covered by the quoted price.');
      if (!decimal(minimum)) rowErrors.push('Enter the order quantity from which this single price applies.');
      if (!currencies.includes(currency) || currency !== plan.currency) rowErrors.push(`Offer currency must explicitly match quote currency ${plan.currency}; conversion is not performed.`);
      for (const field of ['minimum_order_quantity', 'pack_quantity', 'order_multiple'] as const) {
        if (!whole(get(field))) rowErrors.push(`${csvHardwareFields.find(item => item.key === field)?.label} requires an explicit whole number from 1 to 100,000,000.`);
      }
      if (quotedOn && !validDate(quotedOn)) rowErrors.push('Quoted date must be a real YYYY-MM-DD date.');
      if (validUntil && !validDate(validUntil)) rowErrors.push('Expiry must be a real YYYY-MM-DD date.');
      if (quotedOn && validUntil && validDate(quotedOn) && validDate(validUntil) && validUntil < quotedOn) rowErrors.push('Offer expiry precedes its quoted date.');
      if (freight && !decimal(freight)) rowErrors.push('Freight must be an explicit nonnegative decimal or left unknown.');
      if (!quotedOn) warnings.push('Quoted date is unknown; supply a current dated offer before approval.');
      if (!freight) warnings.push('Freight is unknown; confirm its cost or an explicit zero.');
      if (!rowErrors.length) offer = {
        id: `csv-offer-${file.id}-${row.row_number}`, manufacturer, mpn, supplier, currency,
        price_unit_quantity: priceUnit, price_breaks: [{ minimum_quantity: minimum, price }],
        pack_quantity: Number(get('pack_quantity')), minimum_order_quantity: Number(get('minimum_order_quantity')), order_multiple: Number(get('order_multiple')),
        quoted_on: quotedOn || null, valid_until: validUntil || null, max_age_days: 30, applicable: false, freight: freight || null,
        evidence: { ...newEvidence(), source, note: 'Mapped supplier offer, unreviewed. Confirm price basis, applicability and the 30-day freshness policy.' },
      };
    } else warnings.push('BOM demand only; no supplier offer or inventory value imported.');
    const hardware: HardwareLine | null = rowErrors.length ? null : {
      id: `csv-hardware-${file.id}-${row.row_number}`, part_id: partId, manufacturer, mpn, quantity_per_part: quantity,
      stock_available: 0, stock_unit_value: null, offer,
      evidence: { ...newEvidence(), source, note: 'Mapped CSV demand per made part. No inventory allocated. Review identity and quantity before approval.' },
    };
    if (hardware && plan.hardware.some(item => item.id === hardware.id)) rowErrors.push('This source record has already been imported. Reconcile the existing line.');
    return { row: row.row_number, partId, manufacturer, mpn, quantity, supplier: get('supplier'), price: options.includeOffers ? get('price') : '', currency: options.includeOffers ? get('currency') : '', errors: rowErrors, warnings, hardware: rowErrors.length ? null : hardware };
  });
  return { fileId: file.id, planSignature: csvImportPlanSignature(plan), rows, errors, canImport: !errors.length && rows.length > 0 && rows.every(row => !row.errors.length && row.hardware) };
}

export function appendCsvHardwareImport(plan: QuotePlan, preview: CsvHardwarePreview): QuotePlan {
  if (!preview.canImport || preview.rows.some(row => !row.hardware || row.errors.length)) throw new Error('Resolve CSV mapping and row errors before importing.');
  if (preview.planSignature !== csvImportPlanSignature(plan)) throw new Error('The quote changed after this preview. Preview the import again.');
  const hardware = preview.rows.map(row => row.hardware as HardwareLine);
  const affected = new Set(hardware.map(row => row.part_id));
  return {
    ...plan, hardware: [...plan.hardware, ...hardware],
    parts: plan.parts.map(part => affected.has(part.id) ? { ...part, costing_complete: false } : part),
    source_reviews: plan.source_reviews.filter(review => review.file_id !== preview.fileId),
  };
}
