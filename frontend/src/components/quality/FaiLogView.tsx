/**
 * The FAI log of record — GET /quality/fai/log, one line per first article inspection,
 * newest first.
 *
 * The ERP is the company's FAI log of record; the old spreadsheet (form QF 7.5-3.4) is a
 * frozen archive. The first columns are that log's (FAI Report #, date, part #,
 * description, customer, job #, complete); the rest are what it never captured. Customer,
 * PO and job come from the linked work order (customer falls back to the part's), and a
 * line keeps naming a since-deleted part or work order — it is a historical record, so
 * nothing is hidden client-side.
 *
 * Server-paged (offset, no total: one row is over-fetched to learn whether another page
 * exists). Filters and the page live in the URL so a reload or a shared link lands on
 * the same view. The date filters are inclusive CENTRAL dates on the log date and are
 * sent as plain `YYYY-MM-DD` — the server compares them to the shop day, not to a UTC
 * instant. "Export log" (Admin / Manager, the server's EXPORT_ROLES) downloads the same
 * filtered log through the audited GET /exports/fai/export.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowDownTrayIcon, DocumentMagnifyingGlassIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { DataTable, LoadingButton, MobileDataCard, StatusBadge, useToast } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatCentralDate } from '../../utils/centralTime';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import type { FAILogFilters, FAILogRow, FAIStatus } from '../../types/quality';

export const FAI_LOG_PAGE_SIZE = 50;

const STATUS_OPTIONS: Array<{ value: '' | FAIStatus; label: string }> = [
  { value: '', label: 'All results' },
  { value: 'pending', label: 'Pending' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'passed', label: 'Passed' },
  { value: 'conditional', label: 'Conditional' },
  { value: 'failed', label: 'Failed' },
];

const PARAM = {
  search: 'log_q',
  customer: 'log_customer',
  status: 'log_status',
  from: 'log_from',
  to: 'log_to',
  page: 'log_page',
} as const;

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

async function exportErrorDetail(err: any): Promise<string> {
  const data = err?.response?.data;
  if (typeof data?.detail === 'string' && data.detail) return data.detail;
  // A blob request's error body arrives as a Blob, not parsed JSON.
  if (data && typeof data.text === 'function') {
    try {
      const parsed = JSON.parse(await data.text());
      if (typeof parsed?.detail === 'string' && parsed.detail) return parsed.detail;
    } catch {
      // fall through to the generic message
    }
  }
  return 'Could not export the FAI log.';
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

interface FaiLogViewProps {
  /** Open an FAI's detail (the log's row click-through). */
  onOpenFai: (faiId: number) => void;
  /** The server's EXPORT_ROLES (Admin / Manager). */
  canExport: boolean;
  /**
   * Bumped by the page after every FAI write it makes (create, a detail write, prefill):
   * the log re-reads the current page, so a disposition or a new FAI shows without a
   * filter change or a reload.
   */
  refreshKey?: number;
}

