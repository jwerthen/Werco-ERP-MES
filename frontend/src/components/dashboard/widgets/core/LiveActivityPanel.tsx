import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { UserGroupIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import type { ActiveAssignment } from '../../../../types';
import { formatCentralDate, formatCentralTime, isDateBeforeTodayInCentral } from '../../../../utils/centralTime';
import { formatOperationLabel, hasOperationNumber } from '../../../../utils/operationLabel';
import { operationTargetQuantity } from '../../../../utils/operationQuantity';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { useCoreDashboard } from './CoreDashboardContext';
import {
  focusIdleOperators,
  formatElapsed,
  getEntryTypeLabel,
  getOperatorDisplayName,
  getRoleBadgeClass,
  sortActiveAssignments,
} from './shared';

/**
 * Live Shop Activity — active jobs by work center. Group anchors `#wc-live-<id>` and row
 * anchors `#assign-<time_entry_id>` are the cross-link targets of Work Center Status and
 * the presence chips.
 */
export function LiveActivityPanel() {
  const { data, openOperation } = useCoreDashboard();
  const { nowMs, isVisible, canOpen } = useDashboardWidgetEnv();
  const activeAssignments = useMemo(() => sortActiveAssignments(data?.active_assignments), [data?.active_assignments]);
  const assignmentsByWorkCenter = useMemo(() => {
    return activeAssignments.reduce<Record<string, ActiveAssignment[]>>((groups, assignment) => {
      const key = assignment.work_center.name || 'Unassigned';
      if (!groups[key]) {
        groups[key] = [];
      }
      groups[key].push(assignment);
      return groups;
    }, {});
  }, [activeAssignments]);
  const idleSignedInUsers = (data?.signed_in_users || []).filter(user => !user.has_active_job);
  const presenceVisible = isVisible('presence');

  return (
    <CockpitPanel
      title="Live Shop Activity"
      subtitle="Active jobs by work center"
      footer={`${activeAssignments.length} active assignment${activeAssignments.length === 1 ? '' : 's'}`}
    >
      {activeAssignments.length > 0 ? (
        <div>
          {Object.entries(assignmentsByWorkCenter).map(([workCenterName, assignments]) => {
            const workCenterId = assignments[0]?.work_center.id;
            return (
              <div key={workCenterName} id={`wc-live-${workCenterId}`} className="scroll-mt-2">
                <div className="sticky top-0 z-10 bg-fd-panel border-b border-fd-line py-1.5">
                  <p className="truncate text-xs font-semibold uppercase tracking-wide text-slate-300">
                    {workCenterName} <span className="text-slate-400">· {assignments.length}</span>
                  </p>
                </div>
                <div className="divide-y divide-fd-line">
                  {assignments.map(assignment => (
                    <ActiveAssignmentRow
                      key={assignment.time_entry_id}
                      assignment={assignment}
                      nowMs={nowMs}
                      canOpenWorkOrder={canOpen(`/work-orders/${assignment.work_order.id}`)}
                      onOpenOperation={() => openOperation(assignment)}
                    />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <EmptyState
          icon={UserGroupIcon}
          title="No one is clocked into a job right now"
          description={
            idleSignedInUsers.length > 0
              ? `${idleSignedInUsers.length} signed-in ${idleSignedInUsers.length === 1 ? 'operator is' : 'operators are'} idle and available to start work.`
              : 'Signed-in users still appear in the presence panel.'
          }
          action={
            idleSignedInUsers.length > 0 && presenceVisible
              ? { label: 'View idle operators', onClick: focusIdleOperators }
              : undefined
          }
        />
      )}
    </CockpitPanel>
  );
}

function ActiveAssignmentRow({
  assignment,
  nowMs,
  canOpenWorkOrder,
  onOpenOperation,
}: {
  assignment: ActiveAssignment;
  nowMs: number;
  canOpenWorkOrder: boolean;
  onOpenOperation: () => void;
}) {
  // Progress here is OPERATION-scoped, because a row IS one assignment to one
  // operation. It must print the same fraction Dispatch, the WO routing table and
  // Shop Floor Operations already print for that operation: a running nest reads
  // "0/2 runs", not "0/102" — 102 being the sheet count of all 21 nests on the job.
  //
  // BOTH HALVES MUST COME FROM THE SAME SCOPE. Mixing them is the defect this
  // replaced: an operation numerator over a work-order denominator rendered a nest
  // sitting at 0 of 2 as "0/102 (0%)", which matched neither the nest (0/2) nor the
  // job (38/102). So the two fall back together — an entry with no operation
  // (indirect/setup labor) prints the work order's own pair, never a half of each.
  //
  // The target is server-resolved (`operation_target_quantity`); `operationTargetQuantity`
  // is the shared client mirror of that same rule. The fallback covers DEPLOY SKEW —
  // this SPA ships to Vercel and the API to Railway independently, so this build can be
  // live against a backend that does not send the field yet — and it degrades to the
  // work-order figure, i.e. exactly the pre-fix behavior. Not the ETag cache, which is
  // an in-memory Map that starts empty every page load and so can never hold a payload
  // older than the bundle reading it. Keep the fallback: without it, a frontend that
  // ships ahead of the backend renders 0/102 again, and no test catches it.
  const hasOperation = assignment.operation?.id != null;
  const nest = assignment.operation.laser_nest;
  const operationName = nest
    ? [nest.cnc_number ? `CNC# ${nest.cnc_number}` : nest.cnc_file_name, nest.nest_name].filter(Boolean).join(' · ')
    : assignment.operation.name;
  const orderedQty = hasOperation
    ? Number(
        assignment.operation.quantity_ordered ??
          operationTargetQuantity(assignment.operation, assignment.work_order.quantity_ordered)
      )
    : Number(assignment.work_order.quantity_ordered || 0);
  const completeQty = hasOperation
    ? Number(assignment.operation.quantity_complete || 0)
    : Number(assignment.work_order.quantity_complete || 0);
  // Unclamped percent, clamped bar — the ShopFloorSimple convention for this same
  // figure. It matters more now that the denominator is one nest's sheet count
  // rather than the whole job's: over-completion is reachable at this scale, and a
  // clamped "3/2 (100%)" would contradict the raw fraction printed beside it.
  const progress = orderedQty > 0 ? Math.round((completeQty / orderedQty) * 100) : 0;
  const dueDate = assignment.work_order.due_date;
  const isOverdue = Boolean(dueDate && isDateBeforeTodayInCentral(dueDate));

  // Fields not shown inline (full name, employee id, entry type, due date,
  // customer, priority, part name) are preserved in the row title so no data
  // is lost from the compact two-line layout.
  const rowTitle = [
    assignment.user.employee_id
      ? `${assignment.user.name || getOperatorDisplayName(assignment.user)} (${assignment.user.employee_id})`
      : assignment.user.name || getOperatorDisplayName(assignment.user),
    getEntryTypeLabel(assignment.entry_type),
    `Started ${formatCentralTime(assignment.clock_in)}`,
    // The WHOLE-JOB figure is kept, but EXPLICITLY LABELLED so it can never be read
    // as this operation's progress — the inline fraction beside the bar is the
    // operation's and stays the operation's.
    //
    // Only when the two actually DIFFER. On an ordinary routing operation the
    // operation target IS the work order's quantity_ordered, so the segment would
    // repeat the inline fraction verbatim on the majority of non-laser rows; and on a
    // row with no operation the inline fraction already IS the work order's pair.
    // It earns its place on exactly the rows that provoked this fix — a nest showing
    // 0/2 whose job is really 38/102.
    orderedQty !== Number(assignment.work_order.quantity_ordered || 0)
      ? `Work order total ${Number(assignment.work_order.quantity_complete || 0)}/${Number(
          assignment.work_order.quantity_ordered || 0
        )}`
      : null,
    `Due ${dueDate ? formatCentralDate(dueDate, { year: undefined }) : 'none'}`,
    assignment.work_order.customer_name || null,
    assignment.work_order.priority ? `Priority ${assignment.work_order.priority}` : null,
    assignment.work_order.part_name || null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div id={`assign-${assignment.time_entry_id}`} className="px-4 py-1.5 scroll-mt-10" title={rowTitle}>
      {/* Line 1: operator identity + elapsed time */}
      <div className="flex items-center gap-1.5">
        {isOverdue && <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-fd-red" aria-label="Overdue" />}
        <span className="truncate font-medium text-slate-200">{getOperatorDisplayName(assignment.user)}</span>
        <span
          className={`flex-shrink-0 rounded-sm px-1 py-0.5 text-[10px] font-medium ${getRoleBadgeClass(assignment.user.role)}`}
        >
          {assignment.user.role.replace('_', ' ')}
        </span>
        <span className="ml-auto flex-shrink-0 text-xs text-slate-400 tabular-nums">
          {formatElapsed(assignment.clock_in, nowMs)}
        </span>
      </div>
      {/* Line 2: job identity + progress */}
      <div className="mt-0.5 flex items-center gap-2">
        {canOpenWorkOrder ? (
          <Link
            to={`/work-orders/${assignment.work_order.id}`}
            className="flex-shrink-0 text-xs font-semibold text-fd-link hover:text-sky-200"
          >
            {assignment.work_order.work_order_number}
          </Link>
        ) : (
          <span className="flex-shrink-0 text-xs font-semibold text-slate-300">
            {assignment.work_order.work_order_number}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">
          {assignment.work_order.part_number}
          {hasOperationNumber(assignment.operation.operation_number)
            ? ` · ${formatOperationLabel(assignment.operation.operation_number)}`
            : ''}
        </span>
        <div className="h-1 w-16 flex-shrink-0 rounded-sm bg-slate-800">
          <div className="h-1 rounded-sm bg-werco-500" style={{ width: `${Math.min(100, progress)}%` }} />
        </div>
        <span className="flex-shrink-0 whitespace-nowrap text-[10px] text-slate-400 tabular-nums">
          {completeQty}/{orderedQty || 0} ({progress}%)
        </span>
      </div>
      {hasOperation && (
        <button
          type="button"
          onClick={onOpenOperation}
          aria-label={`View operation details for ${operationName || assignment.work_order.work_order_number}`}
          className="mt-1 flex w-full flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded border border-fd-line px-2 py-1.5 text-left text-xs text-fd-link hover:bg-slate-800/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-fd-blue"
        >
          <span className={`min-w-0 break-words font-semibold ${nest ? 'font-mono' : ''}`}>
            {operationName || 'Operation details'}
          </span>
          <span className="shrink-0 text-[11px]">View operation →</span>
        </button>
      )}
    </div>
  );
}
