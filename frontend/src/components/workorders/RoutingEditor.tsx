import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowDownIcon,
  ArrowUturnLeftIcon,
  ArrowUpIcon,
  ForwardIcon,
  PencilSquareIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { Button } from '../ui/Button';
import { InputDialog } from '../ui/InputDialog';
import { useToast } from '../ui/Toast';
import api from '../../services/api';
import { apiErrorDetail } from '../../utils/apiError';
import { formatOperationLabel } from '../../utils/operationLabel';
import { statusColor } from '../../utils/statusColors';
import {
  canEditOperation,
  canRestoreOperation,
  canSkipOperation,
  operationHasRecordedWork,
  isOperationVersionConflict,
  operationsBySequence,
  swapWithNeighbour,
} from '../../utils/routingChange';
import type { OperationSequenceChange, WorkOrder, WorkOrderOperation } from '../../types';
import RoutingOperationModal, { RoutingOperationModalMode } from './RoutingOperationModal';

interface RoutingEditorProps {
  workOrder: WorkOrder;
  /** Re-read the work order after a change the server accepted (non-optimistic). */
  onChanged: () => Promise<void> | void;
  onClose: () => void;
}

type ReasonAction =
  | { kind: 'skip'; operation: WorkOrderOperation }
  | { kind: 'restore'; operation: WorkOrderOperation }
  | {
      kind: 'move';
      operation: WorkOrderOperation;
      neighbour: WorkOrderOperation;
      direction: 'up' | 'down';
      changes: OperationSequenceChange[];
    };

const label = (op: WorkOrderOperation) => formatOperationLabel(op.operation_number, op.sequence);

const ICON_BUTTON =
  'inline-flex h-7 w-7 items-center justify-center rounded-sm border border-fd-line text-fd-body hover:border-fd-line-bright hover:text-fd-ink disabled:cursor-not-allowed disabled:opacity-40';
const TEXT_BUTTON =
  'inline-flex items-center gap-1 rounded-sm border border-fd-line px-2 py-1 text-xs font-medium hover:border-fd-line-bright disabled:cursor-not-allowed disabled:opacity-40';

function reasonCopy(action: ReasonAction): { title: string; message: string; submitLabel: string } {
  switch (action.kind) {
    case 'skip':
      return {
        title: `Skip ${label(action.operation)} — ${action.operation.name}`,
        message:
          'The step stays on the traveler marked Skipped, with your name, the time and this reason. The steps after it can proceed.',
        submitLabel: 'Skip step',
      };
    case 'restore':
      return {
        title: `Restore ${label(action.operation)} — ${action.operation.name}`,
        message: 'The step goes back on the route as Pending. Steps after it that have not started wait for it again.',
        submitLabel: 'Restore step',
      };
    case 'move':
      return {
        title: `Move ${label(action.operation)} ${action.direction}`,
        message: `${label(action.operation)} (${action.operation.name}) and ${label(action.neighbour)} (${action.neighbour.name}) swap places in the run order. Operation numbers do not change.`,
        submitLabel: 'Reorder',
      };
  }
}

/** The same action against the re-read route (fresh versions), or `null` when it no longer applies. */
function rebuildAction(action: ReasonAction, operations: WorkOrderOperation[]): ReasonAction | null {
  const live = operations.find(op => op.id === action.operation.id);
  if (!live) return null;
  if (action.kind !== 'move') return { ...action, operation: live };
  const swap = swapWithNeighbour(operations, live, action.direction);
  if (!swap) return null;
  return { ...action, operation: live, neighbour: swap.neighbour, changes: swap.changes };
}

/**
 * The routing editor for a work order (owner decision 2026-11-25): add a step, skip / restore
 * one, reorder steps that have not started, edit one that is not finished.
 *
 * Offered only to ADMIN / MANAGER / SUPERVISOR on a work order whose routing can still change
 * (the page decides; see `routingEditableWorkOrder`). Every action is SERVER-GATED and
 * NON-optimistic: nothing on screen moves until the server accepted the change and the work
 * order was re-read; a refusal is shown verbatim and the dialog stays open. Skip, restore and
 * reorder always need a reason (the server requires one on every work order, draft included).
 */
