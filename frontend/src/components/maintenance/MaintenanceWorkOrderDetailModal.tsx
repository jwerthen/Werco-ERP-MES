import React, { useCallback, useId, useState } from 'react';
import { CheckCircleIcon, XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { StatusBadge } from '../ui/StatusBadge';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { formatCentralDate, formatCentralDateTime } from '../../utils/centralTime';
import { formatCurrency } from '../../utils/numberFormat';
import {
  MAINTENANCE_PRIORITY_LABELS,
  MAINTENANCE_TYPE_LABELS,
  completionChecklist,
  parseChecklistResults,
  targetText,
} from '../../utils/maintenance';
import type { MaintenanceWorkOrder } from '../../types/maintenance';
import { MaintenanceLogTimeline } from './MaintenanceLogTimeline';
import { AddMaintenanceLogForm } from './AddMaintenanceLogForm';
import { useMaintenanceLogs } from './useMaintenanceLogs';

interface Props {
  /** The work order to show (the list row); null = closed. */
  workOrder: MaintenanceWorkOrder | null;
  /** Its PM schedule's name, when the page knows it. */
  scheduleName?: string | null;
  /** The schedule's steps, for an older API whose work order does not carry `schedule_checklist_items`. */
  scheduleChecklistItems?: string[];
  /** Admin / Manager / Supervisor / Operator: may file log entries (`canPerformMaintenance`). */
  canAddLog: boolean;
  onClose: () => void;
}

const dash = '—';
const textOr = (value: string | null | undefined) => (value && value.trim() ? value : dash);
const labelOf = (labels: Record<string, string>, value: string | null | undefined) =>
  value ? (labels[value] ?? value.replace(/_/g, ' ')) : dash;
const moneyOr = (value: number | null | undefined) => (value == null ? dash : formatCurrency(value));

/**
 * One maintenance work order as a record -- its target, dates, checklist (with the PM schedule's
 * steps it is judged on), notes, findings, parts and costs -- above its log timeline
 * (`GET /maintenance/work-orders/{id}/logs`). People who perform maintenance can add a log entry
 * here; anything typed in it is guarded by the unsaved-changes prompt.
 */
export function MaintenanceWorkOrderDetailModal({
  workOrder,
  scheduleName,
  scheduleChecklistItems,
  canAddLog,
  onClose,
}: Props) {
  const titleId = useId();
  const woId = workOrder?.id ?? null;
  const { logs, loading, error, reload } = useMaintenanceLogs(woId === null ? null : `work-order:${woId}`, async () =>
    woId === null ? [] : (await api.getMaintenanceWorkOrderLogs(woId)).logs
  );
  const [formDirty, setFormDirty] = useState(false);
  const { confirmDiscard } = useUnsavedChanges(woId !== null && formDirty);

  const close = () => {
    if (!confirmDiscard()) return;
    setFormDirty(false);
    onClose();
  };
  const onAdded = useCallback(() => reload(), [reload]);

  const checklist = workOrder
    ? completionChecklist(
        parseChecklistResults(workOrder.checklist_results),
        workOrder.schedule_checklist_items ?? scheduleChecklistItems
      )
    : [];
  const doneCount = checklist.filter(entry => entry.done).length;

  const facts: Array<[string, React.ReactNode]> = workOrder
    ? [
        ['Target', `${targetText(workOrder)}${workOrder.target_type === 'asset' ? ' (asset)' : ''}`],
        ...(workOrder.schedule_id != null
          ? ([['PM schedule', scheduleName || `Schedule #${workOrder.schedule_id}`]] as Array<[string, string]>)
          : []),
        ['Type', labelOf(MAINTENANCE_TYPE_LABELS, workOrder.maintenance_type)],
        ['Priority', labelOf(MAINTENANCE_PRIORITY_LABELS, workOrder.priority)],
        ['Requires shutdown', workOrder.requires_shutdown ? 'Yes' : 'No'],
        ['Scheduled', workOrder.scheduled_date ? formatCentralDate(workOrder.scheduled_date) : dash],
        ['Due', workOrder.due_date ? formatCentralDate(workOrder.due_date) : dash],
        ['Started', workOrder.started_at ? formatCentralDateTime(workOrder.started_at) : dash],
        ['Completed', workOrder.completed_at ? formatCentralDateTime(workOrder.completed_at) : dash],
        ['Actual hours', workOrder.actual_duration_hours != null ? `${workOrder.actual_duration_hours} h` : dash],
        ['Downtime', workOrder.downtime_minutes != null ? `${workOrder.downtime_minutes} min` : dash],
        ['Labor cost', moneyOr(workOrder.labor_cost)],
        ['Parts cost', moneyOr(workOrder.parts_cost)],
        ['Total cost', moneyOr(workOrder.total_cost)],
      ]
    : [];

  return (
    <Modal open={!!workOrder} onClose={close} size="3xl" closeOnBackdrop={false} ariaLabelledBy={titleId}>
      {workOrder && (
        <div className="space-y-4">
          <div className="flex items-start justify-between gap-3 border-b border-slate-700 pb-3">
            <div className="min-w-0">
              <h2 id={titleId} className="text-lg font-semibold text-white">
                <span className="font-mono text-werco-navy-300">{workOrder.wo_number}</span> · {workOrder.title}
              </h2>
              <div className="mt-1">
                <StatusBadge status={workOrder.status} />
              </div>
            </div>
            <button type="button" onClick={close} aria-label="Close dialog" className="text-slate-400 hover:text-white">
              <XMarkIcon className="h-5 w-5" aria-hidden="true" />
            </button>
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
            {facts.map(([label, value]) => (
              <div key={label}>
                <dt className="text-[11px] uppercase tracking-wide text-slate-400">{label}</dt>
                <dd className="text-slate-100">{value}</dd>
              </div>
            ))}
          </dl>

          {workOrder.description && (
            <p className="whitespace-pre-wrap break-words text-sm text-slate-300">{workOrder.description}</p>
          )}

          <section aria-label="Checklist">
            <h3 className="mb-1 text-sm font-semibold text-slate-200">
              Checklist{checklist.length > 0 ? ` (${doneCount} of ${checklist.length} done)` : ''}
            </h3>
            {checklist.length === 0 ? (
              <p className="text-sm text-slate-500">No checklist.</p>
            ) : (
              <ul className="space-y-0.5 text-sm">
                {checklist.map((entry, index) => (
                  <li key={`${index}-${entry.item}`} className="flex items-start gap-2">
                    {entry.done ? (
                      <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden="true" />
                    ) : (
                      <span aria-hidden="true" className="mt-1 h-3 w-3 shrink-0 border border-slate-500" />
                    )}
                    <span className={entry.done ? 'text-slate-200' : 'text-slate-400'}>{entry.item}</span>
                    <span className="sr-only">{entry.done ? '(done)' : '(not done)'}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <dl className="grid gap-3 text-sm sm:grid-cols-3">
            {(
              [
                ['Notes', workOrder.notes],
                ['Findings', workOrder.findings],
                ['Parts used', workOrder.parts_used],
              ] as Array<[string, string | null | undefined]>
            ).map(([label, value]) => (
              <div key={label}>
                <dt className="text-[11px] uppercase tracking-wide text-slate-400">{label}</dt>
                <dd className="whitespace-pre-wrap break-words text-slate-200">{textOr(value)}</dd>
              </div>
            ))}
          </dl>

          <section aria-labelledby={`${titleId}-log`} className="space-y-3 border-t border-slate-700 pt-3">
            <h3 id={`${titleId}-log`} className="text-sm font-semibold text-slate-200">
              Log
            </h3>
            {canAddLog && (
              <AddMaintenanceLogForm
                key={workOrder.id}
                workOrderId={workOrder.id}
                onAdded={onAdded}
                onDirtyChange={setFormDirty}
              />
            )}
            <MaintenanceLogTimeline
              logs={logs}
              loading={loading}
              error={error}
              onRetry={reload}
              label={`Maintenance log for ${workOrder.wo_number}`}
              emptyDescription="Starting, completing and every entry filed against this work order appear here."
            />
          </section>
        </div>
      )}
    </Modal>
  );
}

export default MaintenanceWorkOrderDetailModal;
