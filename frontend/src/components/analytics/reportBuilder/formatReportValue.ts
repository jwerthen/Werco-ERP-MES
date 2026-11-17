import type { ReportFieldType } from '../../../types/reportBuilder';
import { formatCentralDate, formatCentralDateTime } from '../../../utils/centralTime';

/**
 * A report cell as text. Datetimes arrive as UTC `Z` instants and render in Central;
 * dates arrive as `YYYY-MM-DD` and render as that calendar day (never shifted by a zone).
 */
export function formatReportValue(value: unknown, type: ReportFieldType): string {
  if (value === null || value === undefined || value === '') return '—';
  if (type === 'datetime' && typeof value === 'string') return formatCentralDateTime(value);
  if (type === 'date' && typeof value === 'string') return formatCentralDate(value);
  if (typeof value === 'number') return value.toLocaleString('en-US', { maximumFractionDigits: 4 });
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** A primitive to sort a report cell by (null sorts first, as DataTable expects). */
export function reportSortValue(value: unknown): string | number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number' || typeof value === 'string') return value;
  return String(value);
}
