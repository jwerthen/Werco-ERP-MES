import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { OperationHold, User, WorkOrder, WorkOrderOperation, LaserNestInfo, WorkCenter } from '../types';
import { WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerSeverity } from '../types/aiForward';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import LaserNestManualModal from '../components/laser/LaserNestManualModal';
import LaserNestImportWizard from '../components/laser/LaserNestImportWizard';
import LaserNestPdfPreview from '../components/laser/LaserNestPdfPreview';
import { CompleteWorkModal, CompleteWorkSubmit } from '../components/workorders/CompleteWorkModal';
import MaterialTiesPanel from '../components/workorders/MaterialTiesPanel';
import OperationMaterialTieModal from '../components/workorders/OperationMaterialTieModal';
import DuplicateWorkOrderModal from '../components/workorders/DuplicateWorkOrderModal';
import SaveAsTemplateModal from '../components/workorders/SaveAsTemplateModal';
import BackflushPreviewPanel from '../components/workorders/BackflushPreviewPanel';
import OperationStepsPanel from '../components/processSheets/OperationStepsPanel';
import {
  extractStepsBypassed,
  extractStepsIncomplete,
  stepsBypassedMessage,
  stepsIncompleteMessage,
} from '../utils/processSheetErrors';
import { Breadcrumbs } from '../components/ui/Breadcrumbs';
import { getBreadcrumbParent } from '../utils/routeMeta';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { ContextualAIStrip } from '../components/ai';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  useToast,
  statusColor,
  Button,
  FormField,
  InputDialog,
  LoadingButton,
  Modal,
  Spinner,
  UnitBadge,
} from '../components/ui';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { formatCentralDate, formatCentralDateTime, getCentralDateStamp } from '../utils/centralTime';
import { formatOperationLabel, hasOperationNumber } from '../utils/operationLabel';
// The held-operation vocabulary the two kiosks already speak. Imported rather
// than re-derived so the office page and the floor name one hold the same way:
// same category labels, same "Held by Dana R. · <Central time>" attribution line,
// same independence of reason from attribution. Pure helpers over the shared
// `OperationHold` / `ResumeOperationResult` shapes -- no kiosk runtime is pulled in.
import {
  clearHoldOutcome,
  formatHoldAttribution,
  holdIsUnexplained,
  holdReasonLabel,
  holdSeverityLabel,
  holdTitleText,
  openBlockerLine,
} from '../components/kiosk/heldOperations';
import { sortWorkCentersForLaserDispatch } from '../utils/laserWorkCenters';
import {
  ArrowLeftIcon,
  ArrowDownTrayIcon,
  ArrowPathIcon,
  PlayIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PrinterIcon,
  CubeIcon,
  TrashIcon,
  ArrowUpTrayIcon,
  DocumentTextIcon,
  EyeIcon,
  PaperClipIcon,
  PlusIcon,
  PencilSquareIcon,
  CheckIcon,
  CalendarDaysIcon,
  FlagIcon,
  MinusCircleIcon,
  XMarkIcon,
  BuildingOffice2Icon,
  HashtagIcon,
  IdentificationIcon,
  ClockIcon,
  ChartBarIcon,
  ClipboardDocumentCheckIcon,
  UserGroupIcon,
  WrenchScrewdriverIcon,
  DocumentDuplicateIcon,
  BookmarkSquareIcon,
  InformationCircleIcon,
} from '@heroicons/react/24/outline';

const CURRENT_WORK_ORDER_STATUSES = ['released', 'in_progress', 'on_hold'];

/**
 * Cap for the inline Notes / Special Instructions editor.
 *
 * Mirrors `WorkOrderUpdate.notes` / `.special_instructions`
 * (`Field(None, max_length=2000)` in backend/app/schemas/work_order.py). Kept in
 * sync deliberately: without it an over-long note comes back as a raw 422 after
 * the planner has already typed it.
 */
const WO_NOTE_MAX_LENGTH = 2000;
// Mirrors WorkOrderUpdate.unit_number (Field(None, max_length=50)) for the same
// reason WO_NOTE_MAX_LENGTH mirrors the notes cap: without it an over-long value
// comes back as a raw 422 after the round trip instead of being refused at the key.
const WO_UNIT_NUMBER_MAX_LENGTH = 50;

/**
 * Which fields a concurrent editor moved since an inline editor opened, named
 * for the lost-update dialog — "notes", "due date", "notes and special
 * instructions". Named rather than generic so the planner knows where to look
 * before deciding to overwrite someone else's work.
 *
 * Both maps are keyed by the human label. Values are compared trimmed, matching
 * what the save writes, so whitespace alone never reads as someone else's edit.
 * `fallback` covers the caller that detected a difference some other way.
 */
// Labels the lost-update dialog uses to name a field. Consts, not inline
// literals: describeChangedFields keys both maps by label, so the same string
// spelled two ways would make that field read as permanently changed.
const NOTES_LABEL = 'notes';
const INSTRUCTIONS_LABEL = 'special instructions';
const DUE_DATE_LABEL = 'due date';
const UNIT_NUMBER_LABEL = 'unit #';

function describeChangedFields(
  current: Record<string, string>,
  baseline: Record<string, string>,
  fallback: string
): string {
  const changed = Object.keys(current).filter(
    (label) => (current[label] ?? '').trim() !== (baseline[label] ?? '').trim()
  );
  return changed.join(' and ') || fallback;
}

/**
 * How many runs this operation is expected to produce — nest planned runs, else
 * its own component quantity, else the work order's ordered quantity.
 *
 * Extracted so the Qty column and the material-tie editor's default planned
 * total cannot drift apart: the tie's plan figure is `qty_per_run × runs`, and a
 * different `runs` in the two places would show a planner one number and tie
 * another.
 */
function operationRunTarget(op: WorkOrderOperation, quantityOrdered: number | null | undefined): number {
  return Number(op.laser_nest?.planned_runs || op.component_quantity || quantityOrdered || 0);
}

interface MaterialRequirement {
  bom_item_id: number;
  item_number: number;
  part_id: number;
  part_number: string;
  part_name: string;
  part_type: string;
  quantity_per_assembly: number;
  quantity_required: number;
  scrap_factor: number;
  scrap_allowance: number;
  total_required: number;
  unit_of_measure: string;
  item_type: string;
  is_optional: boolean;
  notes: string | null;
}

interface MaterialRequirementsResponse {
  work_order_id: number;
  work_order_number: string;
  quantity_ordered: number;
  has_bom: boolean;
  bom_id?: number;
  bom_revision?: string;
  materials: MaterialRequirement[];
}

interface ActiveShopUser {
  user_id: number;
  user_name?: string;
  work_order_number?: string;
  operation?: string;
  work_center?: string;
  clock_in?: string;
  entry_type?: string;
}

interface WorkOrderDocument {
  id: number;
  document_number: string;
  revision: string;
  title: string;
  document_type: string;
  description?: string | null;
  part_id?: number | null;
  work_order_id?: number | null;
  vendor_id?: number | null;
  file_name?: string | null;
  file_size?: number | null;
  mime_type?: string | null;
  status: string;
  created_at: string;
}

const formatDateTimeCT = (value?: string) =>
  formatCentralDateTime(value, { timeZoneName: 'short' });

/**
 * "Op 20" for an operation, falling back to its sequence when the routing never
 * got an operation number. Same expression the Report-Blocker picker uses; named
 * so the Clear Hold copy and that picker cannot drift apart.
 */
const operationLabel = (op: WorkOrderOperation): string =>
  hasOperationNumber(op.operation_number) ? formatOperationLabel(op.operation_number) : `Op ${op.sequence}`;

/**
 * The lines that answer "why is this held?", in reading order.
 *
 * Shared by the compact in-row disclosure and the full Clear Hold dialog so the
 * two can never say different things about one hold.
 *
 * TWO RULES HERE ARE CORRECTNESS, NOT COPY:
 *
 * 1. **Reason and attribution are INDEPENDENT.** A BARE hold (no note, category
 *    OTHER) -- exactly the accidental fat-finger case -- files no blocker at all,
 *    so `blocker` is null while `held_by_name` / `held_at` still carry provenance.
 *    Gating one on the other makes the mis-tap render as both anonymous and
 *    reasonless, the single case that most needs to read as an accident.
 * 2. **Free text is read DIRECTLY off `note` / `title`, never behind `has_note`.**
 *    The work-order response deliberately omits `has_note` (nothing is withheld
 *    from an identified office session, so the flag is redundant here), and an
 *    absent flag is falsy -- a `has_note` gate would print "no reason recorded"
 *    over a hold that has a written one.
 *
 * All-null is a REAL state, not an error: the server reports what was recorded
 * and never infers a holder from `operation.updated_at`.
 */
interface HoldSummary {
  /** "Machine down · High" -- category and severity. Null when no blocker is open. */
  headline: string | null;
  /** The blocker's own title, dropped when it is only restating the headline. */
  title: string | null;
  /** The operator's written note, verbatim. */
  note: string | null;
  /** "Held by Dana R. · Aug 11, 2026, 2:14 PM" -- Central, via the shared formatter. */
  attribution: string | null;
  /** Nothing was recorded at all: no open blocker AND no holder. */
  unexplained: boolean;
}

/**
 * ABSENT is not the same claim as EMPTY, and conflating them puts a false
 * statement on a quality record. Returns null when the block did not arrive.
 *
 * `hold_contexts_for_operations` gives EVERY id it is asked about a key -- an
 * operation with neither a blocker nor a hold event maps to an ALL-NULL
 * `HoldContext`, not to a missing one -- and `_enrich_work_order_operations`
 * passes every ON_HOLD row through it. So on a served response an `on_hold` row
 * ALWAYS carries an object, and `hold_context == null` can only mean the block
 * did not arrive. Two reachable paths produce that:
 *
 *  - `hydrateOperationsFromShopFloor` overwrites `status` from
 *    `GET /shop-floor/operations/{id}`, whose `all_operations` rows carry no hold
 *    block, so a row that only reads `on_hold` AFTER hydration still holds the
 *    work-order read's null; or
 *  - SPA/API deploy skew, before the field ships.
 *
 * Returning null (rather than an all-null summary) is what stops both cases
 * rendering as the affirmative "On hold -- reason not recorded" / "Who placed the
 * hold was not recorded". That is a claim no reason was ever WRITTEN, printed
 * directly above the control that lifts an AS9100D hold, over a hold that may
 * have an open NCR-driven blocker behind it. Same posture as the shop floor's
 * `<OperationHoldReason>`, which renders nothing when `hold` is absent.
 *
 * The genuinely-recorded-nothing case still reports as such, and still reads as
 * the mis-tap it usually is -- it just has to come from a block the server sent.
 */
function summarizeHold(hold?: OperationHold | null): HoldSummary | null {
  if (!hold) return null;
  const blocker = hold?.blocker ?? null;
  const category = holdReasonLabel(blocker?.category);
  const severity = holdSeverityLabel(blocker?.severity);
  const headline = [category, severity].filter(Boolean).join(' \u00b7 ') || null;
  return {
    headline,
    // Echo suppression (a title that only restates the category chip) lives in the
    // shared `holdTitleText`, so this panel and the shop-floor `OperationHoldReason`
    // cannot decide differently about the same blocker.
    title: holdTitleText(hold),
    note: (blocker?.note || '').trim() || null,
    attribution: formatHoldAttribution(hold),
    unexplained: holdIsUnexplained(hold),
  };
}

/**
 * The Clear Hold confirm body: why it is held, who held it, and the two things
 * clearing it does NOT do.
 *
 * A plain string because `ConfirmDialog.message` renders `whitespace-pre-line` --
 * the blank lines below are load-bearing paragraph breaks, not padding.
 *
 * The second paragraph is the honesty the reported bug turned on: clearing a hold
 * and closing a blocker are decoupled server-side (the resume endpoint lifts the
 * status and hands back whatever blockers are still open), so a dialog that
 * implied otherwise would let a live quality stop read as cleared.
 */
function clearHoldMessage(op: WorkOrderOperation, workOrderNumber: string): string {
  const hold = summarizeHold(op.hold_context);
  const lines: string[] = [`${workOrderNumber} \u00b7 ${operationLabel(op)} \u2014 ${op.name}`, ''];

  if (!hold) {
    // The block did not arrive (see `summarizeHold`) -- which is NOT the same as
    // "nothing was recorded", and must not be worded as it. Say the reason could
    // not be loaded and give the reader a way to get it, so nobody lifts a hold
    // believing this screen already told them there was no reason behind it.
    lines.push('Why it is held: could not be loaded. Refresh the page to see the reason before clearing it.');
  } else if (hold.unexplained) {
    lines.push(
      'Why it is held: not recorded. No blocker is open on it, and no one was recorded placing the hold.'
    );
  } else {
    lines.push(`Why it is held: ${hold.headline ?? 'no open blocker explains it'}`);
    if (hold.title) lines.push(hold.title);
    if (hold.note) lines.push(`\u201c${hold.note}\u201d`);
    lines.push(hold.attribution ?? 'Who placed the hold was not recorded.');
  }

  lines.push('');
  lines.push(
    'Clearing the hold does NOT close the blocker. It stays open until a supervisor or manager resolves it in the Blockers panel below.'
  );
  lines.push('');
  lines.push(
    'The operation goes back to where it was. If nobody has clocked time on it yet it returns to Pending, and it will not show on the dispatch board or at the kiosk until the work order is released and any earlier operations are finished.'
  );
  return lines.join('\n');
}

const isPdfDocument = (document: WorkOrderDocument) =>
  document.mime_type === 'application/pdf' || Boolean(document.file_name?.toLowerCase().endsWith('.pdf'));

