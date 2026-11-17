import React, { useMemo } from 'react';
import { DocumentChartBarIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Button, DataTable, Spinner } from '../../ui';
import type { ButtonProps, DataTableColumn } from '../../ui';
import type { ReportDataSourceCatalog, ReportTemplate } from '../../../types/reportBuilder';
import { formatCentralDate } from '../../../utils/centralTime';

export type TemplateAction = 'run' | 'export';

interface RowActionProps extends Omit<ButtonProps, 'size'> {
  /** This row's request is in flight: a spinner, and no second click. */
  loading?: boolean;
}

/**
 * One saved-template row action. Every action in the row is the shared `<Button size="sm">`
 * (`.btn-sm`), so Open / Run / Export CSV / Edit / Delete share one height, padding and type
 * size. `<LoadingButton size="sm">` is deliberately not used for Run / Export: it applies its
 * own utility padding and `text-sm` (and a slate background for `secondary`), which out-rank
 * `.btn-sm` and drew those two visibly larger than their neighbors. The in-flight state is
 * LoadingButton's: a spinner inside a natively disabled button.
 */
function RowAction({ loading = false, disabled, className = '', children, ...rest }: RowActionProps) {
  return (
    <Button
      {...rest}
      size="sm"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`whitespace-nowrap ${className}`.trim()}
    >
      {loading && <Spinner size="sm" />}
      {children}
    </Button>
  );
}

interface SavedTemplatesPanelProps {
  templates: ReportTemplate[];
  catalog: ReportDataSourceCatalog;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /** The template open in the builder (`?template=`), if any. */
  openTemplateId: number | null;
  /** The template row action in flight, if any. */
  busy: { id: number; action: TemplateAction } | null;
  /** The author or an Admin may edit / delete (the server enforces it too). */
  canManage: (template: ReportTemplate) => boolean;
  onOpen: (template: ReportTemplate) => void;
  onRun: (template: ReportTemplate) => void;
  onExport: (template: ReportTemplate) => void;
  onEdit: (template: ReportTemplate) => void;
  onDelete: (template: ReportTemplate) => void;
}

/**
 * The caller's own and shared report templates. A template whose stored definition
 * no longer runs (a field was retired, a rule tightened) is flagged "Needs editing"
 * with the server's reasons; Run and Export are withheld until it is fixed (the
 * server would answer 409), and Open loads it into the builder to fix it.
 */
export function SavedTemplatesPanel({
  templates,
  catalog,
  loading,
  error,
  onRetry,
  openTemplateId,
  busy,
  canManage,
  onOpen,
  onRun,
  onExport,
  onEdit,
  onDelete,
}: SavedTemplatesPanelProps) {
  const columns = useMemo<Array<DataTableColumn<ReportTemplate>>>(
    () => [
      {
        key: 'name',
        header: 'Template',
        sortable: true,
        accessor: template => template.name,
        render: template => {
          const broken = template.definition_errors.length > 0;
          return (
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-fd-ink">{template.name}</span>
                {template.id === openTemplateId && (
                  <span className="rounded-sm border border-fd-blue/60 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-fd-blue">
                    Open
                  </span>
                )}
                {template.is_shared && (
                  <span className="rounded-sm border border-fd-line-bright bg-fd-raised px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-fd-body">
                    Shared
                  </span>
                )}
                {broken && (
                  <span className="inline-flex items-center gap-1 rounded-sm border border-amber-500/60 bg-amber-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300">
                    <ExclamationTriangleIcon className="h-3 w-3" aria-hidden="true" />
                    Needs editing
                  </span>
                )}
              </div>
              {template.description && <p className="text-xs text-fd-mute">{template.description}</p>}
              {broken && (
                <ul aria-label={`Why ${template.name} needs editing`} className="list-disc pl-4 text-xs text-amber-200">
                  {template.definition_errors.map(message => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              )}
            </div>
          );
        },
      },
      {
        key: 'data_source',
        header: 'Data source',
        sortable: true,
        accessor: template => catalog[template.data_source]?.label ?? template.data_source,
      },
      {
        key: 'updated_at',
        header: 'Updated',
        sortable: true,
        accessor: template => template.updated_at ?? template.created_at,
        render: template => formatCentralDate(template.updated_at ?? template.created_at),
      },
      {
        key: 'actions',
        header: <span className="sr-only">Actions</span>,
        align: 'right',
        render: template => {
          const broken = template.definition_errors.length > 0;
          const isBusy = busy?.id === template.id;
          const manage = canManage(template);
          const brokenHint = broken ? 'Open it in the builder and fix it first' : undefined;
          // Two clusters that each stay on one line: when the cell is narrow the row wraps
          // between them (use | manage), never through the middle of a cluster.
          return (
            <div className="flex flex-wrap items-center justify-end gap-1.5">
              <div className="flex items-center gap-1.5">
                <RowAction variant="secondary" onClick={() => onOpen(template)} aria-label={`Open ${template.name}`}>
                  Open
                </RowAction>
                <RowAction
                  variant="secondary"
                  loading={isBusy && busy?.action === 'run'}
                  disabled={broken || isBusy}
                  title={brokenHint}
                  onClick={() => onRun(template)}
                  aria-label={`Run ${template.name}`}
                >
                  Run
                </RowAction>
                <RowAction
                  variant="secondary"
                  loading={isBusy && busy?.action === 'export'}
                  disabled={broken || isBusy}
                  title={brokenHint}
                  onClick={() => onExport(template)}
                  aria-label={`Export CSV for ${template.name}`}
                >
                  Export CSV
                </RowAction>
              </div>
              {manage && (
                <div className="flex items-center gap-1.5">
                  <RowAction variant="ghost" onClick={() => onEdit(template)} aria-label={`Edit ${template.name}`}>
                    Edit
                  </RowAction>
                  <RowAction
                    variant="ghost"
                    className="text-fd-red"
                    onClick={() => onDelete(template)}
                    aria-label={`Delete ${template.name}`}
                  >
                    Delete
                  </RowAction>
                </div>
              )}
            </div>
          );
        },
      },
    ],
    [busy, canManage, catalog, onDelete, onEdit, onExport, onOpen, onRun, openTemplateId]
  );

  return (
    <section aria-labelledby="report-templates-heading" className="card card-compact space-y-3">
      <header className="flex items-baseline justify-between gap-2">
        <h2 id="report-templates-heading" className="text-base font-semibold text-fd-ink">
          Saved templates
        </h2>
        <span className="text-xs tabular-nums text-fd-mute">{loading ? '' : `${templates.length} saved`}</span>
      </header>
      <DataTable
        columns={columns}
        data={templates}
        rowKey={template => template.id}
        loading={loading}
        error={error ?? undefined}
        onRetry={onRetry}
        defaultSort={{ key: 'name', dir: 'asc' }}
        pageSize={10}
        dense
        empty={{
          icon: DocumentChartBarIcon,
          title: 'No saved templates yet',
          description: 'Build a report below and choose "Save as new" to keep it here.',
        }}
      />
    </section>
  );
}

export default SavedTemplatesPanel;
