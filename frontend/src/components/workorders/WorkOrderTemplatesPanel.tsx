/**
 * Work Order Templates — the catalog of jobs the shop re-runs.
 *
 * A template is a NAME plus a POINTER at the work order whose plan it stands
 * for. Nothing about the plan is stored on the template row, so everything this
 * panel shows under "Plan" is computed by the server on every read, off the LIVE
 * source work order. That is the whole reason the summary is worth rendering: a
 * stored `nest_count` goes stale the first time somebody deletes a nest on the
 * source, and the planner picks a template believing it carries 21 nests and
 * gets 20.
 *
 * ---------------------------------------------------------------------------
 * A DELETED SOURCE JOB IS CONTEXT, NOT A BROKEN TEMPLATE
 * ---------------------------------------------------------------------------
 * A template must not stop working because somebody deleted a job. It cannot,
 * either: `source_work_order_id` is NOT NULL with no `ON DELETE`, so the source
 * row can only ever be SOFT-deleted, and a soft-deleted work order still holds
 * every operation, nest, tie and process-sheet step it had. The server therefore
 * reads the plan straight through it — `available` stays true, the counts are
 * real, Use works — and reports the deletion as the informational
 * `plan.source_work_order_deleted`.
 *
 * So that row gets a MUTED note, not the red refusal it used to get. The note is
 * context ("the job it came from is gone, the plan is not"), and it must not read
 * as a warning: there is nothing here for the planner to fix.
 *
 * It does still say WHERE the job went, because someone who wants the JOB back —
 * a different want from using the template — otherwise has nowhere to look: Work
 * Orders → Deleted, the `deleted_only=true` archive with a Restore control on
 * every row. That pointer renders only for the admin/manager population that can
 * actually restore (`canRestoreWorkOrders`); for everyone else the tab falls back
 * to the orders list, and a dead link is worse than a shorter note.
 *
 * ---------------------------------------------------------------------------
 * AN UNUSABLE TEMPLATE IS FLAGGED, NEVER HIDDEN
 * ---------------------------------------------------------------------------
 * A source the server genuinely cannot resolve still comes back with
 * `plan.available = false` and a reason. Filtering it out here would be the mask
 * trap invariant 3 documents: the row would simply vanish, and the fixes all start
 * with seeing it. So the row renders, carries the reason in words, and its Use
 * action is disabled to match the server's 409. The reason vocabulary is the
 * server's and is OPEN — an unrecognized token renders its own sentence rather
 * than being dropped.
 *
 * ---------------------------------------------------------------------------
 * EVERY WRITE HERE IS SERVER-GATED, THEREFORE NON-OPTIMISTIC
 * ---------------------------------------------------------------------------
 * Rename can collide with another live name (409, case-insensitive); delete can
 * 404 on a row another session already removed; use can be refused for a retired
 * part or an unreleased process sheet. Nothing is painted before the server
 * answers: the dialogs hold `pending`, and the catalog is re-read from the
 * server rather than patched in place.
 *
 * Reads and writes alike are role-gated to admin/manager/supervisor — the trio
 * `work_orders:edit` maps to — so the tab itself is gated on that permission and
 * this panel assumes it.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  BookmarkSquareIcon,
  MagnifyingGlassIcon,
  PencilSquareIcon,
  PlayIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';

import api from '../../services/api';
import {
  Button,
  ConfirmDialog,
  DataTable,
  InputDialog,
  StatusBadge,
  useToast,
} from '../ui';
import type { DataTableColumn, DataTableEmpty } from '../ui';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { formatCentralDate } from '../../utils/centralTime';
import { serverErrorDetail } from './copyPlanSkips';
import UseTemplateModal, { templateUnavailableSentence } from './UseTemplateModal';
import { TEMPLATE_NAME_MAX_LENGTH } from './SaveAsTemplateModal';
import type { WorkOrderDuplicateResult, WorkOrderTemplate } from '../../types';

export interface WorkOrderTemplatesPanelProps {
  /**
   * Where a created draft goes. The panel does not navigate itself — the same
   * split the Duplicate dialog uses, so the hand-off shape stays one thing.
   */
  onUsed: (result: WorkOrderDuplicateResult) => void;
  /**
   * Does this user hold the admin/manager (+superuser) tier that
   * `GET /work-orders/?deleted_only=true` and `POST /work-orders/{id}/restore` admit?
   *
   * Only decides whether the "source work order deleted" note points at the Deleted
   * tab. It never gates the template itself — that note is context and the row is
   * usable either way. Passed in rather than read from `useAuth` so this panel stays
   * a presentation component with no provider requirement, and defaults to the
   * narrower answer.
   */
  canRestoreWorkOrders?: boolean;
}

