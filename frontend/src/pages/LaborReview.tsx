/**
 * Labor Review — see time entries by production day, approve them, and close a forgotten clock.
 *
 * Reads `GET /shop-floor/time-entries` (Admin / Manager / Supervisor / Quality; server-paged,
 * pure read). Filters live in URL params so a filtered view survives a reload and can be linked.
 * Row actions call the existing approve / unapprove verbs and the set-clock-out verb; all three
 * are server-gated (own entry, still clocked in, approved entry, stale version), so they are
 * NON-optimistic — a button shows its in-flight state and the list is re-read after the server
 * answers. Which buttons a row offers comes from the server's `can_*` fields, never a client
 * re-derivation.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowDownTrayIcon, ClockIcon, FunnelIcon, XMarkIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { DataTable, DataTableColumn, LoadingButton, StatusBadge, buildCsv, useToast } from '../components/ui';
import { SetClockOutDialog, describeHoursResult } from '../components/laborReview/SetClockOutDialog';
import type { SetClockOutTarget } from '../components/laborReview/SetClockOutDialog';
import { useAuth } from '../context/AuthContext';
import { usePermissions } from '../hooks/usePermissions';
import { canAccessPath } from '../utils/routeAccess';
import { apiErrorDetail } from '../utils/apiError';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import { formatOperationLabel, hasOperationNumber } from '../utils/operationLabel';
import type { WorkCenter } from '../types';
import {
  LABOR_REVIEW_FLAGS,
  type LaborReviewEntry,
  type LaborReviewFlag,
  type LaborReviewListParams,
  type LaborReviewListResponse,
  type SetClockOutResponse,
} from '../types/laborReview';

export const LABOR_REVIEW_PAGE_SIZE = 50;
/** Page size for the CSV export's walk over every page of the filtered result. */
const EXPORT_PAGE_SIZE = 200;
/** Stop the export after this many pages (10,000 rows) rather than walk forever. */
const EXPORT_MAX_PAGES = 50;

export const FLAG_LABELS: Record<LaborReviewFlag, string> = {
  open_past_shift_end: 'Open past shift end',
  long_entry: 'Long entry',
  overlap: 'Overlap',
  open_on_closed_work: 'Open on closed work',
  long_zero_pieces: 'Long, zero pieces',
  backfill: 'Back-entered',
};

function flagDescription(flag: LaborReviewFlag, data: LaborReviewListResponse | null): string {
  const longHours = data?.long_entry_hours ?? 12;
  const zeroHours = data?.long_zero_pieces_hours ?? 4;
  switch (flag) {
    case 'open_past_shift_end':
      return 'Still clocked in after the shift it started in ended.';
    case 'long_entry':
      return `Longer than ${longHours} hours.`;
    case 'overlap':
      return 'The same person has another labor entry at the same time.';
    case 'open_on_closed_work':
      return 'Still clocked in on a completed operation or a finished work order.';
    case 'long_zero_pieces':
      return `A run or rework entry of ${zeroHours} hours or more with no pieces and no scrap.`;
    case 'backfill':
      return 'A paper back-entry or an import.';
    default:
      return '';
  }
}

const FLAG_CHIP_CLASS: Record<LaborReviewFlag, string> = {
  open_past_shift_end: 'border-fd-red/50 bg-fd-red/10 text-red-200',
  open_on_closed_work: 'border-fd-red/50 bg-fd-red/10 text-red-200',
  long_entry: 'border-fd-amber/50 bg-fd-amber/10 text-amber-200',
  overlap: 'border-fd-amber/50 bg-fd-amber/10 text-amber-200',
  long_zero_pieces: 'border-fd-amber/50 bg-fd-amber/10 text-amber-200',
  backfill: 'border-fd-line bg-slate-800/60 text-slate-300',
};

