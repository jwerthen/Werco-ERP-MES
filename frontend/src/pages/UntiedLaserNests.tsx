/**
 * Untied Laser Nests — the office worklist of laser nests with no sheet tied,
 * and the report that sizes the sheet that left stock without a deduction.
 *
 * A nest imported without a sheet part completes normally and deducts nothing,
 * by design. Until this page there was no screen listing such nests, so sheet
 * on-hand drifted high by every sheet an untied nest burned.
 *
 * What this page does, and what it deliberately does not:
 *
 * 1. **Everything it reads is a pure read** (`GET /laser-nests/untied`,
 *    `/untied/coverage`, `/untied/sheet-report`): no ledger row, no audit row,
 *    no event. The ONLY writes are two existing verbs a person clicks:
 *    "Tie this sheet" posts the matcher's part through the existing
 *    `POST /work-orders/{id}/material-allocations`, and "Start cycle count"
 *    posts the existing `POST /inventory/cycle-counts`. Neither is optimistic:
 *    each keeps a loading state, shows a refusal verbatim, and the list is
 *    re-read after a success.
 * 2. **No back-dated deduction.** A completed untied nest's sheet is corrected
 *    by counting the part, never by posting a deduction against the past.
 * 3. **Office only.** ADMIN / MANAGER / SUPERVISOR (`work_orders:edit`), the
 *    same trio the endpoints and the tie verb allow. The dispatch board and the
 *    kiosk show nothing for an untied nest, by recorded contract.
 * 4. **Grouping is approximate.** Nest sheet descriptors are free text from the
 *    nest report; the sheet report groups by the matcher's grade / thickness /
 *    size key and says so on screen.
 *
 * Filters, view and page live in the URL so they survive a reload.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ChartBarIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  LinkIcon,
  RectangleStackIcon,
  ScissorsIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { Button, DataTable, ErrorState, FormField, StatusBadge, Tabs, useToast } from '../components/ui';
import type { DataTableColumn, DataTableEmpty } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { TieNestSheetDialog, tieOptions } from '../components/laser/TieNestSheetDialog';
import { StartSheetCountDialog, countOptions } from '../components/laser/StartSheetCountDialog';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import { formatOperationLabel, hasOperationNumber } from '../utils/operationLabel';
import { apiErrorDetail } from '../utils/apiError';
import type { WorkCenter } from '../types';
import type {
  NestTieCoverageResponse,
  SheetSuggestionView,
  UntiedNestListResponse,
  UntiedNestRow,
  UntiedNestStatusFilter,
  UntiedSheetGroup,
  UntiedSheetReportResponse,
} from '../types/untiedNests';

const PAGE_SIZE = 50;
const COVERAGE_MONTH_CHOICES = [6, 12, 24];
const STATUS_CHOICES: Array<{ value: UntiedNestStatusFilter; label: string }> = [
  { value: 'all', label: 'Open and completed' },
  { value: 'open', label: 'Open only' },
  { value: 'completed', label: 'Completed only' },
];
type View = 'worklist' | 'sheets';

function positiveInt(value: string | null): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return parsed > 0 ? parsed : null;
}

function isoDay(value: string | null): string | null {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

function formatQty(value: number): string {
  return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

/** "Mar 3, 2026 – Jun 1, 2026" from the server's UTC window (end is exclusive). */
function windowLabel(from: string | null, to: string | null): string | null {
  if (!from) return null;
  const end = to ? new Date(Date.parse(to) - 1).toISOString() : null;
  return end ? `${formatCentralDate(from)} – ${formatCentralDate(end)}` : `since ${formatCentralDate(from)}`;
}

function monthLabel(month: string): string {
  const [year, mon] = month.split('-').map(Number);
  if (!year || !mon) return month;
  return new Date(Date.UTC(year, mon - 1, 15)).toLocaleString('en-US', {
    month: 'short',
    year: '2-digit',
    timeZone: 'UTC',
  });
}