export default function FaiLogView({ onOpenFai, canExport, refreshKey = 0 }: FaiLogViewProps) {
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlSearch = searchParams.get(PARAM.search) || '';
  const urlCustomer = searchParams.get(PARAM.customer) || '';
  const rawStatus = searchParams.get(PARAM.status) || '';
  const status = STATUS_OPTIONS.some(option => option.value === rawStatus) ? (rawStatus as '' | FAIStatus) : '';
  const rawFrom = searchParams.get(PARAM.from) || '';
  const rawTo = searchParams.get(PARAM.to) || '';
  const startDate = DATE_PATTERN.test(rawFrom) ? rawFrom : '';
  const endDate = DATE_PATTERN.test(rawTo) ? rawTo : '';
  const pageParam = Number.parseInt(searchParams.get(PARAM.page) || '1', 10);
  const page = Number.isInteger(pageParam) && pageParam > 0 ? pageParam : 1;

  // Typed text is debounced into the URL so the log is not re-queried per keystroke.
  const [searchInput, setSearchInput] = useState(urlSearch);
  const [customerInput, setCustomerInput] = useState(urlCustomer);
  // URL -> input only when the URL says something the box does not (back/forward, a shared
  // link, Clear) — never re-writing what is being typed (a trailing space mid-phrase).
  useEffect(() => setSearchInput(current => (current.trim() === urlSearch ? current : urlSearch)), [urlSearch]);
  useEffect(() => setCustomerInput(current => (current.trim() === urlCustomer ? current : urlCustomer)), [urlCustomer]);
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const debouncedCustomer = useDebouncedValue(customerInput, 300);
  const pushed = useRef({ search: debouncedSearch, customer: debouncedCustomer });

  const updateParams = useCallback(
    (changes: Record<string, string>, resetPage = true) => {
      setSearchParams(
        current => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(changes)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          if (resetPage) next.delete(PARAM.page);
          return next;
        },
        { replace: true }
      );
    },
    [setSearchParams]
  );

  // Input -> URL when the DEBOUNCED text changes (not when the URL does: a Clear must not
  // be undone by a debounce still holding the old text). One update for both boxes, so
  // neither write is lost to the other's stale copy of the query string.
  useEffect(() => {
    const changes: Record<string, string> = {};
    if (debouncedSearch !== pushed.current.search) changes[PARAM.search] = debouncedSearch.trim();
    if (debouncedCustomer !== pushed.current.customer) changes[PARAM.customer] = debouncedCustomer.trim();
    pushed.current = { search: debouncedSearch, customer: debouncedCustomer };
    if (Object.keys(changes).length > 0) updateParams(changes);
  }, [debouncedSearch, debouncedCustomer, updateParams]);

  const filters = useMemo<FAILogFilters>(() => {
    const value: FAILogFilters = {};
    if (urlSearch) value.search = urlSearch;
    if (urlCustomer) value.customer = urlCustomer;
    if (status) value.status = status;
    if (startDate) value.start_date = startDate;
    if (endDate) value.end_date = endDate;
    return value;
  }, [urlSearch, urlCustomer, status, startDate, endDate]);

  const [rows, setRows] = useState<FAILogRow[]>([]);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setLoadError(false);
    api
      .getFAILog({ ...filters, limit: FAI_LOG_PAGE_SIZE + 1, offset: (page - 1) * FAI_LOG_PAGE_SIZE })
      .then(result => {
        if (!active) return;
        const list = Array.isArray(result) ? result : [];
        setHasNext(list.length > FAI_LOG_PAGE_SIZE);
        setRows(list.slice(0, FAI_LOG_PAGE_SIZE));
      })
      .catch(() => {
        if (!active) return;
        setRows([]);
        setHasNext(false);
        setLoadError(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [filters, page, attempt, refreshKey]);

  const handleExport = async () => {
    if (exporting || !canExport) return;
    setExporting(true);
    try {
      const { blob, filename } = await api.exportFAILog({ ...filters, format: 'xlsx' });
      saveDownload(blob, filename);
      showToast('success', 'FAI log exported');
    } catch (err) {
      showToast('error', await exportErrorDetail(err));
    } finally {
      setExporting(false);
    }
  };

  const hasFilters = !!(urlSearch || urlCustomer || status || startDate || endDate);

  const columns = useMemo<Array<DataTableColumn<FAILogRow>>>(
    () => [
      {
        key: 'fai_number',
        header: 'FAI Report #',
        className: 'font-medium whitespace-nowrap',
        accessor: row => row.fai_number,
      },
      {
        key: 'log_date',
        header: 'Date',
        accessor: row => row.log_date,
        csv: row => formatCentralDate(row.log_date),
        render: row => <span className="whitespace-nowrap">{formatCentralDate(row.log_date)}</span>,
      },
      {
        key: 'part_number',
        header: 'Part #',
        accessor: row => row.part_number ?? '',
        csv: row => `${row.part_number ?? ''}${row.part_revision ? ` Rev ${row.part_revision}` : ''}`.trim(),
        render: row => (
          <span className="whitespace-nowrap">
            {row.part_number || '—'}
            {row.part_revision && <span className="ml-1 text-slate-500">Rev {row.part_revision}</span>}
          </span>
        ),
      },
      {
        key: 'part_name',
        header: 'Description',
        className: 'max-w-[16rem] truncate',
        accessor: row => row.part_name ?? '',
        render: row => row.part_name || '—',
      },
      {
        key: 'customer',
        header: 'Customer',
        accessor: row => row.customer ?? '',
        render: row => row.customer || '—',
      },
      {
        key: 'job_number',
        header: 'Job #',
        accessor: row => row.job_number ?? '',
        render: row => <span className="font-mono">{row.job_number || '—'}</span>,
      },
      {
        key: 'customer_po',
        header: 'PO',
        accessor: row => row.customer_po ?? '',
        render: row => row.customer_po || '—',
      },
      {
        key: 'status',
        header: 'Result',
        accessor: row => row.status,
        csv: row => row.status.replace(/_/g, ' '),
        render: row => <StatusBadge status={row.status} />,
      },
      {
        key: 'is_complete',
        header: 'Complete',
        align: 'center',
        headerClassName: 'text-center',
        accessor: row => (row.is_complete ? 'X' : ''),
        render: row => (row.is_complete ? <span className="font-mono font-bold">X</span> : ''),
      },
      {
        key: 'customer_approval',
        header: 'Customer approval',
        accessor: row => (row.customer_approved ? (row.customer_approval_date ?? 'yes') : ''),
        csv: row =>
          row.customer_approved
            ? `Approved ${row.customer_approval_date ? formatCentralDate(row.customer_approval_date) : ''}`.trim()
            : row.customer_approval_required
              ? 'Required'
              : '',
        render: row =>
          row.customer_approved ? (
            <span className="whitespace-nowrap text-fd-green">
              Approved{row.customer_approval_date ? ` ${formatCentralDate(row.customer_approval_date)}` : ''}
            </span>
          ) : row.customer_approval_required ? (
            <span className="text-fd-amber">Required</span>
          ) : (
            <span className="text-slate-500">—</span>
          ),
      },
    ],
    []
  );

  return (
    <div className="space-y-3" data-testid="fai-log-view">
      <p className="text-sm text-slate-400">
        The FAI log of record. It replaces the QF 7.5-3.4 spreadsheet, which is kept as a frozen archive.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[12rem] flex-1">
          <label htmlFor="fai-log-search" className="label">
            Search
          </label>
          <input
            id="fai-log-search"
            className="input"
            placeholder="FAI #, part, description, job #, PO or serial…"
            maxLength={255}
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
          />
        </div>
        <div className="min-w-[10rem]">
          <label htmlFor="fai-log-customer" className="label">
            Customer
          </label>
          <input
            id="fai-log-customer"
            className="input"
            maxLength={255}
            value={customerInput}
            onChange={event => setCustomerInput(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor="fai-log-status" className="label">
            Result
          </label>
          <select
            id="fai-log-status"
            className="input"
            value={status}
            onChange={event => updateParams({ [PARAM.status]: event.target.value })}
          >
            {STATUS_OPTIONS.map(option => (
              <option key={option.value || 'all'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="fai-log-from" className="label">
            From
          </label>
          <input
            id="fai-log-from"
            type="date"
            className="input"
            value={startDate}
            onChange={event => updateParams({ [PARAM.from]: event.target.value })}
          />
        </div>
        <div>
          <label htmlFor="fai-log-to" className="label">
            To
          </label>
          <input
            id="fai-log-to"
            type="date"
            className="input"
            value={endDate}
            onChange={event => updateParams({ [PARAM.to]: event.target.value })}
          />
        </div>
        {hasFilters && (
          <button
            type="button"
            className="pb-2 text-sm text-fd-link underline"
            onClick={() => {
              setSearchInput('');
              setCustomerInput('');
              updateParams({
                [PARAM.search]: '',
                [PARAM.customer]: '',
                [PARAM.status]: '',
                [PARAM.from]: '',
                [PARAM.to]: '',
              });
            }}
          >
            Clear filters
          </button>
        )}
        {canExport && (
          <LoadingButton
            variant="secondary"
            loading={exporting}
            loadingText="Exporting…"
            onClick={() => void handleExport()}
            className="ml-auto flex items-center gap-1.5"
          >
            <ArrowDownTrayIcon className="h-4 w-4" aria-hidden="true" />
            Export log
          </LoadingButton>
        )}
      </div>
      <DataTable<FAILogRow>
        columns={columns}
        data={rows}
        rowKey={row => row.fai_id}
        onRowClick={row => onOpenFai(row.fai_id)}
        loading={loading && rows.length === 0}
        error={loadError ? 'Could not load the FAI log.' : false}
        onRetry={() => setAttempt(value => value + 1)}
        manualSorting
        serverPagination={{
          page,
          pageSize: FAI_LOG_PAGE_SIZE,
          hasNext,
          loading,
          onPageChange: next => updateParams({ [PARAM.page]: next > 1 ? String(next) : '' }, false),
        }}
        empty={{
          icon: DocumentMagnifyingGlassIcon,
          title: hasFilters ? 'No FAIs match these filters' : 'The FAI log is empty',
          description: hasFilters
            ? 'Clear or widen the filters to see more of the log.'
            : 'First article inspections appear here as they are created.',
        }}
        mobileCards={row => (
          <MobileDataCard
            title={row.fai_number}
            subtitle={`${row.part_number ?? ''}${row.part_revision ? ` Rev ${row.part_revision}` : ''}${
              row.part_name ? ` · ${row.part_name}` : ''
            }`}
            badge={<StatusBadge status={row.status} />}
            onClick={() => onOpenFai(row.fai_id)}
            fields={[
              { label: 'Date', value: formatCentralDate(row.log_date) },
              { label: 'Customer', value: row.customer || '—' },
              { label: 'Job #', value: row.job_number || '—' },
              { label: 'PO', value: row.customer_po || '—' },
              { label: 'Complete', value: row.is_complete ? 'X' : '—' },
            ]}
          />
        )}
      />
    </div>
  );
}