const ENTRY_TYPE_LABELS: Record<string, string> = {
  setup: 'Setup',
  run: 'Run',
  rework: 'Rework',
  inspection: 'Inspection',
  downtime: 'Downtime',
  break: 'Break',
};

const entryTypeLabel = (value: string | null) => (value ? (ENTRY_TYPE_LABELS[value] ?? value) : '—');

const isFlag = (value: string | null): value is LaborReviewFlag =>
  !!value && (LABOR_REVIEW_FLAGS as readonly string[]).includes(value);

const positiveInt = (value: string | null): number | undefined => {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
};

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const isoDateOrUndefined = (value: string | null) => (value && ISO_DATE.test(value) ? value : undefined);

/** Read the list query from the URL. Unknown or malformed values are ignored, never sent. */
export function paramsFromSearch(search: URLSearchParams): LaborReviewListParams {
  const params: LaborReviewListParams = { page_size: LABOR_REVIEW_PAGE_SIZE };
  const start = isoDateOrUndefined(search.get('start_date'));
  const end = isoDateOrUndefined(search.get('end_date'));
  if (start) params.start_date = start;
  if (end) params.end_date = end;
  const status = search.get('status');
  if (status === 'open' || status === 'closed') params.status = status;
  const approved = search.get('approved');
  if (approved === 'true') params.approved = true;
  if (approved === 'false') params.approved = false;
  const flag = search.get('flag');
  if (isFlag(flag)) params.flag = flag;
  if (search.get('flagged_only') === 'true') params.flagged_only = true;
  const userId = positiveInt(search.get('user_id'));
  if (userId) params.user_id = userId;
  const workCenterId = positiveInt(search.get('work_center_id'));
  if (workCenterId) params.work_center_id = workCenterId;
  const workOrderId = positiveInt(search.get('work_order_id'));
  if (workOrderId) params.work_order_id = workOrderId;
  params.page = positiveInt(search.get('page')) ?? 1;
  return params;
}

const hoursText = (entry: LaborReviewEntry) => {
  const hours = entry.duration_hours ?? entry.elapsed_hours;
  return Number.isFinite(hours) ? hours.toFixed(2) : '—';
};

const jobLabel = (entry: LaborReviewEntry) =>
  [
    entry.work_order_number,
    hasOperationNumber(entry.operation_number) ? formatOperationLabel(entry.operation_number) : null,
    entry.operation_name,
  ]
    .filter(Boolean)
    .join(' · ');

const employeeLabel = (entry: LaborReviewEntry) => entry.employee_name || `User #${entry.user_id}`;

export function toClockOutTarget(entry: LaborReviewEntry): SetClockOutTarget {
  return {
    id: entry.id,
    version: entry.version,
    employeeName: employeeLabel(entry),
    clockIn: entry.clock_in,
    clockOut: entry.clock_out,
    shiftEnd: entry.shift_end,
    jobLabel: jobLabel(entry) || null,
  };
}