function SuggestionCell({
  suggestion,
  matcherAvailable,
}: {
  suggestion: SheetSuggestionView | null;
  matcherAvailable: boolean;
}) {
  if (!suggestion) {
    return <span className="text-xs text-fd-mute">{matcherAvailable ? '—' : 'Matcher unavailable'}</span>;
  }
  if (suggestion.status === 'matched' && suggestion.candidate) {
    const candidate = suggestion.candidate;
    return (
      <span className="block min-w-0" title={candidate.reason}>
        <span className="font-mono text-sm text-fd-ink">{candidate.part_number}</span>
        <span className="block truncate text-xs text-fd-mute">
          {candidate.part_name}
          {candidate.on_hand_known && ` · ${formatQty(candidate.on_hand)} on hand`}
        </span>
      </span>
    );
  }
  if (suggestion.status === 'ambiguous') {
    return (
      <span className="block min-w-0" title={suggestion.diagnostic ?? undefined}>
        <span className="text-sm text-fd-amber">
          {suggestion.candidates.length} possible part{suggestion.candidates.length === 1 ? '' : 's'}
        </span>
        <span className="block truncate font-mono text-xs text-fd-mute">
          {suggestion.candidates.map(candidate => candidate.part_number).join(', ')}
        </span>
      </span>
    );
  }
  return (
    <span className="block text-xs text-fd-mute" title={suggestion.diagnostic ?? undefined}>
      No stock part matched{suggestion.diagnostic ? ` — ${suggestion.diagnostic}` : ''}
    </span>
  );
}

