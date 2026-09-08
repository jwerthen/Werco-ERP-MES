import { WorkOrderBrowseParams } from '../types/workOrderBrowse';
import { isDateBeforeTodayInCentral, isDateTodayInCentral } from '../utils/centralTime';

/** Adapt the existing page interaction fixtures to the bounded wire response.
 * SQL filtering/tenancy are independently verified by backend API tests.
 */
export function workOrderBrowseFixture(rows: any[], params: WorkOrderBrowseParams = {}) {
  const customers = Array.from(new Set(rows.map(row => row.customer_name).filter(Boolean))).sort();
  const filtered = rows.filter(row => {
    if (params.hide_cots && ['purchased', 'hardware', 'raw_material'].includes(row.part_type)) return false;
    if (params.customer && row.customer_name !== params.customer) return false;
    if (params.scope && ['complete', 'closed', 'cancelled'].includes(row.status)) return false;
    if (params.scope === 'overdue' && (!row.due_date || !isDateBeforeTodayInCentral(row.due_date))) return false;
    if (params.scope === 'due_today' && (!row.due_date || !isDateTodayInCentral(row.due_date))) return false;
    return true;
  });
  const key =
    params.sort === 'part' ? 'part_number' : params.sort === 'customer' ? 'customer_name' : params.sort || 'priority';
  filtered.sort(
    (a, b) =>
      String(a[key] ?? '').localeCompare(String(b[key] ?? ''), undefined, { numeric: true }) *
      (params.direction === 'desc' ? -1 : 1)
  );
  const skip = params.skip || 0,
    limit = params.limit || 50;
  return {
    items: filtered.slice(skip, skip + limit),
    total: filtered.length,
    skip,
    limit,
    has_next: skip + limit < filtered.length,
    customers,
    customers_truncated: false,
    group_totals: {},
    stats: {
      overdue: filtered.filter(
        row =>
          row.due_date &&
          isDateBeforeTodayInCentral(row.due_date) &&
          !['complete', 'closed', 'cancelled'].includes(row.status)
      ).length,
      due_today: filtered.filter(row => row.due_date && isDateTodayInCentral(row.due_date)).length,
      in_progress: filtered.filter(row => row.status === 'in_progress').length,
    },
  };
}
