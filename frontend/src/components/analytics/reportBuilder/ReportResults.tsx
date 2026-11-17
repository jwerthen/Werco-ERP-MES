import React, { useMemo } from 'react';
import { ExclamationTriangleIcon, InformationCircleIcon, TableCellsIcon } from '@heroicons/react/24/outline';
import { DataTable, EmptyState, ErrorState } from '../../ui';
import type { DataTableColumn } from '../../ui';
import type {
  CustomReportResult,
  ReportDataSourceCatalog,
  ReportDefinition,
  ReportRow,
} from '../../../types/reportBuilder';
import { ReportChart, chartSpec } from './ReportChart';
import { formatReportValue, reportSortValue } from './formatReportValue';

export const RESULT_PAGE_SIZE = 50;

export interface ReportRunOutcome {
  /** What ran: "Unsaved report" or a template's name. */
  title: string;
  definition: ReportDefinition;
  result: CustomReportResult;
}

export interface ReportRunError {
  title: string;
  message: string;
  /** Offer Retry: a network / server failure, not a refusal the same input would repeat. */
  retryable: boolean;
}

interface ReportResultsProps {
  outcome: ReportRunOutcome | null;
  error: ReportRunError | null;
  running: boolean;
  catalog: ReportDataSourceCatalog;
  onRetry: () => void;
}

interface IndexedRow {
  index: number;
  row: ReportRow;
}

/**
 * A run's output: the truncation and server notes, a chart for a grouped result,
 * and the table — which is the data of record (client sort + paging, headers from
 * the server's column labels). No client-side CSV here on purpose: exporting goes
 * through the server's export endpoint for a saved template.
 */
export function ReportResults({ outcome, error, running, catalog, onRetry }: ReportResultsProps) {
  const columns = useMemo<Array<DataTableColumn<IndexedRow>>>(
    () =>
      (outcome?.result.columns ?? []).map(column => ({
        key: column.key,
        header: column.label,
        sortable: true,
        align: column.type === 'number' ? 'right' : 'left',
        className: column.type === 'number' ? 'tabular-nums' : undefined,
        accessor: entry => reportSortValue(entry.row[column.key]),
        render: entry => formatReportValue(entry.row[column.key], column.type),
      })),
    [outcome]
  );
  const rows = useMemo<IndexedRow[]>(
    () => (outcome?.result.rows ?? []).map((row, index) => ({ index, row })),
    [outcome]
  );
  const spec = useMemo(
    () => (outcome ? chartSpec(outcome.result, outcome.definition, catalog) : null),
    [outcome, catalog]
  );

  if (error) {
    return (
      <section aria-label="Report results" className="space-y-3">
        <ErrorState title={error.title} message={error.message} onRetry={error.retryable ? onRetry : undefined} />
      </section>
    );
  }

  if (!outcome) {
    return running ? (
      <p className="text-sm text-fd-mute" role="status">
        Running report…
      </p>
    ) : null;
  }

  const { result } = outcome;
  return (
    <section aria-label="Report results" aria-busy={running} className="card card-compact space-y-3">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold text-fd-ink">{outcome.title}</h2>
        <span className="text-xs tabular-nums text-fd-mute" role="status">
          {running ? 'Running…' : `${result.rows.length.toLocaleString()} row${result.rows.length === 1 ? '' : 's'}`}
        </span>
      </header>

      {result.truncated && (
        <p
          role="note"
          className="flex items-start gap-2 rounded-sm border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
        >
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          {`Showing the first ${result.rows.length.toLocaleString()} rows; narrow the filters or raise the limit.`}
        </p>
      )}
      {result.notes.length > 0 && (
        <ul aria-label="Report notes" className="space-y-1">
          {result.notes.map(note => (
            <li
              key={`${note.code}-${note.fields.join(',')}`}
              className="flex items-start gap-2 rounded-sm border border-fd-line bg-fd-raised px-3 py-2 text-sm text-fd-body"
            >
              <InformationCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-fd-blue" aria-hidden="true" />
              {note.message}
            </li>
          ))}
        </ul>
      )}

      {result.rows.length === 0 ? (
        <EmptyState
          icon={TableCellsIcon}
          title="No rows match"
          description="Loosen the filters or choose another data source, then run the report again."
        />
      ) : (
        <>
          {spec && <ReportChart spec={spec} result={result} />}
          <DataTable columns={columns} data={rows} rowKey={entry => entry.index} pageSize={RESULT_PAGE_SIZE} dense />
        </>
      )}
    </section>
  );
}

export default ReportResults;