function CoveragePanel({
  coverage,
  loading,
  error,
  months,
  onMonthsChange,
  onRetry,
}: {
  coverage: NestTieCoverageResponse | null;
  loading: boolean;
  error: string | null;
  months: number;
  onMonthsChange: (months: number) => void;
  onRetry: () => void;
}) {
  return (
    <section className="rounded-sm border border-fd-line bg-fd-panel p-3" aria-labelledby="nest-tie-coverage-title">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="nest-tie-coverage-title" className="text-sm font-semibold uppercase tracking-wider text-fd-ink">
            Sheet tie coverage
          </h2>
          <p className="text-xs text-fd-mute">
            Completed nests per month (company time zone), and the share that had a sheet tied when counted.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="nest-coverage-months" className="text-xs text-fd-mute">
            Months
          </label>
          <select
            id="nest-coverage-months"
            className="input py-1 text-sm"
            value={months}
            onChange={e => onMonthsChange(Number(e.target.value))}
          >
            {COVERAGE_MONTH_CHOICES.map(choice => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        </div>
      </div>
      {error ? (
        <ErrorState title="Couldn't load tie coverage" message={error} onRetry={onRetry} />
      ) : loading && !coverage ? (
        <p className="text-sm text-fd-mute">Loading coverage…</p>
      ) : coverage ? (
        <div className="flex flex-col gap-3 lg:flex-row">
          <div className="shrink-0 lg:w-48" data-testid="nest-coverage-total">
            <p className="font-mono text-3xl tabular-nums text-fd-ink">
              {coverage.tied_percent == null ? '—' : `${coverage.tied_percent}%`}
            </p>
            <p className="text-xs text-fd-mute">
              {coverage.tied_nests.toLocaleString()} of {coverage.completed_nests.toLocaleString()} completed nests tied
            </p>
          </div>
          <ol className="grid min-w-0 flex-1 grid-cols-3 gap-1.5 sm:grid-cols-6" aria-label="Tie coverage by month">
            {coverage.months.map(month => {
              const pct = month.tied_percent;
              return (
                <li
                  key={month.month}
                  className="rounded-sm border border-fd-line px-2 py-1.5"
                  data-testid={`nest-coverage-${month.month}`}
                >
                  <p className="font-mono text-[11px] uppercase text-fd-mute">{monthLabel(month.month)}</p>
                  <p
                    className={`font-mono text-base tabular-nums ${
                      pct == null
                        ? 'text-fd-mute'
                        : pct >= 90
                          ? 'text-fd-green'
                          : pct >= 50
                            ? 'text-fd-amber'
                            : 'text-fd-red'
                    }`}
                  >
                    {pct == null ? '—' : `${pct}%`}
                  </p>
                  <div className="mt-1 h-1 w-full bg-fd-line" aria-hidden="true">
                    <div className="h-1 bg-fd-blue" style={{ width: `${Math.max(0, Math.min(100, pct ?? 0))}%` }} />
                  </div>
                  <p className="mt-1 text-[11px] text-fd-mute">
                    {month.tied_nests}/{month.completed_nests} tied
                  </p>
                </li>
              );
            })}
          </ol>
        </div>
      ) : null}
    </section>
  );
}

export default function UntiedLaserNests() {
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  const view: View = searchParams.get('view') === 'sheets' ? 'sheets' : 'worklist';
  const statusParam = searchParams.get('status');
  const status: UntiedNestStatusFilter = statusParam === 'open' || statusParam === 'completed' ? statusParam : 'all';
  const workOrderId = positiveInt(searchParams.get('work_order_id'));
  const workCenterId = positiveInt(searchParams.get('work_center_id'));
  const completedFrom = isoDay(searchParams.get('from'));
  const completedTo = isoDay(searchParams.get('to'));
  const page = positiveInt(searchParams.get('page')) ?? 1;
  const monthsParam = positiveInt(searchParams.get('months'));
  const coverageMonths = monthsParam && COVERAGE_MONTH_CHOICES.includes(monthsParam) ? monthsParam : 6;

  const updateParams = useCallback(
    (changes: Record<string, string | null>, options: { resetPage?: boolean } = { resetPage: true }) => {
      setSearchParams(
        previous => {
          const next = new URLSearchParams(previous);
          Object.entries(changes).forEach(([key, value]) => {
            if (value == null || value === '') next.delete(key);
            else next.set(key, value);
          });
          if (options.resetPage !== false && !('page' in changes)) next.delete('page');
          return next;
        },
        { replace: true }
      );
    },
    [setSearchParams]
  );

  // --- Worklist ---------------------------------------------------------------
  const [list, setList] = useState<UntiedNestListResponse | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const listRequest = useRef(0);

  const loadList = useCallback(async () => {
    const requestId = ++listRequest.current;
    setListLoading(true);
    setListError(null);
    try {
      const value = await api.getUntiedLaserNests({
        status,
        work_order_id: workOrderId ?? undefined,
        work_center_id: workCenterId ?? undefined,
        completed_from: completedFrom ?? undefined,
        completed_to: completedTo ?? undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      if (requestId !== listRequest.current) return;
      setList(value);
    } catch (err) {
      if (requestId !== listRequest.current) return;
      setListError(apiErrorDetail(err, "Couldn't load the untied-nest worklist."));
    } finally {
      if (requestId === listRequest.current) setListLoading(false);
    }
  }, [status, workOrderId, workCenterId, completedFrom, completedTo, page]);

  // --- Coverage ---------------------------------------------------------------
  const [coverage, setCoverage] = useState<NestTieCoverageResponse | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const coverageRequest = useRef(0);

  const loadCoverage = useCallback(async () => {
    const requestId = ++coverageRequest.current;
    setCoverageLoading(true);
    setCoverageError(null);
    try {
      const value = await api.getLaserNestTieCoverage(coverageMonths);
      if (requestId !== coverageRequest.current) return;
      setCoverage(value);
    } catch (err) {
      if (requestId !== coverageRequest.current) return;
      setCoverageError(apiErrorDetail(err, 'The coverage figures could not be read.'));
    } finally {
      if (requestId === coverageRequest.current) setCoverageLoading(false);
    }
  }, [coverageMonths]);

  // --- Sheet report -----------------------------------------------------------
  const [report, setReport] = useState<UntiedSheetReportResponse | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const reportRequest = useRef(0);

  const loadReport = useCallback(async () => {
    const requestId = ++reportRequest.current;
    setReportLoading(true);
    setReportError(null);
    try {
      const value = await api.getUntiedSheetReport({
        completed_from: completedFrom ?? undefined,
        completed_to: completedTo ?? undefined,
      });
      if (requestId !== reportRequest.current) return;
      setReport(value);
    } catch (err) {
      if (requestId !== reportRequest.current) return;
      setReportError(apiErrorDetail(err, "Couldn't load the untied-sheet report."));
    } finally {
      if (requestId === reportRequest.current) setReportLoading(false);
    }
  }, [completedFrom, completedTo]);

  useEffect(() => {
    if (view === 'worklist') void loadList();
  }, [view, loadList]);

  useEffect(() => {
    void loadCoverage();
  }, [loadCoverage]);

  useEffect(() => {
    if (view === 'sheets') void loadReport();
  }, [view, loadReport]);

  // --- Work centers (filter options; optional) --------------------------------
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  useEffect(() => {
    let cancelled = false;
    api
      .getWorkCenters()
      .then(value => {
        if (!cancelled && Array.isArray(value)) setWorkCenters(value);
      })
      .catch(() => {
        // The filter is a convenience; without it the worklist still loads.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Actions ----------------------------------------------------------------
  const [tieTarget, setTieTarget] = useState<UntiedNestRow | null>(null);
  const [countTarget, setCountTarget] = useState<UntiedSheetGroup | null>(null);

  const handleTied = (row: UntiedNestRow, partNumber: string) => {
    setTieTarget(null);
    showToast('success', `${partNumber} tied to ${row.cnc_number || row.nest_name} on ${row.work_order_number}.`);
    void loadList();
    void loadCoverage();
  };

  const handleCountStarted = (group: UntiedSheetGroup, countNumber: string | null) => {
    setCountTarget(null);
    showToast(
      'success',
      countNumber
        ? `Cycle count ${countNumber} scheduled for ${group.sheet_descriptor}. Record it in Inventory → Cycle Counts.`
        : `Cycle count scheduled for ${group.sheet_descriptor}.`
    );
    void loadReport();
  };

  // --- Worklist table ---------------------------------------------------------
  const items = useMemo(() => list?.items ?? [], [list]);
  const total = list?.total ?? 0;
  const hasNext = list ? list.offset + list.items.length < list.total : false;
  const matcherAvailable = list?.matcher_available ?? true;
  const hasFilters =
    status !== 'all' || workOrderId != null || workCenterId != null || !!completedFrom || !!completedTo;
  const workOrderLabel = useMemo(() => {
    if (workOrderId == null) return null;
    const row = items.find(item => item.work_order_id === workOrderId || item.parent_work_order_id === workOrderId);
    if (!row) return `Work order #${workOrderId}`;
    return row.work_order_id === workOrderId
      ? row.work_order_number
      : (row.parent_work_order_number ?? row.work_order_number);
  }, [items, workOrderId]);

  const columns: Array<DataTableColumn<UntiedNestRow>> = useMemo(
    () => [
      {
        key: 'job',
        header: 'Job',
        accessor: row => row.work_order_number,
        render: row => (
          <span className="block min-w-0">
            <Link to={`/work-orders/${row.work_order_id}`} className="font-mono text-sm text-fd-blue hover:underline">
              {row.work_order_number}
            </Link>
            {row.parent_work_order_id != null && row.parent_work_order_number && (
              <span className="block text-xs text-fd-mute">
                for{' '}
                <Link to={`/work-orders/${row.parent_work_order_id}`} className="hover:underline">
                  {row.parent_work_order_number}
                </Link>
              </span>
            )}
          </span>
        ),
      },
      {
        key: 'nest',
        header: 'Program',
        accessor: row => row.cnc_number || row.nest_name,
        render: row => (
          <span className="block min-w-0">
            <span className="font-mono text-sm text-fd-ink">{row.cnc_number || '—'}</span>
            <span className="block truncate text-xs text-fd-mute">
              {hasOperationNumber(row.operation_number) ? `${formatOperationLabel(row.operation_number)} · ` : ''}
              {row.nest_name}
              {row.work_center_name ? ` · ${row.work_center_name}` : ''}
            </span>
          </span>
        ),
      },
      {
        key: 'sheet',
        header: 'Sheet',
        accessor: row => row.sheet_descriptor,
        render: row => <span className="font-mono text-sm text-fd-body">{row.sheet_descriptor}</span>,
      },
      {
        key: 'runs',
        header: 'Runs',
        align: 'right',
        accessor: row => row.runs_complete + row.runs_scrapped,
        csv: row => `${row.runs_complete + row.runs_scrapped}/${row.planned_runs}`,
        render: row => (
          <span className="font-mono text-sm tabular-nums text-fd-body" title="Good + scrapped runs of planned">
            {formatQty(row.runs_complete + row.runs_scrapped)} / {formatQty(row.planned_runs)}
          </span>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        accessor: row => row.operation_status,
        csv: row =>
          row.completed_at
            ? `${row.operation_status} ${formatCentralDateTime(row.completed_at)}`
            : row.operation_status,
        render: row => (
          <span className="block">
            <StatusBadge status={row.operation_status} />
            {row.completed_at && (
              <span className="mt-0.5 block text-xs text-fd-mute">{formatCentralDateTime(row.completed_at)}</span>
            )}
          </span>
        ),
      },
      {
        key: 'suggestion',
        header: 'Matched sheet',
        accessor: row => row.suggestion?.candidate?.part_number ?? row.suggestion?.status ?? '',
        render: row => <SuggestionCell suggestion={row.suggestion} matcherAvailable={matcherAvailable} />,
      },
      {
        key: 'action',
        header: <span className="sr-only">Action</span>,
        align: 'right',
        csv: () => '',
        render: row => {
          if (!row.tieable) {
            return (
              <span className="block max-w-[14rem] text-xs text-fd-mute">
                {row.tie_blocked_reason ?? 'Job finished'}
              </span>
            );
          }
          const counted =
            row.proposed_tie != null &&
            (row.counted_since_completion_part_ids ?? []).includes(row.proposed_tie.part_id);
          if (row.proposed_tie) {
            return (
              <span className="inline-flex flex-col items-end gap-1">
                <Button size="sm" onClick={() => setTieTarget(row)}>
                  <LinkIcon className="mr-1 h-4 w-4" aria-hidden="true" />
                  Tie this sheet
                </Button>
                {counted && (
                  <span data-testid="untied-row-counted" className="max-w-[14rem] text-right text-[11px] text-fd-amber">
                    Counted since this nest completed — tying may deduct twice
                  </span>
                )}
              </span>
            );
          }
          if (tieOptions(row).length > 0) {
            return (
              <Button size="sm" variant="secondary" onClick={() => setTieTarget(row)}>
                Choose sheet
              </Button>
            );
          }
          return (
            <Link to={`/work-orders/${row.work_order_id}`} className="text-xs text-fd-blue hover:underline">
              Tie on the job
            </Link>
          );
        },
      },
    ],
    [matcherAvailable]
  );

  const worklistEmpty: DataTableEmpty = useMemo(() => {
    if (page > 1) {
      return {
        icon: RectangleStackIcon,
        title: 'Nothing on this page',
        description: 'The worklist has fewer rows than this page now — some nests were tied since.',
        action: { label: 'Back to page 1', onClick: () => updateParams({ page: null }) },
      };
    }
    if (hasFilters) {
      return {
        icon: RectangleStackIcon,
        title: 'No untied nests match these filters',
        description: 'Clear the filters to see every untied nest.',
      };
    }
    return {
      icon: RectangleStackIcon,
      title: 'Every nest has a sheet tied',
      description: 'No open nest, and no nest completed in the window, is missing its sheet tie.',
    };
  }, [page, hasFilters, updateParams]);

  // --- Sheet report table -----------------------------------------------------
  const groupColumns: Array<DataTableColumn<UntiedSheetGroup>> = useMemo(
    () => [
      {
        key: 'sheet',
        header: 'Sheet spec',
        accessor: group => group.sheet_descriptor,
        render: group => <span className="font-mono text-sm text-fd-ink">{group.sheet_descriptor}</span>,
      },
      {
        key: 'nests',
        header: 'Nests',
        align: 'right',
        sortable: true,
        accessor: group => group.nest_count,
      },
      {
        key: 'sheets',
        header: 'Sheets burned',
        align: 'right',
        sortable: true,
        accessor: group => group.sheets_burned,
        render: group => <span className="font-mono tabular-nums text-fd-amber">{formatQty(group.sheets_burned)}</span>,
      },
      {
        key: 'tieable',
        header: 'Still tieable',
        align: 'right',
        sortable: true,
        accessor: group => group.tieable_sheets_burned ?? 0,
        render: group =>
          (group.tieable_sheets_burned ?? 0) > 0 ? (
            <span
              className="font-mono tabular-nums text-fd-body"
              title="Sheets on jobs still open: tie these from the worklist rather than counting them"
            >
              {formatQty(group.tieable_sheets_burned ?? 0)}
            </span>
          ) : (
            <span className="text-fd-mute">—</span>
          ),
      },
      {
        key: 'jobs',
        header: 'Jobs',
        accessor: group => group.work_order_numbers.join(' '),
        render: group => (
          <span className="block max-w-[16rem] text-xs text-fd-body">
            <span className="font-mono">{group.work_order_numbers.join(', ')}</span>
            {group.work_order_count > group.work_order_numbers.length && (
              <span className="text-fd-mute"> +{group.work_order_count - group.work_order_numbers.length} more</span>
            )}
          </span>
        ),
      },
      {
        key: 'last',
        header: 'Last completed',
        sortable: true,
        accessor: group => group.last_completed_at ?? '',
        render: group => (
          <span className="text-xs text-fd-mute">
            {group.last_completed_at ? formatCentralDate(group.last_completed_at) : '—'}
          </span>
        ),
      },
      {
        key: 'part',
        header: 'Matched part',
        accessor: group => group.suggestion?.candidate?.part_number ?? group.suggestion?.status ?? '',
        render: group => (
          <SuggestionCell suggestion={group.suggestion} matcherAvailable={report?.matcher_available ?? true} />
        ),
      },
      {
        key: 'count',
        header: 'Cycle count',
        align: 'right',
        csv: group => group.open_cycle_counts.map(count => count.count_number).join(' '),
        render: group => {
          if (group.open_cycle_counts.length > 0) {
            return (
              <span className="flex flex-col items-end gap-0.5">
                {group.open_cycle_counts.map(count => (
                  <Link
                    key={count.id}
                    to={`/inventory?inventory_tab=counts&cycle_count=${count.id}`}
                    className="font-mono text-xs text-fd-blue hover:underline"
                  >
                    {count.count_number} · {count.status.replace('_', ' ')}
                  </Link>
                ))}
              </span>
            );
          }
          if (countOptions(group).length === 0) {
            return <span className="text-xs text-fd-mute">No part to count</span>;
          }
          return (
            <Button size="sm" variant="secondary" onClick={() => setCountTarget(group)}>
              Start cycle count
            </Button>
          );
        },
      },
    ],
    [report?.matcher_available]
  );

  const coverageWindow = windowLabel(
    view === 'sheets' ? (report?.completed_from ?? null) : (list?.completed_from ?? null),
    view === 'sheets' ? (report?.completed_to ?? null) : (list?.completed_to ?? null)
  );

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <ScissorsIcon className="h-7 w-7 flex-shrink-0 text-fd-blue" aria-hidden="true" />
          <div>
            <h1 className="text-2xl font-bold text-fd-ink">Untied Laser Nests</h1>
            <p className="max-w-3xl text-sm text-fd-mute">
              Laser nests with no sheet tied. An untied nest completes normally but deducts no sheet from stock, so
              sheet on-hand stays overstated by what it burns. Tie open nests from here; count the sheet that already
              left.
            </p>
          </div>
        </div>
      </div>

      <CoveragePanel
        coverage={coverage}
        loading={coverageLoading}
        error={coverageError}
        months={coverageMonths}
        onMonthsChange={months => updateParams({ months: months === 6 ? null : String(months) }, { resetPage: false })}
        onRetry={() => void loadCoverage()}
      />

      <Tabs
        tabs={[
          { id: 'worklist', label: 'Worklist', icon: RectangleStackIcon, panelId: 'untied-nests-worklist' },
          { id: 'sheets', label: 'Sheet report', icon: ChartBarIcon, panelId: 'untied-nests-sheets' },
        ]}
        activeTab={view}
        onChange={tab => updateParams({ view: tab === 'sheets' ? 'sheets' : null })}
      />

      {/* Filters shared by both views (the window); status / job / work center are the worklist's. */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-wrap items-end gap-3">
          {view === 'worklist' && (
            <FormField label="Show" className="min-w-[11rem]">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={status}
                  onChange={e => updateParams({ status: e.target.value === 'all' ? null : e.target.value })}
                >
                  {STATUS_CHOICES.map(choice => (
                    <option key={choice.value} value={choice.value}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          )}
          {view === 'worklist' && workCenters.length > 0 && (
            <FormField label="Work center" className="min-w-[11rem]">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={workCenterId ?? ''}
                  onChange={e => updateParams({ work_center_id: e.target.value || null })}
                >
                  <option value="">All work centers</option>
                  {workCenters.map(center => (
                    <option key={center.id} value={center.id}>
                      {center.name}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          )}
          <FormField label="Completed from" className="min-w-[10rem]">
            {field => (
              <input
                {...field}
                type="date"
                className="input"
                value={completedFrom ?? ''}
                onChange={e => updateParams({ from: e.target.value || null })}
              />
            )}
          </FormField>
          <FormField label="Completed to" className="min-w-[10rem]">
            {field => (
              <input
                {...field}
                type="date"
                className="input"
                value={completedTo ?? ''}
                onChange={e => updateParams({ to: e.target.value || null })}
              />
            )}
          </FormField>
          {view === 'worklist' && workOrderId != null && (
            <span className="inline-flex items-center gap-2 rounded-sm border border-fd-blue/50 bg-fd-blue/10 px-2 py-1 text-sm text-fd-body">
              Job <span className="font-mono">{workOrderLabel}</span>
              <button
                type="button"
                className="text-xs text-fd-blue hover:underline"
                onClick={() => updateParams({ work_order_id: null })}
              >
                Show every job
              </button>
            </span>
          )}
          {hasFilters && (
            <Button
              variant="secondary"
              onClick={() =>
                updateParams({ status: null, work_order_id: null, work_center_id: null, from: null, to: null })
              }
            >
              Clear filters
            </Button>
          )}
        </div>
        <p className="mt-2 text-[11px] text-fd-mute">
          Open nests are always listed. Completed nests are limited to the window
          {coverageWindow ? ` (${coverageWindow})` : ''}; with no dates set it is the last 90 days, or every completion
          when the list is filtered to one work order.
        </p>
      </div>

      {view === 'worklist' ? (
        <div
          id="untied-nests-worklist"
          role="tabpanel"
          aria-labelledby="untied-nests-worklist-tab"
          className="space-y-3"
        >
          {!matcherAvailable && (
            <div
              className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-amber"
              role="status"
            >
              The sheet matcher could not run for this page, so no sheet is proposed. Tie nests from their work order
              instead.
            </div>
          )}
          <MiniStatStrip className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <MiniStat
              icon={ExclamationTriangleIcon}
              iconBg={total > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
              iconColor={total > 0 ? 'text-fd-amber' : 'text-fd-green'}
              label="Untied nests"
              value={listLoading && !list ? '—' : total.toLocaleString()}
              valueColor={total > 0 ? 'text-fd-amber' : 'text-fd-green'}
              subtitle="Matching the current filters"
            />
            <MiniStat
              icon={LinkIcon}
              iconBg="bg-fd-blue/15"
              iconColor="text-fd-blue"
              label="One-click tie on this page"
              value={listLoading && !list ? '—' : items.filter(row => row.proposed_tie).length}
              subtitle={`Page ${page} · ${PAGE_SIZE} per page`}
            />
          </MiniStatStrip>
          <DataTable<UntiedNestRow>
            columns={columns}
            data={items}
            rowKey={row => row.work_order_operation_id}
            loading={listLoading}
            error={listError ?? false}
            onRetry={() => void loadList()}
            serverPagination={{
              loading: listLoading,
              page,
              pageSize: PAGE_SIZE,
              hasNext,
              onPageChange: next => updateParams({ page: next > 1 ? String(next) : null }, { resetPage: false }),
            }}
            csvExport={{ filename: 'untied-laser-nests' }}
            empty={worklistEmpty}
            dense
          />
        </div>
      ) : (
        <div id="untied-nests-sheets" role="tabpanel" aria-labelledby="untied-nests-sheets-tab" className="space-y-3">
          <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
            <div className="flex items-start gap-2.5">
              <InformationCircleIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-blue" aria-hidden="true" />
              <div className="text-sm text-fd-body">
                <p className="font-semibold text-fd-ink">Grouping is approximate.</p>
                <p className="mt-1">
                  Completed untied nests are grouped by grade, thickness and sheet size as read from free-text nest
                  reports, so one physical sheet can still appear twice. Sheets burned counts one sheet per good or
                  scrapped run. None of these sheets was deducted; to correct on-hand, count the part — nothing here
                  posts a back-dated deduction.
                </p>
              </div>
            </div>
          </div>
          {report?.truncated && (
            <div
              className="rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-amber"
              role="status"
              data-testid="untied-sheet-report-truncated"
            >
              More completed untied nests existed than the report reads at once, so these totals are a floor. Narrow the
              date window and run it again.
            </div>
          )}
          {reportError ? (
            <ErrorState
              title="Couldn't load the sheet report"
              message={reportError}
              onRetry={() => void loadReport()}
            />
          ) : (
            <>
              <MiniStatStrip className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <MiniStat
                  icon={ScissorsIcon}
                  iconBg="bg-fd-amber/15"
                  iconColor="text-fd-amber"
                  label="Sheets burned, not deducted"
                  value={
                    reportLoading && !report
                      ? '—'
                      : `${report?.truncated ? '≥ ' : ''}${formatQty(report?.sheets_burned ?? 0)}`
                  }
                  valueColor="text-fd-amber"
                  subtitle={coverageWindow ?? 'In the window'}
                />
                <MiniStat
                  icon={RectangleStackIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Completed untied nests"
                  value={reportLoading && !report ? '—' : (report?.nest_count ?? 0).toLocaleString()}
                />
                <MiniStat
                  icon={ChartBarIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Sheet specs"
                  value={reportLoading && !report ? '—' : (report?.groups.length ?? 0).toLocaleString()}
                />
              </MiniStatStrip>
              <DataTable<UntiedSheetGroup>
                columns={groupColumns}
                data={report?.groups ?? []}
                rowKey={group => group.spec_key}
                loading={reportLoading}
                csvExport={{ filename: 'untied-sheet-report' }}
                empty={{
                  icon: ChartBarIcon,
                  title: 'No completed untied nests in this window',
                  description: 'Every nest that completed in the window had a sheet tied.',
                }}
                dense
              />
            </>
          )}
        </div>
      )}

      <TieNestSheetDialog row={tieTarget} onClose={() => setTieTarget(null)} onTied={handleTied} />
      <StartSheetCountDialog group={countTarget} onClose={() => setCountTarget(null)} onStarted={handleCountStarted} />
    </div>
  );
}
