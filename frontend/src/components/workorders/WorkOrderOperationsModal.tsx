import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowPathIcon, ArrowTopRightOnSquareIcon, CheckCircleIcon, XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { OfficeOperationCompleteResult, WorkOrder, WorkOrderOperation } from '../../types';
import { hasPermission } from '../../utils/permissions';
import { formatOperationLabel, operationNumberText, sortOperationsForDisplay } from '../../utils/operationLabel';
import { SHORT_EPSILON, formatShortQuantity, shortUnitLabel } from '../../utils/shortCount';
import { joinOperatorNames, operationMayHaveLabor, operationUnaccountedChip } from '../../utils/officeCompletion';
import {
  extractApiErrorDetail,
  extractStepsBypassed,
  extractStepsIncomplete,
  stepsBypassedMessage,
  stepsIncompleteMessage,
} from '../../utils/processSheetErrors';
import { Button, EmptyState, ErrorState, Modal, StatusBadge } from '../ui';
import { CompleteWorkModal, CompleteWorkSubmit } from './CompleteWorkModal';

interface WorkOrderOperationsModalProps {
  workOrderId: number;
  workOrderNumber: string;
  onClose: () => void;
  onUpdated: () => void;
}

type CompletionTarget = { kind: 'work_order' } | { kind: 'operation'; operation: WorkOrderOperation };

/** The operation TARGET (runs on a nest) — the server's `operation_target_quantity`. */
function operationTarget(operation: WorkOrderOperation, workOrder: WorkOrder) {
  return Number(operation.laser_nest?.planned_runs || operation.component_quantity || workOrder.quantity_ordered || 0);
}

function completionBlockReason(operation: WorkOrderOperation, workOrder: WorkOrder): string | null {
  if (operation.cancelled_nest_id != null) return 'This nest is cancelled.';
  if (operation.status === 'on_hold') return 'Clear the operation hold before completing.';
  // A WORK-ORDER hold is lifted with "Resume Work Order" on the work order's
  // page — never "Clear Hold", which names the operation-level action above.
  if (workOrder.status === 'on_hold') return 'Resume the work order (Resume Work Order on its page) before completing.';
  if (workOrder.sequential_operations && workOrder.work_order_type !== 'laser_cutting') {
    const previous = workOrder.operations
      .filter(candidate => candidate.sequence < operation.sequence && candidate.status !== 'complete')
      .sort((a, b) => a.sequence - b.sequence)[0];
    if (previous) return `Complete operation ${operationNumberText(previous.operation_number, previous.sequence)} (${previous.name}) first.`;
  }
  if (operation.status === 'pending') return 'This operation must be ready or in progress before completing.';
  return null;
}

