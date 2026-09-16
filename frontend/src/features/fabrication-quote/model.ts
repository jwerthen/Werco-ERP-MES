import { CalculationResult, QuotePlan, QuoteRecord, QuoteWrite } from './types';

const decimalKeys = new Set(['quantity', 'purchase_unit_cost', 'batch_size', 'consumed_quantity', 'unit_cost', 'labor_seconds', 'machine_seconds', 'cut_length_mm', 'speed_mm_per_second', 'pierce_seconds', 'noncut_machine_seconds', 'dynamics_allowance_seconds', 'seconds_per_hit', 'handling_seconds', 'inspection_seconds', 'weld_length_mm', 'weld_size_mm', 'travel_speed_mm_per_second', 'nonweld_labor_seconds', 'nonweld_machine_seconds', 'setup_labor_seconds', 'setup_machine_seconds', 'labor_rate_per_hour', 'machine_rate_per_hour', 'consumables_cost_per_run', 'outside_cost_per_run', 'minimum_quantity', 'price', 'price_unit_quantity', 'freight', 'quantity_per_part', 'stock_unit_value', 'target_margin']);
const integerKeys = new Set(['pierces', 'hits', 'crew_size', 'pack_quantity', 'minimum_order_quantity', 'order_multiple', 'max_age_days', 'stock_available']);

/** Prevent malformed transient form input from being sent as zero/NaN. Business checks stay in the engine. */
export function inputErrors(write: QuoteWrite): string[] {
  const errors: string[] = [];
  if (!write.title.trim()) errors.push('Give this quote a title.');
  if (write.title.length > 200) errors.push('Keep the quote title within 200 characters.');
  const walk = (value: unknown, path: string) => {
    if (!value || typeof value !== 'object') return;
    Object.entries(value).forEach(([key, child]) => {
      const next = `${path}.${key}`;
      if (decimalKeys.has(key) && child !== null && (typeof child !== 'string' || !/^\d+(?:\.\d{1,9})?$/.test(child) || child.length > 24)) errors.push(`${next}: enter a nonnegative decimal with up to 9 decimal places, or leave unknown values blank.`);
      if (integerKeys.has(key) && (typeof child !== 'number' || !Number.isInteger(child) || child < 0)) errors.push(`${next}: enter a whole number.`);
      if (typeof child === 'object') walk(child, next);
    });
  };
  walk(write.plan, 'Plan');
  write.plan.hardware.forEach((line, i) => {
    if (!line.manufacturer.trim() || !line.mpn.trim()) errors.push(`Hardware ${i + 1}: enter the exact manufacturer and part number.`);
    if (line.offer && (!line.offer.supplier.trim() || !line.offer.manufacturer.trim() || !line.offer.mpn.trim())) errors.push(`Hardware ${i + 1}: complete the supplier offer identity.`);
  });
  write.plan.assumptions.forEach((a, i) => { if (!a.description.trim()) errors.push(`Assumption ${i + 1}: add a description.`); });
  return errors;
}
export function snapshot(write: QuoteWrite): string { return JSON.stringify(write); }
export function writeFromRecord(record: QuoteRecord): QuoteWrite { return { title: record.title, customer_id: record.customer_id ?? null, plan: record.plan }; }
export function isEditable(status: string): boolean { return ['draft', 'review'].includes(status.toLowerCase()); }
export function mayApprove(record: QuoteRecord | null, dirty: boolean, busy: boolean, result: CalculationResult | null, resultCurrent: boolean): boolean {
  return !!record && isEditable(record.status) && !dirty && !busy && resultCurrent && !!result?.can_approve && !result.issues.some(i => i.severity === 'blocking') && record.files.every(file => record.plan.source_reviews.some(review => review.file_id === file.id && review.sha256 === file.sha256 && review.note.trim()));
}
export function scenarioPlan(plan: QuotePlan, quantity: string): QuotePlan {
  if (plan.roots.length !== 1) throw new Error('Quantity scenarios require exactly one root assembly.');
  if (!/^\d+(?:\.\d{1,9})?$/.test(quantity) || Number(quantity) <= 0) throw new Error('Enter a positive scenario quantity.');
  return { ...plan, roots: [{ ...plan.roots[0], quantity }] };
}
export function readableError(error: unknown): string {
  const e = error as { response?: { status?: number; data?: { detail?: unknown } }; message?: string };
  if (e.response?.status === 409) return 'This quote changed on the server. Your edits are still here. Reload the saved revision before saving or approving again.';
  const detail = e.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return e.message || 'The request could not be completed. Try again.';
}
export function money(value: unknown, currency: string): string {
  if (value === null || value === undefined || value === '') return 'Unresolved';
  const number = Number(value);
  if (!Number.isFinite(number)) return 'Unresolved';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(number); } catch { return `${currency} ${value}`; }
}