/**
 * The muted note on a template whose source job was soft-deleted.
 *
 * Deliberately short and free of any instruction: the plan copies, Use works, and
 * there is nothing to fix. Anything longer reads as a warning again.
 */
const SOURCE_DELETED_NOTE = 'Its source work order was deleted — the saved plan still copies.';

/** "3 ops · 21 nests · 63 runs · 2 ties" — only the parts that are non-zero. */
function planSummaryParts(template: WorkOrderTemplate): string[] {
  const plan = template.plan;
  if (!plan.available) return [];
  const parts: string[] = [`${plan.operation_count} op${plan.operation_count === 1 ? '' : 's'}`];
  if (plan.nest_count > 0) {
    parts.push(`${plan.nest_count} nest${plan.nest_count === 1 ? '' : 's'}`);
    parts.push(`${plan.planned_runs_total} run${plan.planned_runs_total === 1 ? '' : 's'}`);
  }
  if (plan.open_material_tie_count > 0) {
    parts.push(`${plan.open_material_tie_count} open tie${plan.open_material_tie_count === 1 ? '' : 's'}`);
  }
  return parts;
}

export default function WorkOrderTemplatesPanel({
  onUsed,
  canRestoreWorkOrders = false,
}: WorkOrderTemplatesPanelProps) {
  const { showToast } = useToast();
  const [templates, setTemplates] = useState<WorkOrderTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search.trim(), 250);

  const [useTarget, setUseTarget] = useState<WorkOrderTemplate | null>(null);
  const [renameTarget, setRenameTarget] = useState<WorkOrderTemplate | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<WorkOrderTemplate | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Guards a slow response from overwriting a newer one (the search box can
  // change the query while a request is in flight).
  const loadRequestRef = useRef(0);

  const loadTemplates = useCallback(async () => {
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;
    try {
      const response = await api.listWorkOrderTemplates(
        debouncedSearch ? { search: debouncedSearch } : undefined
      );
      if (requestId !== loadRequestRef.current) return;
      // The envelope, not a bare array — read `.templates` rather than trusting
      // the response to be iterable.
      setTemplates(response?.templates ?? []);
      setLoadError(false);
    } catch (err) {
      if (requestId !== loadRequestRef.current) return;
      console.error('Failed to load work order templates:', err);
      setLoadError(true);
    } finally {
      if (requestId !== loadRequestRef.current) return;
      setLoading(false);
    }
  }, [debouncedSearch]);

  useEffect(() => {
    loadTemplates();
  // The panel refetches on mount and after its own rename/delete. It needs no
  // external refresh signal: it is rendered only on the templates tab, and the
  // save-as-template dialog lives on the ORDERS tab, so the two are never
  // co-mounted and switching tabs remounts this component.
  }, [loadTemplates]);

  const handleRename = useCallback(
    async (name: string) => {
      const target = renameTarget;
      if (!target || renaming) return;
      setRenaming(true);
      try {
        await api.updateWorkOrderTemplate(target.id, { name });
        showToast('success', `Template renamed to "${name}".`);
        // Closes only on success: a 409 on a colliding name keeps the dialog up
        // with the typed value, which is the one thing the planner needs to edit.
        setRenameTarget(null);
        await loadTemplates();
      } catch (err) {
        showToast('error', serverErrorDetail(err, 'Failed to rename this template'));
      } finally {
        setRenaming(false);
      }
    },
    [renameTarget, renaming, loadTemplates, showToast]
  );

  const handleDelete = useCallback(async () => {
    const target = deleteTarget;
    if (!target || deleting) return;
    setDeleting(true);
    try {
      await api.deleteWorkOrderTemplate(target.id);
      showToast(
        'success',
        `Template "${target.name}" deleted. The work orders it created are untouched, and so is ` +
          'the job it pointed at.'
      );
      setDeleteTarget(null);
      await loadTemplates();
    } catch (err) {
      showToast('error', serverErrorDetail(err, 'Failed to delete this template'));
      setDeleteTarget(null);
      // A 404 here means somebody else already deleted it — re-read so the row
      // stops being offered rather than sitting there refusing every click.
      await loadTemplates();
    } finally {
      setDeleting(false);
    }
  }, [deleteTarget, deleting, loadTemplates, showToast]);

  const columns = useMemo<Array<DataTableColumn<WorkOrderTemplate>>>(
    () => [
      {
        key: 'name',
        header: 'Template',
        sortable: true,
        accessor: (template) => template.name,
        render: (template) => (
          <div className="min-w-0">
            <p className="font-semibold text-surface-900">{template.name}</p>
            {template.notes?.trim() ? (
              <p className="text-sm text-surface-500 line-clamp-2">{template.notes.trim()}</p>
            ) : null}
            {/* Context, in the secondary text colour the notes above use — NOT the
                red error treatment. The template works; only the job it was saved
                from is gone. The Deleted-tab pointer rides along for the reader who
                wants that JOB back, and only when they could actually restore it. */}
            {template.plan.source_work_order_deleted === true && (
              <p
                data-testid={`template-source-deleted-${template.id}`}
                className="mt-1 text-xs text-surface-500"
              >
                {SOURCE_DELETED_NOTE}
                {canRestoreWorkOrders && (
                  <>
                    {' '}
                    <Link to="/work-orders?tab=deleted" className="underline hover:no-underline">
                      Find it on the Deleted tab.
                    </Link>
                  </>
                )}
              </p>
            )}
            {!template.plan.available && (
              <p
                data-testid={`template-unavailable-${template.id}`}
                className="mt-1 text-xs font-medium text-fd-red"
              >
                {templateUnavailableSentence(template.plan.unavailable_reason)}
              </p>
            )}
          </div>
        ),
      },
      {
        key: 'kind',
        header: 'Kind',
        sortable: true,
        accessor: (template) => (template.plan.nest_count > 0 ? 'Nest group' : 'Production'),
        render: (template) => {
          const nestBearing = template.plan.nest_count > 0;
          return (
            <div className="flex flex-wrap items-center gap-1.5">
              <span
                className={`badge ${
                  nestBearing
                    ? 'border border-fd-blue/40 bg-fd-blue/10 text-fd-blue'
                    : 'border border-fd-line bg-fd-panel text-slate-300'
                }`}
              >
                {nestBearing ? 'Nest group' : 'Production'}
              </span>
              {/* `sequential_operations === false` is a same-work-center dispatch
                  POOL: operations sharing a work center promote together and are
                  mutually startable. The copy carries the setting, so it belongs
                  on the card a planner picks from. */}
              {template.plan.sequential_operations === false && (
                <span
                  className="badge border border-fd-amber/40 bg-fd-amber/10 text-fd-amber"
                  title="Same-work-center operations run as a dispatch pool rather than in sequence"
                >
                  Pool
                </span>
              )}
            </div>
          );
        },
      },
      {
        key: 'source',
        header: 'Source WO',
        sortable: true,
        accessor: (template) => template.plan.source_work_order_number ?? '',
        csv: (template) => template.plan.source_work_order_number ?? '',
        render: (template) => (
          <div className="min-w-0">
            <p className="font-mono text-sm text-surface-800">
              {template.plan.source_work_order_number ?? `#${template.source_work_order_id}`}
            </p>
            {template.plan.source_status ? (
              <div className="mt-1">
                <StatusBadge status={template.plan.source_status} />
              </div>
            ) : null}
          </div>
        ),
      },
      {
        key: 'plan',
        header: 'Plan',
        accessor: (template) => template.plan.operation_count,
        csv: (template) => planSummaryParts(template).join(' / '),
        render: (template) => {
          const parts = planSummaryParts(template);
          if (parts.length === 0) return <span className="text-surface-500">—</span>;
          return (
            <span className="text-sm text-surface-700 tabular-nums">{parts.join(' · ')}</span>
          );
        },
      },
      {
        key: 'work_centers',
        header: 'Work centers',
        accessor: (template) => template.plan.work_centers.join(', '),
        render: (template) =>
          template.plan.work_centers.length > 0 ? (
            <span className="text-sm text-surface-600 line-clamp-2">
              {template.plan.work_centers.join(' → ')}
            </span>
          ) : (
            <span className="text-surface-500">—</span>
          ),
      },
      {
        key: 'created',
        header: 'Saved',
        sortable: true,
        accessor: (template) => template.created_at ?? '',
        csv: (template) => (template.created_at ? formatCentralDate(template.created_at) : ''),
        render: (template) => (
          <span className="text-sm text-surface-600">
            {template.created_at ? formatCentralDate(template.created_at) : '—'}
          </span>
        ),
      },
      {
        key: 'actions',
        header: '',
        className: 'w-56',
        render: (template) => {
          const unusable = !template.plan.available;
          // No stopPropagation guard on the wrapper: this DataTable has no
          // onRowClick, so there is nothing to stop. Adding one "just in case" is an
          // inert handler that reads as if row click-through exists.
          return (
            <div className="flex items-center justify-end gap-1.5">
              <Button
                size="sm"
                onClick={() => setUseTarget(template)}
                disabled={unusable}
                title={unusable ? templateUnavailableSentence(template.plan.unavailable_reason) : undefined}
                aria-label={`Use template ${template.name}`}
              >
                <PlayIcon className="h-4 w-4 mr-1" aria-hidden="true" />
                Use
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setRenameTarget(template)}
                aria-label={`Rename template ${template.name}`}
              >
                <PencilSquareIcon className="h-4 w-4 mr-1" aria-hidden="true" />
                Rename
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setDeleteTarget(template)}
                className="text-red-300 hover:text-red-200"
                aria-label={`Delete template ${template.name}`}
              >
                <TrashIcon className="h-4 w-4 mr-1" aria-hidden="true" />
                Delete
              </Button>
            </div>
          );
        },
      },
    ],
    [canRestoreWorkOrders]
  );

  const empty: DataTableEmpty = useMemo(
    () =>
      debouncedSearch
        ? {
            icon: BookmarkSquareIcon,
            title: 'No templates match that search',
            description: 'The search covers template names and notes. Clear it to see the whole catalog.',
          }
        : {
            icon: BookmarkSquareIcon,
            title: 'No work order templates yet',
            description:
              'Open a work order and choose "Save as template". It saves the plan under a name — operations, ' +
              'nests and material ties are copied fresh each time somebody uses it, onto a new draft.',
          },
    [debouncedSearch]
  );

  return (
    <div className="space-y-4">
      <div className="card rounded-sm border-fd-line p-2.5 sm:p-3">
        <div className="relative min-w-0">
          <MagnifyingGlassIcon
            className="h-5 w-5 absolute left-4 top-1/2 transform -translate-y-1/2 text-surface-400"
            aria-hidden="true"
          />
          <input
            type="text"
            placeholder="Search templates by name or note..."
            aria-label="Search work order templates"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-11"
          />
        </div>
        <p className="mt-2.5 pt-2.5 border-t border-fd-line text-xs text-surface-500">
          Each template points at a live work order and copies its plan onto a new{' '}
          <strong className="text-surface-700">draft</strong> — nothing reaches the dispatch board or the kiosk
          until somebody releases it.
        </p>
      </div>

      <DataTable
        columns={columns}
        data={templates}
        rowKey={(template) => template.id}
        loading={loading}
        error={loadError}
        onRetry={loadTemplates}
        empty={empty}
        defaultSort={{ key: 'name', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: 'work-order-templates' }}
        rowClassName={(template) => (template.plan.available ? '' : 'opacity-70')}
      />

      <UseTemplateModal
        open={useTarget !== null}
        template={useTarget}
        onClose={() => setUseTarget(null)}
        onUsed={onUsed}
      />

      <InputDialog
        open={renameTarget !== null}
        title="Rename template"
        message={
          renameTarget
            ? `Renames the label only — "${renameTarget.name}" keeps pointing at the same work order, and every draft it already created is untouched.`
            : ''
        }
        label={`Template name (max ${TEMPLATE_NAME_MAX_LENGTH} characters)`}
        defaultValue={renameTarget?.name ?? ''}
        submitLabel="Rename"
        pending={renaming}
        onSubmit={handleRename}
        onCancel={() => {
          if (!renaming) setRenameTarget(null);
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete template"
        message={
          deleteTarget
            ? `Delete the template "${deleteTarget.name}"?\n\nThis removes a name from this list and nothing else: ` +
              'the work order it points at, and every draft it has already created, are untouched. The name ' +
              'becomes available again immediately, and you can re-create the template in one click from the ' +
              'same work order.'
            : ''
        }
        confirmLabel="Delete"
        pending={deleting}
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
      />
    </div>
  );
}