/** The selected job stays mounted when refreshing the list removes its completed row. */
export default function WorkOrderOperationsModal({
  workOrderId,
  workOrderNumber,
  onClose,
  onUpdated,
}: WorkOrderOperationsModalProps) {
  const { user } = useAuth();
  const canComplete = hasPermission(user?.role, 'work_orders:edit') || !!user?.is_superuser || user?.role === 'quality';
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [target, setTarget] = useState<CompletionTarget | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [completionError, setCompletionError] = useState<string | null>(null);
  // A SAVED-BUT-STILL-OPEN result (owner ruling 1) is a partial success: the warning
  // tone, announced as an alert — never the plain "completed" status.
  const [notice, setNotice] = useState<{ text: string; tone: 'info' | 'warning' } | null>(null);
  const request = useRef(0);
  const pending = useRef(false);

  const load = useCallback(async () => {
    const requestId = ++request.current;
    setLoading(true);
    setLoadError(false);
    try {
      const result: WorkOrder = await api.getWorkOrder(workOrderId);
      if (request.current === requestId) setWorkOrder(result);
    } catch {
      if (request.current === requestId) setLoadError(true);
    } finally {
      if (request.current === requestId) setLoading(false);
    }
  }, [workOrderId]);

  useEffect(() => {
    void load();
    return () => {
      ++request.current;
    };
  }, [load]);

  const openCompletion = (next: CompletionTarget) => {
    setCompletionError(null);
    setNotice(null);
    setTarget(next);
  };
  const submitCompletion = async (values: CompleteWorkSubmit) => {
    if (!target || !workOrder || !canComplete || pending.current || loading || loadError) return;
    pending.current = true;
    setSubmitting(true);
    setCompletionError(null);
    try {
      const { quantityComplete, quantityScrapped, scrapReason, scrapReasonCodeId, overrideReason } = values;
      let message: { text: string; tone: 'info' | 'warning' };
      if (target.kind === 'operation') {
        const operation = target.operation;
        const result: OfficeOperationCompleteResult = await api.completeWOOperation(operation.id, quantityComplete, {
          quantityScrapped,
          scrapReason,
          scrapReasonCodeId,
          overrideReason,
          acknowledgeOpenEntries: values.acknowledgeOpenEntries,
        });
        const identity = `operation ${operationNumberText(operation.operation_number, operation.sequence)} (${operation.name})`;
        if (result?.is_fully_complete === false) {
          const serverMessage = typeof result.message === 'string' ? result.message : '';
          const left = Math.max(0, Number(result.operation?.quantity_unaccounted ?? 0));
          message = serverMessage.startsWith('No change')
            ? { text: serverMessage, tone: 'info' }
            : {
                text:
                  left > SHORT_EPSILON
                    ? `Progress saved — ${identity} left open, ${formatShortQuantity(left)} ${shortUnitLabel(!!operation.laser_nest, left)} not yet accounted for.`
                    : `Progress saved — ${identity} left open.`,
                tone: 'warning',
              };
        } else {
          const closedNames = joinOperatorNames((result?.closed_time_entries ?? []).map(entry => entry.operator_name));
          message = {
            text: closedNames
              ? `Operation ${operationNumberText(operation.operation_number, operation.sequence)} (${operation.name}) completed — clocked out ${closedNames}.`
              : `Operation ${operationNumberText(operation.operation_number, operation.sequence)} (${operation.name}) completed.`,
            tone: 'info',
          };
        }
      } else {
        const result: unknown = await api.completeWorkOrder(
          workOrder.id,
          quantityComplete,
          quantityScrapped,
          scrapReason,
          scrapReasonCodeId,
          overrideReason
        );
        const bypassed = extractStepsBypassed(result);
        message = {
          text: bypassed ? stepsBypassedMessage(bypassed) : `${workOrder.work_order_number} completed.`,
          tone: 'info',
        };
      }
      setTarget(null);
      setNotice(message);
    } catch (error) {
      const missing = extractStepsIncomplete(error);
      const detail = extractApiErrorDetail(error);
      setCompletionError(
        missing
          ? stepsIncompleteMessage(missing)
          : typeof detail === 'string' && detail
            ? detail
            : detail && typeof detail === 'object' && 'detail' in detail && typeof detail.detail === 'string'
              ? detail.detail
              : 'Could not complete this work. Please try again.'
      );
      pending.current = false;
      setSubmitting(false);
      return;
    }

    // The write succeeded. A failed refresh must never invite repeating it.
    await load();
    onUpdated();
    pending.current = false;
    setSubmitting(false);
  };

  const operations = sortOperationsForDisplay(workOrder?.operations || []);
  const completeCount = operations.filter(operation => operation.status === 'complete').length;
  const active = workOrder && ['released', 'in_progress', 'on_hold'].includes(workOrder.status);
  const disabled = loading || loadError || submitting;
  const close = () => {
    if (!pending.current) onClose();
  };

  return (
    <>
      <Modal
        open
        onClose={close}
        size="5xl"
        padded={false}
        scroll={false}
        ariaLabel={`Operations for ${workOrderNumber}`}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-fd-line p-4 sm:px-6">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-fd-ink">{workOrderNumber}</h2>
            <p className="text-sm text-fd-mute">Operations · Review progress and complete work</p>
          </div>
          <Button variant="secondary" size="sm" onClick={close} disabled={submitting} aria-label="Close operations">
            <XMarkIcon className="h-5 w-5" aria-hidden="true" />
          </Button>
        </div>

        <div className="min-h-0 overflow-y-auto p-4 sm:p-6 space-y-4">
          {notice &&
            (notice.tone === 'warning' ? (
              <p role="alert" className="rounded border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-body">
                {notice.text}
              </p>
            ) : (
              <p role="status" className="rounded border border-fd-blue/30 bg-fd-blue/10 p-3 text-sm text-fd-body">
                {notice.text}
              </p>
            ))}
          {loadError && (
            <ErrorState
              title="Could not refresh operations"
              message={
                workOrder
                  ? 'Displayed operations may be out of date. Refresh before completing more work.'
                  : 'Try loading this work order again.'
              }
              onRetry={() => void load()}
            />
          )}
          {loading && (
            <p role="status" className="text-sm text-fd-mute">
              Loading operations…
            </p>
          )}
          {workOrder && (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3 text-sm text-fd-body">
                  <StatusBadge status={workOrder.status} />
                  <span>
                    {completeCount} / {operations.length} operations complete
                  </span>
                  <span>
                    Job quantity: {workOrder.quantity_complete} / {workOrder.quantity_ordered}
                  </span>
                </div>
                {canComplete && active && (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={
                      disabled ||
                      workOrder.status === 'on_hold' ||
                      operations.some(
                        operation => operation.status === 'on_hold' || operation.cancelled_nest_id != null
                      )
                    }
                    onClick={() => openCompletion({ kind: 'work_order' })}
                  >
                    Complete work order
                  </Button>
                )}
              </div>
              {workOrder.status === 'draft' && (
                <p className="text-sm text-fd-mute">Release this work order before completing operations.</p>
              )}
              {workOrder.status === 'on_hold' && (
                <p className="text-sm text-fd-mute">
                  Resume the work order (
                  <Link
                    to={`/work-orders/${workOrderId}`}
                    onClick={event => {
                      if (pending.current) event.preventDefault();
                    }}
                    className="text-fd-blue underline hover:text-blue-300"
                  >
                    Resume Work Order on its page
                  </Link>
                  ) before completing operations.
                </p>
              )}
              {operations.length === 0 ? (
                <EmptyState
                  title="No operations on this work order"
                  description="Open the full work order to review its routing."
                />
              ) : (
                <ul
                  className="divide-y divide-fd-line rounded-lg border border-fd-line"
                  aria-label="Work order operations"
                >
                  {operations.map(operation => {
                    const blockReason = completionBlockReason(operation, workOrder);
                    const unaccounted = operationUnaccountedChip(operation, operationTarget(operation, workOrder));
                    return (
                      <li
                        key={operation.id}
                        className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0 space-y-1">
                          <h3 className="font-semibold text-fd-ink">
                            {formatOperationLabel(operation.operation_number, operation.sequence)}{' '}
                            · {operation.name}
                          </h3>
                          <p className="text-sm text-fd-mute">
                            {operation.work_center_name || 'Work center unassigned'}
                            {operation.component_part_number ? ` · ${operation.component_part_number}` : ''}
                          </p>
                          <p className="text-sm text-fd-body">
                            {operation.quantity_complete} / {operationTarget(operation, workOrder)}{' '}
                            {operation.laser_nest ? 'runs' : 'completed'}
                            {operation.quantity_scrapped > 0 ? ` · ${operation.quantity_scrapped} scrapped` : ''}
                            {unaccounted > SHORT_EPSILON && (
                              <span className="text-fd-amber"> · {formatShortQuantity(unaccounted)} unaccounted</span>
                            )}
                          </p>
                          {blockReason && active && operation.status !== 'complete' && (
                            <p className="text-xs text-fd-amber">{blockReason}</p>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center justify-between gap-3 sm:justify-end">
                          <StatusBadge status={operation.cancelled_nest_id != null ? 'cancelled' : operation.status} />
                          {canComplete && active && operation.status !== 'complete' && (
                            <Button
                              size="sm"
                              disabled={disabled || !!blockReason}
                              title={blockReason || undefined}
                              aria-label={`Complete operation ${operationNumberText(operation.operation_number, operation.sequence)}: ${operation.name}`}
                              onClick={() => openCompletion({ kind: 'operation', operation })}
                            >
                              <CheckCircleIcon className="mr-1 h-4 w-4" aria-hidden="true" /> Complete
                            </Button>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-fd-line p-4 sm:px-6">
          <Button variant="secondary" size="sm" disabled={loading || submitting} onClick={() => void load()}>
            <ArrowPathIcon className="mr-1 h-4 w-4" aria-hidden="true" /> Refresh
          </Button>
          <Link
            to={`/work-orders/${workOrderId}`}
            onClick={event => {
              if (pending.current) event.preventDefault();
            }}
            aria-disabled={submitting}
            className={`btn-secondary btn-sm ${submitting ? 'pointer-events-none opacity-50' : ''}`}
          >
            Open full work order <ArrowTopRightOnSquareIcon className="ml-1 h-4 w-4" aria-hidden="true" />
          </Link>
        </div>
      </Modal>
      {target && workOrder && (
        <CompleteWorkModal
          open
          onClose={() => {
            if (!pending.current) setTarget(null);
          }}
          submitting={submitting}
          onSubmit={submitCompletion}
          error={completionError}
          title={
            target.kind === 'operation'
              ? `Complete operation "${target.operation.name}"`
              : `Complete work order ${workOrderNumber}`
          }
          // Owner ruling 1: totals prefilled with what is RECORDED, judged against
          // the server's own figures (see CompleteWorkModal).
          {...(target.kind === 'operation'
            ? {
                mode: 'operation' as const,
                subtitle: `Target: ${operationTarget(target.operation, workOrder)}${target.operation.laser_nest ? ' runs' : ''}. Enter the totals, including work already recorded.`,
                target: operationTarget(target.operation, workOrder),
                recordedComplete: Number(target.operation.quantity_complete || 0),
                recordedScrapped: Number(target.operation.quantity_scrapped || 0),
                upstreamShort: target.operation.upstream_quantity_short,
                goodCeiling: target.operation.quantity_good_ceiling,
                laborRecorded: operationMayHaveLabor(target.operation),
                isNest: !!target.operation.laser_nest,
                subjectLabel: formatOperationLabel(target.operation.operation_number, target.operation.sequence),
              }
            : {
                mode: 'work_order' as const,
                subtitle: `Ordered: ${workOrder.quantity_ordered}. This completes all remaining operations and may bypass required process-step records.`,
                target: Number(workOrder.quantity_ordered || 0),
                recordedComplete: Number(workOrder.quantity_complete || 0),
                recordedScrapped: Number(workOrder.quantity_scrapped || 0),
                accountedLoss: workOrder.quantity_accounted_loss,
                deliverable: workOrder.quantity_deliverable,
                subjectLabel: workOrderNumber,
              })}
        />
      )}
    </>
  );
}
