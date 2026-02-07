# Werco ERP Operator Training Guide

Version: 1.0  
Audience: Operators and team leads running day-to-day work on the shop floor  
Scope: Every operator-facing function in kiosk and standard shop-floor workflows

## 1. Training Objectives

After training, an operator should be able to:

1. Log in and use kiosk mode safely.
2. Find highest-priority work quickly.
3. Start, hold, resume, and complete operations correctly.
4. Record production quantity and notes accurately.
5. Use operation details, instructions, and traceability views to avoid errors.

## 2. What Operators Can Do

Typical operator permissions:

1. View work orders, parts, BOMs, routings, inventory, quality, analytics.
2. Complete work-order/operation execution tasks.
3. No engineering master-data edits by default.

Reference:

1. `frontend/src/utils/permissions.ts`
2. `docs/RBAC_PERMISSIONS.md`

## 3. Login and Start of Shift

## 3.1 Login

1. Open app URL.
2. Sign in with assigned account.
3. If kiosk mode is enabled, operator routes to `/shop-floor/operations?kiosk=1`.

## 3.2 Start-of-Shift Checklist

1. Confirm correct work center is selected.
2. Review `Most Important Next` queue.
3. Set filters for `Actionable only`, status (`pending`, `ready`, `in_progress`, `on_hold`), and search by WO/part as needed.
4. Open operation details before first start on a new part.

## 4. Shop Floor Operations (Primary Screen)

This is the main operator execution page.

## 4.1 Understand Queue Prioritization

`Most Important Next` is ranked by:

1. Overdue/due pressure
2. Work order priority (`P1` highest, `P10` lowest)
3. Dispatch scoring and due date tie-breakers

How to use it:

1. Run top queue items first unless supervisor gives override.
2. If you must deviate, add notes when completing steps.

## 4.2 Find Work Fast

Use filters and search:

1. Work center dropdown
2. Status dropdown
3. Due today toggle
4. Actionable-only toggle
5. Text search for WO number or part number

## 4.3 Start an Operation

1. Locate operation card with status `pending` or `ready`.
2. Click `Start Operation` (`Check In` on mobile text).
3. Confirm status changes to `in_progress`.
4. Verify instructions in details modal if needed.

If start fails:

1. Previous operation may not be complete.
2. Operation may be held or blocked.
3. Ask supervisor to check routing sequence and schedule status.

## 4.4 Put Work on Hold

1. On an `in_progress` operation, click hold button.
2. Confirm status changes to `on_hold`.
3. Communicate hold reason to lead/supervisor immediately.

## 4.5 Resume Held Work

1. Find operation with `on_hold`.
2. Click `Resume`.
3. Confirm status returns to active workflow.

## 4.6 Complete or Partially Complete

1. On an `in_progress` operation, click `Mark Complete` (`Check Out` on mobile text).
2. In modal, enter `Quantity Complete`, add notes for issues/scrap/setup observations, then submit.

Rules:

1. If entered quantity is less than ordered, operation remains in progress with updated progress.
2. If quantity reaches ordered quantity, operation is marked complete.

## 4.7 View Operation Details

Use the eye/details button to review:

1. Work order header and part info.
2. Current operation and status.
3. Setup instructions.
4. Run instructions.
5. All operation sequence statuses.
6. Recent history events.

Use this before setup and after any handoff.

## 5. Shop Floor Time Clock Page (If Enabled)

Some teams use `Shop Floor` queue page with explicit clock-in/out.

## 5.1 Clock In

1. Select work center.
2. In queue row, click `Start`.
3. Confirm active-job banner appears.

## 5.2 Clock Out

1. Open active job banner.
2. Click `Clock Out`.
3. Enter quantity produced, quantity scrapped, and notes (recommended).
4. Submit to close time entry.

## 6. Priority Handling on the Floor

If your role includes priority edit permission:

1. Use priority dropdown on row/card.
2. Set `P1` to `P10`.
3. Optional: add `Priority Reason` before change.
4. Reason applies to next priority update and is logged for traceability.

If your role does not include permission:

1. Escalate to supervisor/manager to change priority.

## 7. Work Order Detail and Traveler Use

From operation details or WO links:

1. Open full work order detail page.
2. Verify due date, customer PO, and notes.
3. Review operation list and statuses.
4. Confirm material requirements for assembly jobs.
5. Use traveler printouts where your cell requires hard-copy routing packets.

## 8. Quality, Traceability, and Good Data Habits

Operator expectations:

1. Always log meaningful notes for abnormal conditions.
2. Do not bypass hold/resume flow for nonconforming conditions.
3. Use lot/serial traceability search when directed by quality or lead.
4. Report scrap and rework accurately at time of occurrence.

## 9. End-of-Shift Checklist

1. Ensure no operation is left `in_progress` unintentionally.
2. Add notes to any partially completed operation.
3. Put blocked work on hold with handoff note to next shift.
4. Confirm active time entries are closed if required by your area.
5. Notify lead of urgent overdue items still open.

## 10. Operator Troubleshooting

## 10.1 Cannot Start Operation

1. Confirm status is `pending` or `ready`.
2. Check whether earlier sequence is incomplete.
3. Refresh and retry.
4. Escalate to supervisor for sequence/scheduling fix.

## 10.2 Completion Rejected

1. Verify quantity is numeric and non-negative.
2. Ensure quantity does not exceed expected totals unless instructed.
3. Retry with notes.
4. Escalate if persistent.

## 10.3 Queue Looks Wrong

1. Confirm selected work center.
2. Clear filters/search.
3. Use refresh button.
4. If still wrong, ask supervisor to verify scheduling and work center assignment.

## 10.4 Priority Looks Incorrect

1. Do not self-resequence outside policy.
2. Raise to supervisor with WO number and reason.

## 11. Live Training Drills for Operators

Drill A: Normal flow

1. Find WO by search.
2. Start operation.
3. Record partial completion.
4. Resume and complete full quantity.

Drill B: Exception flow

1. Start operation.
2. Put on hold for issue.
3. Add note context.
4. Resume after approval.
5. Complete with final note.

Drill C: Instruction-driven setup

1. Open operation details.
2. Read setup/run instructions out loud.
3. Execute first article run.
4. Log setup observations in notes.

## 12. Function Coverage Appendix (Operator-Facing)

Functions used in operator execution pages:

1. `ShopFloorSimple`: completeOperation, getDashboardWithCache, getOperationDetails, getShopFloorOperations, getWorkCenters, holdOperation, resumeOperation, startOperation, updateWorkOrderPriority
2. `ShopFloor`: clockIn, clockOut, getMyActiveJob, getWorkCenterQueue, getWorkCenters, getWorkOrder, updateWorkOrderPriority
3. `WorkOrderDetail`: completeWOOperation, completeWorkOrder, getActiveUsers, getMaterialRequirements, getUsers, getWorkOrder, releaseWorkOrder, startWorkOrder
4. `Traceability`: searchLots, traceLot
5. `Dashboard` (view metrics): getDashboardWithCache, getEquipmentDueSoon, getLowStockAlerts, getQualitySummary

Read-only reference pages frequently used by leads/operators:

1. `WorkOrders`: getWorkOrders
2. `Parts`: getParts, getBOMByPart, getRoutings
3. `Inventory`: getInventory, getInventorySummary, getLowStockAlerts
