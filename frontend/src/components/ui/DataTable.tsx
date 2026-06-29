/**
 * DataTable<T>
 *
 * Generic, instrument-panel-styled data table that standardizes the shared table
 * behaviors across the app: client-side sort, client- or server-side pagination,
 * row click-through, row selection + bulk actions, CSV export, sticky header,
 * responsive mobile cards, and the Batch-3 loading / error / empty states routed
 * through the shared <Skeleton> / <ErrorState> / <EmptyState> primitives.
 *
 * Styling matches the existing `.table` / `.table-container` chrome (sharp
 * corners, hairline `--fd-line` borders, mono uppercase headers, dense rows).
 *
 * Sorting and pagination are pure: they never mutate the `data` prop.
 *
 * See WorkOrders.tsx for the reference migration / copy-paste usage.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  ArrowDownTrayIcon,
} from '@heroicons/react/24/outline';
import { Skeleton } from './Skeleton';
import { ErrorState } from './ErrorState';
import { EmptyState } from './EmptyState';
import { MobileDataList } from './MobileDataCard';

export type SortDir = 'asc' | 'desc';
export type ColumnAlign = 'left' | 'right' | 'center';

export interface DataTableColumn<T> {
  /** Stable key — used for sort state, aria, React keys. */
  key: string;
  /** Header cell content. */
  header: React.ReactNode;
  /** Cell renderer. Falls back to `accessor` (then '') when omitted. */
  render?: (row: T) => React.ReactNode;
  /** Primitive value for client-side sort + CSV when no `csv()` is given. */
  accessor?: (row: T) => string | number | null;
  /** Enable header click-to-sort (asc → desc → none). */
  sortable?: boolean;
  align?: ColumnAlign;
  /** Extra classes on the <td>. */
  className?: string;
  /** Extra classes on the <th>. */
  headerClassName?: string;
  /** Value for the CSV cell. Overrides `accessor`/`render` for export. */
  csv?: (row: T) => string | number;
}

export interface DataTableEmpty {
  icon?: React.ComponentType<{ className?: string }>;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
}

export interface DataTableSelection<K extends string | number = string | number> {
  selectedKeys: Set<K>;
  onChange: (keys: Set<K>) => void;
}

export interface DataTableServerPagination {
  page: number;
  pageSize: number;
  hasNext: boolean;
  onPageChange: (page: number) => void;
}

export interface DataTableProps<T> {
  columns: Array<DataTableColumn<T>>;
  data: T[];
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  loading?: boolean;
  error?: boolean | string;
  onRetry?: () => void;
  empty?: DataTableEmpty;
  defaultSort?: { key: string; dir: SortDir };
  /** Client-side page size. Ignored when `serverPagination` is set. */
  pageSize?: number;
  serverPagination?: DataTableServerPagination;
  selection?: DataTableSelection;
  bulkActions?: React.ReactNode;
  csvExport?: { filename: string };
  stickyHeader?: boolean;
  dense?: boolean;
  className?: string;
  /** Render a row as a mobile card below the md breakpoint instead of scrolling the table. */
  mobileCards?: (row: T) => React.ReactNode;
}

const alignClass: Record<ColumnAlign, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
};