function FlagChips({ flags, data }: { flags: LaborReviewFlag[]; data: LaborReviewListResponse | null }) {
  if (flags.length === 0) return <span className="text-slate-500">—</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {flags.map(flag => (
        <span
          key={flag}
          title={flagDescription(flag, data)}
          className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 text-[11px] font-medium ${FLAG_CHIP_CLASS[flag]}`}
        >
          {FLAG_LABELS[flag]}
        </span>
      ))}
    </div>
  );
}

export default function LaborReview() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  const { can } = usePermissions();
  const { showToast } = useToast();

  const query = useMemo(() => paramsFromSearch(searchParams), [searchParams]);
  const queryKey = JSON.stringify(query);

  const [data, setData] = useState<LaborReviewListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [pendingAction, setPendingAction] = useState<{ id: number; kind: 'approve' | 'unapprove' } | null>(null);
  const [clockTarget, setClockTarget] = useState<SetClockOutTarget | null>(null);
  const [exporting, setExporting] = useState(false);
  const requestSeq = useRef(0);

  const load = useCallback(async (params: LaborReviewListParams) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setLoadError(null);
    try {
      const response = await api.listLaborReviewEntries(params);
      if (seq !== requestSeq.current) return;
      setData(response);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setLoadError(apiErrorDetail(err, 'Could not load time entries.'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(JSON.parse(queryKey) as LaborReviewListParams);
  }, [load, queryKey]);

  useEffect(() => {
    let cancelled = false;
    api
      .getWorkCenters(false)
      .then(list => {
        if (!cancelled) setWorkCenters(list);
      })
      .catch(() => {
        // The work-center filter is a convenience; the list still works without it.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const reload = useCallback(() => load(query), [load, query]);

  /** Change URL params; any filter change returns to page 1 unless the page itself is the change. */
  const updateParams = useCallback(
    (changes: Record<string, string | null>) => {
      setSearchParams(
        prev => {
          const next = new URLSearchParams(prev);
          Object.entries(changes).forEach(([key, value]) => {
            if (value === null || value === '') next.delete(key);
            else next.set(key, value);
          });
          if (!('page' in changes)) next.delete('page');
          return next;
        },
        { replace: true }
      );
    },
    [setSearchParams]
  );

  const runApproval = useCallback(
    async (entry: LaborReviewEntry, kind: 'approve' | 'unapprove') => {
      setPendingAction({ id: entry.id, kind });
      try {
        // The row's version: the server refuses 409 if the entry (its hours) changed since
        // this list was read, so an approval never lands on hours the approver did not see.
        if (kind === 'approve') await api.approveTimeEntry(entry.id, entry.version);
        else await api.unapproveTimeEntry(entry.id, entry.version);
        showToast(
          'success',
          kind === 'approve'
            ? `Approved ${employeeLabel(entry)}'s time entry.`
            : `Cleared the approval on ${employeeLabel(entry)}'s time entry.`
        );
        await reload();
      } catch (err) {
        showToast(
          'error',
          apiErrorDetail(err, kind === 'approve' ? 'Could not approve.' : 'Could not clear the approval.')
        );
        // A 409 (stale version, still clocked in) means the row on screen is out of date.
        await reload();
      } finally {
        setPendingAction(null);
      }
    },
    [reload, showToast]
  );

  const handleClockSaved = useCallback(
    (response: SetClockOutResponse) => {
      setClockTarget(null);
      if (!response.changed) {
        showToast('info', 'That is already the clock-out on this entry. Nothing was changed.');
      } else {
        const hoursNote = describeHoursResult(response.hours);
        const base = `Clock-out set for ${employeeLabel(response.entry)}.`;
        if (hoursNote?.warn) showToast('warning', `${base} ${hoursNote.text}`);
        else showToast('success', hoursNote ? `${base} ${hoursNote.text}` : base);
      }
      void reload();
    },
    [reload, showToast]
  );

  const canOpenWorkOrder = useCallback((id: number) => canAccessPath(`/work-orders/${id}`, can), [can]);

  const columns = useMemo<Array<DataTableColumn<LaborReviewEntry>>>(
    () => [
      {
        key: 'employee',
        header: 'Employee',
        sortable: false,
        accessor: entry => employeeLabel(entry),
        csv: entry => (entry.employee_id ? `${employeeLabel(entry)} (${entry.employee_id})` : employeeLabel(entry)),
        render: entry => (
          <div className="min-w-0">
            <div className="flex items-center gap-1">
              <span className="font-medium text-slate-200">{employeeLabel(entry)}</span>
              {query.user_id !== entry.user_id && (
                <button
                  type="button"
                  onClick={() => updateParams({ user_id: String(entry.user_id) })}
                  className="rounded-sm p-0.5 text-slate-500 hover:bg-slate-800 hover:text-slate-200"
                  aria-label={`Show only ${employeeLabel(entry)}'s entries`}
                  title="Show only this person's entries"
                >
                  <FunnelIcon className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            {entry.employee_id && <div className="font-mono text-xs text-slate-400">{entry.employee_id}</div>}
          </div>
        ),
      },
      {
        key: 'job',
        header: 'Job',
        sortable: false,
        accessor: entry => jobLabel(entry),
        csv: entry => jobLabel(entry),
        render: entry => (
          <div className="min-w-0">
            {entry.work_order_id && entry.work_order_number ? (
              canOpenWorkOrder(entry.work_order_id) ? (
                <Link
                  to={`/work-orders/${entry.work_order_id}`}
                  className="font-semibold text-fd-link hover:text-sky-200"
                >
                  {entry.work_order_number}
                </Link>
              ) : (
                <span className="font-semibold text-slate-300">{entry.work_order_number}</span>
              )
            ) : (
              <span className="text-slate-500">No work order</span>
            )}
            {(hasOperationNumber(entry.operation_number) || entry.operation_name) && (
              <div className="text-xs text-slate-400">
                {[
                  hasOperationNumber(entry.operation_number) ? formatOperationLabel(entry.operation_number) : null,
                  entry.operation_name,
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            )}
          </div>
        ),
      },
      {
        key: 'work_center',
        header: 'Work center',
        sortable: false,
        accessor: entry => entry.work_center_name ?? '',
        render: entry => entry.work_center_name || <span className="text-slate-500">—</span>,
      },
      {
        key: 'entry_type',
        header: 'Type',
        sortable: false,
        accessor: entry => entryTypeLabel(entry.entry_type),
      },
      {
        key: 'clock_in',
        header: 'Clock in',
        sortable: false,
        className: 'whitespace-nowrap',
        accessor: entry => entry.clock_in,
        csv: entry => formatCentralDateTime(entry.clock_in),
        render: entry => (
          <div>
            <div className="tabular-nums">{formatCentralDateTime(entry.clock_in)}</div>
            <div className="text-xs text-slate-400">
              {formatCentralDate(entry.production_day)}
              {entry.shift_name ? ` · ${entry.shift_name}` : ''}
            </div>
          </div>
        ),
      },
      {
        key: 'clock_out',
        header: 'Clock out',
        sortable: false,
        className: 'whitespace-nowrap',
        accessor: entry => entry.clock_out ?? '',
        csv: entry => (entry.clock_out ? formatCentralDateTime(entry.clock_out) : 'Clocked in'),
        render: entry =>
          entry.clock_out ? (
            <span className="tabular-nums">{formatCentralDateTime(entry.clock_out)}</span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-sm border border-fd-amber/50 bg-fd-amber/10 px-1.5 py-0.5 text-[11px] font-medium text-amber-200">
              <ClockIcon className="h-3 w-3" aria-hidden="true" />
              Clocked in
            </span>
          ),
      },
      {
        key: 'hours',
        header: 'Hours',
        align: 'right',
        sortable: false,
        accessor: entry => entry.duration_hours ?? entry.elapsed_hours,
        csv: entry => hoursText(entry),
        render: entry => (
          <span className="tabular-nums" title={entry.is_open ? 'Elapsed so far; the entry is still open' : undefined}>
            {hoursText(entry)}
            {entry.is_open && <span className="ml-1 text-[10px] text-slate-400">so far</span>}
          </span>
        ),
      },
      {
        key: 'pieces',
        header: 'Good / scrap',
        align: 'right',
        sortable: false,
        accessor: entry => `${entry.quantity_produced} / ${entry.quantity_scrapped}`,
        render: entry => (
          <span className="tabular-nums">
            {entry.quantity_produced}
            <span className="text-slate-500"> / </span>
            <span className={entry.quantity_scrapped > 0 ? 'text-red-300' : undefined}>{entry.quantity_scrapped}</span>
          </span>
        ),
      },
      {
        key: 'flags',
        header: 'Flags',
        sortable: false,
        accessor: entry => entry.flags.map(flag => FLAG_LABELS[flag] ?? flag).join('; '),
        render: entry => <FlagChips flags={entry.flags} data={data} />,
      },
      {
        key: 'approval',
        header: 'Approval',
        sortable: false,
        accessor: entry => (entry.approved ? 'Approved' : 'Unapproved'),
        csv: entry =>
          entry.approved
            ? `Approved ${formatCentralDateTime(entry.approved)}${entry.approved_by_name ? ` by ${entry.approved_by_name}` : ''}`
            : 'Unapproved',
        render: entry =>
          entry.approved ? (
            <div>
              <StatusBadge status="approved" />
              <div className="mt-0.5 text-[11px] text-slate-400">
                {entry.approved_by_name ? `${entry.approved_by_name} · ` : ''}
                {formatCentralDateTime(entry.approved)}
              </div>
            </div>
          ) : (
            <StatusBadge status="unapproved" />
          ),
      },
      {
        key: 'actions',
        header: <span className="sr-only">Actions</span>,
        sortable: false,
        render: entry => {
          const busy = pendingAction?.id === entry.id;
          const own = user?.id === entry.user_id;
          return (
            <div className="flex flex-wrap justify-end gap-1">
              {entry.can_approve && (
                <LoadingButton
                  size="sm"
                  variant="primary"
                  loading={busy && pendingAction?.kind === 'approve'}
                  disabled={busy}
                  onClick={() => void runApproval(entry, 'approve')}
                  aria-label={`Approve ${employeeLabel(entry)}'s entry`}
                >
                  Approve
                </LoadingButton>
              )}
              {entry.can_unapprove && (
                <LoadingButton
                  size="sm"
                  variant="secondary"
                  loading={busy && pendingAction?.kind === 'unapprove'}
                  disabled={busy}
                  onClick={() => void runApproval(entry, 'unapprove')}
                  aria-label={`Clear approval on ${employeeLabel(entry)}'s entry`}
                >
                  Unapprove
                </LoadingButton>
              )}
              {entry.can_set_clock_out && (
                <LoadingButton
                  size="sm"
                  variant={entry.is_open ? 'danger' : 'ghost'}
                  disabled={busy}
                  onClick={() => setClockTarget(toClockOutTarget(entry))}
                  aria-label={`${entry.is_open ? 'Close clock for' : 'Correct clock-out for'} ${employeeLabel(entry)}`}
                >
                  {entry.is_open ? 'Close clock' : 'Correct'}
                </LoadingButton>
              )}
              {own && <span className="text-[11px] text-slate-500">Your entry</span>}
              {!own && entry.is_open && !entry.can_approve && !entry.approved && !entry.can_set_clock_out && (
                <span className="text-[11px] text-slate-500">Still clocked in</span>
              )}
            </div>
          );
        },
      },
    ],
    [canOpenWorkOrder, data, pendingAction, query.user_id, runApproval, updateParams, user?.id]
  );

  const handleExport = useCallback(async () => {
    setExporting(true);
    try {
      const rows: LaborReviewEntry[] = [];
      let page = 1;
      let truncated = false;
      for (;;) {
        const response = await api.listLaborReviewEntries({ ...query, page, page_size: EXPORT_PAGE_SIZE });
        rows.push(...response.items);
        if (!response.has_next) break;
        if (page >= EXPORT_MAX_PAGES) {
          truncated = true;
          break;
        }
        page += 1;
      }
      const csv = buildCsv(
        [
          ...columns.filter(column => column.key !== 'actions'),
          {
            key: 'production_day',
            header: 'Production day',
            accessor: (entry: LaborReviewEntry) => entry.production_day,
          },
          { key: 'shift', header: 'Shift', accessor: (entry: LaborReviewEntry) => entry.shift_name ?? '' },
          { key: 'source', header: 'Source', accessor: (entry: LaborReviewEntry) => entry.source ?? '' },
          { key: 'entry_id', header: 'Time entry ID', accessor: (entry: LaborReviewEntry) => entry.id },
        ],
        rows
      );
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `labor-review-${data?.start_date ?? 'all'}-${data?.end_date ?? 'open'}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (truncated) {
        showToast('warning', `Exported the first ${rows.length} entries. Narrow the filters to export the rest.`);
      }
    } catch (err) {
      showToast('error', apiErrorDetail(err, 'Could not export time entries.'));
    } finally {
      setExporting(false);
    }
  }, [columns, data?.end_date, data?.start_date, query, showToast]);

  const workCenterName = (id: number | undefined) => workCenters.find(wc => wc.id === id)?.name;
  const filteredUserName =
    query.user_id != null ? data?.items.find(entry => entry.user_id === query.user_id)?.employee_name : undefined;
  const filteredWorkOrder =
    query.work_order_id != null
      ? data?.items.find(entry => entry.work_order_id === query.work_order_id)?.work_order_number
      : undefined;

  const rangeText = data
    ? data.start_date || data.end_date
      ? `${data.start_date ? formatCentralDate(data.start_date) : 'Any time'} – ${
          data.end_date ? formatCentralDate(data.end_date) : 'today'
        }`
      : 'Every open entry'
    : '';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Labor Review</h1>
          <p className="text-sm text-slate-400">
            Time entries by production day. Approve closed entries, and close a clock someone forgot.
            {rangeText && <span className="ml-1 text-slate-300">Showing {rangeText}.</span>}
          </p>
        </div>
      </div>

      {/* Flag summary — counts across every page of the filtered result; a chip filters to it. */}
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by review flag">
        {LABOR_REVIEW_FLAGS.map(flag => {
          const count = data?.flag_counts[flag] ?? 0;
          const active = query.flag === flag;
          return (
            <button
              key={flag}
              type="button"
              aria-pressed={active}
              title={flagDescription(flag, data)}
              onClick={() => updateParams({ flag: active ? null : flag })}
              className={`inline-flex items-center gap-2 rounded-sm border px-2 py-1 text-xs font-medium ${
                active ? 'border-fd-blue bg-fd-blue/20 text-white' : `${FLAG_CHIP_CLASS[flag]} hover:brightness-125`
              }`}
            >
              {FLAG_LABELS[flag]}
              <span className="tabular-nums text-slate-200">{count}</span>
            </button>
          );
        })}
      </div>

      {/* Filters */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="labor-start" className="label">
              From (production day)
            </label>
            <input
              id="labor-start"
              type="date"
              className="input"
              value={query.start_date ?? ''}
              onChange={event => updateParams({ start_date: event.target.value || null })}
            />
          </div>
          <div>
            <label htmlFor="labor-end" className="label">
              To
            </label>
            <input
              id="labor-end"
              type="date"
              className="input"
              value={query.end_date ?? ''}
              onChange={event => updateParams({ end_date: event.target.value || null })}
            />
          </div>
          <div>
            <label htmlFor="labor-status" className="label">
              Clock
            </label>
            <select
              id="labor-status"
              className="input"
              value={query.status ?? ''}
              onChange={event => updateParams({ status: event.target.value || null })}
            >
              <option value="">Open and closed</option>
              <option value="open">Still clocked in</option>
              <option value="closed">Closed</option>
            </select>
          </div>
          <div>
            <label htmlFor="labor-approved" className="label">
              Approval
            </label>
            <select
              id="labor-approved"
              className="input"
              value={query.approved === undefined ? '' : String(query.approved)}
              onChange={event => updateParams({ approved: event.target.value || null })}
            >
              <option value="">Any</option>
              <option value="false">Unapproved</option>
              <option value="true">Approved</option>
            </select>
          </div>
          <div>
            <label htmlFor="labor-work-center" className="label">
              Work center
            </label>
            <select
              id="labor-work-center"
              className="input"
              value={query.work_center_id ?? ''}
              onChange={event => updateParams({ work_center_id: event.target.value || null })}
            >
              <option value="">All work centers</option>
              {workCenters.map(wc => (
                <option key={wc.id} value={wc.id}>
                  {wc.name}
                </option>
              ))}
            </select>
          </div>
          <label htmlFor="labor-flagged-only" className="flex items-center gap-2 pb-2 text-sm text-slate-300">
            <input
              id="labor-flagged-only"
              type="checkbox"
              checked={query.flagged_only === true}
              onChange={event => updateParams({ flagged_only: event.target.checked ? 'true' : null })}
            />
            Flagged only
          </label>
        </div>
        {(query.user_id != null || query.work_order_id != null || query.work_center_id != null || query.flag) && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-slate-400">Filtered to:</span>
            {query.user_id != null && (
              <button
                type="button"
                onClick={() => updateParams({ user_id: null })}
                className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-800/60 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                aria-label={`Remove the employee filter${filteredUserName ? ` (${filteredUserName})` : ''}`}
              >
                {filteredUserName ?? `Employee #${query.user_id}`}
                <XMarkIcon className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
            {query.work_order_id != null && (
              <button
                type="button"
                onClick={() => updateParams({ work_order_id: null })}
                className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-800/60 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                aria-label="Remove the work order filter"
              >
                {filteredWorkOrder ?? `Work order #${query.work_order_id}`}
                <XMarkIcon className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
            {query.work_center_id != null && (
              <button
                type="button"
                onClick={() => updateParams({ work_center_id: null })}
                className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-800/60 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                aria-label="Remove the work center filter"
              >
                {workCenterName(query.work_center_id) ?? `Work center #${query.work_center_id}`}
                <XMarkIcon className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
            {query.flag && (
              <button
                type="button"
                onClick={() => updateParams({ flag: null })}
                className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-800/60 px-2 py-0.5 text-slate-200 hover:bg-slate-700"
                aria-label="Remove the flag filter"
              >
                {FLAG_LABELS[query.flag]}
                <XMarkIcon className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
          </div>
        )}
      </div>

      <DataTable<LaborReviewEntry>
        columns={columns}
        data={data?.items ?? []}
        rowKey={entry => entry.id}
        loading={loading && !data}
        error={loadError ?? false}
        onRetry={() => void reload()}
        dense
        serverPagination={{
          loading,
          page: query.page ?? 1,
          pageSize: LABOR_REVIEW_PAGE_SIZE,
          hasNext: data?.has_next ?? false,
          onPageChange: page => updateParams({ page: page > 1 ? String(page) : null }),
        }}
        toolbarStart={
          <div className="flex items-center gap-3">
            <span className="text-xs text-slate-400 tabular-nums">
              {data ? `${data.total} ${data.total === 1 ? 'entry' : 'entries'}` : ''}
            </span>
            <LoadingButton
              size="sm"
              variant="secondary"
              loading={exporting}
              loadingText="Exporting"
              disabled={!data || data.total === 0}
              onClick={() => void handleExport()}
            >
              <ArrowDownTrayIcon className="mr-1 h-4 w-4" aria-hidden="true" />
              Export CSV
            </LoadingButton>
          </div>
        }
        rowClassName={entry => (entry.flags.length > 0 ? 'bg-fd-amber/[0.03]' : '')}
        empty={{
          icon: ClockIcon,
          title: 'No time entries match',
          description: 'Try a wider date range or clear a filter.',
        }}
      />

      {clockTarget && (
        <SetClockOutDialog
          key={`${clockTarget.id}:${clockTarget.version}`}
          open
          target={clockTarget}
          onClose={() => setClockTarget(null)}
          onSaved={handleClockSaved}
          onRefused={() => void reload()}
        />
      )}
    </div>
  );
}