const formatFileSize = (bytes?: number | null) => {
  if (!bytes) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const operationProgressKey = (op: WorkOrderOperation) => {
  if (op.sequence !== undefined && op.sequence !== null) {
    return `sequence|${Number(op.sequence)}`;
  }
  const operationNumber = String(op.operation_number || '').replace(/\D/g, '');
  if (operationNumber) {
    return `operation_number|${operationNumber}`;
  }
  const name = (op.name || '').trim().toLowerCase().replace(/\s+/g, ' ');
  return [
    op.work_center_id || '',
    op.component_part_id || '',
    op.operation_group || '',
    name || op.operation_number || op.sequence || op.id,
  ].join('|');
};

const getOperationProgressMetrics = (workOrder: WorkOrder) => {
  const operations = workOrder.operations || [];
  if (operations.length === 0) {
    const ordered = Number(workOrder.quantity_ordered || 0);
    const complete = Number(workOrder.quantity_complete || 0);
    return {
      operation_count: 0,
      operations_complete: 0,
      percent: ordered > 0 ? Math.min(100, Math.max(0, (complete / ordered) * 100)) : 0,
      label: `${complete}/${ordered}`,
    };
  }

  const progressByKey = new Map<string, number>();
  const completeByKey = new Map<string, boolean>();
  operations.forEach((op) => {
    const target = Number(op.component_quantity || workOrder.quantity_ordered || 0);
    const complete = Number(op.quantity_complete || 0);
    const hasCompletionEvidence = op.status === 'complete' || Boolean(op.actual_end && op.completed_by);
    const ratio = hasCompletionEvidence
      ? 1
      : target > 0
        ? Math.min(1, Math.max(0, complete / target))
        : 0;
    const key = operationProgressKey(op);
    progressByKey.set(key, Math.max(progressByKey.get(key) || 0, ratio));
    completeByKey.set(key, Boolean(completeByKey.get(key)) || hasCompletionEvidence);
  });

  const operationCount = progressByKey.size;
  const operationsComplete = Array.from(completeByKey.values()).filter(Boolean).length;
  const progressTotal = Array.from(progressByKey.values()).reduce((sum, ratio) => sum + ratio, 0);
  const percent = operationCount > 0 ? Math.round((progressTotal / operationCount) * 1000) / 10 : 0;

  return {
    operation_count: operationCount,
    operations_complete: operationsComplete,
    percent,
    label: `${operationsComplete}/${operationCount} ops`,
  };
};

const syncOperationProgressSummary = (workOrder: WorkOrder): WorkOrder => {
  const metrics = getOperationProgressMetrics(workOrder);
  return {
    ...workOrder,
    operation_count: metrics.operation_count,
    operations_complete: metrics.operations_complete,
    operation_progress_percent: metrics.percent,
  };
};

const getDetailWorkOrderProgress = (workOrder: WorkOrder) => getOperationProgressMetrics(workOrder);

const hydrateOperationsFromShopFloor = async (workOrder: WorkOrder): Promise<WorkOrder> => {
  const firstOperationId = workOrder.operations?.[0]?.id;
  if (!firstOperationId) return syncOperationProgressSummary(workOrder);

  try {
    const details = await api.getOperationDetails(firstOperationId);
    const liveOperations = Array.isArray(details?.all_operations) ? details.all_operations : [];
    if (liveOperations.length === 0) return syncOperationProgressSummary(workOrder);

    const liveById = new Map<number, Partial<WorkOrderOperation>>(
      liveOperations.map((op: Partial<WorkOrderOperation> & { id: number }) => [op.id, op])
    );
    return syncOperationProgressSummary({
      ...workOrder,
      operations: workOrder.operations.map((op) => {
        const liveOp = liveById.get(op.id);
        if (!liveOp) return op;

        return {
          ...op,
          status: liveOp.status ?? op.status,
          quantity_complete: liveOp.quantity_complete ?? op.quantity_complete,
          quantity_scrapped: liveOp.quantity_scrapped ?? op.quantity_scrapped,
          actual_setup_hours: liveOp.actual_setup_hours ?? op.actual_setup_hours,
          actual_run_hours: liveOp.actual_run_hours ?? op.actual_run_hours,
          actual_start: liveOp.actual_start ?? op.actual_start,
          actual_end: liveOp.actual_end ?? op.actual_end,
          started_by: liveOp.started_by ?? op.started_by,
          completed_by: liveOp.completed_by ?? op.completed_by,
          laser_nest: liveOp.laser_nest ?? op.laser_nest,
        };
      }),
    });
  } catch {
    return syncOperationProgressSummary(workOrder);
  }
};

export default function WorkOrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { showToast } = useToast();
  const isAdminView = user?.role === 'admin' || !!user?.is_superuser;
  // Soft-deleting a WO is admin/manager (plus superuser) — the backend widened
  // the DELETE gate to include manager. Kept separate from isAdminView so only
  // the Delete button opens to managers; other admin-only chrome is unchanged.
  const canDeleteWorkOrder = user?.role === 'admin' || user?.role === 'manager' || !!user?.is_superuser;
  // Manual laser-nest manage actions are limited to admin/manager/supervisor —
  // the same trio the backend RBAC allows (routings:create maps to exactly that
  // set plus platform_admin).
  const canManageNests = hasPermission(user?.role, 'routings:create');
  // Office over-count correction is a supervisor-tier action: work_orders:edit
  // maps to exactly admin/manager/supervisor. The backend enforces RBAC too —
  // this only keeps the UI honest about what the server will allow.
  const canCorrectCount = hasPermission(user?.role, 'work_orders:edit') || !!user?.is_superuser;
  // Inline notes / special-instructions edit — same work_orders:edit tier, and
  // the same endpoint (PUT /work-orders/{id}, require_role ADMIN/MANAGER/
  // SUPERVISOR). Named separately so the gate reads at its call site.
  const canEditNotes = canCorrectCount;
  const canEditUnitNumber = canCorrectCount;
  // Material-tie PATCH/untie is require_role([ADMIN, MANAGER, SUPERVISOR]) on
  // the backend — the same tier work_orders:edit maps to. Reads are open to any
  // authenticated tenant user, so the panel itself is not gated.
  const canEditMaterialTies = canCorrectCount;
  // Office operation-complete mirrors the backend gate, which is
  // require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY]) — the same set as
  // complete_work_order, its larger sibling. QUALITY is included deliberately:
  // it can complete an entire work order, so refusing it a single operation
  // would be incoherent. It matters more than it looks — completing an
  // operation now consumes tied material, so this button moves stock and writes
  // hash-chain rows. Operators complete work from the shop floor / kiosk.
  const canCompleteOperation = canCorrectCount || user?.role === 'quality';
  // Duplicate is require_role([ADMIN, MANAGER, SUPERVISOR]) on the backend —
  // the same trio work_orders:edit maps to. Named separately from
  // canCorrectCount so the gate reads at its call site: hiding the control and
  // refusing the call must agree, or a supervisor sees a button that 403s.
  const canDuplicateWorkOrder = canCorrectCount;
  // Saving a template is the same backend trio (require_role ADMIN/MANAGER/
  // SUPERVISOR on every /work-order-templates verb), which is exactly what
  // work_orders:edit maps to. Named separately so the gate reads at its call
  // site — a hidden control and a refused call have to agree.
  const canSaveAsTemplate = canCorrectCount;
  // Flipping the operation-sequencing mode writes through the same
  // PUT /work-orders/{id} the due-date and notes editors use, whose backend gate
  // is require_role([ADMIN, MANAGER, SUPERVISOR]) — exactly what work_orders:edit
  // maps to. Named separately so the gate reads at its call site: hiding the
  // control and refusing the call have to agree, or a supervisor sees a switch
  // that 403s.
  const canEditSequencing = canCorrectCount;
  // Closing a blocker is ADMIN/MANAGER/SUPERVISOR on the backend
  // (`POST /work-order-blockers/{id}/resolve`, require_role([ADMIN, MANAGER,
  // SUPERVISOR])) -- exactly what work_orders:edit maps to. Until this gate
  // existed the Resolve button rendered for EVERY role, so an operator or a
  // viewer looking at a stuck job was offered the one control that would 403 on
  // them, and nothing on the page offered the one that would work. Named
  // separately from canCorrectCount so the gate reads at its call site: a hidden
  // control and a refused call have to agree. Clearing the HOLD is deliberately
  // NOT gated with it -- `PUT /shop-floor/operations/{id}/resume` takes
  // `get_current_user`, any authenticated tenant user, which is why the copy
  // under the hidden button points there.
  const canResolveBlocker = canCorrectCount;
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);

  // Inline due-date edit shares the same work_orders:edit tier, AND is refused on a
  // finished job: `PUT /work-orders/{id}` returns 409 when a due date would change on
  // a COMPLETE/CLOSED/CANCELLED work order, because that date is the promise date its
  // delivery performance was scored against. The server is the enforcement; this keeps
  // the pencil from offering an edit it will reject. The work-order list gates the
  // same way. Note this is due-date-specific — `notes` / `special_instructions` /
  // `unit_number` deliberately stay editable at any status, so their editors are
  // gated on the permission alone.
  const canEditDueDate =
    canCorrectCount && !['complete', 'closed', 'cancelled'].includes(workOrder?.status ?? '');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [duplicateOpen, setDuplicateOpen] = useState(false);
  const [saveTemplateOpen, setSaveTemplateOpen] = useState(false);
  // In-flight flag for the "Sequential operations" switch. It ONLY drives the
  // pending chrome — the switch's checked state is read from the server value,
  // never from a local draft (see handleSequencingToggle).
  const [savingSequencing, setSavingSequencing] = useState(false);
  const [deleteNestTarget, setDeleteNestTarget] = useState<LaserNestInfo | null>(null);
  const [completing, setCompleting] = useState(false);
  const [completingOpId, setCompletingOpId] = useState<number | null>(null);
  // Which operation's read-only "Process steps" evidence panel is expanded
  // (one at a time — it fetches the steps view on open).
  const [stepsOpenOpId, setStepsOpenOpId] = useState<number | null>(null);
  // Per-operation material tie editor. `null` = closed; otherwise the operation
  // every tie created in that dialog is scoped to (operation scope is hard-coded
  // there, not a default — see the modal's docstring).
  const [tieTarget, setTieTarget] = useState<WorkOrderOperation | null>(null);
  // Freshness seam for MaterialTiesPanel. A tie write does NOT bump
  // `work_orders.updated_at`, which is the panel's only other load dependency,
  // so without this a tie created from the Operations table would leave a stale
  // list sitting directly beneath it.
  const [tieRefreshToken, setTieRefreshToken] = useState(0);
  // Drives the CompleteWorkModal: either the work-order-level completion or a
  // specific operation. `null` = closed. The modal collects qty complete + qty
  // scrapped + a scrap reason (required when scrap > 0) before we call the API.
  const [completeTarget, setCompleteTarget] = useState<
    { kind: 'work_order' } | { kind: 'operation'; operation: WorkOrderOperation } | null
  >(null);
  // Supervisor "Correct count" (office reduce-production): the target operation,
  // its small quantity+reason form, and the server refusal rendered INLINE in
  // the modal (verbatim — the whole point of a server-gated correction is that
  // the user reads WHY it was refused). `null` target = closed. Gated below to
  // work_orders:edit (admin/manager/supervisor — operators use the shop floor).
  const [correctTarget, setCorrectTarget] = useState<WorkOrderOperation | null>(null);
  const [correctData, setCorrectData] = useState({ quantity: 1, reason: '' });
  const [correctError, setCorrectError] = useState<string | null>(null);
  const [correctingOpId, setCorrectingOpId] = useState<number | null>(null);
  const [materialReqs, setMaterialReqs] = useState<MaterialRequirementsResponse | null>(null);
  const [blockers, setBlockers] = useState<WorkOrderBlocker[]>([]);
  const [blockerForm, setBlockerForm] = useState<{
    operation_id: string;
    category: WorkOrderBlockerCategory;
    severity: WorkOrderBlockerSeverity;
    note: string;
  }>({
    operation_id: '',
    category: 'material_missing',
    severity: 'high',
    note: '',
  });
  const [submittingBlocker, setSubmittingBlocker] = useState(false);
  const [resolvingBlockerId, setResolvingBlockerId] = useState<number | null>(null);
  const [resolveBlockerTarget, setResolveBlockerTarget] = useState<WorkOrderBlocker | null>(null);
  // Clear Hold (office lift of an ON_HOLD operation). `clearHoldTarget` is the
  // operation the confirm is open for; `clearingHoldOpId` is the in-flight id.
  // NON-OPTIMISTIC by convention: this is a server-GATED action (409 on a
  // cancelled-nest tombstone, 400 when the row is not actually on hold, and the
  // status it lands on is the server's promotion rule to decide, not ours), so
  // nothing on the page moves until the refetch returns.
  const [clearHoldTarget, setClearHoldTarget] = useState<WorkOrderOperation | null>(null);
  const [clearingHoldOpId, setClearingHoldOpId] = useState<number | null>(null);
  const [userNameById, setUserNameById] = useState<Record<number, string>>({});
  const [activeUsersOnWorkOrder, setActiveUsersOnWorkOrder] = useState<ActiveShopUser[]>([]);
  // Batch ZIP import runs through the LaserNestImportWizard modal.
  const [nestImportWizardOpen, setNestImportWizardOpen] = useState(false);
  // Manual nest entry + per-nest PDF management.
  const [nestModalOpen, setNestModalOpen] = useState(false);
  const [nestModalTarget, setNestModalTarget] = useState<LaserNestInfo | null>(null);
  // The operation the edited nest sits on. The nest itself carries no operation
  // id, but material ties are OPERATION-scoped, so without this the modal can't
  // read or write the tie for the nest being edited.
  const [nestModalOperationId, setNestModalOperationId] = useState<number | undefined>(undefined);
  const [previewNestId, setPreviewNestId] = useState<number | null>(null);
  const [nestActionId, setNestActionId] = useState<number | null>(null);
  const [nestActionError, setNestActionError] = useState('');
  const nestAttachInputRef = useRef<HTMLInputElement | null>(null);
  const [nestAttachTargetId, setNestAttachTargetId] = useState<number | null>(null);
  // Inline due-date edit (pencil in the Due Date tile). NON-optimistic — the
  // tile shows only what the server returns after the refresh. `version` is
  // real optimistic locking: the WO endpoint rejects a stale version with 409
  // ("modified by someone else"). On 409 we refetch the work order so the
  // next save attempt carries a fresh version.
  const [dueDateEditing, setDueDateEditing] = useState(false);
  const [dueDateDraft, setDueDateDraft] = useState('');
  const [savingDueDate, setSavingDueDate] = useState(false);
  // What the server held when the due-date editor opened, for the same
  // lost-update guard the notes editor uses — see the notesBaseline comment.
  const [dueDateBaseline, setDueDateBaseline] = useState('');
  // Unit # inline edit — same shape and same lost-update guard as the due date
  // above, for the same reason: `version` cannot carry the guard, because the page
  // refetches on every work_order broadcast and this endpoint emits one.
  const [unitNumberEditing, setUnitNumberEditing] = useState(false);
  const [unitNumberDraft, setUnitNumberDraft] = useState('');
  const [savingUnitNumber, setSavingUnitNumber] = useState(false);
  const [unitNumberBaseline, setUnitNumberBaseline] = useState('');
  // Inline Notes / Special Instructions edit (pencil in the panel header).
  // Deliberately NOT gated on work-order status — draft, released, in progress,
  // on hold, complete, closed and cancelled are all editable. That is the point:
  // the shop-floor instruction worth writing down ("item 80 uses R.375, not the
  // usual R.19") is usually learned AFTER release, and a note is documentation,
  // not production record — it moves no stock and completes no operation. The
  // server agrees: PUT /work-orders/{id} carries no status gate on these two
  // fields (its only 409s are status TRANSITIONS), so hiding the control on a
  // released WO would refuse something the API allows. Gated on work_orders:edit
  // alone, which is exactly the role trio the endpoint enforces.
  // Same posture as the due-date edit: NON-optimistic — the panel renders only
  // what the refetch returns — and the optimistic-lock `version` is sent, so a
  // 409 refetches and keeps the editor open holding the user's draft.
  //
  // But `version` alone does NOT protect this editor, and the lost update it
  // misses is the whole reason `notesBaseline` exists below: this page refetches
  // on every work_order broadcast (`scheduleRealtimeRefresh`), and PUT
  // /work-orders/{id} broadcasts one. So a concurrent planner's note edit
  // quietly advances `workOrder.version` UNDER the open editor, and the next
  // save then returns a clean 200 having erased words this user never saw.
  const [notesEditing, setNotesEditing] = useState(false);
  const [notesDraft, setNotesDraft] = useState('');
  const [instructionsDraft, setInstructionsDraft] = useState('');
  const [savingNotes, setSavingNotes] = useState(false);
  // What the server held when the editor opened — the reference the lost-update
  // guard in handleNotesSave compares against. Seeded by startNotesEdit.
  const [notesBaseline, setNotesBaseline] = useState({ notes: '', special_instructions: '' });
  // Non-null while the lost-update dialog is open, holding which editor hit the
  // conflict and what the server had when we noticed. One dialog serves both
  // inline editors so their concurrency behavior cannot drift apart.
  // `fields` is frozen at detection rather than recomputed for the dialog:
  // Replace adopts the new baseline before the write finishes, which would leave
  // the still-open dialog describing a difference that no longer exists while
  // its spinner runs.
  const [fieldConflict, setFieldConflict] = useState<
    | { kind: 'notes'; fields: string; notes: string; special_instructions: string }
    | { kind: 'due_date'; fields: string; due_date: string }
    | { kind: 'unit_number'; fields: string; unit_number: string }
    | null
  >(null);
  // Active work centers (laser-first order) for the per-nest reassign selects,
  // loaded once when the laser card is manageable and has nest ops.
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [reassigningOpId, setReassigningOpId] = useState<number | null>(null);
  const [workOrderDocuments, setWorkOrderDocuments] = useState<WorkOrderDocument[]>([]);
  const [availablePdfDocuments, setAvailablePdfDocuments] = useState<WorkOrderDocument[]>([]);
  const [documentUploadFile, setDocumentUploadFile] = useState<File | null>(null);
  const [documentTitle, setDocumentTitle] = useState('');
  const [attachDocumentId, setAttachDocumentId] = useState('');
  const [documentBusy, setDocumentBusy] = useState(false);
  const [documentError, setDocumentError] = useState('');
  const [documentUploadInputKey, setDocumentUploadInputKey] = useState(0);
  const [selectedDocumentId, setSelectedDocumentId] = useState<number | null>(null);
  const [documentPreviewUrl, setDocumentPreviewUrl] = useState<string | null>(null);
  const [documentPreviewLoading, setDocumentPreviewLoading] = useState(false);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const loadRequestRef = useRef(0);
  const documentPreviewObjectUrlRef = useRef<string | null>(null);
  const workOrderId = useMemo(() => (id ? parseInt(id, 10) : null), [id]);
  const realtimeUrl = useMemo(() => {
    if (!id) return null;
    const token = getAccessToken();
    if (!token) return null;
    return buildWsUrl(`/ws/work-order/${id}`, { token });
  }, [id]);

  const replaceDocumentPreviewUrl = useCallback((url: string | null) => {
    if (documentPreviewObjectUrlRef.current) {
      window.URL.revokeObjectURL(documentPreviewObjectUrlRef.current);
    }
    documentPreviewObjectUrlRef.current = url;
    setDocumentPreviewUrl(url);
  }, []);

  const loadWorkOrder = useCallback(async () => {
    if (!id) return;
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;
    const currentWorkOrderId = parseInt(id, 10);

    try {
      setError('');
      const response = await api.getWorkOrder(currentWorkOrderId);
      if (requestId !== loadRequestRef.current) return;
      const hydratedWorkOrder = await hydrateOperationsFromShopFloor(response);
      if (requestId !== loadRequestRef.current) return;
      setWorkOrder(hydratedWorkOrder);
      
      // Load material requirements
      try {
        const matReqs = await api.getMaterialRequirements(currentWorkOrderId);
        if (requestId !== loadRequestRef.current) return;
        setMaterialReqs(matReqs);
      } catch {
        if (requestId !== loadRequestRef.current) return;
        // Material requirements may not exist for all parts
        setMaterialReqs(null);
      }
      try {
        const blockerRows = await api.getWorkOrderBlockers({ work_order_id: currentWorkOrderId, limit: 50 });
        if (requestId !== loadRequestRef.current) return;
        setBlockers(blockerRows);
      } catch {
        if (requestId !== loadRequestRef.current) return;
        setBlockers([]);
      }
      try {
        const [attachedRows, availableRows] = await Promise.all([
          api.getDocuments({ work_order_id: currentWorkOrderId, limit: 100 }),
          api.getDocuments({ limit: 500 }),
        ]);
        if (requestId !== loadRequestRef.current) return;

        const attachedPdfRows = (attachedRows as WorkOrderDocument[]).filter(isPdfDocument);
        const attachedIds = new Set(attachedPdfRows.map((document) => document.id));
        setWorkOrderDocuments(attachedPdfRows);
        setAvailablePdfDocuments(
          (availableRows as WorkOrderDocument[])
            .filter(isPdfDocument)
            .filter((document) => !document.work_order_id && !attachedIds.has(document.id))
        );
      } catch {
        if (requestId !== loadRequestRef.current) return;
        setWorkOrderDocuments([]);
        setAvailablePdfDocuments([]);
      }
    } catch {
      if (requestId !== loadRequestRef.current) return;
      setError('Failed to load work order');
    } finally {
      if (requestId !== loadRequestRef.current) return;
      setLoading(false);
    }
  }, [id]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadWorkOrder();
    }, 500);
  }, [loadWorkOrder]);

  useWebSocket({
    url: realtimeUrl,
    enabled: Boolean(realtimeUrl),
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (!['work_order_update', 'shop_floor_update', 'dashboard_update'].includes(message.type)) return;
      const messageWorkOrderId = message.data?.work_order_id;
      if (workOrderId && messageWorkOrderId && messageWorkOrderId !== workOrderId) return;
      if (workOrderId && !messageWorkOrderId) return;
      scheduleRealtimeRefresh();
    }
  });

  useEffect(() => {
    setLoading(true);
    setError('');
    setWorkOrder(null);
    setMaterialReqs(null);
    setBlockers([]);
    setNestImportWizardOpen(false);
    setDueDateEditing(false);
    // Same reason as the due-date editor: the route keeps this component
    // mounted across an :id change, so an editor left open would carry the
    // PREVIOUS work order's draft text onto the next one — and Save would write
    // it there. Drafts themselves need no reset; startNotesEdit re-seeds them.
    setNotesEditing(false);
    setFieldConflict(null);
    setWorkOrderDocuments([]);
    setAvailablePdfDocuments([]);
    setDocumentUploadFile(null);
    setDocumentTitle('');
    setDocumentUploadInputKey((key) => key + 1);
    setAttachDocumentId('');
    setDocumentError('');
    setSelectedDocumentId(null);
    replaceDocumentPreviewUrl(null);
  }, [workOrderId, replaceDocumentPreviewUrl]);

  useEffect(() => {
    loadWorkOrder();
  }, [loadWorkOrder]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
      replaceDocumentPreviewUrl(null);
    };
  }, [replaceDocumentPreviewUrl]);

  useEffect(() => {
    if (workOrderDocuments.length === 0) {
      setSelectedDocumentId(null);
      return;
    }
    if (!selectedDocumentId || !workOrderDocuments.some((document) => document.id === selectedDocumentId)) {
      setSelectedDocumentId(workOrderDocuments[0].id);
    }
  }, [selectedDocumentId, workOrderDocuments]);

  useEffect(() => {
    if (!selectedDocumentId) {
      replaceDocumentPreviewUrl(null);
      setDocumentPreviewLoading(false);
      return;
    }

    let cancelled = false;

    const loadPreview = async () => {
      setDocumentPreviewLoading(true);
      try {
        const response = await api.downloadDocument(selectedDocumentId);
        const url = window.URL.createObjectURL(new Blob([response], { type: 'application/pdf' }));
        if (cancelled) {
          window.URL.revokeObjectURL(url);
          return;
        }
        replaceDocumentPreviewUrl(url);
      } catch {
        if (!cancelled) {
          replaceDocumentPreviewUrl(null);
        }
      } finally {
        if (!cancelled) {
          setDocumentPreviewLoading(false);
        }
      }
    };

    loadPreview();
    return () => {
      cancelled = true;
    };
  }, [replaceDocumentPreviewUrl, selectedDocumentId]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      loadWorkOrder();
    }, 30000);

    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        loadWorkOrder();
      }
    };

    const refreshOnFocus = () => {
      loadWorkOrder();
    };

    document.addEventListener('visibilitychange', refreshWhenVisible);
    window.addEventListener('focus', refreshOnFocus);

    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      window.removeEventListener('focus', refreshOnFocus);
    };
  }, [loadWorkOrder]);

  useEffect(() => {
    if (!isAdminView) {
      setUserNameById({});
      return;
    }

    let cancelled = false;

    const loadUserDirectory = async () => {
      try {
        const users: User[] = await api.getUsers(true);
        if (cancelled) return;
        const lookup: Record<number, string> = {};
        users.forEach((item) => {
          const fullName = `${item.first_name || ''} ${item.last_name || ''}`.trim();
          lookup[item.id] = fullName || item.email || `User #${item.id}`;
        });
        setUserNameById(lookup);
      } catch {
        if (!cancelled) {
          setUserNameById({});
        }
      }
    };

    loadUserDirectory();
    return () => {
      cancelled = true;
    };
  }, [isAdminView]);

  useEffect(() => {
    if (!isAdminView || !workOrder?.work_order_number) {
      setActiveUsersOnWorkOrder([]);
      return;
    }

    let cancelled = false;

    const loadActiveUsers = async () => {
      try {
        const response = await api.getActiveUsers();
        if (cancelled) return;
        const activeUsers: ActiveShopUser[] = Array.isArray(response?.active_users)
          ? response.active_users
          : [];
        setActiveUsersOnWorkOrder(
          activeUsers.filter((entry) => entry.work_order_number === workOrder.work_order_number)
        );
      } catch {
        if (!cancelled) {
          setActiveUsersOnWorkOrder([]);
        }
      }
    };

    loadActiveUsers();

    return () => {
      cancelled = true;
    };
  }, [isAdminView, workOrder?.work_order_number, workOrder?.updated_at]);

  // Load the active work centers once the laser card is both manageable and
  // populated — they feed the per-nest reassign selects (laser-first order).
  const hasNestOps = Boolean(workOrder?.operations?.some((op) => op.laser_nest));
  useEffect(() => {
    if (!canManageNests || !hasNestOps) return;

    let cancelled = false;

    const loadWorkCenters = async () => {
      try {
        const centers = await api.getWorkCenters(true);
        if (cancelled) return;
        setWorkCenters(sortWorkCentersForLaserDispatch((centers ?? []).filter((wc) => wc.is_active)));
      } catch {
        if (!cancelled) setWorkCenters([]);
      }
    };

    loadWorkCenters();
    return () => {
      cancelled = true;
    };
  }, [canManageNests, hasNestOps]);

  const handleRelease = async () => {
    try {
      await api.releaseWorkOrder(workOrder!.id);
      loadWorkOrder();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release work order');
    }
  };

  const handleStart = async () => {
    try {
      await api.startWorkOrder(workOrder!.id);
      loadWorkOrder();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to start work order');
    }
  };

  const handleDelete = () => {
    if (!workOrder || deleting) return;
    setDeleteConfirmOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (!workOrder || deleting) return;
    setDeleting(true);
    try {
      await api.deleteWorkOrder(workOrder.id);
      navigate('/work-orders');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete work order');
      setDeleting(false);
      setDeleteConfirmOpen(false);
    }
  };

  // Opening the modal is decoupled from the API call. The header / per-row
  // "Complete" buttons just set the target; the CompleteWorkModal collects the
  // quantities + scrap reason and calls handleCompleteSubmit on confirm. The
  // in-flight guards (`completing` / `completingOpId`) wrap only the API call,
  // not the dialog, so a server-gated completion reflects only what the server
  // returns (non-optimistic).
  const handleComplete = () => {
    if (completing) return;
    setCompleteTarget({ kind: 'work_order' });
  };

  const handleCompleteOperation = (operation: WorkOrderOperation) => {
    if (completingOpId === operation.id) return;
    setCompleteTarget({ kind: 'operation', operation });
  };

  const handleCompleteSubmit = async (values: CompleteWorkSubmit) => {
    if (!completeTarget) return;
    const { quantityComplete, quantityScrapped, scrapReason, scrapReasonCodeId } = values;
    if (completeTarget.kind === 'work_order') {
      setCompleting(true);
      try {
        // The WO-level endpoint understands the structured scrap code id; the
        // operation-level endpoint below is text-only (the modal guarantees a
        // non-blank scrapReason whenever scrap > 0, so both stay valid).
        const completeRes: unknown = await api.completeWorkOrder(
          workOrder!.id,
          quantityComplete,
          quantityScrapped,
          scrapReason,
          scrapReasonCodeId
        );
        setCompleteTarget(null);
        // Force-complete override summary: an authorized user completed the WO
        // with required step records bypassed (deliberate, audited — the action
        // SUCCEEDED by design, so this is an info notice, never an error).
        const bypassed = extractStepsBypassed(completeRes);
        if (bypassed) {
          showToast('info', stepsBypassedMessage(bypassed));
        }
        loadWorkOrder();
      } catch (err: any) {
        // Server-gated: surface the server's verbatim refusal, never a success.
        // (String guard: object details must never reach the toast renderer.)
        const detail = err.response?.data?.detail;
        showToast('error', typeof detail === 'string' && detail ? detail : 'Failed to complete work order');
      } finally {
        setCompleting(false);
      }
    } else {
      const operationId = completeTarget.operation.id;
      setCompletingOpId(operationId);
      try {
        await api.completeWOOperation(operationId, quantityComplete, quantityScrapped, scrapReason);
        setCompleteTarget(null);
        loadWorkOrder();
      } catch (err: any) {
        // 409 STEPS_INCOMPLETE: required process-sheet steps lack conforming
        // records. The detail is an OBJECT (not a string), so surface the
        // missing labels/serials readably and open the operation's evidence
        // panel so the gaps are visible inline.
        const missing = extractStepsIncomplete(err);
        if (missing) {
          showToast('error', stepsIncompleteMessage(missing));
          setCompleteTarget(null);
          setStepsOpenOpId(operationId);
        } else {
          const detail = err.response?.data?.detail;
          showToast('error', typeof detail === 'string' && detail ? detail : 'Failed to complete operation');
        }
      } finally {
        setCompletingOpId(null);
      }
    }
  };

  // Supervisor "Correct count": opens the small quantity+reason modal for an
  // operation. Server-gated ⇒ NON-optimistic — the on-screen count never moves
  // locally; success refetches the WO, refusal renders the verbatim `detail`
  // INLINE in the modal.
  const openCorrectModal = (operation: WorkOrderOperation) => {
    setCorrectTarget(operation);
    setCorrectData({ quantity: 1, reason: '' });
    setCorrectError(null);
  };

  const closeCorrectModal = () => {
    setCorrectTarget(null);
    setCorrectData({ quantity: 1, reason: '' });
    setCorrectError(null);
  };

  const handleCorrectSubmit = async () => {
    if (!correctTarget || correctingOpId !== null) return;
    const quantity = Number(correctData.quantity || 0);
    const reason = correctData.reason.trim();
    if (quantity <= 0 || !reason) return;
    setCorrectingOpId(correctTarget.id);
    setCorrectError(null);
    try {
      await api.reduceWOOperationProduction(correctTarget.id, {
        quantity_delta: quantity,
        reason,
        source: 'desktop',
      });
      showToast('success', `Removed ${quantity} from operation ${correctTarget.sequence} ${correctTarget.name}`);
      closeCorrectModal();
      loadWorkOrder();
    } catch (err: any) {
      // Verbatim server refusal, inline (string guard: object details must
      // never reach the renderer).
      const detail = err.response?.data?.detail;
      setCorrectError(typeof detail === 'string' && detail ? detail : 'Failed to correct the completed quantity');
    } finally {
      setCorrectingOpId(null);
    }
  };

  // Called by the import wizard after a successful import. The wizard owns the
  // pick → preview → review → import flow; here we just close it and route to
  // the freshly-created child laser WO. When THIS work order is itself
  // laser_cutting the backend imports onto it directly and returns its own id —
  // navigating to the same route wouldn't remount, so refresh in place instead.
  const handleNestPackageImported = (childWorkOrderId?: number) => {
    setNestImportWizardOpen(false);
    if (childWorkOrderId && childWorkOrderId !== workOrder?.id) {
      navigate(`/work-orders/${childWorkOrderId}`);
    } else {
      loadWorkOrder();
    }
  };

  // --- Shared write for the page's inline work-order field editors ----------
  // Both the due-date pencil and the notes panel patch the same endpoint with
  // the same concurrency policy, so they share one writer: the version is read
  // here (never from a caller's snapshot), and the 409 response is handled in
  // exactly one place. Callers keep their own in-flight flag and their own
  // `onSuccess` so the editor closes before the refetch, preserving the
  // non-optimistic posture — read mode shows server state, never the draft.
  //
  // Returns true only on a committed write, so a caller can leave its dialog up
  // on failure.
  const saveWorkOrderPatch = async (
    patch: Record<string, unknown>,
    options: { successMessage: string; failureMessage: string; onSuccess?: () => void }
  ): Promise<boolean> => {
    if (!workOrder) return false;
    try {
      await api.updateWorkOrder(workOrder.id, { ...patch, version: workOrder.version });
      showToast('success', options.successMessage);
      options.onSuccess?.();
      await loadWorkOrder();
      return true;
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      showToast('error', typeof detail === 'string' && detail ? detail : options.failureMessage);
      if (err?.response?.status === 409) {
        // Stale version — someone else changed the WO between our last refetch
        // and this write. Refetch so the next attempt carries a fresh version;
        // the editor stays open holding the user's draft so a retry doesn't cost
        // them their typing. The refetch leaves the editor's baseline behind on
        // purpose: if that concurrent change touched a field this editor owns,
        // the retry trips the lost-update guard and asks before overwriting.
        await loadWorkOrder();
      }
      return false;
    }
  };

  // --- Inline due-date edit ------------------------------------------------
  // Same lost-update guard as the notes editor, for the same reason: `version`
  // cannot carry it, because the page refetches on every work_order broadcast
  // and this endpoint emits one. Without the guard, a concurrent reschedule is
  // silently overwritten with a clean 200 — and a due date is a promise date
  // feeding OTD, so losing someone's change is not cosmetic.
  const dueDateServerStamp = workOrder?.due_date ? getCentralDateStamp(workOrder.due_date) : '';

  const startDueDateEdit = () => {
    // getCentralDateStamp passes date-only strings through verbatim and
    // normalizes datetimes to the Central calendar date.
    setDueDateDraft(dueDateServerStamp);
    setDueDateBaseline(dueDateServerStamp);
    setDueDateEditing(true);
  };

  const performDueDateSave = async () => {
    setSavingDueDate(true);
    try {
      await saveWorkOrderPatch(
        { due_date: dueDateDraft || null },
        {
          successMessage: dueDateDraft
            ? `Due date set to ${formatCentralDate(dueDateDraft)}`
            : 'Due date cleared',
          failureMessage: 'Failed to update due date',
          onSuccess: () => setDueDateEditing(false),
        }
      );
    } finally {
      setSavingDueDate(false);
    }
  };

  const handleDueDateSave = async () => {
    if (!workOrder || savingDueDate) return;
    if (dueDateServerStamp !== dueDateBaseline) {
      setFieldConflict({
        kind: 'due_date',
        due_date: dueDateServerStamp,
        fields: DUE_DATE_LABEL,
      });
      return;
    }
    await performDueDateSave();
  };

  // --- Inline unit-# edit --------------------------------------------------
  // Unit assignment routinely happens AFTER the work order is raised, so this has
  // to be editable, not create-only. Empty clears it (`|| null`), matching the
  // due-date editor — a unit typed onto the wrong work order has to be removable,
  // not just overwritten, because the badge is on the kiosk and the TV wall.
  const unitNumberServerValue = workOrder?.unit_number ?? '';

  const startUnitNumberEdit = () => {
    setUnitNumberDraft(unitNumberServerValue);
    setUnitNumberBaseline(unitNumberServerValue);
    setUnitNumberEditing(true);
  };

  const performUnitNumberSave = async () => {
    setSavingUnitNumber(true);
    try {
      const next = unitNumberDraft.trim();
      await saveWorkOrderPatch(
        { unit_number: next || null },
        {
          successMessage: next ? `Unit # set to ${next}` : 'Unit # cleared',
          failureMessage: 'Failed to update unit #',
          onSuccess: () => setUnitNumberEditing(false),
        }
      );
    } finally {
      setSavingUnitNumber(false);
    }
  };

  const handleUnitNumberSave = async () => {
    if (!workOrder || savingUnitNumber) return;
    if (unitNumberServerValue !== unitNumberBaseline) {
      setFieldConflict({
        kind: 'unit_number',
        unit_number: unitNumberServerValue,
        fields: UNIT_NUMBER_LABEL,
      });
      return;
    }
    await performUnitNumberSave();
  };

  // --- Operation sequencing mode -------------------------------------------
  // `sequential_operations` decides which rule promotes an operation to READY:
  // walk the routing in order (an operation unlocks only once every
  // lower-sequence operation is COMPLETE, its own work center included), or pool
  // by work center (operations sharing a machine go READY together). Pooling is
  // right for a laser package and for the 18-item batch WOs; it is wrong for a
  // routing like the 4-op weld assembly whose first three operations sit on one
  // weld cell and all unlocked at once.
  //
  // Absent reads as OFF — pooled is what a work order served without the column
  // actually behaves as, and what migration 081's server_default gives every row
  // that predates it. Do not flip that fallback to `?? true`.
  const sequentialOperations = workOrder?.sequential_operations ?? false;
  // The flag is IGNORED on laser_cutting work orders: nest dispatch
  // (`is_laser_dispatch_work_order`) short-circuits above it at every backend
  // seam and is strictly fuller — it drops predecessor gating entirely, across
  // work centers. So the switch is not rendered there at all; a control that
  // changes a column the server never reads is worse than no control.
  const sequencingApplies = workOrder?.work_order_type !== 'laser_cutting';

  // The lowest-sequence operation that is NOT complete, or `null` when nothing
  // on this work order blocks anything. It is the client-side mirror of the
  // server's out-of-sequence guard: both office operation verbs
  // (`POST /work-orders/operations/{id}/start` and `.../complete`) run the shared
  // `operation_action_gates.operation_blocked_by_predecessors` and refuse 400
  // "Previous operations must be completed first" while a LOWER-sequence
  // operation of the same work order is not COMPLETE. "Some lower-sequence
  // operation is incomplete" is exactly "this operation sits above the lowest
  // incomplete one", so one scan answers it for every row.
  //
  // Without this the page offers Complete on all three weld-cell operations of a
  // sequenced routing — the shape this feature exists for — and the server 400s
  // whichever one the user picks, AFTER they have filled in the quantity/scrap
  // modal. Pooled, that same call succeeded, so sequencing turns a rare mismatch
  // into the common one.
  //
  // Three boundaries are the server's, not this file's taste:
  //
  //  * SEQUENCED ONLY. A pooled work order is deliberately left ungated, exactly
  //    as it renders today. Disabling a control the server would have accepted is
  //    the worse failure of the two — it hides a legal action with no override —
  //    and the pooled rule additionally waives same-work-center predecessors
  //    unless one is ON_HOLD, so a half-mirror of it would refuse work the floor
  //    is allowed to start.
  //  * NO WORK-CENTER LOGIC. Under sequencing `work_order_allows_same_work_center`
  //    resolves to False, so a predecessor blocks from EVERY work center, its own
  //    included. A same-cell waiver here would re-open the reported defect.
  //  * LASER IS NEVER BLOCKED. `is_laser_dispatch_work_order` short-circuits above
  //    the flag at every backend seam and drops predecessor gating entirely —
  //    which is what `sequencingApplies` already encodes, so a nest WO carrying a
  //    stored `sequential_operations = true` still gates nothing.
  //
  // Advisory only, like every other capability on this page: the server is still
  // the enforcement, and this never enables a control the server would refuse.
  const lowestIncompleteOperation = useMemo(() => {
    if (!workOrder || !sequencingApplies || !sequentialOperations) return null;
    return workOrder.operations.reduce<WorkOrderOperation | null>(
      (lowest, candidate) =>
        candidate.status === 'complete' || (lowest !== null && lowest.sequence <= candidate.sequence)
          ? lowest
          : candidate,
      null
    );
  }, [workOrder, sequencingApplies, sequentialOperations]);

  // NON-OPTIMISTIC, and the refetch inside `saveWorkOrderPatch` is the reason
  // rather than a nicety: turning sequencing ON demotes un-started READY
  // operations back to PENDING server-side, so the Status column sitting beside
  // this switch is stale the instant the write lands. Patching the flag locally
  // would leave three weld operations reading READY under a rule that just
  // unlocked one of them. The switch renders `workOrder.sequential_operations`
  // and nothing else, so a refused write (409 stale version, 403 role) leaves it
  // showing the value the server still holds.
  const handleSequencingToggle = async () => {
    if (!workOrder || savingSequencing || !sequencingApplies || !canEditSequencing) return;
    const next = !sequentialOperations;
    setSavingSequencing(true);
    try {
      await saveWorkOrderPatch(
        { sequential_operations: next },
        {
          successMessage: next
            ? 'Operations now run in sequence — each one unlocks when the previous is complete'
            : 'Operations at the same work center can now run in any order',
          failureMessage: 'Failed to change operation sequencing',
        }
      );
    } finally {
      setSavingSequencing(false);
    }
  };

  // --- Inline notes / special-instructions edit ----------------------------
  // Compared against the server values (not a snapshot taken at open) so that a
  // draft typed back to the saved text stops counting as dirty.
  // Trimmed on both sides because the save writes trimmed text: a trailing space
  // typed into a saved note is not an unsaved change, and prompting to discard
  // one (or writing a byte-identical value that still bumps `version` and costs
  // an audit row) would be noise.
  const notesDirty =
    notesEditing &&
    (notesDraft.trim() !== (workOrder?.notes ?? '').trim() ||
      instructionsDraft.trim() !== (workOrder?.special_instructions ?? '').trim());
  const { confirmDiscard: confirmDiscardNotes } = useUnsavedChanges(
    notesDirty,
    'You have unsaved note changes. Discard them?'
  );
  // Only reachable for a value that was already over the cap when it arrived
  // (maxLength stops the user typing past it), but it turns that into a legible
  // refusal instead of a raw 422 from the server's max_length validator.
  const notesTooLong = notesDraft.length > WO_NOTE_MAX_LENGTH;
  const instructionsTooLong = instructionsDraft.length > WO_NOTE_MAX_LENGTH;

  const startNotesEdit = () => {
    const notes = workOrder?.notes ?? '';
    const instructions = workOrder?.special_instructions ?? '';
    setNotesDraft(notes);
    setInstructionsDraft(instructions);
    setNotesBaseline({ notes, special_instructions: instructions });
    setNotesEditing(true);
  };

  const cancelNotesEdit = () => {
    if (!confirmDiscardNotes()) return;
    setNotesEditing(false);
  };

  // The write itself. Reached either straight from Save (no conflict) or from
  // the lost-update dialog's Replace — the two entry points share it so the
  // null-on-empty rule can't drift between them.
  const performNotesSave = async () => {
    setSavingNotes(true);
    try {
      await saveWorkOrderPatch(
        {
          // An emptied field is sent as null, not '': "cleared" should read as
          // absent everywhere downstream (traveler print, kiosk, notifications),
          // not as a present-but-blank note.
          notes: notesDraft.trim() || null,
          special_instructions: instructionsDraft.trim() || null,
        },
        {
          successMessage: 'Notes updated',
          failureMessage: 'Failed to update notes',
          onSuccess: () => setNotesEditing(false),
        }
      );
    } finally {
      setSavingNotes(false);
    }
  };

  const handleNotesSave = async () => {
    if (!workOrder || savingNotes || notesTooLong || instructionsTooLong) return;

    // Lost-update guard — see the notesBaseline comment in the state block for
    // why `version` cannot carry this. Compare what the server holds NOW against
    // what it held when the editor opened, and refuse a save that would
    // overwrite someone else's words, putting the choice in front of the user
    // instead. Replacing their note stays allowed — as a decision, not an
    // accident. Deliberately keyed on the note TEXT, not on `version`: the
    // WorkOrder row maps version_id_col, so every operation completion bumps it
    // and a version-keyed check would make notes unsavable on any live job.
    //
    // Scope, precisely: this is refetch-then-compare, so it catches a concurrent
    // edit the page has ALREADY pulled in. One landing between the last refetch
    // and this click still gets through to the server — the optimistic-lock 409
    // is the net for that window, and saveWorkOrderPatch leaves the baseline
    // behind on a 409 on purpose so the retry lands here and asks.
    const serverNotes = workOrder.notes ?? '';
    const serverInstructions = workOrder.special_instructions ?? '';
    if (
      serverNotes.trim() !== notesBaseline.notes.trim() ||
      serverInstructions.trim() !== notesBaseline.special_instructions.trim()
    ) {
      setFieldConflict({
        kind: 'notes',
        notes: serverNotes,
        special_instructions: serverInstructions,
        fields: describeChangedFields(
          { [NOTES_LABEL]: serverNotes, [INSTRUCTIONS_LABEL]: serverInstructions },
          { [NOTES_LABEL]: notesBaseline.notes, [INSTRUCTIONS_LABEL]: notesBaseline.special_instructions },
          NOTES_LABEL
        ),
      });
      return;
    }

    await performNotesSave();
  };

  // Which editor's in-flight flag this dialog should reflect. Scoped to the kind
  // that RAISED the conflict, never `savingNotes || savingDueDate`: both editors
  // can be open at once, `pending` disables Confirm AND Cancel and refuses
  // backdrop/Escape, so an unrelated save in flight on the other editor would
  // freeze this dialog — spinner running for work it isn't doing.
  const conflictPending =
    fieldConflict?.kind === 'due_date'
      ? savingDueDate
      : fieldConflict?.kind === 'unit_number'
        ? savingUnitNumber
        : savingNotes;

  // Confirmed overwrite, for whichever editor raised the conflict. Adopting the
  // server's value as the new baseline is what makes this confirmation cover
  // exactly the change we showed them, so a retry after a FAILED write doesn't
  // re-ask about a change already approved. It is not a promise that a third
  // edit can't slip in: one landing while this dialog is open is refetched (so
  // `version` is fresh and there is no 409) and this Replace overwrites it
  // unasked. Closing that would mean re-comparing at click time and re-opening
  // the dialog with the newer value.
  const handleConflictReplace = async () => {
    if (!fieldConflict || conflictPending) return;
    if (fieldConflict.kind === 'notes') {
      setNotesBaseline({
        notes: fieldConflict.notes,
        special_instructions: fieldConflict.special_instructions,
      });
      await performNotesSave();
    } else if (fieldConflict.kind === 'due_date') {
      setDueDateBaseline(fieldConflict.due_date);
      await performDueDateSave();
    } else if (fieldConflict.kind === 'unit_number') {
      setUnitNumberBaseline(fieldConflict.unit_number);
      await performUnitNumberSave();
    }
    // Closed either way. On success the editor is gone; on failure the error
    // toast carries the reason and the editor is still open holding the draft,
    // so a retry from Save re-runs the guard against the adopted baseline.
    setFieldConflict(null);
  };

  // --- Per-nest work-center reassign ---------------------------------------
  // Server-gated (refused while the op is in progress) ⇒ NON-optimistic: the
  // select stays on the op's current WC until the refetch confirms the move,
  // and a refusal surfaces the server's verbatim detail.
  const handleReassignNestWorkCenter = async (operation: WorkOrderOperation, nextWorkCenterId: number) => {
    if (!nextWorkCenterId || nextWorkCenterId === operation.work_center_id) return;
    setReassigningOpId(operation.id);
    try {
      await api.updateOperation(operation.id, {
        work_center_id: nextWorkCenterId,
        version: operation.version,
      });
      const target = workCenters.find((wc) => wc.id === nextWorkCenterId);
      showToast(
        'success',
        `Op ${operation.sequence} moved to ${target ? target.name || target.code : `work center #${nextWorkCenterId}`}`
      );
      await loadWorkOrder();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      showToast('error', typeof detail === 'string' && detail ? detail : 'Failed to reassign work center');
    } finally {
      setReassigningOpId(null);
    }
  };

  const handleUploadWorkOrderPdf = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workOrder || !documentUploadFile) return;

    const isPdfFile =
      documentUploadFile.type === 'application/pdf' ||
      documentUploadFile.name.toLowerCase().endsWith('.pdf');
    if (!isPdfFile) {
      setDocumentError('Only PDF files can be attached to the work order preview.');
      return;
    }

    setDocumentBusy(true);
    setDocumentError('');
    try {
      const formData = new FormData();
      formData.append('file', documentUploadFile);
      formData.append('title', documentTitle.trim() || documentUploadFile.name.replace(/\.pdf$/i, ''));
      formData.append('document_type', 'drawing');
      formData.append('revision', 'A');
      formData.append('work_order_id', String(workOrder.id));
      const uploadedDocument = await api.uploadDocument(formData);
      setDocumentUploadFile(null);
      setDocumentTitle('');
      setDocumentUploadInputKey((key) => key + 1);
      setSelectedDocumentId(uploadedDocument.id);
      await loadWorkOrder();
    } catch (err: any) {
      setDocumentError(err.response?.data?.detail || 'Failed to upload work order PDF');
    } finally {
      setDocumentBusy(false);
    }
  };

  const handleAttachExistingPdf = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workOrder || !attachDocumentId) return;

    setDocumentBusy(true);
    setDocumentError('');
    try {
      const attachedDocument = await api.attachDocumentToWorkOrder(Number(attachDocumentId), workOrder.id);
      setAttachDocumentId('');
      setSelectedDocumentId(attachedDocument.id);
      await loadWorkOrder();
    } catch (err: any) {
      setDocumentError(err.response?.data?.detail || 'Failed to attach PDF to work order');
    } finally {
      setDocumentBusy(false);
    }
  };

  const handleDownloadWorkOrderPdf = async (document: WorkOrderDocument) => {
    try {
      const response = await api.downloadDocument(document.id);
      const url = window.URL.createObjectURL(new Blob([response], { type: document.mime_type || 'application/pdf' }));
      const link = window.document.createElement('a');
      link.href = url;
      link.setAttribute('download', document.file_name || `${document.title}.pdf`);
      window.document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch {
      setDocumentError('Failed to download PDF');
    }
  };

  const handleCreateBlocker = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workOrder) return;

    setSubmittingBlocker(true);
    try {
      await api.createWorkOrderBlocker(workOrder.id, {
        operation_id: blockerForm.operation_id ? Number(blockerForm.operation_id) : undefined,
        category: blockerForm.category,
        severity: blockerForm.severity,
        note: blockerForm.note.trim() || undefined,
        put_operation_on_hold: true,
      });
      setBlockerForm({ operation_id: '', category: 'material_missing', severity: 'high', note: '' });
      await loadWorkOrder();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to report blocker');
    } finally {
      setSubmittingBlocker(false);
    }
  };

  // Replaced the native prompt() note capture with the shared InputDialog. The
  // Resolve button opens the dialog; submit resolves with the entered (trimmed,
  // non-empty) note, non-optimistically — the dialog stays open and pending
  // until the server answers, and closes only on success.
  const handleResolveBlocker = async (note: string) => {
    const blocker = resolveBlockerTarget;
    if (!blocker || resolvingBlockerId !== null) return;
    setResolvingBlockerId(blocker.id);
    try {
      await api.resolveWorkOrderBlocker(blocker.id, note);
      await loadWorkOrder();
      setResolveBlockerTarget(null);
      showToast('success', `Resolved blocker "${blocker.title}"`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to resolve blocker');
    } finally {
      setResolvingBlockerId(null);
    }
  };

  /**
   * CLEAR HOLD -- the office lift of an ON_HOLD operation.
   *
   * The bug this closes: this page had no control that cleared a hold at all
   * (`resumeOperation` had zero call sites here), so an owner who held a nest
   * could only get it back by walking to a kiosk. The three existing call sites
   * are all shop-floor screens.
   *
   * NON-OPTIMISTIC, per the app convention for a server-GATED action: nothing
   * moves until `loadWorkOrder()` returns, and a refusal renders the server's
   * `detail` verbatim.
   *
   * THE RESPONSE IS BOUND, AND THAT IS THE POINT. `PUT /shop-floor/operations/
   * {id}/resume` returns two facts a green toast would bury:
   *
   * 1. `open_blockers` -- resuming does NOT resolve the blocker that caused the
   *    hold; the endpoint returns whatever is still open precisely so operation
   *    status and blocker status cannot silently diverge. Swallowing it lets a
   *    live quality stop read as cleared.
   * 2. `status === "pending"` -- resume RESTORES, it does not release. An
   *    operation with no labor evidence is floored at PENDING and lifted to
   *    READY only by the server's own promotion rule, so a hold placed on a
   *    PENDING op (or on one whose WO is still DRAFT, or whose predecessor is
   *    incomplete) comes back PENDING and stays off the board -- the dispatch
   *    board and the kiosk surface READY work only. "Resumed" there sends the
   *    shop looking for a card that is never going to appear.
   *
   * Either one earns the `warning` variant -- the repo's documented "succeeded
   * but did not do everything asked" case. BOTH at once compose into ONE toast:
   * two stacked toasts about one click read as two failures.
   *
   * NOT PRE-CHECKED HERE, deliberately: the cancelled-nest tombstone. The resume
   * endpoint 409s on an operation whose laser nest was soft-deleted, but THIS
   * branch's `WorkOrderOperationResponse` carries no `cancelled_nest_id` (it
   * ships with the laser-nest-removal work, which is not merged here) and the
   * enrich step nulls a soft-deleted nest out of the row entirely -- so the page
   * has no signal to gate on before the click. The non-optimistic path is what
   * keeps that honest: the row does not move and the server's own 409 reason is
   * what the user reads. When that field lands, guard the button on
   * `op.cancelled_nest_id != null && op.status !== 'complete'` (the status pair
   * matters: the read path can flip a marked operation to COMPLETE, and calling
   * that row a leftover nest would claim work that never happened).
   */
  const handleConfirmClearHold = async () => {
    const op = clearHoldTarget;
    if (!op || !workOrder || clearingHoldOpId !== null) return;
    setClearingHoldOpId(op.id);
    try {
      const result = await api.resumeOperation(op.id);
      await loadWorkOrder();
      setClearHoldTarget(null);

      const label = `${workOrder.work_order_number} \u00b7 ${operationLabel(op)}`;
      // The success-vs-warning JUDGEMENT comes from the shared `clearHoldOutcome`
      // (heldOperations.ts), the same one the two shop-floor screens use; only the
      // WORDING below is office-specific. Re-deriving `status === 'pending'` here
      // is how three screens end up disagreeing about one server answer.
      const { landedPending, openBlockers: open, fellShort } = clearHoldOutcome(result);
      const shortfalls: string[] = [];
      if (landedPending) {
        shortfalls.push(
          'It did NOT go back on the board \u2014 it is Pending again, so it will not show on the dispatch ' +
            'board or at the kiosk until the work order is released and any earlier operations are finished.'
        );
      }
      if (open.length > 0) {
        shortfalls.push(
          `${open.length === 1 ? 'A blocker is' : `${open.length} blockers are`} still open ` +
            `(${open.map(openBlockerLine).join('; ')}). Clearing a hold does not close a blocker \u2014 ` +
            'a supervisor or manager closes it in the Blockers panel.'
        );
      }
      if (fellShort) {
        showToast('warning', `${label}: hold cleared. ${shortfalls.join(' ')}`);
      } else {
        showToast('success', `${label}: hold cleared.`);
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to clear the hold');
    } finally {
      setClearingHoldOpId(null);
    }
  };

  // --- Manual laser nest handlers -----------------------------------------
  const openAddNestModal = () => {
    setNestModalTarget(null);
    // A nest being CREATED has no operation yet — the modal ties on the
    // operation the create returns, not on one passed in here.
    setNestModalOperationId(undefined);
    setNestActionError('');
    setNestModalOpen(true);
  };

  const openEditNestModal = (nest: LaserNestInfo, operationId?: number) => {
    setNestModalTarget(nest);
    setNestModalOperationId(operationId);
    setNestActionError('');
    setNestModalOpen(true);
  };

  // The modal calls this on every successful save. On a partial create (nest
  // persisted but its PDF failed to attach) it passes a non-fatal warning we
  // surface in the nest-action banner so the operator knows to retry via the
  // per-nest "Attach PDF" action.
  const handleNestSaved = async (warning?: string) => {
    setNestActionError(warning || '');
    // The nest modal's edit path can create/update/cancel the tie on the nest's
    // operation, and a tie write does NOT bump `work_orders.updated_at` — so the
    // WO refetch below is not enough to refresh MaterialTiesPanel. Bump its own
    // freshness seam, exactly like the OperationMaterialTieModal wiring.
    setTieRefreshToken((token) => token + 1);
    await loadWorkOrder();
  };

  const handleDeleteNest = (nest: LaserNestInfo) => {
    if (nestActionId !== null) return;
    setDeleteNestTarget(nest);
  };

  const handleConfirmDeleteNest = async () => {
    const nest = deleteNestTarget;
    if (!nest || nestActionId !== null) return;
    setNestActionId(nest.id);
    setNestActionError('');
    try {
      await api.deleteLaserNest(nest.id);
      if (previewNestId === nest.id) setPreviewNestId(null);
      await loadWorkOrder();
    } catch (err: any) {
      setNestActionError(err?.response?.data?.detail || 'Failed to delete laser nest');
    } finally {
      setNestActionId(null);
      setDeleteNestTarget(null);
    }
  };

  const handleDetachNestPdf = async (nest: LaserNestInfo) => {
    setNestActionId(nest.id);
    setNestActionError('');
    try {
      await api.detachLaserNestDocument(nest.id);
      if (previewNestId === nest.id) setPreviewNestId(null);
      await loadWorkOrder();
    } catch (err: any) {
      setNestActionError(err?.response?.data?.detail || 'Failed to detach PDF');
    } finally {
      setNestActionId(null);
    }
  };

  // Trigger the hidden file input for the nest whose "Attach PDF" was clicked.
  const promptAttachNestPdf = (nestId: number) => {
    setNestAttachTargetId(nestId);
    setNestActionError('');
    nestAttachInputRef.current?.click();
  };

  const handleNestAttachFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    const targetId = nestAttachTargetId;
    // Reset the input so re-selecting the same file fires onChange again.
    event.target.value = '';
    if (!file || !targetId || !workOrder) return;

    const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
    if (!isPdf) {
      setNestActionError('Only PDF files can be attached to a laser nest.');
      return;
    }

    setNestActionId(targetId);
    setNestActionError('');
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('title', file.name.replace(/\.pdf$/i, ''));
      formData.append('document_type', 'drawing');
      formData.append('revision', 'A');
      formData.append('work_order_id', String(workOrder.id));
      const uploaded = await api.uploadDocument(formData);
      await api.attachLaserNestDocument(targetId, uploaded.id);
      await loadWorkOrder();
    } catch (err: any) {
      setNestActionError(err?.response?.data?.detail || 'Failed to attach PDF');
    } finally {
      setNestActionId(null);
      setNestAttachTargetId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (error || !workOrder) {
    return (
      <ErrorState
        title="Couldn't load work order"
        message={error || 'Work order not found'}
        onRetry={() => {
          setLoading(true);
          loadWorkOrder();
        }}
      />
    );
  }

  const operationProgress = getDetailWorkOrderProgress(workOrder);
  const selectedDocument = workOrderDocuments.find((document) => document.id === selectedDocumentId) || null;
  // Laser nests surface per-operation on the WorkOrderResponse; collect them
  // (with their operation context) for the Laser Nest card's nest list.
  const laserNests = (workOrder.operations || [])
    .filter((op): op is WorkOrderOperation & { laser_nest: LaserNestInfo } => Boolean(op.laser_nest))
    .map((op) => ({ operation: op, nest: op.laser_nest }));
  // The Laser Nest Package card renders the full per-nest detail (material,
  // thickness, sheet, runs, PDF actions) on every WO type — on laser_cutting
  // WOs (child or standalone) the import/manual endpoints operate on the WO
  // directly, so the card is the re-import / manual-add surface there too.
  // When nests exist we de-dup: the Operations table cell collapses to a
  // compact identifier + cross-link to the panel by stable nest id, rather
  // than repeating the same fields.
  const nestPanelShown = laserNests.length > 0;

  // Parent crumb resolved from the shared route source (keeps label/href in sync
  // with the sidebar + top-bar title); falls back to the Work Orders list.
  const woParent = getBreadcrumbParent('/work-orders/0') ?? { label: 'Work Orders', href: '/work-orders' };

  return (
    <div className="space-y-6">
      {/* Breadcrumbs — Work Orders › {WO number} */}
      <Breadcrumbs
        crumbs={[
          { label: woParent.label, href: woParent.href },
          { label: workOrder.work_order_number },
        ]}
      />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center">
          <button onClick={() => navigate(woParent.href)} className="mr-4 text-slate-400 hover:text-slate-300">
            <ArrowLeftIcon className="h-6 w-6" />
          </button>
          <div>
            <div className="flex flex-wrap items-center gap-2.5">
              <h1 className="text-2xl font-bold text-white">{workOrder.work_order_number}</h1>
              <UnitBadge unitNumber={workOrder.unit_number} size="md" />
            </div>
            <p className="text-slate-400">Work Order Details</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className={`px-3 py-1 rounded-full text-sm font-medium capitalize ${statusColor(workOrder.status)}`}>
            {workOrder.status.replace('_', ' ')}
          </span>
          {workOrder.status === 'draft' && (
            <Button onClick={handleRelease} className="flex items-center">
              <PlayIcon className="h-5 w-5 mr-2" />
              Release
            </Button>
          )}
          {workOrder.status === 'released' && (
            <button onClick={handleStart} className="btn-success flex items-center">
              <PlayIcon className="h-5 w-5 mr-2" />
              Start
            </button>
          )}
          {workOrder.status === 'in_progress' && (
            <Button
              onClick={handleComplete}
              disabled={completing}
              className="flex items-center disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {completing ? (
                <ArrowPathIcon className="h-5 w-5 mr-2 animate-spin" />
              ) : (
                <CheckCircleIcon className="h-5 w-5 mr-2" />
              )}
              {completing ? 'Completing...' : 'Complete'}
            </Button>
          )}
          {canDuplicateWorkOrder && (
            <Button
              variant="secondary"
              onClick={() => setDuplicateOpen(true)}
              className="flex items-center"
            >
              <DocumentDuplicateIcon className="h-5 w-5 mr-2" />
              Duplicate
            </Button>
          )}
          {canSaveAsTemplate && (
            <Button
              variant="secondary"
              onClick={() => setSaveTemplateOpen(true)}
              className="flex items-center"
            >
              <BookmarkSquareIcon className="h-5 w-5 mr-2" aria-hidden="true" />
              Save as template
            </Button>
          )}
          <Button
            variant="secondary"
            onClick={() => window.open(`/print/traveler/${workOrder.id}?autoprint=1`, '_blank')}
            className="flex items-center"
          >
            <PrinterIcon className="h-5 w-5 mr-2" />
            Print Traveler
          </Button>
          {canDeleteWorkOrder && (
            <Button
              variant="secondary"
              onClick={handleDelete}
              disabled={deleting}
              className="flex items-center text-red-300 hover:text-red-200 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <TrashIcon className="h-5 w-5 mr-2" />
              {deleting ? 'Deleting...' : 'Delete'}
            </Button>
          )}
        </div>
      </div>

      <ContextualAIStrip
        entityType="work_order"
        entityId={workOrder.id}
        title="AI for this work order"
      />

      {/* Work Order Information — compact KPI strip */}
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-9 gap-2">
        <MiniStat
          icon={CubeIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Qty Ordered"
          value={workOrder.quantity_ordered}
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Qty Complete"
          value={workOrder.quantity_complete}
          valueColor="text-green-600"
        />
        <MiniStat
          icon={ChartBarIcon}
          iconBg="bg-werco-navy-600/20"
          iconColor="text-werco-400"
          label="Op Progress"
          value={`${operationProgress.percent}%`}
          valueColor="text-werco-400"
          subtitle={operationProgress.label}
        />
        <MiniStat
          icon={CalendarDaysIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Due Date"
          value={
            dueDateEditing ? (
              <span className="flex items-center gap-1">
                <label htmlFor="wo-due-date-edit" className="sr-only">
                  Due date
                </label>
                <input
                  id="wo-due-date-edit"
                  type="date"
                  value={dueDateDraft}
                  onChange={(e) => setDueDateDraft(e.target.value)}
                  disabled={savingDueDate}
                  className="input !px-1.5 !py-0.5 text-sm font-normal"
                />
                <button
                  type="button"
                  onClick={handleDueDateSave}
                  disabled={savingDueDate}
                  aria-label="Save due date"
                  className="text-fd-green hover:text-fd-green/80 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <CheckIcon className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setDueDateEditing(false)}
                  disabled={savingDueDate}
                  aria-label="Cancel due date edit"
                  className="text-fd-mute hover:text-fd-red disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5">
                {workOrder.due_date ? formatCentralDate(workOrder.due_date) : '-'}
                {canEditDueDate && (
                  <button
                    type="button"
                    onClick={startDueDateEdit}
                    aria-label="Edit due date"
                    className="text-fd-mute hover:text-fd-blue"
                  >
                    <PencilSquareIcon className="h-4 w-4" />
                  </button>
                )}
              </span>
            )
          }
        />
        <MiniStat
          icon={FlagIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Priority"
          value={workOrder.priority}
        />
        <MiniStat
          icon={BuildingOffice2Icon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Customer"
          value={workOrder.customer_name || '-'}
        />
        <MiniStat
          icon={IdentificationIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Unit #"
          value={
            unitNumberEditing ? (
              <span className="flex items-center gap-1">
                <label htmlFor="wo-unit-number-edit" className="sr-only">
                  Unit #
                </label>
                <input
                  id="wo-unit-number-edit"
                  type="text"
                  maxLength={WO_UNIT_NUMBER_MAX_LENGTH}
                  value={unitNumberDraft}
                  onChange={(e) => setUnitNumberDraft(e.target.value)}
                  disabled={savingUnitNumber}
                  className="input !px-1.5 !py-0.5 text-sm font-normal"
                />
                <button
                  type="button"
                  onClick={handleUnitNumberSave}
                  disabled={savingUnitNumber}
                  aria-label="Save unit #"
                  className="text-fd-green hover:text-fd-green/80 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <CheckIcon className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setUnitNumberEditing(false)}
                  disabled={savingUnitNumber}
                  aria-label="Cancel unit # edit"
                  className="text-fd-mute hover:text-fd-red disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <XMarkIcon className="h-4 w-4" />
                </button>
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5">
                {workOrder.unit_number || '-'}
                {canEditUnitNumber && (
                  <button
                    type="button"
                    onClick={startUnitNumberEdit}
                    aria-label="Edit unit #"
                    className="text-fd-mute hover:text-fd-blue"
                  >
                    <PencilSquareIcon className="h-4 w-4" />
                  </button>
                )}
              </span>
            )
          }
        />
        <MiniStat
          icon={HashtagIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Customer PO"
          value={workOrder.customer_po || '-'}
        />
        <MiniStat
          icon={ClockIcon}
          iconBg="bg-fd-mute/15"
          iconColor="text-fd-mute"
          label="Actual Hours"
          value={Number(workOrder.actual_hours || 0).toFixed(2)}
        />
      </MiniStatStrip>

      {/* Notes & Instructions — folded into a compact panel, editable in place
          at ANY status (see the notesEditing state block for why). */}
      <CockpitPanel
        title="Notes & Instructions"
        // Opt out of CockpitPanel's lg height cap while editing. Verified in the
        // browser: with the cap on, the two textareas push Save/Cancel below the
        // fold of the panel's internal scroll area, so the editor opens with no
        // visible way to commit it. Read mode keeps the cap.
        bodyClassName={`space-y-3 text-sm${notesEditing ? ' lg:max-h-none' : ''}`}
        headerExtra={
          canEditNotes && !notesEditing ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={startNotesEdit}
              className="flex items-center gap-1.5"
            >
              <PencilSquareIcon className="h-4 w-4" />
              Edit
            </Button>
          ) : undefined
        }
      >
        {notesEditing ? (
          <div className="space-y-3">
            <FormField
              label="Notes"
              error={notesTooLong ? `Notes must be ${WO_NOTE_MAX_LENGTH} characters or fewer` : null}
              help={`${notesDraft.length} / ${WO_NOTE_MAX_LENGTH}`}
            >
              {(field) => (
                <textarea
                  {...field}
                  rows={3}
                  maxLength={WO_NOTE_MAX_LENGTH}
                  value={notesDraft}
                  onChange={(e) => setNotesDraft(e.target.value)}
                  disabled={savingNotes}
                  className="input w-full"
                  placeholder="Planning notes for this work order"
                />
              )}
            </FormField>
            <FormField
              label="Special Instructions"
              error={
                instructionsTooLong
                  ? `Special instructions must be ${WO_NOTE_MAX_LENGTH} characters or fewer`
                  : null
              }
              help={`${instructionsDraft.length} / ${WO_NOTE_MAX_LENGTH}`}
            >
              {(field) => (
                <textarea
                  {...field}
                  rows={3}
                  maxLength={WO_NOTE_MAX_LENGTH}
                  value={instructionsDraft}
                  onChange={(e) => setInstructionsDraft(e.target.value)}
                  disabled={savingNotes}
                  className="input w-full"
                  placeholder="Instructions the shop should read before running this job"
                />
              )}
            </FormField>
            <div className="flex items-center gap-2">
              <LoadingButton
                size="sm"
                loading={savingNotes}
                loadingText="Saving..."
                disabled={notesTooLong || instructionsTooLong}
                onClick={handleNotesSave}
              >
                Save
              </LoadingButton>
              <Button variant="secondary" size="sm" onClick={cancelNotesEdit} disabled={savingNotes}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div>
              <p className="text-xs uppercase tracking-wide text-slate-400">Notes</p>
              {/* pre-wrap: the editor accepts newlines, so a multi-line note has
                  to render as the planner typed it rather than as one run-on. */}
              <p className="mt-1 whitespace-pre-wrap break-words text-fd-body">
                {workOrder.notes || 'No notes'}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-slate-400">Special Instructions</p>
              <p className="mt-1 whitespace-pre-wrap break-words text-fd-body">
                {workOrder.special_instructions || 'No special instructions'}
              </p>
            </div>
          </>
        )}
      </CockpitPanel>

      <div className="card card-compact">
        <div className="flex flex-col xl:flex-row xl:items-start xl:justify-between gap-3 mb-3">
          <div className="flex items-start gap-3 min-w-0">
            <DocumentTextIcon className="h-5 w-5 text-fd-blue mt-0.5 flex-shrink-0" />
            <div className="min-w-0">
              <h2 className="card-title">Part Drawing PDF</h2>
              <p className="card-subtitle truncate">
                {selectedDocument
                  ? `${selectedDocument.title} • Rev ${selectedDocument.revision || '-'}`
                  : 'Attach a PDF drawing to show the part preview on this work order.'}
              </p>
            </div>
          </div>
          <span className="text-xs font-semibold px-2 py-1 rounded-sm bg-fd-blue/15 text-fd-blue w-fit flex-shrink-0">
            {workOrderDocuments.length} attached
          </span>
        </div>

        {documentError && (
          <div className="mb-3 rounded-sm border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {documentError}
          </div>
        )}

        <div className="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-4">
          <div className="space-y-4">
            <div className="rounded-lg border border-fd-line bg-slate-900/40">
              <div className="border-b border-fd-line px-4 py-3">
                <h3 className="text-sm font-semibold text-white">Attached PDFs</h3>
              </div>
              <div className="divide-y divide-slate-700">
                {workOrderDocuments.length === 0 ? (
                  <EmptyState
                    icon={DocumentTextIcon}
                    title="No drawing PDF attached"
                    description="Upload a PDF or attach an existing drawing to preview the part here."
                    className="px-4 py-5"
                  />
                ) : (
                  workOrderDocuments.map((document) => (
                    <button
                      key={document.id}
                      type="button"
                      aria-label={`Preview ${document.title}`}
                      onClick={() => setSelectedDocumentId(document.id)}
                      className={`w-full px-4 py-3 text-left transition-colors ${
                        selectedDocumentId === document.id
                          ? 'bg-fd-blue/10 text-white'
                          : 'hover:bg-slate-800/50 text-slate-300'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-semibold">{document.title}</div>
                          <div className="mt-1 truncate text-xs text-slate-400">
                            {document.file_name || document.document_number}
                          </div>
                        </div>
                        <span className="shrink-0 rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
                          {formatFileSize(document.file_size)}
                        </span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </div>

            <form onSubmit={handleUploadWorkOrderPdf} className="rounded-lg border border-fd-line bg-slate-900/40 p-4 space-y-3">
              <h3 className="text-sm font-semibold text-white">Upload PDF</h3>
              <label className="block">
                <span className="text-xs font-medium text-slate-400">PDF File</span>
                <input
                  key={documentUploadInputKey}
                  type="file"
                  aria-label="PDF File"
                  accept=".pdf,application/pdf"
                  onChange={(event) => {
                    const file = event.target.files?.[0] || null;
                    setDocumentUploadFile(file);
                    if (file && !documentTitle.trim()) {
                      setDocumentTitle(file.name.replace(/\.pdf$/i, ''));
                    }
                    setDocumentError('');
                  }}
                  className="mt-1 block w-full text-sm text-slate-300 file:mr-3 file:rounded file:border-0 file:bg-slate-700 file:px-3 file:py-2 file:text-sm file:font-semibold file:text-slate-100 hover:file:bg-slate-600"
                />
              </label>
              <label className="block">
                <span className="text-xs font-medium text-slate-400">Title</span>
                <input
                  type="text"
                  aria-label="Title"
                  value={documentTitle}
                  onChange={(event) => setDocumentTitle(event.target.value)}
                  placeholder="Drawing title"
                  className="input mt-1 w-full"
                />
              </label>
              <Button
                type="submit"
                disabled={documentBusy || !documentUploadFile}
                className="w-full flex items-center justify-center"
              >
                <ArrowUpTrayIcon className="h-4 w-4 mr-2" />
                {documentBusy ? 'Uploading...' : 'Upload PDF'}
              </Button>
            </form>

            <form onSubmit={handleAttachExistingPdf} className="rounded-lg border border-fd-line bg-slate-900/40 p-4 space-y-3">
              <h3 className="text-sm font-semibold text-white">Attach Existing PDF</h3>
              <select
                value={attachDocumentId}
                onChange={(event) => setAttachDocumentId(event.target.value)}
                className="input w-full"
              >
                <option value="">Select unassigned PDF</option>
                {availablePdfDocuments.map((document) => (
                  <option key={document.id} value={document.id}>
                    {document.title} - {document.file_name || document.document_number}
                  </option>
                ))}
              </select>
              <Button
                type="submit"
                variant="secondary"
                disabled={documentBusy || !attachDocumentId}
                className="w-full flex items-center justify-center"
              >
                <PaperClipIcon className="h-4 w-4 mr-2" />
                {documentBusy ? 'Attaching...' : 'Attach PDF'}
              </Button>
            </form>
          </div>

          <div className="rounded-sm border border-fd-line bg-slate-950/60 overflow-hidden">
            <div className="flex items-center justify-between border-b border-fd-line px-4 py-2.5 gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-white">
                  {selectedDocument?.file_name || selectedDocument?.title || 'Preview'}
                </h3>
                <p className="text-xs text-slate-400 truncate">
                  {selectedDocument ? `${selectedDocument.document_number} • ${formatCentralDate(selectedDocument.created_at)}` : 'No PDF selected'}
                </p>
              </div>
              {selectedDocument && (
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setSelectedDocumentId(selectedDocument.id)}
                    className="flex items-center"
                  >
                    <EyeIcon className="h-4 w-4 mr-1" />
                    Preview
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleDownloadWorkOrderPdf(selectedDocument)}
                    className="flex items-center"
                  >
                    <ArrowDownTrayIcon className="h-4 w-4 mr-1" />
                    Download
                  </Button>
                </div>
              )}
            </div>
            {documentPreviewLoading ? (
              <div className="flex h-72 lg:h-[clamp(320px,46vh,520px)] items-center justify-center text-sm text-slate-400">
                Loading PDF preview...
              </div>
            ) : documentPreviewUrl ? (
              <iframe
                title={selectedDocument?.title || 'Work order drawing PDF'}
                src={documentPreviewUrl}
                className="h-72 lg:h-[clamp(320px,46vh,520px)] w-full bg-white"
              />
            ) : (
              <div className="flex h-32 flex-col items-center justify-center px-4 text-center text-slate-400">
                <DocumentTextIcon className="mb-2 h-8 w-8 text-slate-600" />
                <p className="text-sm">No PDF preview available.</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Rendered for every WO type: on laser_cutting WOs (child or standalone)
          the backend operates on the WO directly, so this card doubles as the
          re-import / manual-add surface. Hidden only when there is nothing to
          show (no nests) AND nothing the viewer could do (no manage rights). */}
      {(canManageNests || laserNests.length > 0) && (
        <div className="card card-compact">
          <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-3 mb-3">
            <div className="flex items-start gap-3 min-w-0">
              <ArrowUpTrayIcon className="h-5 w-5 text-fd-red mt-0.5 flex-shrink-0" />
              <div className="min-w-0">
                <h2 className="card-title">Laser Nest Package</h2>
                <p className="card-subtitle truncate">
                  {workOrder.work_order_type === 'laser_cutting'
                    ? 'Import a zipped Ermaksan folder, a nest-report PDF, or a server folder path to add nests to this laser work order.'
                    : 'Import a zipped Ermaksan folder, a nest-report PDF, or a server folder path to create the linked laser cutting work order.'}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {canManageNests && (
                <>
                  <Button
                    size="sm"
                    onClick={() => setNestImportWizardOpen(true)}
                    className="flex items-center gap-1.5 whitespace-nowrap"
                  >
                    <ArrowUpTrayIcon className="h-4 w-4" />
                    Import nest package
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={openAddNestModal}
                    className="flex items-center gap-1.5 whitespace-nowrap"
                  >
                    <PlusIcon className="h-4 w-4" />
                    Add nest manually
                  </Button>
                </>
              )}
            </div>
          </div>

          {/* Hidden file input shared by all per-nest "Attach PDF" actions. */}
          <input
            ref={nestAttachInputRef}
            type="file"
            aria-label="Attach nest PDF"
            accept="application/pdf"
            onChange={handleNestAttachFileChange}
            className="hidden"
          />

          {nestActionError && (
            <div className="mt-3 rounded border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-sm text-fd-red">
              {nestActionError}
            </div>
          )}

          {laserNests.length > 0 && (
            <div className="mt-5">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fd-mute">
                Nests on this work order
              </h3>
              <div className="space-y-2">
                {laserNests.map(({ operation, nest }) => {
                  const acting = nestActionId === nest.id;
                  const showPreview = previewNestId === nest.id;
                  return (
                    <div
                      key={nest.id}
                      id={`nest-${nest.id}`}
                      className="scroll-mt-20 rounded-sm border border-fd-line bg-fd-sunken p-3"
                    >
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-lg font-bold text-fd-ink">
                              {nest.cnc_number || nest.nest_name}
                            </span>
                            {nest.cnc_number && nest.nest_name !== nest.cnc_number && (
                              <span className="text-sm text-fd-mute">{nest.nest_name}</span>
                            )}
                            {nest.has_document && (
                              <span className="inline-flex items-center gap-1 rounded bg-fd-blue/15 px-2 py-0.5 text-xs font-semibold text-fd-blue">
                                <PaperClipIcon className="h-3.5 w-3.5" />
                                PDF
                              </span>
                            )}
                          </div>
                          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-fd-mute">
                            <span>
                              Runs:{' '}
                              <span className="font-semibold tabular-nums text-fd-body">
                                {nest.completed_runs}/{nest.planned_runs}
                              </span>
                            </span>
                            {(nest.material || nest.thickness) && (
                              <span>{[nest.material, nest.thickness].filter(Boolean).join(' • ')}</span>
                            )}
                            {nest.sheet_size && <span>Sheet: {nest.sheet_size}</span>}
                            <span>Op {operation.sequence}</span>
                            <span>
                              WC:{' '}
                              <span className="font-semibold text-fd-body">
                                {operation.work_center_name || `#${operation.work_center_id}`}
                              </span>
                            </span>
                          </div>
                        </div>

                        {canManageNests && (
                          <div className="flex flex-wrap items-center gap-1.5">
                            <select
                              value={String(operation.work_center_id)}
                              onChange={(e) => handleReassignNestWorkCenter(operation, Number(e.target.value))}
                              // A finished run's labor history belongs to the machine it
                              // ran on — the server 409s completed/in-progress moves, so
                              // don't offer them.
                              disabled={
                                reassigningOpId === operation.id ||
                                operation.status === 'complete' ||
                                operation.status === 'in_progress'
                              }
                              aria-label={`Work center for ${nest.cnc_number || nest.nest_name}`}
                              title={
                                operation.status === 'complete'
                                  ? 'Completed operations cannot be moved'
                                  : operation.status === 'in_progress'
                                    ? 'Clock out before moving the operation'
                                    : undefined
                              }
                              className="input !h-8 !py-0 !px-2 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {/* Keep the current WC selectable even if it fell off the
                                  active list, so the select never renders blank. */}
                              {!workCenters.some((wc) => wc.id === operation.work_center_id) && (
                                <option value={String(operation.work_center_id)}>
                                  {operation.work_center_name || `WC #${operation.work_center_id}`}
                                </option>
                              )}
                              {workCenters.map((wc) => (
                                <option key={wc.id} value={String(wc.id)}>
                                  {wc.name || wc.code}
                                </option>
                              ))}
                            </select>
                            {nest.has_document ? (
                              <>
                                <button
                                  type="button"
                                  onClick={() => setPreviewNestId(showPreview ? null : nest.id)}
                                  className="btn-secondary btn-sm flex items-center gap-1"
                                >
                                  <EyeIcon className="h-4 w-4" />
                                  {showPreview ? 'Hide PDF' : 'View PDF'}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => handleDetachNestPdf(nest)}
                                  disabled={acting}
                                  className="btn-secondary btn-sm"
                                  title="Detach PDF"
                                >
                                  Detach
                                </button>
                              </>
                            ) : (
                              <button
                                type="button"
                                onClick={() => promptAttachNestPdf(nest.id)}
                                disabled={acting}
                                className="btn-secondary btn-sm flex items-center gap-1"
                              >
                                <PaperClipIcon className="h-4 w-4" />
                                Attach PDF
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => openEditNestModal(nest, operation.id)}
                              className="btn-secondary btn-sm flex items-center gap-1"
                              title="Edit nest"
                            >
                              <PencilSquareIcon className="h-4 w-4" />
                              Edit
                            </button>
                            <button
                              type="button"
                              onClick={() => handleDeleteNest(nest)}
                              disabled={acting}
                              className="btn-secondary btn-sm flex items-center gap-1 text-fd-red hover:text-fd-red/80"
                              title="Delete nest"
                            >
                              <TrashIcon className="h-4 w-4" />
                            </button>
                          </div>
                        )}
                      </div>

                      {showPreview && nest.has_document && (
                        <div className="mt-3">
                          <LaserNestPdfPreview
                            laserNestId={nest.id}
                            fileName={nest.document_file_name}
                            heightClassName="h-72 lg:h-[clamp(320px,42vh,460px)]"
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
        {isAdminView && (
          <CockpitPanel
            title="Operator Activity (Admin)"
            className="xl:col-span-5"
            headerExtra={
              <span className="text-xs text-slate-400">Live: {activeUsersOnWorkOrder.length} clocked in</span>
            }
          >
            {activeUsersOnWorkOrder.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-700">
                  <thead className="bg-slate-800/50">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Operator</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Work Center</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Entry Type</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Clocked In (CT)</th>
                    </tr>
                  </thead>
                  <tbody className="bg-fd-panel divide-y divide-slate-700">
                    {activeUsersOnWorkOrder.map((entry) => (
                      <tr key={`${entry.user_id}-${entry.clock_in ?? ''}-${entry.operation ?? 'op'}`} className="hover:bg-slate-800/50">
                        <td className="px-3 py-2 text-sm font-medium text-white truncate">
                          {entry.user_name || userNameById[entry.user_id] || `User #${entry.user_id}`}
                        </td>
                        <td className="px-3 py-2 text-sm text-slate-300 truncate">{entry.operation || '-'}</td>
                        <td className="px-3 py-2 text-sm text-slate-300 truncate">{entry.work_center || '-'}</td>
                        <td className="px-3 py-2 text-sm text-slate-300">
                          {entry.entry_type ? entry.entry_type.toString().replace('_', ' ') : '-'}
                        </td>
                        <td className="px-3 py-2 text-sm text-slate-300">{formatDateTimeCT(entry.clock_in)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                icon={UserGroupIcon}
                title="No active operators"
                description="No one is currently clocked in on this work order."
              />
            )}
          </CockpitPanel>
        )}

      <CockpitPanel
        title="Blockers"
        subtitle="Open issues that can stop this work order from moving cleanly."
        className={isAdminView ? 'xl:col-span-7' : 'xl:col-span-12'}
        bodyClassName="lg:max-h-none"
        headerExtra={
          <span className="text-xs font-semibold px-2 py-1 rounded-sm bg-amber-500/20 text-amber-300 w-fit flex items-center gap-1">
            <ExclamationTriangleIcon className="h-3.5 w-3.5" />
            {blockers.filter((item) => item.status === 'open' || item.status === 'acknowledged').length} open
          </span>
        }
      >
        <div className="grid grid-cols-1 xl:grid-cols-[1fr_320px] gap-4">
          <div className="space-y-3 xl:max-h-[440px] xl:overflow-y-auto pr-1">
            {blockers.length === 0 ? (
              <div className="rounded-sm border border-fd-line bg-slate-900/40">
                <EmptyState
                  icon={CheckCircleIcon}
                  title="No blockers reported"
                  description="This work order has no open issues. Use the form to report one if the job is stuck."
                />
              </div>
            ) : (
              blockers.map((blocker) => (
                <div key={blocker.id} className="rounded-sm border border-fd-line bg-slate-900/40 p-3">
                  <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-white">{blocker.title}</span>
                        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                          blocker.severity === 'critical' || blocker.severity === 'high'
                            ? 'bg-red-500/20 text-red-300'
                            : blocker.severity === 'medium'
                              ? 'bg-amber-500/20 text-amber-300'
                              : 'bg-blue-500/20 text-blue-300'
                        }`}>
                          {blocker.severity}
                        </span>
                        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                          blocker.status === 'resolved'
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : 'bg-slate-700 text-slate-200'
                        }`}>
                          {blocker.status.replace('_', ' ')}
                        </span>
                      </div>
                      <div className="text-sm text-slate-400 mt-1">
                        {blocker.category.replace('_', ' ')}
                        {blocker.operation_name ? ` • ${blocker.operation_name}` : ''}
                        {blocker.material_part_number ? ` • ${blocker.material_part_number}` : ''}
                      </div>
                      {blocker.note && <p className="text-sm text-slate-300 mt-2">{blocker.note}</p>}
                    </div>
                    {/* RESOLVE is ADMIN/MANAGER/SUPERVISOR on the server. It used to
                        render for every role, so an operator or a viewer staring at a
                        stuck job was shown the one button guaranteed to 403 -- and no
                        other control on this page could get the job moving. Below the
                        gate the copy says who closes a blocker AND points at the thing
                        the reader can actually do, since clearing the hold is open to
                        any authenticated user. Do NOT widen the endpoint to match. */}
                    {(blocker.status === 'open' || blocker.status === 'acknowledged') &&
                      (canResolveBlocker ? (
                        <button
                          type="button"
                          onClick={() => setResolveBlockerTarget(blocker)}
                          disabled={resolvingBlockerId === blocker.id}
                          className="btn-success btn-sm"
                        >
                          {resolvingBlockerId === blocker.id ? 'Resolving...' : 'Resolve'}
                        </button>
                      ) : (
                        <p className="text-xs text-slate-400 md:max-w-[15rem] md:text-right">
                          A supervisor or manager closes a blocker. If it left an operation on hold,
                          you can still clear that hold yourself in Operations / Routing above —
                          clearing a hold does not close the blocker.
                        </p>
                      ))}
                  </div>
                </div>
              ))
            )}
          </div>

          <form onSubmit={handleCreateBlocker} className="rounded-sm border border-fd-line bg-slate-900/40 p-3 space-y-3">
            <h3 className="font-semibold text-white">Report Blocker</h3>
            <div>
              <label htmlFor="blocker-operation" className="text-sm text-slate-400 block mb-1">Operation</label>
              <select
                id="blocker-operation"
                value={blockerForm.operation_id}
                onChange={(e) => setBlockerForm({ ...blockerForm, operation_id: e.target.value })}
                className="input"
              >
                <option value="">Whole work order</option>
                {workOrder.operations.map((op) => (
                  <option key={op.id} value={op.id}>
                    {`${
                      hasOperationNumber(op.operation_number)
                        ? formatOperationLabel(op.operation_number)
                        : `Op ${op.sequence}`
                    } - ${op.name}`}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="blocker-category" className="text-sm text-slate-400 block mb-1">Category</label>
                <select
                  id="blocker-category"
                  value={blockerForm.category}
                  onChange={(e) => setBlockerForm({ ...blockerForm, category: e.target.value as WorkOrderBlockerCategory })}
                  className="input"
                >
                  <option value="material_missing">Material missing</option>
                  <option value="machine_down">Machine down</option>
                  <option value="tooling_missing">Tooling missing</option>
                  <option value="quality_hold">Quality hold</option>
                  <option value="labor_unavailable">Labor unavailable</option>
                  <option value="engineering_question">Engineering question</option>
                  <option value="other">Other</option>
                </select>
              </div>
              <div>
                <label htmlFor="blocker-severity" className="text-sm text-slate-400 block mb-1">Severity</label>
                <select
                  id="blocker-severity"
                  value={blockerForm.severity}
                  onChange={(e) => setBlockerForm({ ...blockerForm, severity: e.target.value as WorkOrderBlockerSeverity })}
                  className="input"
                >
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                  <option value="critical">Critical</option>
                  <option value="low">Low</option>
                </select>
              </div>
            </div>
            <div>
              <label htmlFor="blocker-note" className="text-sm text-slate-400 block mb-1">Note</label>
              <textarea
                id="blocker-note"
                aria-label="Note"
                value={blockerForm.note}
                onChange={(e) => setBlockerForm({ ...blockerForm, note: e.target.value })}
                className="input"
                rows={3}
                maxLength={2000}
                placeholder="What is stopping the job?"
              />
            </div>
            <Button type="submit" disabled={submittingBlocker} className="w-full">
              {submittingBlocker ? 'Reporting...' : 'Report Blocker'}
            </Button>
          </form>
        </div>
      </CockpitPanel>
      </div>

      {/* Operations */}
      <div className="card card-compact">
        <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <h2 className="card-title">Operations / Routing</h2>
          {/* Operation sequencing mode. Rendered beside the routing it governs,
              because the Status column below is what it changes. */}
          {!sequencingApplies ? (
            <p className="flex items-start gap-2 rounded-sm border border-fd-line bg-fd-sunken px-3 py-2 text-xs text-fd-mute lg:max-w-md">
              <InformationCircleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
              <span>
                Nest work orders are always pooled — nests can be run in any order, so
                operation sequencing does not apply here.
              </span>
            </p>
          ) : (
            <div className="flex items-start gap-3 rounded-sm border border-fd-line bg-fd-sunken px-3 py-2 lg:max-w-md">
              {canEditSequencing ? (
                <button
                  type="button"
                  role="switch"
                  aria-checked={sequentialOperations}
                  aria-label="Sequential operations"
                  aria-describedby="wo-sequencing-help"
                  disabled={savingSequencing}
                  onClick={handleSequencingToggle}
                  className={`relative mt-0.5 h-[22px] w-[40px] shrink-0 rounded-full border transition-colors duration-150 ease-out disabled:cursor-not-allowed disabled:opacity-50 ${
                    sequentialOperations
                      ? 'border-fd-blue bg-fd-blue'
                      : 'border-fd-line-bright bg-slate-800'
                  }`}
                >
                  <span
                    aria-hidden="true"
                    className={`absolute top-[2px] h-4 w-4 rounded-full bg-white transition-all duration-150 ease-out ${
                      sequentialOperations ? 'right-[2px]' : 'left-[2px]'
                    }`}
                  />
                </button>
              ) : (
                // Read-only viewers still need to know WHICH rule the floor is
                // working under to read the Status column, so the mode is shown
                // even where the control is not offered.
                <span
                  className={`mt-0.5 inline-flex shrink-0 rounded-sm px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${
                    sequentialOperations ? 'bg-fd-blue/15 text-fd-blue' : 'bg-slate-800 text-slate-300'
                  }`}
                >
                  {sequentialOperations ? 'On' : 'Off'}
                </span>
              )}
              <div className="min-w-0">
                <p className="text-xs font-semibold uppercase tracking-wide text-fd-ink">
                  Sequential operations
                </p>
                <p id="wo-sequencing-help" className="mt-0.5 text-xs text-fd-mute">
                  {sequentialOperations
                    ? 'Each operation unlocks when the previous one is complete'
                    : 'Operations at the same work center can run in any order'}
                </p>
              </div>
              {savingSequencing && <Spinner size="sm" className="mt-0.5 shrink-0" />}
            </div>
          )}
        </div>

        {workOrder.operations.length === 0 ? (
          <EmptyState
            icon={WrenchScrewdriverIcon}
            title="No operations defined"
            description="This work order has no routing operations yet."
          />
        ) : (
          <div className="overflow-x-auto lg:max-h-[clamp(360px,55vh,640px)] lg:overflow-y-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Seq</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Group</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Est. Hours</th>
                  {isAdminView && (
                    <>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Started By</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Started At (CT)</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Completed By</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Completed At (CT)</th>
                    </>
                  )}
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-fd-panel divide-y divide-slate-700">
                {(() => {
                  let lastGroup = '';
                  return workOrder.operations.map((op) => {
                    const isNewGroup = op.operation_group && op.operation_group !== lastGroup;
                    if (op.operation_group) lastGroup = op.operation_group;
                    const operationTarget = operationRunTarget(op, workOrder.quantity_ordered);
                    // Non-null only while the SERVER would refuse this operation's
                    // office verbs — see `lowestIncompleteOperation`. Doubles as the
                    // hover explanation, so the disabled control says why it is
                    // disabled instead of looking broken. Strict `>` mirrors the
                    // gate's `sequence < candidate.sequence`: operations sharing a
                    // sequence number do not block each other, and the blocking
                    // operation never blocks itself.
                    const sequenceBlockReason =
                      lowestIncompleteOperation && op.sequence > lowestIncompleteOperation.sequence
                        ? `Previous operations must be completed first — this work order runs its operations in sequence, and operation ${lowestIncompleteOperation.sequence} (${lowestIncompleteOperation.name}) is not complete.`
                        : null;
                    
                    // WHY this row is held, for the compact in-row disclosure below.
                    // Computed once and reused by the confirm dialog's copy so the
                    // pre-click reason and the in-dialog reason cannot diverge.
                    // Non-null only for an ON_HOLD row -- `hold_context` is null on
                    // every other one, by construction on the server.
                    const holdSummary = op.status === 'on_hold' ? summarizeHold(op.hold_context) : null;

                    const groupColors: Record<string, string> = {
                      'LASER': 'bg-fd-red/15 text-fd-red',
                      'MACHINE': 'bg-fd-blue/15 text-fd-blue',
                      'BEND': 'bg-fd-amber/15 text-fd-amber',
                      'WELD': 'bg-amber-500/15 text-amber-300',
                      'FINISH': 'bg-fd-cyan/15 text-fd-cyan',
                      'ASSEMBLY': 'bg-fd-green/15 text-fd-green',
                      'INSPECT': 'bg-fd-blue/15 text-fd-blue',
                    };
                    
                    return (
                      <React.Fragment key={op.id}>
                      <tr
                        className={`hover:bg-slate-800/50 ${isNewGroup ? 'border-t-2 border-slate-600' : ''}`}
                      >
                        <td className="px-4 py-3 font-medium text-sm">{op.sequence}</td>
                        <td className="px-4 py-3">
                          {op.operation_group && (
                            <span className={`inline-flex px-2 py-1 rounded text-xs font-bold ${groupColors[op.operation_group] || 'bg-slate-800 text-slate-100'}`}>
                              {op.operation_group}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div>
                            <div className="font-medium text-sm">{op.name}</div>
                            {op.description && (
                              <div className="text-xs text-slate-400 mt-0.5">{op.description}</div>
                            )}
                            {op.laser_nest && nestPanelShown ? (
                              // De-dup: full nest detail lives once in the Laser
                              // Nest Package card; cross-link here by stable id.
                              <a
                                href={`#nest-${op.laser_nest.id}`}
                                className="mt-2 inline-flex flex-wrap items-center gap-1.5 rounded-sm border border-fd-line bg-slate-900/50 px-2 py-1 text-xs font-medium text-fd-red hover:border-fd-line-bright"
                                title="View nest detail in Laser Nest Package"
                              >
                                <DocumentTextIcon className="h-4 w-4" />
                                {op.laser_nest.cnc_number ? (
                                  <span className="font-mono">CNC# {op.laser_nest.cnc_number}</span>
                                ) : (
                                  op.laser_nest.nest_name
                                )}
                                {op.laser_nest.has_document && (
                                  <PaperClipIcon className="h-3.5 w-3.5 text-fd-blue" title="Reference PDF attached" />
                                )}
                                <span className="tabular-nums text-slate-400">
                                  {op.laser_nest.completed_runs}/{op.laser_nest.planned_runs}
                                </span>
                              </a>
                            ) : op.laser_nest ? (
                              <div className="mt-2 rounded-sm border border-fd-line bg-slate-900/50 px-2 py-1.5 text-xs text-slate-300">
                                <div className="flex flex-wrap items-center gap-1.5 font-medium text-fd-red">
                                  <DocumentTextIcon className="h-4 w-4" />
                                  {op.laser_nest.cnc_number ? (
                                    <span className="font-mono">CNC# {op.laser_nest.cnc_number}</span>
                                  ) : (
                                    op.laser_nest.nest_name
                                  )}
                                  {op.laser_nest.has_document && (
                                    <PaperClipIcon className="h-3.5 w-3.5 text-fd-blue" title="Reference PDF attached" />
                                  )}
                                </div>
                                <div className="mt-1 grid grid-cols-1 sm:grid-cols-2 gap-x-3 gap-y-1">
                                  {op.laser_nest.cnc_file_name && <span>File: {op.laser_nest.cnc_file_name}</span>}
                                  <span>Runs: {op.laser_nest.completed_runs}/{op.laser_nest.planned_runs}</span>
                                  {(op.laser_nest.material || op.laser_nest.thickness) && (
                                    <span>{[op.laser_nest.material, op.laser_nest.thickness].filter(Boolean).join(' • ')}</span>
                                  )}
                                  {op.laser_nest.sheet_size && <span>Sheet: {op.laser_nest.sheet_size}</span>}
                                </div>
                              </div>
                            ) : null}
                            {/* WHY IT IS HELD -- disclosed on the row, BEFORE anyone
                                clicks Clear Hold. Reason and attribution render on
                                their own terms: a bare hold (mis-tap at the kiosk --
                                no note, category OTHER) files no blocker, so it has a
                                holder and no reason, and gating one on the other would
                                make exactly that case read as anonymous AND reasonless.
                                Free text is read straight off note/title: this response
                                withholds nothing, and `has_note` is not sent. */}
                            {holdSummary && (
                              <div className="mt-2 rounded-sm border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs">
                                <div className="flex flex-wrap items-center gap-1.5 font-semibold text-amber-300">
                                  <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
                                  <span>{holdSummary.headline ?? 'On hold \u2014 reason not recorded'}</span>
                                </div>
                                {holdSummary.title && (
                                  <div className="mt-1 text-amber-100">{holdSummary.title}</div>
                                )}
                                {holdSummary.note && (
                                  <p className="mt-1 text-amber-100">{holdSummary.note}</p>
                                )}
                                <div className="mt-1 text-amber-200/70">
                                  {holdSummary.attribution ?? 'Who placed the hold was not recorded'}
                                </div>
                              </div>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {op.component_part_number ? (
                            <div>
                              <div className="font-medium text-sm text-blue-600">{op.component_part_number}</div>
                              {op.component_part_name && (
                                <div className="text-xs text-slate-400">{op.component_part_name}</div>
                              )}
                            </div>
                          ) : (
                            <span className="text-slate-500 text-sm">-</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div>
                            <span className="font-medium text-sm">{op.quantity_complete}</span>
                            <span className="text-slate-400 text-sm">/{operationTarget}</span>
                            {op.laser_nest && <div className="text-xs text-slate-400">runs</div>}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-sm">
                          {(Number(op.setup_time_hours || 0) + Number(op.run_time_hours || 0)).toFixed(2)}
                        </td>
                        {isAdminView && (
                          <>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {op.started_by ? (userNameById[op.started_by] || `User #${op.started_by}`) : '-'}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {formatDateTimeCT(op.actual_start)}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {op.completed_by ? (userNameById[op.completed_by] || `User #${op.completed_by}`) : '-'}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {formatDateTimeCT(op.actual_end)}
                            </td>
                          </>
                        )}
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium capitalize ${statusColor(op.status)}`}>
                            {op.status.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-center">
                          <div className="flex items-center justify-center gap-3">
                            {/* CLEAR HOLD -- first in the group because on a held row
                                it is the only action that moves the job. Ungated on
                                purpose: `PUT /shop-floor/operations/{id}/resume` takes
                                `get_current_user`, i.e. any authenticated tenant user,
                                so gating it here would hide a control the server allows.
                                Styled like its siblings (icon + text) rather than as a
                                <Button>, which is the established chrome for this cell. */}
                            {op.status === 'on_hold' && (
                              <button
                                type="button"
                                onClick={() => setClearHoldTarget(op)}
                                disabled={clearingHoldOpId !== null}
                                className="text-amber-400 hover:text-amber-300 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                                title="Lift the hold on this operation"
                              >
                                {clearingHoldOpId === op.id ? (
                                  <>
                                    <ArrowPathIcon className="h-5 w-5 inline animate-spin" /> Clearing...
                                  </>
                                ) : (
                                  <>
                                    <PlayIcon className="h-5 w-5 inline" /> Clear hold
                                  </>
                                )}
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => setStepsOpenOpId(stepsOpenOpId === op.id ? null : op.id)}
                              aria-expanded={stepsOpenOpId === op.id}
                              className="text-fd-cyan hover:text-cyan-300 text-sm font-medium"
                              title="Process steps evidence"
                            >
                              <ClipboardDocumentCheckIcon className="h-5 w-5 inline" /> Steps
                            </button>
                            {/* Per-operation material tie. Gated on
                                `canEditMaterialTies` (work_orders:edit — the
                                same trio the tie endpoints enforce), NOT on
                                `canCompleteOperation`, which is a larger set:
                                QUALITY may complete an operation but may not
                                decide what stock it eats. The dialog is always
                                OPERATION-scoped; it never creates a
                                whole-work-order tie. */}
                            {canEditMaterialTies && (
                              <button
                                type="button"
                                onClick={() => setTieTarget(op)}
                                className="text-fd-blue hover:text-blue-300 text-sm font-medium"
                                title="Tie stock material to this operation"
                              >
                                <CubeIcon className="h-5 w-5 inline" /> Material
                              </button>
                            )}
                            {canCompleteOperation && op.status !== 'complete' && workOrder.status !== 'draft' && (
                              <button
                                onClick={() => handleCompleteOperation(op)}
                                disabled={completingOpId === op.id || sequenceBlockReason !== null}
                                className="text-green-600 hover:text-green-300 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                                title={sequenceBlockReason ?? 'Complete Operation'}
                              >
                                {completingOpId === op.id ? (
                                  <>
                                    <ArrowPathIcon className="h-5 w-5 inline animate-spin" /> Completing...
                                  </>
                                ) : (
                                  <>
                                    <CheckCircleIcon className="h-5 w-5 inline" /> Complete
                                  </>
                                )}
                              </button>
                            )}
                            {op.status === 'complete' && (
                              <span className="text-slate-500 text-sm">Done</span>
                            )}
                            {/* Supervisor over-count correction — only when there is
                                a recorded count to walk back. The server decides what
                                is actually correctable (non-optimistic). */}
                            {canCorrectCount && Number(op.quantity_complete || 0) > 0 && (
                              <button
                                type="button"
                                onClick={() => openCorrectModal(op)}
                                disabled={correctingOpId === op.id}
                                className="text-amber-400 hover:text-amber-300 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                                title="Correct over-counted quantity"
                              >
                                <MinusCircleIcon className="h-5 w-5 inline" /> Correct count
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                      {stepsOpenOpId === op.id && (
                        <tr className={isNewGroup ? '' : 'border-t-0'}>
                          <td colSpan={isAdminView ? 12 : 8} className="bg-slate-900/40 p-0">
                            <OperationStepsPanel operationId={op.id} />
                          </td>
                        </tr>
                      )}
                      </React.Fragment>
                    );
                  });
                })()}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Material Ties — the optional link between this work order (or one of
          its operations) and the stock material it depletes. Its own stacked
          section, deliberately not a tab: this page's content is all co-visible.
          `workOrder.updated_at` is the freshness seam — a completion that posts
          consumption bumps the work order, which re-runs the panel's fetch;
          the panel adds no poller of its own. */}
      <MaterialTiesPanel
        workOrderId={workOrder.id}
        workOrderUpdatedAt={workOrder.updated_at}
        // A tie written from the Operations table above does not bump
        // `work_orders.updated_at`, so without this the list right here would go
        // stale the moment someone used the other door onto the same rows.
        refreshToken={tieRefreshToken}
        canEdit={canEditMaterialTies}
        // Only so the panel can compute each tie's live consumption target and flag
        // one that is OVER-consumed — the open loop an office reduce on a COMPLETE
        // operation leaves behind, which nothing else on this page distinguishes
        // from an ordinary tie.
        operations={workOrder.operations}
      />

      {/* Per-operation material tie editor, opened from the Operations table.
          Rendered once here rather than per row so only one dialog exists.
          `onSaved` bumps the token above — a tie write does not touch the work
          order, so nothing else would refresh the panel. */}
      <OperationMaterialTieModal
        open={tieTarget !== null}
        workOrderId={workOrder.id}
        operation={tieTarget}
        operationTarget={tieTarget ? operationRunTarget(tieTarget, workOrder.quantity_ordered) : 0}
        onClose={() => setTieTarget(null)}
        onSaved={() => setTieRefreshToken((token) => token + 1)}
      />

      {/* Duplicate this work order's plan onto a new draft. Navigation is the
          caller's job so the dialog stays reusable; the new WO is a draft, so
          landing on it is where the planner reviews and releases. */}
      {canDuplicateWorkOrder && (
        <DuplicateWorkOrderModal
          open={duplicateOpen}
          workOrder={workOrder}
          // This page already has the operations loaded, so the dialog must not
          // re-read the work order just to learn its quantity is nest-derived.
          hasLaserNests={laserNests.length > 0}
          onClose={() => setDuplicateOpen(false)}
          onDuplicated={(result) => navigate(`/work-orders/${result.work_order.id}`)}
        />
      )}

      {/* Catalog this work order's plan under a name. Unlike Duplicate this
          creates NOTHING to navigate to — one row pointing at this work order —
          and it must leave this page's record completely untouched, so there is
          no reload on success either. */}
      {canSaveAsTemplate && (
        <SaveAsTemplateModal
          open={saveTemplateOpen}
          workOrder={workOrder}
          // This page already has the nests loaded, so the dialog never has to
          // ask what kind of job this is.
          hasLaserNests={laserNests.length > 0}
          onClose={() => setSaveTemplateOpen(false)}
        />
      )}

      {/* Backflush dry run — the OTHER consumption path: BOM/routing components
          pulled automatically at completion when the finished part has opted in.
          A pure read that writes nothing, loaded on demand. Sits next to the tie
          panel because both legs post to the same ledger and a planner needs to
          see them together. */}
      <BackflushPreviewPanel workOrderId={workOrder.id} />

      {/* Material Requirements */}
      {materialReqs && materialReqs.has_bom && materialReqs.materials.length > 0 && (
        <div className="card card-compact">
          <div className="flex items-center justify-between mb-3 gap-3">
            <div className="flex items-center gap-2 min-w-0">
              <CubeIcon className="h-5 w-5 text-slate-400 flex-shrink-0" />
              <h2 className="card-title">Material Requirements</h2>
            </div>
            <span className="text-sm text-slate-400 truncate">
              BOM Rev {materialReqs.bom_revision} • Qty: {materialReqs.quantity_ordered}
            </span>
          </div>

          <div className="overflow-x-auto lg:max-h-[clamp(320px,45vh,520px)] lg:overflow-y-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Item</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part Number</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Description</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty/Asm</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty Required</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Scrap</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Total Needed</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">UOM</th>
                </tr>
              </thead>
              <tbody className="bg-fd-panel divide-y divide-slate-700">
                {materialReqs.materials.map((mat) => (
                  <tr key={mat.bom_item_id} className={mat.is_optional ? 'bg-yellow-500/10' : 'hover:bg-slate-800/50'}>
                    <td className="px-4 py-3 text-sm font-medium">{mat.item_number}</td>
                    <td className="px-4 py-3 text-sm font-medium text-blue-600">{mat.part_number}</td>
                    <td className="px-4 py-3 text-sm text-slate-300">{mat.part_name}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded ${
                        mat.part_type === 'purchased' ? 'bg-green-500/20 text-green-300' :
                        mat.part_type === 'manufactured' ? 'bg-blue-500/20 text-blue-300' :
                        mat.part_type === 'raw_material' ? 'bg-yellow-500/20 text-yellow-300' :
                        'bg-slate-800 text-slate-100'
                      }`}>
                        {mat.part_type.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-right">{mat.quantity_per_assembly}</td>
                    <td className="px-4 py-3 text-sm text-right font-medium">{mat.quantity_required}</td>
                    <td className="px-4 py-3 text-sm text-right text-slate-400">
                      {mat.scrap_allowance > 0 ? `+${mat.scrap_allowance}` : '-'}
                    </td>
                    <td className="px-4 py-3 text-sm text-right font-bold text-green-400">{mat.total_required}</td>
                    <td className="px-4 py-3 text-sm text-slate-400">{mat.unit_of_measure}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          
          <div className="mt-3 text-sm text-slate-400">
            <span className="bg-yellow-500/10 px-2 py-1 rounded-sm">Optional items</span> highlighted in yellow
          </div>
        </div>
      )}

      {/* Part-less (standalone laser nest) WOs have no part to hang a BOM on —
          skip the "No BOM" nudge entirely rather than pointing at a part that
          doesn't exist. */}
      {materialReqs && !materialReqs.has_bom && workOrder.part_id != null && (
        <div className="card card-compact">
          <EmptyState
            icon={CubeIcon}
            title="No BOM defined for this part"
            description="Material requirements will appear here once a bill of materials is added."
          />
        </div>
      )}

      {canManageNests && (
        <>
          <LaserNestManualModal
            open={nestModalOpen}
            onClose={() => setNestModalOpen(false)}
            workOrderId={workOrder.id}
            nest={nestModalTarget}
            // Operation-scoped material ties hang off this id — the edit path
            // had been discarding it even though the caller already has it.
            workOrderOperationId={nestModalOperationId}
            onSaved={handleNestSaved}
          />
          <LaserNestImportWizard
            open={nestImportWizardOpen}
            onClose={() => setNestImportWizardOpen(false)}
            workOrderId={workOrder.id}
            onImported={handleNestPackageImported}
          />
        </>
      )}

      {/* Supervisor "Correct count" — office over-count correction (reduce-
          production). Non-optimistic: the count on screen only changes via the
          post-success refetch, and a refusal renders verbatim INLINE below. */}
      <Modal
        open={correctTarget !== null}
        onClose={() => {
          // Don't let the user dismiss mid-request; reflect only server state.
          if (correctingOpId !== null) return;
          closeCorrectModal();
        }}
        size="md"
        padded={false}
        scroll={false}
      >
        {correctTarget && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Correct Count — Op {correctTarget.sequence} {correctTarget.name}</h3>
              <button
                onClick={closeCorrectModal}
                disabled={correctingOpId !== null}
                className="p-2 rounded-lg hover:bg-slate-800 disabled:opacity-50 disabled:cursor-not-allowed"
                aria-label="Close"
              >
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <div className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200/90">
                Removes over-reported good quantity from this operation — a miscount correction, not
                scrap. Recorded on the audit trail with your name and reason.
              </div>

              <p className="text-sm text-slate-400">
                Completed now:{' '}
                <span className="font-semibold text-white">
                  {correctTarget.quantity_complete} / {Number(correctTarget.component_quantity || workOrder.quantity_ordered || 0)}
                </span>
              </p>

              <div>
                <label htmlFor="wo-correct-qty" className="label">Quantity to remove</label>
                <input
                  id="wo-correct-qty"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={correctData.quantity}
                  onChange={(e) => setCorrectData({ ...correctData, quantity: Number(e.target.value) || 0 })}
                  className="input h-12 text-center text-xl font-bold"
                  aria-label="Quantity to remove"
                  autoFocus
                />
              </div>

              <div>
                <label htmlFor="wo-correct-reason" className="label">
                  Reason for correction{' '}
                  <span aria-hidden="true" className="text-fd-red">*</span>
                </label>
                <input
                  id="wo-correct-reason"
                  type="text"
                  maxLength={255}
                  value={correctData.reason}
                  onChange={(e) => setCorrectData({ ...correctData, reason: e.target.value })}
                  className="input"
                  placeholder="e.g. operator double-scanned the tray"
                  aria-label="Reason for correction"
                  aria-required="true"
                />
                <p className="mt-1 text-xs text-slate-400">Recorded on the audit trail. Required.</p>
              </div>

              {/* Server refusal, INLINE and verbatim — the primary display. */}
              {correctError && (
                <div
                  role="alert"
                  data-testid="wo-correct-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-base font-semibold text-red-300"
                >
                  {correctError}
                </div>
              )}
            </div>

            <div className="modal-footer">
              <Button variant="secondary" onClick={closeCorrectModal} disabled={correctingOpId !== null}>
                Cancel
              </Button>
              <button
                onClick={handleCorrectSubmit}
                disabled={
                  correctingOpId !== null ||
                  Number(correctData.quantity || 0) <= 0 ||
                  correctData.reason.trim().length === 0
                }
                className="btn-danger disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {correctingOpId !== null ? (
                  <ArrowPathIcon className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <MinusCircleIcon className="h-5 w-5 mr-2 inline" />
                    Remove from Completed
                  </>
                )}
              </button>
            </div>
          </>
        )}
      </Modal>

      <CompleteWorkModal
        open={completeTarget !== null}
        onClose={() => {
          // Don't let the user dismiss mid-request; reflect only server state.
          if (completing || completingOpId !== null) return;
          setCompleteTarget(null);
        }}
        submitting={
          completeTarget?.kind === 'operation' ? completingOpId === completeTarget.operation.id : completing
        }
        onSubmit={handleCompleteSubmit}
        title={
          completeTarget?.kind === 'operation'
            ? `Complete operation "${completeTarget.operation.name}"`
            : `Complete work order ${workOrder.work_order_number}`
        }
        subtitle={
          completeTarget?.kind === 'operation'
            ? `Target: ${Number(completeTarget.operation.component_quantity || workOrder.quantity_ordered || 0)}`
            : `Ordered: ${workOrder.quantity_ordered}`
        }
        defaultQuantityComplete={
          completeTarget?.kind === 'operation'
            ? Number(completeTarget.operation.component_quantity || workOrder.quantity_ordered || 0)
            : workOrder.quantity_ordered
        }
      />

      {/* Resolve-blocker note (replaces the native prompt) */}
      <InputDialog
        open={resolveBlockerTarget !== null}
        title="Resolve Blocker"
        message={resolveBlockerTarget ? `Resolve blocker "${resolveBlockerTarget.title}"?` : undefined}
        label="Resolution note"
        defaultValue="Resolved"
        submitLabel="Resolve"
        pending={resolveBlockerTarget !== null && resolvingBlockerId === resolveBlockerTarget.id}
        onSubmit={handleResolveBlocker}
        onCancel={() => {
          if (resolvingBlockerId === null) setResolveBlockerTarget(null);
        }}
      />

      {/* Clear Hold confirm. `variant="warning"` rather than danger: lifting a
          hold destroys nothing, but it is not routine either -- the message
          states WHY the operation is held (who, when, the open blocker's
          category / title / note) and the two things clearing it does NOT do.
          `pending` keeps it non-optimistic: backdrop and Escape are refused
          while the call is on the wire, and it closes only on success, so a
          refusal stays readable against the row that was clicked. */}
      <ConfirmDialog
        open={clearHoldTarget !== null}
        title="Clear hold on this operation"
        message={clearHoldTarget ? clearHoldMessage(clearHoldTarget, workOrder.work_order_number) : ''}
        confirmLabel="Clear hold"
        cancelLabel="Leave it on hold"
        pending={clearHoldTarget !== null && clearingHoldOpId === clearHoldTarget.id}
        variant="warning"
        onConfirm={handleConfirmClearHold}
        onCancel={() => {
          if (clearingHoldOpId === null) setClearHoldTarget(null);
        }}
      />

      {/* Delete work order confirm (soft delete — server may refuse) */}
      <ConfirmDialog
        open={deleteConfirmOpen}
        title="Delete Work Order"
        message={
          CURRENT_WORK_ORDER_STATUSES.includes(workOrder.status)
            ? `Delete current work order ${workOrder.work_order_number}?\n\nThis removes it from active lists, scheduling, and shop floor queues while preserving the record for audit/restore.\n\nYou can put it back from Work Orders \u2192 Deleted.`
            : `Delete work order ${workOrder.work_order_number}?\n\nThis removes it from active lists while preserving the record for audit/restore.\n\nYou can put it back from Work Orders \u2192 Deleted.`
        }
        confirmLabel="Delete"
        pending={deleting}
        variant="danger"
        onConfirm={handleConfirmDelete}
        onCancel={() => {
          if (!deleting) setDeleteConfirmOpen(false);
        }}
      />

      {/* Lost-update guard, shared by both inline editors (notes, due date).
          The page refetches on any work-order broadcast, so the optimistic-lock
          `version` alone would let a concurrent edit be overwritten with a clean
          200 — this is what turns that silent loss into a decision. See
          handleNotesSave / handleDueDateSave. */}
      <ConfirmDialog
        open={fieldConflict !== null}
        title={
          fieldConflict?.kind === 'due_date'
            ? 'Due date changed by someone else'
            : fieldConflict?.kind === 'unit_number'
              ? 'Unit # changed by someone else'
              : 'Notes changed by someone else'
        }
        message={
          fieldConflict
            ? `Someone else changed the ${fieldConflict.fields} ` +
              `on ${workOrder.work_order_number} while you were editing.\n\n` +
              'Saving replaces their version with yours. Your draft is kept either way — ' +
              'keep editing, then cancel the editor if you want to read theirs first.'
            : ''
        }
        confirmLabel="Replace with mine"
        cancelLabel="Keep editing"
        pending={conflictPending}
        variant="warning"
        onConfirm={handleConflictReplace}
        onCancel={() => {
          if (!conflictPending) setFieldConflict(null);
        }}
      />

      {/* Delete laser nest confirm */}
      <ConfirmDialog
        open={deleteNestTarget !== null}
        title="Delete Laser Nest"
        message={
          deleteNestTarget
            ? `Delete laser nest ${deleteNestTarget.cnc_number || deleteNestTarget.nest_name}? This puts its operation on hold.`
            : ''
        }
        confirmLabel="Delete"
        pending={deleteNestTarget !== null && nestActionId === deleteNestTarget.id}
        variant="danger"
        onConfirm={handleConfirmDeleteNest}
        onCancel={() => {
          if (nestActionId === null) setDeleteNestTarget(null);
        }}
      />
    </div>
  );
}