export default function RoutingEditor({ workOrder, onChanged, onClose }: RoutingEditorProps) {
  const { showToast } = useToast();
  const [reasonAction, setReasonAction] = useState<ReasonAction | null>(null);
  const [pending, setPending] = useState(false);
  const [formMode, setFormMode] = useState<RoutingOperationModalMode | null>(null);

  const operations = useMemo(() => operationsBySequence(workOrder.operations || []), [workOrder.operations]);

  // After a stale-version refusal the page is re-read; the open dialog then adopts the fresh
  // versions so the retry the server's text asks for ("Refresh and try again") can succeed.
  // Only then — a background poll must never quietly refresh the version a person is acting
  // on, or the optimistic lock would stop protecting them.
  const adoptLiveVersions = useRef(false);
  useEffect(() => {
    if (!adoptLiveVersions.current) return;
    adoptLiveVersions.current = false;
    setReasonAction(prev => (prev ? rebuildAction(prev, operations) : prev));
  }, [operations]);

  const submitReason = async (reason: string) => {
    if (!reasonAction || pending) return;
    setPending(true);
    const action = reasonAction;
    try {
      if (action.kind === 'skip') {
        await api.skipOperation(action.operation.id, { version: action.operation.version, reason });
      } else if (action.kind === 'restore') {
        await api.unskipOperation(action.operation.id, { version: action.operation.version, reason });
      } else {
        await api.resequenceOperations(workOrder.id, { reason, operations: action.changes });
      }
      await onChanged();
      showToast(
        'success',
        action.kind === 'skip'
          ? `${label(action.operation)} skipped`
          : action.kind === 'restore'
            ? `${label(action.operation)} restored`
            : 'Steps reordered'
      );
      setReasonAction(null);
    } catch (err) {
      // Refused: the server's words, verbatim, and the dialog (with the typed reason) stays.
      showToast('error', apiErrorDetail(err, 'The routing change was not saved'));
      if (isOperationVersionConflict(err)) {
        adoptLiveVersions.current = true;
        try {
          await onChanged();
        } catch {
          adoptLiveVersions.current = false;
        }
      }
    } finally {
      setPending(false);
    }
  };

  const copy = reasonAction ? reasonCopy(reasonAction) : null;

  return (
    <section
      aria-label="Edit routing"
      className="mb-4 rounded-sm border border-fd-blue/40 bg-fd-sunken p-3"
      data-testid="routing-editor"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide text-fd-ink">Edit routing</h3>
          <p className="text-xs text-fd-mute">
            {workOrder.status === 'draft'
              ? 'Skips and reorders are recorded with a reason.'
              : 'This work order has been released: every change is recorded with your reason.'}
          </p>
        </div>
        <div className="flex gap-2">
          <Button size="sm" onClick={() => setFormMode({ kind: 'add' })}>
            <PlusIcon className="mr-1 h-4 w-4" aria-hidden="true" /> Add step
          </Button>
          <Button size="sm" variant="secondary" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>

      <ol className="divide-y divide-fd-line rounded-sm border border-fd-line bg-fd-panel" aria-label="Routing steps">
        {operations.map(op => {
          const up = swapWithNeighbour(operations, op, 'up');
          const down = swapWithNeighbour(operations, op, 'down');
          const skipped = op.status === 'skipped';
          const started =
            !skipped && (op.status === 'in_progress' || op.status === 'complete' || operationHasRecordedWork(op));
          return (
            <li
              key={op.id}
              className={`flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center sm:justify-between ${skipped ? 'opacity-70' : ''}`}
              data-testid={`routing-step-${op.id}`}
            >
              <div className="flex min-w-0 items-center gap-3">
                <span className="w-14 shrink-0 font-mono text-xs tabular-nums text-fd-mute">Seq {op.sequence}</span>
                <span className="shrink-0 text-sm font-medium text-fd-ink">{label(op)}</span>
                <span className={`min-w-0 truncate text-sm ${skipped ? 'line-through text-fd-mute' : 'text-fd-body'}`}>
                  {op.name}
                </span>
                <span className="hidden truncate text-xs text-fd-mute md:inline">{op.work_center_name}</span>
                <span
                  className={`inline-flex shrink-0 rounded-sm px-1.5 py-0.5 text-[11px] font-medium capitalize ${statusColor(op.status)}`}
                >
                  {op.status.replace('_', ' ')}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <button
                  type="button"
                  className={ICON_BUTTON}
                  aria-label={`Move ${label(op)} up`}
                  title={up ? `Move ${label(op)} up` : 'Only steps that have not started can be reordered'}
                  disabled={!up || pending}
                  onClick={() =>
                    up &&
                    setReasonAction({
                      kind: 'move',
                      operation: op,
                      neighbour: up.neighbour,
                      direction: 'up',
                      changes: up.changes,
                    })
                  }
                >
                  <ArrowUpIcon className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className={ICON_BUTTON}
                  aria-label={`Move ${label(op)} down`}
                  title={down ? `Move ${label(op)} down` : 'Only steps that have not started can be reordered'}
                  disabled={!down || pending}
                  onClick={() =>
                    down &&
                    setReasonAction({
                      kind: 'move',
                      operation: op,
                      neighbour: down.neighbour,
                      direction: 'down',
                      changes: down.changes,
                    })
                  }
                >
                  <ArrowDownIcon className="h-4 w-4" aria-hidden="true" />
                </button>
                {canEditOperation(op) && (
                  <button
                    type="button"
                    className={`${TEXT_BUTTON} text-fd-blue`}
                    aria-label={`Edit ${label(op)}`}
                    onClick={() => setFormMode({ kind: 'edit', operation: op })}
                  >
                    <PencilSquareIcon className="h-4 w-4" aria-hidden="true" /> Edit
                  </button>
                )}
                {canRestoreOperation(op) ? (
                  <button
                    type="button"
                    className={`${TEXT_BUTTON} text-fd-amber`}
                    aria-label={`Restore ${label(op)}`}
                    disabled={pending}
                    onClick={() => setReasonAction({ kind: 'restore', operation: op })}
                  >
                    <ArrowUturnLeftIcon className="h-4 w-4" aria-hidden="true" /> Restore
                  </button>
                ) : (
                  op.status !== 'complete' && (
                    <button
                      type="button"
                      className={`${TEXT_BUTTON} text-fd-mute`}
                      aria-label={`Skip ${label(op)}`}
                      title={
                        canSkipOperation(op)
                          ? `Skip ${label(op)}`
                          : started
                            ? 'Work has been recorded on this step, so it cannot be skipped'
                            : 'Only a step that has not started can be skipped'
                      }
                      disabled={!canSkipOperation(op) || pending}
                      onClick={() => setReasonAction({ kind: 'skip', operation: op })}
                    >
                      <ForwardIcon className="h-4 w-4" aria-hidden="true" /> Skip
                    </button>
                  )
                )}
              </div>
            </li>
          );
        })}
      </ol>

      {copy && (
        <InputDialog
          open={reasonAction !== null}
          title={copy.title}
          message={copy.message}
          label="Reason"
          placeholder="Why is the routing changing?"
          submitLabel={copy.submitLabel}
          pending={pending}
          onSubmit={submitReason}
          onCancel={() => setReasonAction(null)}
        />
      )}

      {formMode && (
        <RoutingOperationModal
          open
          mode={formMode}
          liveOperation={formMode.kind === 'edit' ? operations.find(op => op.id === formMode.operation.id) : undefined}
          workOrder={workOrder}
          onClose={() => setFormMode(null)}
          onSaved={onChanged}
          onRefresh={onChanged}
        />
      )}
    </section>
  );
}