function compareValues(a: string | number | null, b: string | number | null): number {
  if (a === b) return 0;
  if (a === null || a === undefined) return -1;
  if (b === null || b === undefined) return 1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/** Quote a CSV field per RFC 4180 when it contains a comma, quote, or newline. */
function escapeCsv(value: string | number): string {
  const s = String(value ?? '');
  if (/[",\n\r]/.test(s)) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function csvCellValue<T>(col: DataTableColumn<T>, row: T): string | number {
  if (col.csv) return col.csv(row);
  if (col.accessor) {
    const v = col.accessor(row);
    return v === null || v === undefined ? '' : v;
  }
  return '';
}

export function buildCsv<T>(columns: Array<DataTableColumn<T>>, rows: T[]): string {
  const exportable = columns.filter((c) => c.csv || c.accessor);
  const headerLine = exportable
    .map((c) => escapeCsv(typeof c.header === 'string' ? c.header : c.key))
    .join(',');
  const dataLines = rows.map((row) =>
    exportable.map((c) => escapeCsv(csvCellValue(c, row))).join(',')
  );
  return [headerLine, ...dataLines].join('\n');
}

export function DataTable<T>({
  columns,
  data,
  rowKey,
  onRowClick,
  loading = false,
  error = false,
  onRetry,
  empty,
  defaultSort,
  pageSize,
  serverPagination,
  selection,
  bulkActions,
  csvExport,
  stickyHeader = false,
  dense = false,
  className = '',
  mobileCards,
}: DataTableProps<T>) {
  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(
    defaultSort ?? null
  );
  const [clientPage, setClientPage] = useState(1);

  const columnByKey = useMemo(() => {
    const map = new Map<string, DataTableColumn<T>>();
    columns.forEach((c) => map.set(c.key, c));
    return map;
  }, [columns]);

  // ---- Sorting (client only; server pagination disables it) ----
  const sortedData = useMemo(() => {
    if (serverPagination || !sort) return data;
    const col = columnByKey.get(sort.key);
    if (!col?.accessor) return data;
    // Copy first — never mutate the data prop.
    const copy = [...data];
    copy.sort((a, b) => {
      const cmp = compareValues(col.accessor!(a), col.accessor!(b));
      return sort.dir === 'asc' ? cmp : -cmp;
    });
    return copy;
  }, [data, sort, serverPagination, columnByKey]);

  // ---- Pagination ----
  const usingClientPagination = !serverPagination && !!pageSize && pageSize > 0;
  const totalRows = sortedData.length;
  const totalPages = usingClientPagination
    ? Math.max(1, Math.ceil(totalRows / (pageSize as number)))
    : 1;
  const safeClientPage = Math.min(clientPage, totalPages);

  // Keep clientPage in range when the data set shrinks (e.g. a filter is applied),
  // so the next Prev/Next click computes from the clamped page, not a stale one.
  useEffect(() => {
    if (clientPage > totalPages) setClientPage(totalPages);
  }, [clientPage, totalPages]);

  const pagedData = useMemo(() => {
    if (!usingClientPagination) return sortedData;
    const start = (safeClientPage - 1) * (pageSize as number);
    return sortedData.slice(start, start + (pageSize as number));
  }, [usingClientPagination, sortedData, safeClientPage, pageSize]);

  const visibleRows = serverPagination ? data : pagedData;

  const handleSort = useCallback(
    (key: string) => {
      setClientPage(1);
      setSort((prev) => {
        if (!prev || prev.key !== key) return { key, dir: 'asc' };
        if (prev.dir === 'asc') return { key, dir: 'desc' };
        return null; // asc → desc → none
      });
    },
    []
  );

  // ---- Selection ----
  const selectableKeys = useMemo(
    () => visibleRows.map((r) => rowKey(r)),
    [visibleRows, rowKey]
  );
  const allVisibleSelected =
    !!selection &&
    selectableKeys.length > 0 &&
    selectableKeys.every((k) => selection.selectedKeys.has(k));
  const someVisibleSelected =
    !!selection && selectableKeys.some((k) => selection.selectedKeys.has(k));

  const toggleRow = useCallback(
    (key: string | number) => {
      if (!selection) return;
      const next = new Set(selection.selectedKeys);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      selection.onChange(next);
    },
    [selection]
  );

  const toggleAll = useCallback(() => {
    if (!selection) return;
    const next = new Set(selection.selectedKeys);
    if (allVisibleSelected) {
      selectableKeys.forEach((k) => next.delete(k));
    } else {
      selectableKeys.forEach((k) => next.add(k));
    }
    selection.onChange(next);
  }, [selection, allVisibleSelected, selectableKeys]);

  // ---- CSV export ----
  const handleExport = useCallback(() => {
    if (!csvExport) return;
    // Client mode: export the full sorted set (all pages). Server mode: only the
    // current page is loaded in memory, so export that (the button reads "Export page").
    const rows = serverPagination ? data : sortedData;
    const csv = buildCsv(columns, rows);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = csvExport.filename.endsWith('.csv')
      ? csvExport.filename
      : `${csvExport.filename}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }, [csvExport, serverPagination, data, sortedData, columns]);

  const cellPad = dense ? 'px-3 py-2' : 'px-4 py-3.5';
  const headPad = dense ? 'px-3 py-2.5' : 'px-4 py-3';

  // ---- Toolbar (export + bulk actions) ----
  const hasSelection = !!selection && selection.selectedKeys.size > 0;
  const showToolbar = !!csvExport || (hasSelection && !!bulkActions);

  const toolbar = showToolbar ? (
    <div className="flex items-center justify-between gap-3 mb-2">
      <div className="flex items-center gap-2 min-w-0">
        {hasSelection && bulkActions ? (
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-xs font-mono uppercase tracking-wider text-fd-mute tabular-nums whitespace-nowrap">
              {selection!.selectedKeys.size} selected
            </span>
            {bulkActions}
          </div>
        ) : null}
      </div>
      {csvExport && (
        <button
          type="button"
          onClick={handleExport}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-mono font-semibold uppercase tracking-wider text-slate-300 border border-fd-line rounded-sm hover:bg-slate-700/40 transition-colors duration-150 shrink-0"
        >
          <ArrowDownTrayIcon className="h-4 w-4" aria-hidden="true" />
          {serverPagination ? 'Export page' : 'Export CSV'}
        </button>
      )}
    </div>
  ) : null;

  // ---- Body content (error / loading / empty / rows) ----
  const isError = error !== false && error !== undefined && error !== '';
  const isEmpty = !loading && !isError && visibleRows.length === 0;

  // Error: replace the table body entirely.
  if (isError) {
    return (
      <div className={className}>
        <ErrorState
          message={typeof error === 'string' ? error : "Couldn't load this data."}
          onRetry={onRetry}
        />
      </div>
    );
  }

  const renderHeader = () => (
    <thead className={stickyHeader ? 'sticky top-0 z-10' : undefined}>
      <tr>
        {selection && (
          <th scope="col" className={`${headPad} w-10`}>
            <input
              type="checkbox"
              className="checkbox"
              aria-label="Select all rows"
              checked={allVisibleSelected}
              ref={(el) => {
                if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected;
              }}
              onChange={toggleAll}
            />
          </th>
        )}
        {columns.map((col) => {
          const isSorted = sort?.key === col.key;
          const ariaSort: React.AriaAttributes['aria-sort'] = isSorted
            ? sort!.dir === 'asc'
              ? 'ascending'
              : 'descending'
            : col.sortable
            ? 'none'
            : undefined;
          return (
            <th
              key={col.key}
              scope="col"
              aria-sort={ariaSort}
              className={`${headPad} font-mono uppercase ${alignClass[col.align ?? 'left']} ${
                col.headerClassName ?? ''
              }`}
              style={{ fontSize: '0.68rem', letterSpacing: '0.1em' }}
            >
              {col.sortable ? (
                <button
                  type="button"
                  onClick={() => handleSort(col.key)}
                  className={`inline-flex items-center gap-1 font-mono uppercase tracking-[0.1em] hover:text-slate-200 transition-colors ${
                    isSorted ? 'text-slate-200' : 'text-fd-mute'
                  } ${col.align === 'right' ? 'flex-row-reverse' : ''}`}
                >
                  <span>{col.header}</span>
                  <span aria-hidden="true" className="text-[0.6rem] w-2">
                    {isSorted ? (sort!.dir === 'asc' ? '▲' : '▼') : ''}
                  </span>
                </button>
              ) : (
                col.header
              )}
            </th>
          );
        })}
      </tr>
    </thead>
  );

  const renderLoadingRows = () => {
    const skeletonRows = pageSize && pageSize <= 25 ? pageSize : 8;
    return (
      <tbody>
        {Array.from({ length: skeletonRows }).map((_, r) => (
          <tr key={r} className="animate-pulse">
            {selection && (
              <td className={cellPad}>
                <Skeleton className="h-4 w-4" />
              </td>
            )}
            {columns.map((col) => (
              <td key={col.key} className={`${cellPad} ${alignClass[col.align ?? 'left']}`}>
                <Skeleton className="h-4 w-full max-w-[8rem]" />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    );
  };

  const renderBody = () => (
    <tbody>
      {visibleRows.map((row) => {
        const key = rowKey(row);
        const selected = !!selection && selection.selectedKeys.has(key);
        return (
          <tr
            key={key}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            className={`${onRowClick ? 'cursor-pointer' : ''} ${
              selected ? 'bg-werco-navy-600/10' : ''
            }`}
          >
            {selection && (
              <td className={cellPad} onClick={(e) => e.stopPropagation()}>
                <input
                  type="checkbox"
                  className="checkbox"
                  aria-label={`Select row ${key}`}
                  checked={selected}
                  onChange={() => toggleRow(key)}
                />
              </td>
            )}
            {columns.map((col) => (
              <td
                key={col.key}
                className={`${cellPad} text-sm ${alignClass[col.align ?? 'left']} ${
                  col.className ?? ''
                }`}
                style={{ borderBottom: '1px solid var(--fd-line)' }}
              >
                {col.render
                  ? col.render(row)
                  : col.accessor
                  ? col.accessor(row)
                  : null}
              </td>
            ))}
          </tr>
        );
      })}
    </tbody>
  );

  // ---- Pagination footer ----
  let footer: React.ReactNode = null;
  if (serverPagination) {
    const start = (serverPagination.page - 1) * serverPagination.pageSize + 1;
    const end = start + visibleRows.length - 1;
    footer = (
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-fd-line">
        <span className="text-xs text-fd-mute tabular-nums">
          {visibleRows.length === 0 ? '0 results' : `${start}–${end}`}
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            className="p-1.5 rounded-sm border border-fd-line text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700/40 transition-colors"
            disabled={serverPagination.page <= 1}
            onClick={() => serverPagination.onPageChange(serverPagination.page - 1)}
            aria-label="Previous page"
          >
            <ChevronLeftIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            className="p-1.5 rounded-sm border border-fd-line text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700/40 transition-colors"
            disabled={!serverPagination.hasNext}
            onClick={() => serverPagination.onPageChange(serverPagination.page + 1)}
            aria-label="Next page"
          >
            <ChevronRightIcon className="h-4 w-4" />
          </button>
        </div>
      </div>
    );
  } else if (usingClientPagination && totalRows > 0) {
    const start = (safeClientPage - 1) * (pageSize as number) + 1;
    const end = Math.min(safeClientPage * (pageSize as number), totalRows);
    footer = (
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-fd-line">
        <span className="text-xs text-fd-mute tabular-nums">
          {start}&ndash;{end} of {totalRows}
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            className="p-1.5 rounded-sm border border-fd-line text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700/40 transition-colors"
            disabled={safeClientPage <= 1}
            onClick={() => setClientPage((p) => Math.max(1, p - 1))}
            aria-label="Previous page"
          >
            <ChevronLeftIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            className="p-1.5 rounded-sm border border-fd-line text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-700/40 transition-colors"
            disabled={safeClientPage >= totalPages}
            onClick={() => setClientPage((p) => Math.min(totalPages, p + 1))}
            aria-label="Next page"
          >
            <ChevronRightIcon className="h-4 w-4" />
          </button>
        </div>
      </div>
    );
  }

  // ---- Mobile cards (below md) ----
  const mobileView = mobileCards ? (
    <div className="md:hidden">
      {loading ? (
        <MobileDataList>
          {Array.from({ length: 3 }).map((_, i) => (
            <div
              key={i}
              className="bg-fd-panel rounded-sm border border-slate-700 p-4 animate-pulse"
            >
              <div className="h-5 bg-slate-700 rounded w-1/2 mb-3" />
              <div className="grid grid-cols-2 gap-3">
                <div className="h-4 bg-slate-700/50 rounded" />
                <div className="h-4 bg-slate-700/50 rounded" />
              </div>
            </div>
          ))}
        </MobileDataList>
      ) : isEmpty && empty ? (
        <EmptyState
          icon={empty.icon}
          title={empty.title}
          description={empty.description}
          action={empty.action}
        />
      ) : (
        <MobileDataList>
          {visibleRows.map((row) => (
            <React.Fragment key={rowKey(row)}>{mobileCards(row)}</React.Fragment>
          ))}
        </MobileDataList>
      )}
      {!isEmpty && footer}
    </div>
  ) : null;

  return (
    <div className={className}>
      {toolbar}

      {/* Desktop / default table view */}
      <div className={mobileCards ? 'hidden md:block' : ''}>
        {isEmpty && empty ? (
          <EmptyState
            icon={empty.icon}
            title={empty.title}
            description={empty.description}
            action={empty.action}
          />
        ) : (
          <div className="table-container border-fd-line">
            <div className={stickyHeader ? 'max-h-[70vh] overflow-y-auto' : 'overflow-x-auto'}>
              <table className="table w-full" data-testid="data-table">
                {renderHeader()}
                {loading ? renderLoadingRows() : renderBody()}
              </table>
            </div>
            {!loading && footer}
          </div>
        )}
      </div>

      {mobileView}
    </div>
  );
}

export default DataTable;
