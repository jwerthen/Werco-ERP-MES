import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowPathIcon, ArrowTopRightOnSquareIcon, CheckCircleIcon, XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import type { WorkOrder, WorkOrderOperation } from '../../types';
import { hasPermission } from '../../utils/permissions';
import { formatOperationLabel, operationNumberText, sortOperationsForDisplay } from '../../utils/operationLabel';
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

function operationTarget(operation: WorkOrderOperation, workOrder: WorkOrder) {
  return Number(operation.laser_nest?.planned_runs || operation.component_quantity || workOrder.quantity_ordered || 0);
}

function completionBlockReason(operation: WorkOrderOperation, workOrder: WorkOrder): string | null {
  if (operation.cancelled_nest_id != null) return 'This nest is cancelled.';
  if (operation.status === 'on_hold') return 'Clear the operation hold before completing.';
  if (workOrder.status === 'on_hold') return 'Clear the work order hold before completing.';
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
  const [notice, setNotice] = useState('');
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
    setNotice('');
    setTarget(next);
  };
  const submitCompletion = async (values: CompleteWorkSubmit) => {
    if (!target || !workOrder || !canComplete || pending.current || loading || loadError) return;
    pending.current = true;
    setSubmitting(true);
    setCompletionError(null);
    try {
      const { quantityComplete, quantityScrapped, scrapReason, scrapReasonCodeId } = values;
      let message: string;
      if (target.kind === 'operation') {
        const result = await api.completeWOOperation(
          target.operation.id,
          quantityComplete,
          quantityScrapped,
          scrapReason
        );
        message =
          result?.message === 'Operation completed'
            ? `Operation ${operationNumberText(target.operation.operation_number, target.operation.sequence)} (${target.operation.name}) completed.`
            : `Progress saved for operation ${operationNumberText(target.operation.operation_number, target.operation.sequence)} (${target.operation.name}).`;
      } else {
        const result: unknown = await api.completeWorkOrder(
          workOrder.id,
          quantityComplete,
          quantityScrapped,
          scrapReason,
          scrapReasonCodeId
        );
        const bypassed = extractStepsBypassed(result);
        message = bypassed ? stepsBypassedMessage(bypassed) : `${workOrder.work_order_number} completed.`;
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
          {notice && (
            <p role="status" className="rounded border border-fd-blue/30 bg-fd-blue/10 p-3 text-sm text-fd-body">
              {notice}
            </p>
          )}
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
                <p className="text-sm text-fd-mute">Clear the work order hold before completing operations.</p>
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
          subtitle={
            target.kind === 'operation'
              ? `Target: ${operationTarget(target.operation, workOrder)}${target.operation.laser_nest ? ' runs' : ''}. Enter the total completed quantity, including work already recorded.`
              : `Ordered: ${workOrder.quantity_ordered}. This completes all remaining operations and may bypass required process-step records.`
          }
          defaultQuantityComplete={
            target.kind === 'operation' ? operationTarget(target.operation, workOrder) : workOrder.quantity_ordered
          }
        />
      )}
    </>
  );
}
