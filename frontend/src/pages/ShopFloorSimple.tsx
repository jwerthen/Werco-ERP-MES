import { getPriorityClasses, getPriorityLabel } from '../utils/priority';
import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import api from '../services/api';
import { usePermissions } from '../hooks/usePermissions';
import { ActiveJob, LaserNestInfo, OperationHold, User } from '../types';
import {
  formatCentralDate,
  formatCentralDateTime,
  formatCentralTime,
  isDateBeforeTodayInCentral,
  isDateTodayInCentral,
} from '../utils/centralTime';
import {
  PlayIcon,
  CheckCircleIcon,
  MinusCircleIcon,
  XMarkIcon,
  WrenchScrewdriverIcon,
  ArrowPathIcon,
  MagnifyingGlassIcon,
  EyeIcon,
  PauseIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  CubeIcon,
} from '@heroicons/react/24/solid';
import { FunnelIcon, QrCodeIcon } from '@heroicons/react/24/outline';
import LaserNestOperatorPanel from '../components/laser/LaserNestOperatorPanel';
import { Button, ConfirmDialog, EmptyState, ErrorState, Modal, SelectField, UnitBadge, statusColor, statusVariant } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { HOLD_REASONS } from '../components/kiosk/kioskConstants';
import { KioskRunOrderChip } from '../components/kiosk/KioskQueueCard';
import { clearHoldToast } from '../components/kiosk/heldOperations';
import OperationHoldReason from '../components/shopfloor/OperationHoldReason';
import {
  EMPTY_SCRAP_SELECTION,
  ScrapReasonFields,
  isScrapSelectionComplete,
  scrapSelectionPayload,
} from '../components/quality/ScrapReasonFields';
import { useScrapReasonCodes } from '../hooks/useScrapReasonCodes';
import { getKioskDept, getKioskWorkCenterCode, getKioskWorkCenterId } from '../utils/kiosk';
import { formatOperationLabel } from '../utils/operationLabel';
import { ScanResolveResult } from '../types/scan';
import { useAuth } from '../context/AuthContext';
import { useCompany } from '../context/CompanyContext';
import { usePhoneLayout } from '../hooks/usePhoneLayout';
import { useShopFloorWorkspace, ShopFloorView } from '../hooks/useShopFloorWorkspace';
import { usePersonalShopFloorSession } from '../hooks/usePersonalShopFloorSession';
import { useShopFloorProduction } from '../hooks/useShopFloorProduction';
import ProductionSaveNotice from '../components/shopfloor/ProductionSaveNotice';
import { isDefinitiveHttpRefusal } from '../components/kiosk/useOneTapPieces';
import MobileCurrentWork from '../components/shopfloor/MobileCurrentWork';
import ShopFloorCameraScanner from '../components/shopfloor/ShopFloorCameraScanner';
import KioskDocViewer, { KioskDocTab, KioskDocTransport } from '../components/kiosk/KioskDocViewer';
import KioskJobNotes from '../components/kiosk/KioskJobNotes';

interface Operation {
  id: number;
  work_order_id: number;
  work_order_number: string;
  /** Unit # this work order builds — absent on work orders that do not track one. */
  unit_number?: string | null;
  part_number: string | null;
  part_name: string | null;
  operation_number: string;
  operation_name: string;
  description: string | null;
  work_center_id: number | null;
  work_center_name: string | null;
  status: string;
  quantity_ordered: number;
  work_order_quantity_ordered?: number;
  component_quantity?: number | null;
  quantity_complete: number;
  quantity_scrapped: number;
  priority: number;
  due_date: string | null;
  customer_name: string | null;
  customer_po: string | null;
  actual_start: string | null;
  setup_instructions: string | null;
  run_instructions: string | null;
  requires_inspection: boolean;
  can_check_in?: boolean;
  blocked_by_previous_operations?: boolean;
  laser_nest?: LaserNestInfo | null;
  /**
   * WHY this operation is held, WHO placed it and WHEN — sent by
   * `GET /shop-floor/operations` on ON_HOLD rows only, and absent on every
   * other row (the payload for unheld work is byte-identical to what it always
   * was). The same batched, pure-read view the kiosk's held list uses, so the
   * desk and the floor cannot tell two stories about one hold.
   *
   * Optional because the SPA and the API deploy independently: a build carrying
   * this can be live against a backend that does not send it yet, and the card
   * then renders exactly as it did before — a hold with no stated reason.
   */
  hold?: OperationHold | null;
  /**
   * Manager-dictated run order (Dispatch Board), 1..N per work center —
   * gap-free display position, same semantics as the kiosk queue payload.
   * `null`/absent = unranked. The server already returns rows in canonical
   * run-order-first order; this rank is DISPLAY-only, never a client sort key.
   */
  run_order?: number | null;
}

interface WorkCenter {
  id: number;
  name: string;
  code: string;
}

interface Toast {
  id: number;
  // 'warning' = the call SUCCEEDED but did not do everything asked — a hold that
  // came off without putting the job back on the board, or one that left a
  // blocker open. 'success' would hide the shortfall; 'error' would claim a
  // failure that did not happen and send the operator looking for held work
  // that is no longer held.
  type: 'success' | 'error' | 'warning' | 'info';
  message: string;
}

// The loaders the 30-second auto-refresh (and manual Refresh) re-run — keys
// for the per-loader toast-spam guard.
type PolledLoader = 'operations' | 'activeJobs' | 'workCenters' | 'dashboardCounts';

// Status pill colors come from the central statusColors source of truth via
// statusColor(); the leading status dot derives from the same canonical variant
// so it can never drift from the pill.
const STATUS_DOT_BY_VARIANT: Record<ReturnType<typeof statusVariant>, string> = {
  green: 'bg-emerald-500/100',
  blue: 'bg-blue-500/100',
  amber: 'bg-amber-500/100',
  red: 'bg-red-500/100',
  slate: 'bg-slate-500',
};
const statusDot = (status: string) => STATUS_DOT_BY_VARIANT[statusVariant(status)];

const WORK_CENTER_STORAGE_KEY = 'shop_floor_work_center_id';

// Production modal state. `mode` toggles the additive "Add completed" path and
// the self-service "Correct over-count" (reduce-production) path — the latter is
// a miscount fix, NOT scrap, bounded server-side to what the operator recorded on
// their own open clock-in. remove_delta/remove_reason back the correct-count form.
type ProductionMode = 'add' | 'remove';
const INITIAL_PRODUCTION_DATA = {
  mode: 'add' as ProductionMode,
  quantity_complete_delta: 1,
  quantity_scrapped_delta: 0,
  scrap: EMPTY_SCRAP_SELECTION,
  notes: '',
  remove_delta: 1,
  remove_reason: '',
};

const formatScanActions = (actions: string[]) => actions.map((action) => action.replace(/_/g, ' ')).join(', ');

const isActiveJobOperation = (job: ActiveJob, operation: Operation) =>
  job.operation_id != null
    ? job.operation_id === operation.id
    : Boolean(job.work_order_id && job.operation_number) &&
      job.work_order_id === operation.work_order_id &&
      String(job.operation_number) === String(operation.operation_number);

const SHOP_FLOOR_DOCS: KioskDocTransport = {
  fetchOperationDocuments: id => api.getOperationDocuments(id),
  fetchDocumentBlob: id => api.fetchShopFloorDocumentBlob(id),
};

export default function ShopFloorSimple() {
  const { user } = useAuth();
  const { currentCompany } = useCompany();
  const platformAdmin = user?.role === 'platform_admin' || user?.is_superuser === true;
  if (platformAdmin && !currentCompany) return <p role="status">Loading your company workspace…</p>;
  const scopedUser = user && platformAdmin ? { ...user, company_id: currentCompany!.id } : user;
  return <ShopFloorWorkspace key={`${scopedUser?.company_id}:${scopedUser?.id}`} user={scopedUser} />;
}

function ShopFloorWorkspace({ user }: { user: User | null }) {
  const phone = usePhoneLayout();
  const { workspace, updateWorkspace } = useShopFloorWorkspace(user);
  const phoneSession = usePersonalShopFloorSession(user);
  const productionSave = useShopFloorProduction<{ timeEntryId: number; data: typeof INITIAL_PRODUCTION_DATA }>({ companyId: user?.company_id, operatorId: user?.id });
  const { can } = usePermissions();
  const navigate = useNavigate();
  const location = useLocation();
  const [operations, setOperations] = useState<Operation[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [activeJobs, setActiveJobs] = useState<ActiveJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  // Poll-failure surfacing — a failed refresh must never fail silently.
  // operationsError swaps the grid for an inline ErrorState; activeJobsStale
  // keeps the last-known clocked-in job on screen (never "not clocked in"
  // just because a poll blipped — that invited a double clock-in) and flags
  // it with an inline ErrorState until a refresh succeeds.
  const [operationsError, setOperationsError] = useState(false);
  const [activeJobsStale, setActiveJobsStale] = useState(false);

  // Filters
  const [workCenterId, _setWorkCenterId] = useState<number | ''>(() => workspace.workCenterId || '');
  const workCenterIdRef = useRef<number | ''>(workspace.workCenterId || '');
  const hadSavedWorkspaceRef = useRef(workspace.savedAt !== null);
  const appliedWorkCenterRouteRef = useRef<string | null>(null);
  const setWorkCenterId = useCallback((id: number | '') => {
    _setWorkCenterId(id);
    workCenterIdRef.current = id;
    updateWorkspace({ workCenterId: id || null });
    if (id) {
      localStorage.setItem(WORK_CENTER_STORAGE_KEY, String(id));
    }
  }, [updateWorkspace]);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [dueTodayOnly, setDueTodayOnly] = useState(false);
  const [actionableOnly, setActionableOnly] = useState(false);
  const operationsQueryKey = JSON.stringify([workCenterId, statusFilter, debouncedSearch, dueTodayOnly]);
  const loadedOperationsQueryRef = useRef<string | null>(null);
  
  // Modal states
  const [checkOutModal, setCheckOutModal] = useState<{ operation: Operation; job: ActiveJob } | null>(null);
  const [completeConfirm, setCompleteConfirm] = useState<Operation | null>(null);
  const [productionModal, setProductionModal] = useState<{ operation: Operation; job: ActiveJob } | null>(null);
  const [detailsModal, setDetailsModal] = useState<any | null>(null);
  const [correctionReview, setCorrectionReview] = useState<{ loading: boolean; details: any | null; error: string | null } | null>(null);
  const [correctionReviewed, setCorrectionReviewed] = useState(false);
  const [documentView, setDocumentView] = useState<{ operationId: number; tab: KioskDocTab } | null>(null);
  const [expandedOperationId, setExpandedOperationId] = useState<number | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [checkOutData, setCheckOutData] = useState({ quantity_produced: 0, quantity_scrapped: 0, scrap: EMPTY_SCRAP_SELECTION, notes: '' });
  const [productionData, setProductionData] = useState(INITIAL_PRODUCTION_DATA);
  useEffect(() => {
    if (productionModal) productionSave.saveDraft(productionModal.operation.id, { timeEntryId: productionModal.job.time_entry_id, data: productionData });
  }, [productionModal, productionData, productionSave.saveDraft]);
  // Server refusal for the production modal (add AND remove modes), rendered
  // INLINE inside the modal. Toasts alone proved unreadable here: the page-local
  // toast container sat at z-50 while the shared Modal overlays at z-[60], so a
  // designed 400 (e.g. the reduce-production bound) appeared dimmed UNDER the
  // open modal's backdrop on the shop floor. Verbatim `detail` only.
  const [productionError, setProductionError] = useState<string | null>(null);
  // Lean Phase 1: company scrap reason codes ([] -> legacy SCRAP_REASONS fallback).
  const { codes: scrapCodes } = useScrapReasonCodes();
  // Hold picker — desktop holds must file a structured WorkOrderBlocker
  // (category + optional note), mirroring the kiosk. The Hold button opens this
  // instead of holding immediately.
  const [holdModal, setHoldModal] = useState<Operation | null>(null);
  const [holdData, setHoldData] = useState<{ category: string; note: string }>({ category: '', note: '' });
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  const [updatingPriorityWorkOrderId, setUpdatingPriorityWorkOrderId] = useState<number | null>(null);
  const [priorityReason, setPriorityReason] = useState('');
  const [showMobileFilters, setShowMobileFilters] = useState(false);
  const [showMobileCenters, setShowMobileCenters] = useState(false);
  const [showScanner, setShowScanner] = useState(false);
  const [scannerCode, setScannerCode] = useState('');
  // A0.4: row to spotlight after an OP:{id} scan (box or ?scan= deep link).
  const [highlightedOperationId, setHighlightedOperationId] = useState<number | null>(null);
  const [operationToFocus, setOperationToFocus] = useState<number | null>(null);
  const [activeJobNavigationRequest, setActiveJobNavigationRequest] = useState(0);
  const pendingActiveJobRef = useRef<ActiveJob | null>(null);
  const operationCardsRef = useRef(new Map<number, HTMLDivElement>());
  const operationsRequestRef = useRef(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  
  // Toast notifications
  const [toasts, setToasts] = useState<Toast[]>([]);
  const operationsRef = useRef<HTMLDivElement | null>(null);
  const [dashboardCounts, setDashboardCounts] = useState<Record<number, { active: number; queued: number }>>({});
  const kioskParams = useMemo(() => {
    return {
      dept: getKioskDept(location.search),
      workCenterId: getKioskWorkCenterId(location.search),
      workCenterCode: getKioskWorkCenterCode(location.search),
    };
  }, [location.search]);

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    workCenterIdRef.current = workCenterId;
  }, [workCenterId]);

  // Monotonic toast id — NOT Date.now(): the failure toasts below can fire for
  // several loaders in the same event-loop turn (one Promise.all refresh with
  // the network down), and same-ms ids duplicated React keys and made the first
  // 4s timeout dismiss every colliding toast at once.
  const toastIdRef = useRef(0);
  const showToast = useCallback((type: Toast['type'], message: string) => {
    const id = ++toastIdRef.current;
    setToasts(prev => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4000);
  }, []);

  // Toast-spam guard for the polled loaders: one entry per loader, true while
  // the LAST attempt failed. A failure toasts only on the ok→failed
  // TRANSITION, so a network outage during 30s polling produces one toast
  // plus the persistent inline state — not a new toast every poll.
  const lastLoadFailedRef = useRef<Record<PolledLoader, boolean>>({
    operations: false,
    activeJobs: false,
    workCenters: false,
    dashboardCounts: false,
  });

  const notifyLoadFailure = useCallback(
    (loader: PolledLoader, message: string) => {
      if (!lastLoadFailedRef.current[loader]) {
        showToast('error', message);
      }
      lastLoadFailedRef.current[loader] = true;
    },
    [showToast]
  );

  // Load data
  const loadOperations = useCallback(async () => {
    const requestId = ++operationsRequestRef.current;
    const jobToReveal = pendingActiveJobRef.current;
    try {
      const params: any = {};
      if (workCenterId) params.work_center_id = workCenterId;
      if (statusFilter) params.status = statusFilter;
      if (dueTodayOnly) params.due_today = true;
      if (debouncedSearch) params.search = debouncedSearch;
      
      const response = await api.getShopFloorOperations(params);
      let nextOperations = response.operations || [];

      if (workCenterId && !statusFilter && !dueTodayOnly && !debouncedSearch && nextOperations.length === 0) {
        const queueResponse = await api.getWorkCenterQueue(workCenterId);
        nextOperations = (queueResponse.queue || []).map((item: any) => ({
          id: item.operation_id,
          work_order_id: item.work_order_id,
          work_order_number: item.work_order_number,
          part_number: item.part_number,
          part_name: item.part_name,
          operation_number: item.operation_number,
          operation_name: item.operation_name,
          description: null,
          work_center_id: workCenterId,
          work_center_name: workCenters.find((wc) => wc.id === workCenterId)?.name || null,
          status: item.status,
          quantity_ordered: item.quantity_ordered,
          work_order_quantity_ordered: item.work_order_quantity_ordered,
          component_quantity: item.component_quantity,
          quantity_complete: item.quantity_complete,
          quantity_scrapped: 0,
          priority: item.priority,
          due_date: item.due_date,
          customer_name: null,
          customer_po: null,
          actual_start: null,
          setup_instructions: null,
          run_instructions: null,
          requires_inspection: false,
          can_check_in: item.can_check_in,
          blocked_by_previous_operations: item.blocked_by_previous_operations,
          laser_nest: item.laser_nest,
          run_order: item.run_order ?? null,
        }));
      }

      // A response for the old station/search must not replace the queue we
      // just requested by tapping a checked-in job.
      if (requestId !== operationsRequestRef.current) return;
      loadedOperationsQueryRef.current = operationsQueryKey;
      setOperations(nextOperations);
      setLastUpdated(Date.now());
      setOperationsError(false);
      lastLoadFailedRef.current.operations = false;
      if (jobToReveal && pendingActiveJobRef.current === jobToReveal) {
        pendingActiveJobRef.current = null;
        const target = nextOperations.find((op: Operation) => isActiveJobOperation(jobToReveal, op));
        if (target) {
          setOperationToFocus(target.id);
        } else {
          showToast('info', 'This operation is no longer in the queue. Refresh your checked-in operations and try again.');
        }
      }
    } catch (err) {
      if (requestId !== operationsRequestRef.current) return;
      console.error('Failed to load operations:', err);
      // Swap the grid for an inline ErrorState instead of silently showing a
      // stale list; toast only on the ok→failed transition.
      setOperationsError(true);
      notifyLoadFailure('operations', 'Failed to load operations');
    }
  }, [workCenterId, statusFilter, debouncedSearch, dueTodayOnly, workCenters, notifyLoadFailure, showToast, operationsQueryKey]);

  const loadDashboardCounts = useCallback(async () => {
    try {
      const response = await api.getDashboard();
      const centers = response.work_centers || [];
      const nextCounts: Record<number, { active: number; queued: number }> = {};
      centers.forEach((wc: any) => {
        nextCounts[wc.id] = {
          active: Number(wc.active_operations || 0),
          queued: Number(wc.queued_operations || 0),
        };
      });
      setDashboardCounts(nextCounts);
      lastLoadFailedRef.current.dashboardCounts = false;
    } catch (err) {
      console.error('Failed to load dashboard counts:', err);
      notifyLoadFailure('dashboardCounts', 'Failed to refresh work center counts');
    }
  }, [notifyLoadFailure]);

  const loadActiveJobs = useCallback(async () => {
    try {
      const response = await api.getMyActiveJob();
      setActiveJobs(response.active_jobs || (response.active_job ? [response.active_job] : []));
      setActiveJobsStale(false);
      lastLoadFailedRef.current.activeJobs = false;
      return response.active_jobs || (response.active_job ? [response.active_job] : []);
    } catch (err) {
      // Safety-critical: a failed poll must NOT clear the operator's
      // clocked-in job. Wiping activeJobs here made the strip vanish — the
      // page read "not clocked in" and invited a double clock-in. Keep the
      // last-known jobs and mark them stale instead.
      console.error('Failed to load active jobs:', err);
      setActiveJobsStale(true);
      notifyLoadFailure('activeJobs', "Couldn't refresh your clocked-in job");
    }
  }, [notifyLoadFailure]);

  const loadWorkCenters = useCallback(async () => {
    try {
      const response = await api.getWorkCenters();
      const centers: WorkCenter[] = response || [];
      setWorkCenters(centers);
      const routeKey = JSON.stringify([kioskParams.workCenterId, kioskParams.workCenterCode, kioskParams.dept]);
      const firstLoad = appliedWorkCenterRouteRef.current === null;
      const routeChanged = appliedWorkCenterRouteRef.current !== routeKey;
      const hasRouteSelection = Boolean(kioskParams.workCenterId || kioskParams.workCenterCode || kioskParams.dept);
      let nextId: number | '' = centers.some(wc => wc.id === workCenterIdRef.current) ? workCenterIdRef.current : '';

      if (routeChanged && hasRouteSelection) {
        // Explicit station links win over a remembered workspace. Apply once
        // per route selection so Refresh cannot undo a later manual choice.
        const matched = kioskParams.workCenterId
          ? centers.find(wc => wc.id === kioskParams.workCenterId)
          : kioskParams.workCenterCode
            ? centers.find(wc => wc.code.toLowerCase() === kioskParams.workCenterCode!.toLowerCase())
            : centers.find(wc => wc.name.toLowerCase().includes(kioskParams.dept!.toLowerCase()) ||
              wc.code.toLowerCase().includes(kioskParams.dept!.toLowerCase()));
        nextId = matched?.id ?? '';
      } else if (firstLoad && !hasRouteSelection && !hadSavedWorkspaceRef.current && !nextId) {
        // Legacy workstation preference is only a first-visit fallback. An
        // operator's saved All selection and manual changes remain authoritative.
        const storedId = Number(localStorage.getItem(WORK_CENTER_STORAGE_KEY));
        nextId = centers.find(wc => wc.id === storedId)?.id ?? '';
      }
      appliedWorkCenterRouteRef.current = routeKey;
      if (nextId !== workCenterIdRef.current) {
        setWorkCenterId(nextId);
        setActionableOnly(nextId !== '');
      }
      lastLoadFailedRef.current.workCenters = false;
    } catch (err) {
      console.error('Failed to load work centers:', err);
      notifyLoadFailure('workCenters', 'Failed to load work centers');
    }
  }, [kioskParams.dept, kioskParams.workCenterCode, kioskParams.workCenterId, setWorkCenterId, notifyLoadFailure]);

  useEffect(() => {
    let cancelled = false;

    const init = async () => {
      setLoading(true);
      try {
        await Promise.all([
          loadWorkCenters(),
          loadDashboardCounts(),
          loadActiveJobs(),
        ]);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    init();

    return () => {
      cancelled = true;
    };
  }, [loadActiveJobs, loadDashboardCounts, loadWorkCenters]);

  useEffect(() => {
    if (!loading) {
      loadOperations();
    }
  }, [workCenterId, statusFilter, debouncedSearch, dueTodayOnly, loadOperations, loading, activeJobNavigationRequest]);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      loadOperations();
      loadDashboardCounts();
      loadActiveJobs();
    }, 30000);
    return () => clearInterval(interval);
  }, [loadOperations, loadDashboardCounts, loadActiveJobs]);

  useEffect(() => {
    const interval = setInterval(() => setNowMs(Date.now()), 30000);
    return () => clearInterval(interval);
  }, []);

  const handleRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      loadOperations(),
      loadDashboardCounts(),
      loadActiveJobs(),
    ]);
    setRefreshing(false);
  };

  const isOverdue = (dueDate: string | null) => {
    if (!dueDate) return false;
    return isDateBeforeTodayInCentral(dueDate);
  };

  const isDueToday = (dueDate: string | null) => {
    return Boolean(dueDate && isDateTodayInCentral(dueDate));
  };
  const canEditPriority = can('work_orders:edit');



  const workCenterBuckets = useMemo(() => {
    const buckets = new Map<number, {
      id: number;
      name: string;
      total: number;
      open: number;
      inProgress: number;
      onHold: number;
      dueToday: number;
      overdue: number;
    }>();

    workCenters.forEach((wc) => {
      buckets.set(wc.id, {
        id: wc.id,
        name: wc.name,
        total: 0,
        open: 0,
        inProgress: 0,
        onHold: 0,
        dueToday: 0,
        overdue: 0,
      });
    });

    operations.forEach((op) => {
      if (!op.work_center_id) return;
      const bucket = buckets.get(op.work_center_id) || {
        id: op.work_center_id,
        name: op.work_center_name || `Work Center ${op.work_center_id}`,
        total: 0,
        open: 0,
        inProgress: 0,
        onHold: 0,
        dueToday: 0,
        overdue: 0,
      };

      bucket.total += 1;
      if (op.status === 'pending' || op.status === 'ready') bucket.open += 1;
      if (op.status === 'in_progress') bucket.inProgress += 1;
      if (op.status === 'on_hold') bucket.onHold += 1;
      if (isDueToday(op.due_date)) bucket.dueToday += 1;
      if (isOverdue(op.due_date)) bucket.overdue += 1;

      buckets.set(bucket.id, bucket);
    });

    Object.entries(dashboardCounts).forEach(([key, counts]) => {
      const id = Number(key);
      const bucket = buckets.get(id);
      if (!bucket) return;
      bucket.inProgress = counts.active;
      bucket.open = counts.queued;
    });

    return Array.from(buckets.values()).sort((a, b) => a.name.localeCompare(b.name));
  }, [operations, workCenters, dashboardCounts]);

  const focusOperations = (centerId: number | '') => {
    setWorkCenterId(centerId);
    setActionableOnly(centerId !== '');
    setTimeout(() => {
      operationsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 100);
  };

  const actionableStatuses = useMemo(() => new Set(['pending', 'ready', 'in_progress', 'on_hold']), []);
  const visibleOperations = useMemo(
    () => (actionableOnly ? operations.filter(op => actionableStatuses.has(op.status)) : operations),
    [actionableOnly, operations, actionableStatuses]
  );
  // Operations render in SERVER order — no client re-sort. GET
  // /shop-floor/operations returns the canonical dispatch order (manager-dictated
  // run_order first, then priority/due date/sequence, grouped by work center), so
  // a single-work-center filter shows EXACTLY the work-center-queue order the
  // kiosks honor. Re-sorting here (the old dispatch-score sort) broke the
  // Dispatch Board's promise that its run order is what operators see.
  const priorityFocusQueue = useMemo(() => {
    return visibleOperations
      .filter((op) => ['pending', 'ready', 'in_progress', 'on_hold'].includes(op.status))
      .slice(0, 5);
  }, [visibleOperations]);

  const selectedWorkCenter = useMemo(
    () => workCenters.find((wc) => wc.id === workCenterId) || null,
    [workCenters, workCenterId]
  );
  const queuedOperationsAcrossCenters = useMemo(
    () => workCenterBuckets.reduce((total, bucket) => total + bucket.open + bucket.inProgress, 0),
    [workCenterBuckets]
  );

  // Compact station summary for the cockpit MiniStat strip — derived from the
  // operations already in view (mirrors the per-bucket counts, aggregated).
  const stationSummary = useMemo(() => {
    let active = 0;
    let queue = 0;
    let dueToday = 0;
    let overdue = 0;
    visibleOperations.forEach((op) => {
      if (op.status === 'in_progress') active += 1;
      if (op.status === 'pending' || op.status === 'ready') queue += 1;
      if (isDueToday(op.due_date)) dueToday += 1;
      if (isOverdue(op.due_date)) overdue += 1;
    });
    return { active, queue, dueToday, overdue };
  }, [visibleOperations]);

  const getActiveJobForOperation = useCallback(
    (operation: Operation) =>
      activeJobs.find((job) => isActiveJobOperation(job, operation)) || null,
    [activeJobs]
  );

  const selectCurrentJob = (job: ActiveJob) => {
    updateWorkspace({ selectedOperationId: job.operation_id || null, activeTimeEntryId: job.time_entry_id, view: 'my-work' });
  };
  // Revalidate the saved identity against fresh check-ins. A completed job or
  // a different operator can never be resurrected from browser storage.
  const selectedActiveJob = activeJobs.find(job => job.time_entry_id === workspace.activeTimeEntryId &&
    (workspace.selectedOperationId === null || job.operation_id === workspace.selectedOperationId)) || activeJobs[0] || null;
  const mobileView = workspace.view;
  const selectMobileView = (view: ShopFloorView) => {
    updateWorkspace({ view });
    if (view !== 'my-work') {
      setStatusFilter('');
      setDueTodayOnly(false);
      setActionableOnly(false);
      setSearch('');
      setDebouncedSearch('');
    }
  };
  const queueOperations = phone && mobileView === 'ready'
    ? visibleOperations.filter(op => op.can_check_in !== false && ['ready', 'pending', 'in_progress'].includes(op.status) && !getActiveJobForOperation(op))
    : visibleOperations;

  const handleGoToActiveJob = (job: ActiveJob) => {
    selectCurrentJob(job);
    if (phone) return;
    const visibleOperation = visibleOperations.find((op) => isActiveJobOperation(job, op));
    if (
      !operationsError && !pendingActiveJobRef.current &&
      loadedOperationsQueryRef.current === operationsQueryKey && search === debouncedSearch &&
      visibleOperation && operationCardsRef.current.has(visibleOperation.id)
    ) {
      pendingActiveJobRef.current = null;
      setOperationToFocus(visibleOperation.id);
      return;
    }
    if (job.operation_id == null && !(job.work_order_id && job.operation_number)) {
      showToast('info', 'This check-in is not linked to an operation.');
      return;
    }

    // Active jobs can be outside the current filters or the first queue page.
    // Narrow to this work order and wait for its actual card to render.
    pendingActiveJobRef.current = job;
    setWorkCenterId(job.work_center_id || '');
    setStatusFilter('');
    setDueTodayOnly(false);
    setActionableOnly(false);
    setSearch(job.work_order_number || '');
    setDebouncedSearch(job.work_order_number || '');
    setShowMobileFilters(false);
    setShowMobileCenters(false);
    setActiveJobNavigationRequest((request) => request + 1);
  };

  useEffect(() => {
    if (operationToFocus === null || operationsError) return;
    const card = operationCardsRef.current.get(operationToFocus);
    if (!card) return;
    card.focus({ preventScroll: true });
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    card.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    setHighlightedOperationId(operationToFocus);
    setOperationToFocus(null);
  }, [operationToFocus, operationsError, visibleOperations]);

  const getElapsedTime = useCallback((clockIn?: string) => {
    if (!clockIn) return '0m';
    const startMs = new Date(clockIn).getTime();
    if (Number.isNaN(startMs)) return '0m';
    const diffMs = Math.max(0, nowMs - startMs);
    const hours = Math.floor(diffMs / 3600000);
    const minutes = Math.floor((diffMs % 3600000) / 60000);
    return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
  }, [nowMs]);

  const getRemainingQuantity = useCallback((operation: Operation) => {
    return Math.max(0, Number(operation.quantity_ordered || 0) - Number(operation.quantity_complete || 0));
  }, []);

  /**
   * Lift a hold. ONE write — `resumeOperation` and nothing else — then a re-read.
   *
   * SPLIT OUT OF `handleCheckIn`, partially reverting the consolidation made in
   * 6cbdb95 ("Improve mobile shop floor flow"), which folded Resume into Check
   * In and put TWO writes behind one tap: `resumeOperation` then `clockIn`. When
   * the second leg was refused — most reachably by the already-clocked-in gate —
   * the FIRST had already committed. The operator saw a red "failed", the card
   * was never refreshed (the catch skips the reload), and a second tap answered
   * "Operation is not on hold", because it wasn't: the resume had worked. The
   * two verbs are now two buttons, so a refusal on either leaves the other
   * untouched and the card always reflects what actually happened.
   *
   * The toast is composed from what the SERVER returned, never from the absence
   * of a throw: resume RESTORES the operation and deliberately does not resolve
   * the blocker, and it lands PENDING (off the board) when the parent is
   * unreleased or a predecessor is incomplete. `clearHoldToast` folds both
   * shortfalls into ONE warning; only a clean lift is `success`.
   */
  const handleClearHold = async (operation: Operation) => {
    if (actionLoading !== null || productionSave.mutationsBlocked) return;

    setActionLoading(operation.id);
    try {
      const result = await api.resumeOperation(operation.id);
      // Non-optimistic: re-read first, so the card moves only because the server
      // moved it. Clear Hold is server-GATED (409 on a cancelled-nest tombstone,
      // 400 when the operation is not actually held).
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
      const { type, message } = clearHoldToast(result, operation.work_order_number);
      showToast(type, message);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || err.message || 'Failed to clear the hold');
    } finally {
      setActionLoading(null);
    }
  };

  // Action handlers
  const handleCheckIn = async (operation: Operation) => {
    if (actionLoading !== null || productionSave.mutationsBlocked) return;
    // A held operation is never checked in from here — Clear Hold is its own
    // button now (see handleClearHold). Guarded rather than assumed: this
    // handler is reachable from the mobile "Next Job" strip and the grid card,
    // and a silent fall-through would put the old two-write path back.
    if (operation.status === 'on_hold') {
      showToast('info', 'This operation is on hold — clear the hold before checking in.');
      return;
    }

    if (operation.can_check_in === false) {
      showToast('info', 'Earlier operations on this work order must be completed first');
      return;
    }

    setActionLoading(operation.id);
    try {
      if (operation.status === 'in_progress') {
        if (!operation.work_center_id) {
          throw new Error('Operation is missing a work center');
        }
        await api.clockIn({
          work_order_id: operation.work_order_id,
          operation_id: operation.id,
          work_center_id: operation.work_center_id,
          entry_type: 'run',
        });
      } else {
        await api.startOperation(operation.id);
      }
      showToast('success', `Checked in to ${operation.work_order_number}`);
      const [, jobs] = await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
      if (phone) {
        const checkedIn = jobs?.find((job: ActiveJob) => isActiveJobOperation(job, operation));
        if (checkedIn) selectCurrentJob(checkedIn);
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || err.message || 'Failed to check in');
    } finally {
      setActionLoading(null);
    }
  };

  const handleOpenCheckOut = (operation: Operation, job: ActiveJob) => {
    selectCurrentJob(job);
    setCheckOutModal({ operation, job });
    setCheckOutData({ quantity_produced: 0, quantity_scrapped: 0, scrap: EMPTY_SCRAP_SELECTION, notes: '' });
  };

  const handleOpenProductionModal = (operation: Operation, job: ActiveJob) => {
    selectCurrentJob(job);
    setProductionModal({ operation, job });
    const draft = productionSave.readDraft(operation.id);
    setProductionData(draft?.timeEntryId === job.time_entry_id ? { ...INITIAL_PRODUCTION_DATA, ...draft.data } : INITIAL_PRODUCTION_DATA);
    setProductionError(null);
  };

  const getOperationForActiveJob = (job: ActiveJob): Operation => {
    const matchingOperation = operations.find((op) => getActiveJobForOperation(op)?.time_entry_id === job.time_entry_id);
    if (matchingOperation) return {
      ...matchingOperation,
      // My Work and its forms must use the same fresh check-in totals, even
      // when a separate queue poll failed and left old metadata in memory.
      quantity_ordered: job.quantity_ordered ?? matchingOperation.quantity_ordered,
      quantity_complete: job.quantity_complete ?? matchingOperation.quantity_complete,
      quantity_scrapped: job.operation_quantity_scrapped ?? matchingOperation.quantity_scrapped,
      laser_nest: job.laser_nest ?? matchingOperation.laser_nest,
    };

    return {
      id: job.operation_id || job.time_entry_id,
      work_order_id: job.work_order_id || 0,
      work_order_number: job.work_order_number || 'Current job',
      part_number: job.part_number || null,
      part_name: job.part_name || null,
      operation_number: job.operation_number || '',
      operation_name: job.operation_name || 'Operation',
      description: null,
      work_center_id: job.work_center_id || null,
      work_center_name: job.work_center_name || null,
      status: 'in_progress',
      quantity_ordered: job.quantity_ordered || 0,
      work_order_quantity_ordered: job.work_order_quantity_ordered,
      component_quantity: job.component_quantity,
      quantity_complete: job.quantity_complete || 0,
      quantity_scrapped: job.operation_quantity_scrapped || 0,
      priority: 5,
      due_date: null,
      customer_name: null,
      customer_po: null,
      actual_start: job.clock_in,
      setup_instructions: job.operation_setup_instructions || null,
      run_instructions: job.operation_run_instructions || null,
      requires_inspection: false,
      can_check_in: true,
      blocked_by_previous_operations: false,
      run_order: null,
      laser_nest: job.laser_nest,
    };
  };

  const handleOpenActiveJobCheckOut = (job: ActiveJob) => {
    handleOpenCheckOut(getOperationForActiveJob(job), job);
  };

  const currentOperation = selectedActiveJob ? getOperationForActiveJob(selectedActiveJob) : null;
  const currentOperationAvailable = Boolean(selectedActiveJob?.operation_id ||
    (selectedActiveJob && operations.some(op => isActiveJobOperation(selectedActiveJob, op))));

  const closeCheckOutModal = () => {
    setCheckOutModal(null);
    setCheckOutData({ quantity_produced: 0, quantity_scrapped: 0, scrap: EMPTY_SCRAP_SELECTION, notes: '' });
  };

  const closeProductionModal = () => {
    setProductionModal(null);
    setProductionData(INITIAL_PRODUCTION_DATA);
    setProductionError(null);
  };

  const adjustGoodQuantity = (delta: number) => {
    setCheckOutData((prev) => ({
      ...prev,
      quantity_produced: Math.max(0, Number(prev.quantity_produced || 0) + delta),
    }));
  };

  const adjustProductionQuantity = (delta: number) => {
    setProductionData((prev) => ({
      ...prev,
      quantity_complete_delta: Math.max(0, Number(prev.quantity_complete_delta || 0) + delta),
    }));
  };

  const adjustRemoveQuantity = (delta: number) => {
    setProductionData((prev) => ({
      ...prev,
      remove_delta: Math.max(0, Number(prev.remove_delta || 0) + delta),
    }));
  };

  const reportProduction = async (
    operation: Operation,
    quantityCompleteDelta: number,
    quantityScrappedDelta = 0,
    notes?: string,
    closeModal = false,
    scrapFields?: { scrap_reason?: string; scrap_reason_code_id?: number }
  ) => {
    if (actionLoading !== null || productionSave.mutationsBlocked) return;
    setActionLoading(operation.id);
    setProductionError(null);
    try {
      await productionSave.submit(operation.id, {
        quantity_complete_delta: quantityCompleteDelta,
        quantity_scrapped_delta: quantityScrappedDelta,
        notes: notes || undefined,
        // Structured scrap reason (company code id and/or free text) required
        // when scrap > 0 (compliance/traceability), mirroring the kiosk.
        // Omitted entirely when nothing was scrapped.
        ...(quantityScrappedDelta > 0 && scrapFields ? scrapFields : {}),
      });
      const label = quantityCompleteDelta > 0
        ? `Added ${quantityCompleteDelta} complete part${quantityCompleteDelta === 1 ? '' : 's'}`
        : `Added ${quantityScrappedDelta} scrap`;
      showToast('success', label);
      if (closeModal) closeProductionModal();
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      // The persistent receipt notice distinguishes refusal from uncertainty.
      // Never tell the operator a lost response means the report failed.
      if (isDefinitiveHttpRefusal(err.response?.status ?? err.status)) {
        const detail = err.response?.data?.detail;
        const message = typeof detail === 'string' && detail ? detail : 'Production was not saved';
        if (closeModal) setProductionError(message);
        showToast('error', message);
      }
    } finally {
      setActionLoading(null);
    }
  };

  const handleSaveProduction = async () => {
    if (!productionModal) return;
    await reportProduction(
      productionModal.operation,
      Number(productionData.quantity_complete_delta || 0),
      Number(productionData.quantity_scrapped_delta || 0),
      productionData.notes,
      true,
      scrapSelectionPayload(scrapCodes, productionData.scrap)
    );
  };

  // Over-count correction (reduce-production). Server-gated ⇒ NON-optimistic: we
  // do NOT touch the on-screen count; on success we refetch and reflect only what
  // the server returns, and on refusal we surface the server's `detail` verbatim.
  const handleReduceProduction = async () => {
    if (!productionModal || actionLoading !== null || productionSave.mutationsBlocked) return;
    const operation = productionModal.operation;
    const delta = Number(productionData.remove_delta || 0);
    const reason = productionData.remove_reason.trim();
    if (delta <= 0 || !reason) return;
    setActionLoading(operation.id);
    setProductionError(null);
    try {
      await productionSave.submitCorrection(operation.id, {
        quantity_delta: delta,
        reason,
        notes: productionData.notes.trim() || undefined,
        source: 'desktop',
      });
      showToast('success', `Removed ${delta} over-counted part${delta === 1 ? '' : 's'}`);
      productionSave.clearDraft(operation.id);
      closeProductionModal();
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      // The refusal is the WHOLE point of a server-gated correction — render it
      // INLINE next to the confirm button (the toast is secondary), verbatim.
      if (isDefinitiveHttpRefusal(err.response?.status ?? err.status)) {
        const detail = err.response?.data?.detail;
        const message = typeof detail === 'string' && detail ? detail : 'Correction was not saved';
        setProductionError(message);
        showToast('error', message);
      }
    } finally {
      setActionLoading(null);
    }
  };

  const handleCompleteOperation = async (operation: Operation) => {
    if (actionLoading !== null || productionSave.mutationsBlocked) return;
    setCompleteConfirm(null);
    setActionLoading(operation.id);
    try {
      await api.completeOperation(operation.id, {
        quantity_complete: Number(operation.quantity_ordered || 0),
      });
      showToast('success', `Completed ${operation.work_order_number}`);
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to complete operation');
    } finally {
      setActionLoading(null);
    }
  };

  const handleClockOut = async () => {
    if (!checkOutModal || actionLoading !== null || productionSave.mutationsBlocked) return;

    setActionLoading(checkOutModal.operation.id);
    try {
      await api.clockOut(checkOutModal.job.time_entry_id, {
        quantity_produced: Number(checkOutData.quantity_produced || 0),
        quantity_scrapped: Number(checkOutData.quantity_scrapped || 0),
        notes: checkOutData.notes || undefined,
        // Structured scrap reason (company code id and/or free text) required
        // when scrap > 0 (compliance/traceability), mirroring the kiosk.
        // Omitted entirely when nothing was scrapped.
        ...(Number(checkOutData.quantity_scrapped || 0) > 0
          ? scrapSelectionPayload(scrapCodes, checkOutData.scrap)
          : {}),
      });
      showToast('success', `Checked out of ${checkOutModal.operation.work_order_number}`);
      closeCheckOutModal();
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to check out');
    } finally {
      setActionLoading(null);
    }
  };

  const scrollToOperations = useCallback(() => {
    setTimeout(() => operationsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
  }, []);

  const openOperationDetails = useCallback(async (operationId: number) => {
    try {
      const details = await api.getOperationDetails(operationId);
      setDetailsModal(details);
    } catch {
      showToast('error', 'Failed to load operation details');
    }
  }, [showToast]);

  // A0.4 resolve-first scan flow. Traveler QRs (URL or OP:{id}/WO:{number}
  // forms) resolve through /scanner/resolve-action into a typed union; codes
  // it does not recognize (employee badges, supplier-part labels, part
  // numbers) fall back to the legacy /scanner/lookup path.
  const resolveScan = useCallback(async (rawCode: string) => {
    const code = rawCode.trim();
    if (!code) return;

    setActionLoading(-1);
    try {
      let resolved: ScanResolveResult | null = null;
      try {
        // Pass the station's work center so legal_actions reflect the
        // station gate (a mismatched station removes clock_in server-side).
        resolved = await api.resolveScanAction(code, workCenterIdRef.current || undefined);
      } catch {
        resolved = null; // resolver unavailable — legacy lookup below
      }

      if (!phone) setShowScanner(false);
      setScannerCode('');

      if (resolved?.kind === 'operation') {
        const op = resolved.operation;
        if (phone) {
          updateWorkspace({ view: 'all' });
          setWorkCenterId(op.work_center_id || '');
          setStatusFilter('');
          setDueTodayOnly(false);
          setActionableOnly(false);
          setDebouncedSearch(op.work_order_number);
          setExpandedOperationId(op.id);
          setOperationToFocus(op.id);
        }
        setSearch(op.work_order_number);
        setHighlightedOperationId(op.id);
        const actions = resolved.legal_actions.length > 0
          ? ` — ${formatScanActions(resolved.legal_actions)} available`
          : '';
        showToast('success', `Found ${op.name} on ${op.work_order_number}${actions}`);
        scrollToOperations();
        await openOperationDetails(op.id);
        return;
      }

      if (resolved?.kind === 'work_order') {
        if (phone) {
          updateWorkspace({ view: 'all' });
          setWorkCenterId('');
          setStatusFilter('');
          setDueTodayOnly(false);
          setActionableOnly(false);
          setDebouncedSearch(resolved.work_order.work_order_number);
        }
        setSearch(resolved.work_order.work_order_number);
        showToast('success', `Found ${resolved.work_order.work_order_number}`);
        scrollToOperations();
        return;
      }

      // kind 'employee' / 'unknown' (or resolver error): legacy behavior.
      if (phone) {
        updateWorkspace({ view: 'all' });
        setWorkCenterId('');
        setStatusFilter('');
        setDueTodayOnly(false);
        setActionableOnly(false);
      }
      const result = await api.scannerLookup(code);
      const nextSearch =
        result?.work_order?.work_order_number ||
        result?.work_order_number ||
        result?.part?.part_number ||
        result?.part_number ||
        code;
      setSearch(nextSearch);
      if (phone) setDebouncedSearch(nextSearch);
      showToast('success', `Found ${nextSearch}`);
      scrollToOperations();
    } catch {
      if (phone) throw new Error('Could not find this traveler. Check the connection and try again.');
      setSearch(code);
      showToast('info', 'Showing scanned code in search');
    } finally {
      setActionLoading(null);
    }
  }, [openOperationDetails, scrollToOperations, showToast, phone, updateWorkspace, setWorkCenterId]);

  const handleScannerSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await resolveScan(scannerCode);
  };

  // Phone-scanned traveler op QRs open /shop-floor/operations?scan=OP:{id}
  // (kiosk mode included). Strip successful scans; keep a failed code in the
  // link so a reconnect/reload can retry without finding the traveler again.
  const scanParamHandledRef = useRef<string | null>(null);
  const scanLocationRef = useRef(location);
  scanLocationRef.current = location;
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const scanCode = params.get('scan');
    if (!scanCode) {
      // Param gone (we stripped it, or plain navigation): re-arm so a LATER
      // client-side navigation to ?scan=... is handled. The ref still
      // suppresses strict-mode's double-invoke within one scan handling.
      scanParamHandledRef.current = null;
      return;
    }
    if (loading || scanParamHandledRef.current === scanCode) return;
    scanParamHandledRef.current = scanCode;
    const locationKey = location.key;
    void resolveScan(scanCode).then(() => {
      // A slow response must not replace a newer navigation or scanned link.
      if (scanLocationRef.current.key !== locationKey) return;
      params.delete('scan');
      navigate({ pathname: location.pathname, search: params.toString() }, { replace: true });
    }).catch(() => {
      if (scanLocationRef.current.key !== locationKey) return;
      showToast('error', 'Could not open the scanned traveler. Your code is kept in this link. Reconnect and reload to retry.');
    });
  }, [loading, location.key, location.pathname, location.search, navigate, resolveScan, showToast]);

  // Let the operator take in the spotlighted row, then fade it.
  useEffect(() => {
    if (highlightedOperationId === null) return;
    const timer = setTimeout(() => setHighlightedOperationId(null), 8000);
    return () => clearTimeout(timer);
  }, [highlightedOperationId]);

  const handleViewDetails = async (operation: Operation) => {
    await openOperationDetails(operation.id);
  };

  const openHoldModal = (operation: Operation) => {
    setHoldModal(operation);
    setHoldData({ category: '', note: '' });
  };

  const closeHoldModal = () => {
    setHoldModal(null);
    setHoldData({ category: '', note: '' });
  };

  const handleConfirmHold = async () => {
    if (!holdModal || !holdData.category || actionLoading !== null || productionSave.mutationsBlocked) return;
    const operationId = holdModal.id;
    const category = holdData.category;
    const note = holdData.note.trim();
    setActionLoading(operationId);
    try {
      await api.holdOperation(operationId, {
        category,
        severity: 'medium',
        // The backend only files a WorkOrderBlocker when the hold carries a note
        // OR a non-OTHER category. Send the operator's note when present; for the
        // category-only "Other" reason, fall back to a stub note so every hold
        // still files a structured blocker (mirrors the kiosk).
        note: note || (category === 'other' ? 'Other (reported on shop floor)' : undefined),
      });
      showToast('info', 'Operation placed on hold');
      closeHoldModal();
      await loadOperations();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to hold operation');
    } finally {
      setActionLoading(null);
    }
  };

  const handlePriorityChange = async (workOrderId: number, priorityRaw: string) => {
    const priority = parseInt(priorityRaw, 10);
    if (Number.isNaN(priority)) return;

    const existing = operations.find((op) => op.work_order_id === workOrderId);
    if (!existing || existing.priority === priority) return;

    setUpdatingPriorityWorkOrderId(workOrderId);
    try {
      const reason = priorityReason.trim() || undefined;
      await api.updateWorkOrderPriority(workOrderId, priority, reason);
      setOperations((prev) =>
        prev.map((op) => (op.work_order_id === workOrderId ? { ...op, priority } : op))
      );
      showToast('success', `Priority updated to P${priority}`);
      if (reason) {
        setPriorityReason('');
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update priority');
    } finally {
      setUpdatingPriorityWorkOrderId(null);
    }
  };

  const retryOriginalProduction = async () => {
    const operationId = productionSave.unconfirmed?.operationId;
    try {
      await productionSave.retry();
      if (productionModal?.operation.id === operationId) closeProductionModal();
      showToast('success', 'Original production report confirmed');
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch {
      // Persistent notice keeps the original immutable report available.
    }
  };

  const openCorrectionReview = async () => {
    const pending = productionSave.unconfirmedCorrection;
    if (!pending) return;
    closeProductionModal();
    setCorrectionReviewed(false);
    setCorrectionReview({ loading: true, details: null, error: null });
    try {
      const details = await api.getOperationDetails(pending.operationId);
      setCorrectionReview(current => current ? { loading: false, details, error: null } : null);
    } catch {
      setCorrectionReview(current => current ? { loading: false, details: null, error: 'Could not load correction history. Reconnect and try again.' } : null);
    }
  };

  const finishCorrectionReview = async () => {
    if (!correctionReviewed || !correctionReview?.details) return;
    try {
      productionSave.acknowledgeCorrectionReview();
      setCorrectionReview(null);
      setCorrectionReviewed(false);
      showToast('info', 'Review finished. The original removal was not resent.');
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      showToast('error', err.message || 'Could not finish correction review');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <ArrowPathIcon className="h-12 w-12 animate-spin text-werco-primary mx-auto mb-4" />
          <p className="text-surface-500">Loading shop floor...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Toast Notifications. z-[70]: must stack ABOVE the shared Modal overlay
          (z-[60]) — at the old z-50 an error toast fired while a modal stayed
          open rendered dimmed UNDER the backdrop and was unreadable. */}
      <div className="fixed top-4 left-4 right-4 z-[70] space-y-2 md:left-auto">
        {toasts.map(toast => (
          <div
            key={toast.id}
            className={`px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 animate-slide-in ${
              toast.type === 'success' ? 'bg-green-600 text-white' :
              toast.type === 'error' ? 'bg-red-600 text-white' :
              toast.type === 'warning' ? 'bg-amber-600 text-white' :
              'bg-blue-600 text-white'
            }`}
            // A warning earns the screen-reader interruption for the same reason
            // an error does: the action succeeded but fell short, and the
            // operator has to act on the shortfall.
            role={toast.type === 'error' || toast.type === 'warning' ? 'alert' : 'status'}
          >
            {toast.type === 'success' && <CheckCircleIcon className="h-5 w-5" />}
            {(toast.type === 'error' || toast.type === 'warning') && <ExclamationTriangleIcon className="h-5 w-5" />}
            {toast.type === 'info' && <ClockIcon className="h-5 w-5" />}
            <span className="font-medium">{toast.message}</span>
          </div>
        ))}
      </div>

      <ProductionSaveNotice phase={productionSave.phase} message={productionSave.message} online={productionSave.online}
        unconfirmed={productionSave.unconfirmed} onRetry={() => void retryOriginalProduction()}
        unconfirmedCorrection={productionSave.unconfirmedCorrection} onReviewCorrection={() => void openCorrectionReview()} />

      {phone && (
        <div className={`space-y-4 ${mobileView === 'my-work' && selectedActiveJob ? 'pb-28' : ''}`}>
          <div className="flex items-center justify-between gap-2">
            <h1 className="text-xl font-bold text-white">My shop floor</h1>
            <div className="flex gap-2">
              <button type="button" className="btn-secondary min-h-11 px-3" onClick={() => setShowScanner(true)} aria-label="Scan traveler">
                <QrCodeIcon className="mr-1 h-5 w-5" /> Scan
              </button>
              <button type="button" className="btn-secondary min-h-11 min-w-11" onClick={handleRefresh} disabled={refreshing} aria-label="Refresh jobs">
                <ArrowPathIcon className={`h-5 w-5 ${refreshing ? 'animate-spin' : ''}`} />
              </button>
            </div>
          </div>
          <nav aria-label="Shop floor views" className="grid grid-cols-3 gap-2">
            {([['my-work', `My work (${activeJobs.length})`], ['ready', 'Ready here'], ['all', 'All operations']] as const).map(([view, label]) => (
              <button key={view} type="button" aria-pressed={mobileView === view} onClick={() => selectMobileView(view)}
                className={`min-h-12 rounded-sm border px-2 text-sm font-semibold ${mobileView === view ? 'border-emerald-500 bg-emerald-500/15 text-emerald-200' : 'border-slate-700 text-slate-300'}`}>{label}</button>
            ))}
          </nav>
          {lastUpdated && <p className="text-xs text-slate-400">Last refreshed {formatCentralTime(new Date(lastUpdated).toISOString())}</p>}
          {activeJobsStale && <ErrorState title="Couldn't refresh your clocked-in job" message="Showing your last known check-ins. Refresh before recording work." onRetry={loadActiveJobs} />}
          {mobileView === 'my-work' ? (
            <MobileCurrentWork jobs={activeJobs} selected={selectedActiveJob} onSelect={selectCurrentJob}
              onQueue={() => selectMobileView('ready')}
              elapsed={getElapsedTime(selectedActiveJob?.clock_in)}
              disabled={actionLoading !== null || activeJobsStale || productionSave.mutationsBlocked}
              operationAvailable={currentOperationAvailable}
              onReport={() => { if (currentOperation && selectedActiveJob) handleOpenProductionModal(currentOperation, selectedActiveJob); }}
              onAddOne={() => { if (currentOperation) void reportProduction(currentOperation, 1); }}
              onDrawing={() => { if (currentOperation) setDocumentView({ operationId: currentOperation.id, tab: 'drawing' }); }}
              onNest={() => { if (currentOperation) setDocumentView({ operationId: currentOperation.id, tab: 'nest' }); }}
              onInstructions={() => { if (currentOperation) void handleViewDetails(currentOperation); }}
              onHold={() => { if (currentOperation) openHoldModal(currentOperation); }}
              onCheckOut={() => { if (currentOperation && selectedActiveJob) handleOpenCheckOut(currentOperation, selectedActiveJob); }}
              onComplete={() => setCompleteConfirm(currentOperation)}
            />
          ) : (
            <div className="space-y-3">
              <label className="block text-sm text-slate-300" htmlFor="mobile-queue-station">Work center</label>
              <select id="mobile-queue-station" value={workCenterId} onChange={event => setWorkCenterId(event.target.value ? Number(event.target.value) : '')} className="input min-h-12 w-full">
                <option value="">All work centers</option>
                {workCenters.map(center => <option key={center.id} value={center.id}>{center.name}</option>)}
              </select>
              <input type="search" value={search} onChange={event => setSearch(event.target.value)} aria-label="Search work orders or parts" placeholder="Search WO or part..." className="input min-h-12 w-full" />
              <p className="text-sm text-slate-400">{queueOperations.length} operations · dispatch order</p>
            </div>
          )}
          {mobileView === 'my-work' && phoneSession.available && user?.role !== 'platform_admin' && !user?.is_superuser && (
            <details className="rounded-sm border border-slate-700 p-3 text-sm">
              <summary className="min-h-11 cursor-pointer py-3 font-semibold text-slate-300">Phone settings</summary>
              <label className="flex min-h-12 items-center gap-3 text-white">
                <input type="checkbox" checked={phoneSession.personalPhone} onChange={event => phoneSession.setPersonalPhone(event.target.checked)} className="h-6 w-6" />
                This is my personal phone
              </label>
              <p className="mt-1 text-sm text-slate-400">Sign out after {phoneSession.timeoutMinutes} minutes without activity. Keep this off on shared devices. Your current job resumes after sign-in.</p>
            </details>
          )}
        </div>
      )}
      {!phone && <>
      {/* Mobile Header */}
      <div className="md:hidden space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold text-white flex items-center gap-2">
              <WrenchScrewdriverIcon className="h-6 w-6 text-fd-link" />
              Shop Floor
            </h1>
            <p className="text-xs text-slate-400">
              {selectedWorkCenter ? `${selectedWorkCenter.name} station` : 'All stations'}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowScanner((prev) => !prev)}
              className={`btn-secondary min-h-11 px-3 text-xs ${showScanner ? 'bg-werco-50 text-werco-700' : ''}`}
              aria-label="Scan traveler"
            >
              <QrCodeIcon className="h-4 w-4" />
              <span className="ml-1.5">Scan</span>
            </button>
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className="btn-secondary min-h-11 px-3 text-xs"
              aria-label="Refresh jobs"
            >
              <ArrowPathIcon className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
        {showScanner && (
          <form onSubmit={handleScannerSubmit} className="card-compact space-y-3">
            <label htmlFor="shopfloor-scan-traveler" className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Scan Traveler
            </label>
            <div className="flex gap-2">
              <input
                id="shopfloor-scan-traveler"
                type="text"
                value={scannerCode}
                onChange={(e) => setScannerCode(e.target.value)}
                className="input h-12 flex-1 text-base"
                placeholder="Scan or enter traveler code"
                aria-label="Scan Traveler"
                autoFocus
              />
              <button
                type="submit"
                disabled={actionLoading === -1 || !scannerCode.trim()}
                className="btn-primary min-h-12 px-4"
              >
                {actionLoading === -1 ? <ArrowPathIcon className="h-5 w-5 animate-spin" /> : 'Find'}
              </button>
            </div>
          </form>
        )}
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search WO or part..."
              aria-label="Search work orders or parts"
              className="input pl-9 text-sm"
            />
          </div>
          <button
            type="button"
            onClick={() => setShowMobileFilters((prev) => !prev)}
            className={`btn-secondary min-h-11 px-3 text-xs ${showMobileFilters ? 'bg-werco-50 text-werco-700' : ''}`}
            aria-label="Filter jobs"
          >
            <FunnelIcon className="h-4 w-4" />
          </button>
        </div>
        {showMobileFilters && (
          <div className="card-compact space-y-3">
            <div className="grid grid-cols-1 gap-2">
              <select
                value={workCenterId}
                onChange={(e) => setWorkCenterId(e.target.value ? Number(e.target.value) : '')}
                className="input text-sm"
              >
                <option value="">All Work Centers</option>
                {workCenters.map(wc => (
                  <option key={wc.id} value={wc.id}>{wc.name}</option>
                ))}
              </select>
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="input text-sm"
              >
                <option value="">All Status</option>
                <option value="pending">Pending</option>
                <option value="ready">Ready</option>
                <option value="in_progress">In Progress</option>
                <option value="on_hold">On Hold</option>
              </select>
            </div>
            {canEditPriority && (
              <div>
                <label htmlFor="shopfloor-priority-reason-mobile" className="text-xs font-medium text-slate-400 block mb-1">
                  Optional Priority Reason
                </label>
                <input
                  id="shopfloor-priority-reason-mobile"
                  type="text"
                  value={priorityReason}
                  onChange={(e) => setPriorityReason(e.target.value)}
                  className="input text-sm"
                  maxLength={500}
                  placeholder="Applied to your next priority change"
                  aria-label="Optional Priority Reason"
                />
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setDueTodayOnly((prev) => !prev)}
                className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
                  dueTodayOnly ? 'border-blue-500 bg-blue-500/20 text-blue-400' : 'border-slate-700 text-slate-400'
                }`}
              >
                Due Today
              </button>
              <button
                type="button"
                onClick={() => setActionableOnly((prev) => !prev)}
                className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
                  actionableOnly ? 'border-werco-500 bg-werco-50 text-werco-700' : 'border-slate-700 text-slate-400'
                }`}
              >
                Actionable Only
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Page Header */}
      <div className="page-header hidden md:flex">
        <div>
          <h1 className="page-title flex items-center gap-3">
            <WrenchScrewdriverIcon className="h-8 w-8 text-fd-link" />
            Shop Floor Operations
          </h1>
          <p className="page-subtitle">Check in and out of work order operations</p>
        </div>
        <div className="page-actions">
          <Button
            variant="secondary"
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <ArrowPathIcon className={`h-5 w-5 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Station summary (desktop) — compact KPI strip for the active queue */}
      <MiniStatStrip className="hidden md:grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={PlayIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Active"
          value={stationSummary.active}
          subtitle={selectedWorkCenter ? selectedWorkCenter.name : 'All work centers'}
        />
        <MiniStat
          icon={ClockIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Queue"
          value={stationSummary.queue}
          subtitle="Pending + ready"
        />
        <MiniStat
          icon={CubeIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Due Today"
          value={stationSummary.dueToday}
          valueColor={stationSummary.dueToday > 0 ? 'text-fd-blue' : undefined}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Overdue"
          value={stationSummary.overdue}
          valueColor={stationSummary.overdue > 0 ? 'text-fd-red' : undefined}
        />
      </MiniStatStrip>

      {activeJobs.length > 0 && (
        <section aria-labelledby="checked-in-operations-title" className="rounded-sm border border-emerald-500/40 bg-emerald-500/10">
          <h2 id="checked-in-operations-title" className="px-4 py-3 text-sm font-semibold text-emerald-300">
            You are checked into {activeJobs.length} {activeJobs.length === 1 ? 'operation' : 'operations'}
          </h2>
          <ul aria-label="Checked-in operations" className="max-h-80 overflow-y-auto divide-y divide-emerald-500/20 px-4">
            {activeJobs.map((job) => (
              <li key={job.time_entry_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <button
                  type="button"
                  onClick={() => handleGoToActiveJob(job)}
                  aria-label={`Go to ${job.work_order_number || 'current job'}, ${formatOperationLabel(job.operation_number)} - ${job.operation_name || 'operation'}`}
                  className="min-h-11 min-w-0 flex-1 rounded-sm p-2 text-left hover:bg-emerald-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300"
                >
                  <span className="block font-semibold text-white break-words">
                    {job.work_order_number || 'Current job'} &middot; {formatOperationLabel(job.operation_number)} - {job.operation_name || 'Operation'}
                  </span>
                  <UnitBadge unitNumber={job.unit_number} size="sm" className="mt-1" />
                  <span className="mt-1 block text-sm text-emerald-100/80">
                    {job.work_center_name || 'Shop floor'} &middot; {getElapsedTime(job.clock_in)}
                    {job.quantity_ordered != null && (
                      <> &middot; {job.quantity_complete || 0}/{job.quantity_ordered} complete</>
                    )}
                  </span>
                  <span className="mt-1 block text-xs font-semibold text-emerald-300">Go to operation &rarr;</span>
                </button>
                <button
                  type="button"
                  onClick={() => handleOpenActiveJobCheckOut(job)}
                  disabled={actionLoading !== null}
                  className="btn-success min-h-11 shrink-0 px-5 disabled:opacity-50"
                >
                  Check Out
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Stale active-job poll: keep the last-known strip above and say so —
          never let a failed refresh silently pretend "not clocked in". */}
      {activeJobsStale && (
        <ErrorState
          title="Couldn't refresh your clocked-in job"
          message="Showing your last known clock-in status until a refresh succeeds."
          onRetry={loadActiveJobs}
        />
      )}

      {/* Filters (desktop) */}
      <div className="card hidden md:block" data-tour="sf-clock">
        <div className="flex flex-wrap gap-4 items-center">
          <div className="flex-1 min-w-[200px]">
            <div className="relative">
              <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-slate-500" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search WO# or Part#..."
                aria-label="Search work orders or parts"
                className="input pl-10"
              />
            </div>
          </div>
          
          <div className="flex items-center gap-2">
            <FunnelIcon className="h-5 w-5 text-slate-500" />
            <select
              value={workCenterId}
              onChange={(e) => setWorkCenterId(e.target.value ? Number(e.target.value) : '')}
              className="input w-48"
            >
              <option value="">All Work Centers</option>
              {workCenters.map(wc => (
                <option key={wc.id} value={wc.id}>{wc.name}</option>
              ))}
            </select>
            
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="input w-40"
            >
              <option value="">All Status</option>
              <option value="pending">Pending</option>
              <option value="ready">Ready</option>
              <option value="in_progress">In Progress</option>
              <option value="on_hold">On Hold</option>
            </select>
          </div>
          
          <div className="text-sm text-slate-400">
            {operations.length} operation{operations.length !== 1 ? 's' : ''}
          </div>
        </div>
        {canEditPriority && (
          <div className="mt-3">
            <label htmlFor="shopfloor-priority-reason-desktop" className="text-xs font-medium text-slate-400 block mb-1">
              Optional Priority Reason
            </label>
            <input
              id="shopfloor-priority-reason-desktop"
              type="text"
              value={priorityReason}
              onChange={(e) => setPriorityReason(e.target.value)}
              className="input text-sm max-w-md"
              maxLength={500}
              placeholder="Applied to your next priority change"
              aria-label="Optional Priority Reason"
            />
          </div>
        )}
      </div>

      {/* Work Cell Buckets */}
      <div className="space-y-4">
        <div className="md:hidden rounded-sm border border-fd-line bg-fd-panel p-4">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Station</p>
              <p className="mt-1 truncate text-lg font-bold text-white">
                {selectedWorkCenter?.name || 'All Work Centers'}
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {selectedWorkCenter ? 'Locked to this work center' : 'Choose a station for the cleanest queue'}
              </p>
            </div>
            <button
              type="button"
              onClick={() => setShowMobileCenters((prev) => !prev)}
              className="btn-secondary min-h-11 shrink-0 px-4 text-sm"
            >
              Change
            </button>
          </div>
        </div>
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold text-white">Work Center Status</h2>
            <p className="text-sm text-slate-400">Real-time work cell availability and queue</p>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setShowMobileCenters((prev) => !prev)}
              className="text-sm font-semibold text-fd-link hover:text-sky-200"
              aria-expanded={showMobileCenters}
              aria-controls="shop-floor-station-status"
            >
              {showMobileCenters ? 'Hide station status' : 'Show station status'}
            </button>
            <button
              type="button"
              onClick={() => focusOperations('')}
              className="hidden md:inline text-sm font-semibold text-fd-link hover:text-sky-200"
            >
              View All
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setStatusFilter('');
              setActionableOnly(false);
            }}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              statusFilter === '' ? 'border-werco-500 bg-werco-50 text-werco-700' : 'border-slate-700 text-slate-400 hover:border-werco-300'
            }`}
          >
            All
          </button>
          <button
            type="button"
            onClick={() => {
              setStatusFilter('pending');
              setActionableOnly(false);
            }}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              statusFilter === 'pending' ? 'border-slate-500 bg-slate-800 text-slate-300' : 'border-slate-700 text-slate-400 hover:border-slate-400'
            }`}
          >
            Open
          </button>
          <button
            type="button"
            onClick={() => {
              setStatusFilter('in_progress');
              setActionableOnly(false);
            }}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              statusFilter === 'in_progress' ? 'border-amber-500 bg-amber-500/20 text-amber-400' : 'border-slate-700 text-slate-400 hover:border-amber-300'
            }`}
          >
            In Progress
          </button>
          <button
            type="button"
            onClick={() => {
              setStatusFilter('on_hold');
              setActionableOnly(false);
            }}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              statusFilter === 'on_hold' ? 'border-red-500 bg-red-500/20 text-red-400' : 'border-slate-700 text-slate-400 hover:border-red-300'
            }`}
          >
            On Hold
          </button>
          <button
            type="button"
            onClick={() => {
              setStatusFilter('ready');
              setActionableOnly(false);
            }}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              statusFilter === 'ready' ? 'border-blue-500 bg-blue-500/20 text-blue-400' : 'border-slate-700 text-slate-400 hover:border-blue-300'
            }`}
          >
            Ready
          </button>
          <button
            type="button"
            onClick={() => setDueTodayOnly((prev) => !prev)}
            className={`min-h-11 rounded-full border px-4 py-2.5 text-sm font-medium transition ${
              dueTodayOnly ? 'border-blue-500 bg-blue-500/20 text-blue-400' : 'border-slate-700 text-slate-400 hover:border-blue-300'
            }`}
          >
            Due Today
          </button>
        </div>
        <div id="shop-floor-station-status" className={`${showMobileCenters ? 'grid' : 'hidden'} grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 lg:max-h-[19rem] lg:overflow-y-auto lg:pr-1`}>
          {workCenterBuckets.map((bucket) => {
            const isActive = bucket.inProgress > 0;
            const hasQueue = bucket.open > 0;
            const statusLabel = isActive ? 'ACTIVE' : hasQueue ? 'QUEUED' : 'AVAILABLE';
            const statusPill =
              statusLabel === 'ACTIVE'
                ? 'bg-amber-500/20 text-amber-400'
                : statusLabel === 'QUEUED'
                ? 'bg-blue-500/20 text-blue-400'
                : 'bg-emerald-500/20 text-emerald-400';
            const statusBar =
              statusLabel === 'ACTIVE'
                ? 'bg-amber-500/100'
                : statusLabel === 'QUEUED'
                ? 'bg-blue-500/100'
                : 'bg-emerald-500/100';
            return (
              <button
                key={bucket.id}
                type="button"
                onClick={() => focusOperations(bucket.id)}
                className={`relative text-left rounded-sm border px-4 py-3 transition hover:shadow-md ${
                  workCenterId === bucket.id
                    ? 'border-werco-500 bg-werco-500/15 shadow-sm shadow-werco-500/20 ring-1 ring-werco-500/40'
                    : 'border-fd-line bg-fd-sunken hover:border-slate-500 hover:bg-slate-900/70'
                }`}
              >
                <div className="flex items-center gap-2 text-xs font-semibold">
                  <span className={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 ${statusPill}`}>
                    <span className="h-2 w-2 rounded-full bg-current" />
                    {statusLabel}
                  </span>
                </div>
                <div className="mt-2 text-base font-semibold text-white truncate">{bucket.name}</div>
                <div className="mt-0.5 text-xs text-slate-400">Work cell queue</div>
                <div className="mt-3 flex items-center gap-4 text-sm text-slate-400">
                  <span>Active: <span className="font-semibold text-slate-100 tabular-nums">{bucket.inProgress}</span></span>
                  <span>Queue: <span className="font-semibold text-slate-100 tabular-nums">{bucket.open}</span></span>
                </div>
                {bucket.dueToday > 0 && (
                  <div className="mt-3 text-xs font-medium text-blue-400">
                    Due Today: {bucket.dueToday}
                  </div>
                )}
                {bucket.overdue > 0 && (
                  <div className="mt-1 text-xs font-medium text-red-400">
                    Overdue: {bucket.overdue}
                  </div>
                )}
                <span className={`absolute right-4 top-6 h-12 w-1.5 rounded-full ${statusBar}`} />
              </button>
            );
          })}
        </div>
      </div>

      {/* Mobile Next Job */}
      {priorityFocusQueue[0] && (
        <div className="md:hidden rounded-sm border border-werco-500/30 bg-werco-500/10 p-4">
          {(() => {
            const op = priorityFocusQueue[0];
            const activeJob = getActiveJobForOperation(op);
            const overdue = isOverdue(op.due_date);
            // priorityFocusQueue includes ON_HOLD work, so this strip could put
            // the old resume+clock-in double write behind a phone-sized button.
            // A held job gets the same one-write Clear Hold the grid card does.
            const onHold = op.status === 'on_hold';
            return (
              <div className="space-y-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-werco-300">
                      Next Recommended Job
                    </p>
                    <div className="mt-1 flex items-center gap-2">
                      <KioskRunOrderChip item={op} size="sm" />
                      <p className="text-lg font-bold text-white">{op.work_order_number}</p>
                      <UnitBadge unitNumber={op.unit_number} size="sm" className="mt-1" />
                    </div>
                    <p className="text-sm text-slate-300">
                      {formatOperationLabel(op.operation_number)} - {op.operation_name}
                    </p>
                  </div>
                  <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${getPriorityClasses(op.priority)}`}>
                    {getPriorityLabel(op.priority)}
                  </span>
                </div>
                <div className="flex items-center justify-between text-sm text-slate-400">
                  <span>{op.part_number || 'No part'}</span>
                  <span className={overdue ? 'font-semibold text-red-400' : ''}>
                    {op.due_date ? `Due ${formatCentralDate(op.due_date, { year: undefined })}` : 'No due date'}
                  </span>
                </div>
                {onHold && (
                  <OperationHoldReason hold={op.hold} testId={`shop-floor-next-hold-reason-${op.id}`} />
                )}
                <button
                  type="button"
                  onClick={() => {
                    if (onHold) return handleClearHold(op);
                    return activeJob ? handleOpenCheckOut(op, activeJob) : handleCheckIn(op);
                  }}
                  disabled={onHold ? actionLoading !== null : actionLoading === op.id}
                  data-testid={onHold ? `shop-floor-next-clear-hold-${op.id}` : undefined}
                  className={`min-h-12 w-full ${activeJob && !onHold ? 'btn-success' : 'btn-primary'} disabled:opacity-50 disabled:cursor-not-allowed`}
                >
                  {actionLoading === op.id ? (
                    <ArrowPathIcon className="h-5 w-5 animate-spin" />
                  ) : onHold ? (
                    <>
                      <PlayIcon className="h-5 w-5 mr-2" />
                      Clear Hold
                    </>
                  ) : activeJob ? (
                    <>
                      <CheckCircleIcon className="h-5 w-5 mr-2" />
                      Check Out
                    </>
                  ) : (
                    <>
                      <PlayIcon className="h-5 w-5 mr-2" />
                      Check In
                    </>
                  )}
                </button>
              </div>
            );
          })()}
        </div>
      )}

      {/*
        De-dup: the desktop "Most Important Next" focus card was the first five
        rows of the operations grid duplicated. The grid below renders the
        server's canonical dispatch order (run_order first), so the top rows ARE
        the focus queue — rendering it twice was redundant. The mobile "Next Job"
        strip stays (it's the single top pick on a small screen where the full
        grid is far down). priorityFocusQueue still backs that strip.
      */}

      </>}

      {/* Operations Grid */}
      {(!phone || mobileView !== 'my-work') && (operationsError ? (
        <ErrorState message="Could not load operations" onRetry={loadOperations} />
      ) : queueOperations.length === 0 ? (
        <EmptyState
          icon={CubeIcon}
          title={selectedWorkCenter ? `No operations found for ${selectedWorkCenter.name}` : 'No operations found'}
          description={
            selectedWorkCenter && queuedOperationsAcrossCenters > 0
              ? 'Other work centers have queued work. View all operations or choose a different station.'
              : 'Try adjusting your filters'
          }
          action={
            selectedWorkCenter ? (
              <Button variant="secondary" onClick={() => focusOperations('')}>
                View All Operations
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div ref={operationsRef} className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4" data-tour="sf-operations">
          {/* Cards render in server order — the same canonical run-order sort
              the kiosks show. Do not re-sort client-side. */}
          {queueOperations.map(op => {
            const progress = op.quantity_ordered > 0
              ? (op.quantity_complete / op.quantity_ordered) * 100 
              : 0;
            const overdue = isOverdue(op.due_date);
            const activeJob = getActiveJobForOperation(op);
            const canCheckIn = op.can_check_in !== false;
            const showCheckIn = op.status === 'pending' || op.status === 'ready' || (op.status === 'in_progress' && !activeJob);
            const remainingQuantity = getRemainingQuantity(op);
            const targetReached = Boolean(activeJob && op.status === 'in_progress' && remainingQuantity <= 0);
            
            return (
              <div
                key={op.id}
                ref={(card) => {
                  if (card) operationCardsRef.current.set(op.id, card);
                  else operationCardsRef.current.delete(op.id);
                }}
                tabIndex={-1}
                data-testid={`shop-floor-op-${op.id}`}
                className={`card ${phone ? '!p-0' : ''} scroll-mt-24 hover:shadow-lg transition-shadow ${overdue ? 'border-red-500/30 bg-red-500/10' : ''} ${
                  highlightedOperationId === op.id ? 'border-werco-500 ring-1 ring-werco-500/60' : ''
                }`}
              >
                {phone && (
                  <button type="button" className="flex min-h-20 w-full items-center justify-between gap-3 p-4 text-left"
                    aria-expanded={expandedOperationId === op.id} aria-controls={`operation-content-${op.id}`}
                    onClick={() => setExpandedOperationId(current => current === op.id ? null : op.id)}>
                    <span className="min-w-0 space-y-1">
                      <span className="block break-words font-bold text-white">{op.work_order_number}</span>
                      <span className="block text-sm text-slate-300">{formatOperationLabel(op.operation_number)} — {op.operation_name}</span>
                      <span className="block text-xs text-slate-400">{op.part_number} · {op.quantity_complete}/{op.quantity_ordered} complete</span>
                      <span className="block text-xs capitalize text-slate-300">{op.status.replace('_', ' ')}</span>
                    </span>
                    <span className="shrink-0 text-sm font-semibold text-blue-300">{expandedOperationId === op.id ? 'Close' : 'Open'}</span>
                  </button>
                )}
                {(!phone || expandedOperationId === op.id) && <div id={`operation-content-${op.id}`} className={phone ? 'border-t border-slate-700 p-4' : undefined}>
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <KioskRunOrderChip item={op} size="sm" />
                      <span className="font-bold text-werco-primary text-lg">{op.work_order_number}</span>
                      <UnitBadge unitNumber={op.unit_number} size="sm" />
                      {overdue && (
                        <span className="px-2 py-0.5 bg-red-500/20 text-red-400 text-xs font-semibold rounded">
                          OVERDUE
                        </span>
                      )}
                    </div>
                    <p className="text-sm text-slate-400">{op.part_number}</p>
                  </div>
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold capitalize ${statusColor(op.status)}`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${statusDot(op.status)}`}></span>
                    {op.status.replace('_', ' ')}
                  </span>
                </div>
                
                {/* Operation Info */}
                <div className="bg-slate-800/50 rounded-lg p-3 mb-3">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-semibold text-white">
                      {formatOperationLabel(op.operation_number)} - {op.operation_name}
                    </span>
                  </div>
                  <p className="text-sm text-slate-400">{op.part_name}</p>
                  {op.work_center_name && (
                    <p className="text-xs text-slate-400 mt-1">
                      Work Center: {op.work_center_name}
                    </p>
                  )}
                  {op.laser_nest && (
                    <div className="mt-2">
                      <LaserNestOperatorPanel nest={op.laser_nest} size="compact" />
                    </div>
                  )}
                </div>

                {/* WHY it stopped — on the card, BEFORE the Clear Hold button.
                    The card showed the "on hold" pill and nothing else, so
                    clearing a hold meant lifting a stop whose reason you could
                    not see. Renders nothing when the payload carries no hold
                    block (a backend that predates the field). */}
                {op.status === 'on_hold' && (
                  <OperationHoldReason
                    hold={op.hold}
                    size="md"
                    className="mb-3"
                    testId={`shop-floor-hold-reason-${op.id}`}
                  />
                )}

                {/* Progress */}
                <div className="mb-3">
                  <div className="flex justify-between text-sm mb-1">
                    <span className="font-medium text-slate-300">{op.laser_nest ? 'Runs' : 'Progress'}</span>
                    <span className="font-semibold tabular-nums">
                      {op.quantity_complete} / {op.quantity_ordered}
                    </span>
                  </div>
                  <div className="h-2.5 bg-slate-700 rounded-full overflow-hidden">
                    <div 
                      className={`h-full rounded-full transition-all ${progress >= 100 ? 'bg-green-500/100' : 'bg-werco-600'}`}
                      style={{ width: `${Math.min(100, progress)}%` }}
                    />
                  </div>
                  <div className="text-right text-xs text-slate-400 mt-0.5">
                    {Math.round(progress)}% complete
                  </div>
                </div>

                {targetReached && (
                  <div className="mb-3 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3">
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <p className="text-sm font-semibold text-emerald-300">Target quantity reached</p>
                        <p className="text-xs text-emerald-200/80">Stay clocked in, complete the operation, or check out.</p>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => setCompleteConfirm(op)}
                          disabled={actionLoading === op.id}
                          className="btn-success btn-sm"
                        >
                          Complete Operation
                        </button>
                        <button
                          onClick={() => handleOpenCheckOut(op, activeJob!)}
                          disabled={actionLoading === op.id}
                          className="btn-secondary btn-sm"
                        >
                          Check Out
                        </button>
                      </div>
                    </div>
                  </div>
                )}
                
                {/* Meta Info */}
                <div className="flex items-center justify-between text-sm text-slate-400 mb-4">
                  <div>
                    {op.due_date && (
                      <span className={overdue ? 'text-red-600 font-medium' : ''}>
                        Due: {formatCentralDate(op.due_date, { year: undefined })}
                      </span>
                    )}
                  </div>
                  <div>
                    {canEditPriority ? (
                      <select
                        value={op.priority}
                        onChange={(e) => handlePriorityChange(op.work_order_id, e.target.value)}
                        disabled={updatingPriorityWorkOrderId === op.work_order_id}
                        className={`px-2 py-0.5 rounded text-xs font-medium border border-transparent ${getPriorityClasses(op.priority)}`}
                        title="Update priority"
                      >
                        {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((p) => (
                          <option key={p} value={p}>
                            {getPriorityLabel(p)}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${getPriorityClasses(op.priority)}`}>
                        {getPriorityLabel(op.priority)}
                      </span>
                    )}
                  </div>
                </div>
                
                {showCheckIn && !canCheckIn && (
                  <p className="mb-3 text-sm text-amber-300">
                    Waiting for earlier operations on this work order to be completed. Open Details to review the routing.
                  </p>
                )}

                {/* Action Buttons */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2" data-tour="sf-complete">
                  {op.status === 'in_progress' && activeJob && !targetReached && (
                    <>
                      <button
                        onClick={() => reportProduction(op, 1)}
                        disabled={actionLoading === op.id || productionSave.mutationsBlocked || remainingQuantity <= 0}
                        className="flex-1 btn-success text-base sm:text-sm py-3 sm:py-2.5 w-full disabled:opacity-50 disabled:cursor-not-allowed"
                        title={remainingQuantity <= 0 ? 'Target quantity reached' : 'Add one completed part'}
                      >
                        {actionLoading === op.id ? (
                          <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                        ) : (
                          <>
                            <CheckCircleIcon className="h-4 w-4 mr-1.5" />
                            <span>+1 Complete</span>
                          </>
                        )}
                      </button>
                      <button
                        onClick={() => handleOpenProductionModal(op, activeJob)}
                        disabled={actionLoading === op.id}
                        className="btn-secondary text-sm py-2.5 px-3 w-full sm:w-auto"
                      >
                        Report quantity
                      </button>
                    </>
                  )}

                  {/* Check In Button */}
                  {showCheckIn && (
                    <button
                      onClick={() => handleCheckIn(op)}
                      disabled={actionLoading === op.id || productionSave.mutationsBlocked || !canCheckIn}
                      className="flex-1 btn-primary text-base sm:text-sm py-3 sm:py-2.5 w-full disabled:opacity-50 disabled:cursor-not-allowed"
                      title={canCheckIn ? 'Check in' : 'Waiting for earlier operations on this work order'}
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : !canCheckIn ? (
                        <>
                          <ClockIcon className="h-4 w-4 mr-1.5" />
                          <span>Waiting</span>
                        </>
                      ) : (
                        <>
                          <PlayIcon className="h-4 w-4 mr-1.5" />
                          <span>Check In</span>
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* Check Out Button */}
                  {op.status === 'in_progress' && activeJob && !targetReached && (
                    <button
                      onClick={() => handleOpenCheckOut(op, activeJob)}
                      disabled={actionLoading === op.id}
                      className="btn-secondary text-sm py-2.5 px-3 w-full sm:w-auto"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <CheckCircleIcon className="h-4 w-4 mr-1.5" />
                          <span>Check Out</span>
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* Hold Button - visible when in progress */}
                  {op.status === 'in_progress' && (
                    <button
                      onClick={() => openHoldModal(op)}
                      disabled={actionLoading === op.id}
                      className="btn-secondary text-sm py-2.5 px-3 w-full sm:w-auto"
                      title="Put on Hold"
                    >
                      <PauseIcon className="h-4 w-4" />
                      <span className="ml-1.5">Hold</span>
                    </button>
                  )}
                  
                  {/* Clear Hold — ONE write (resumeOperation), never a clock-in.
                      This button used to be labelled "Check In" and ran resume +
                      clockIn in one try block; a refusal on the clock-in leg left
                      the resume committed, showed a red error, and skipped the
                      refresh — so the next tap answered "Operation is not on
                      hold". Splitting the verbs is what fixes that. */}
                  {op.status === 'on_hold' && (
                    <button
                      onClick={() => handleClearHold(op)}
                      disabled={actionLoading !== null || productionSave.mutationsBlocked}
                      data-testid={`shop-floor-clear-hold-${op.id}`}
                      className="flex-1 btn-primary text-base sm:text-sm py-3 sm:py-2.5 w-full disabled:opacity-50 disabled:cursor-not-allowed"
                      title="Lifts the hold only — any blocker stays open for a supervisor to resolve"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <PlayIcon className="h-4 w-4 mr-1.5" />
                          <span>Clear Hold</span>
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* View Details Button - always visible */}
                  <button
                    onClick={() => handleViewDetails(op)}
                    className="btn-secondary text-sm py-2.5 px-3 w-full sm:w-auto"
                    title="View Details"
                  >
                    <EyeIcon className="h-4 w-4" />
                    <span className="ml-1.5">Details</span>
                  </button>
                </div>
                </div>}
              </div>
            );
          })}
        </div>
      ))}

      {phone && <ShopFloorCameraScanner open={showScanner} onClose={() => setShowScanner(false)} onScan={resolveScan} />}
      <Modal open={documentView !== null} onClose={() => setDocumentView(null)} size="7xl" padded={false} scroll={false}
        ariaLabel="Operation drawings" className="flex h-[90dvh] flex-col overflow-auto">
        {documentView && <KioskDocViewer operationId={documentView.operationId} initialTab={documentView.tab} transport={SHOP_FLOOR_DOCS} onBack={() => setDocumentView(null)} />}
      </Modal>

      {/* Confirm full-quantity completion (closes the operation, no undo) */}
      <ConfirmDialog
        open={completeConfirm !== null}
        title="Complete operation at full quantity?"
        message="This closes the operation and cannot be undone."
        confirmLabel="Complete Operation"
        cancelLabel="Cancel"
        variant="warning"
        onConfirm={() => {
          if (completeConfirm) handleCompleteOperation(completeConfirm);
        }}
        onCancel={() => setCompleteConfirm(null)}
      />

      {/* Add Production Modal */}
      <Modal
        open={productionModal !== null}
        onClose={closeProductionModal}
        size="md"
        padded={false}
        scroll={false}
      >
        {productionModal && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">
                {productionData.mode === 'remove' ? 'Correct Over-Count' : 'Add Completed Quantity'}
              </h3>
              <button onClick={closeProductionModal} className="p-2 rounded-lg hover:bg-slate-800" aria-label="Close">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <ProductionSaveNotice phase={productionSave.phase} message={productionSave.message} online={productionSave.online}
                unconfirmed={productionSave.unconfirmed} onRetry={() => void retryOriginalProduction()}
                unconfirmedCorrection={productionSave.unconfirmedCorrection} onReviewCorrection={() => void openCorrectionReview()} />
              <div className="bg-slate-800/50 rounded-lg p-4">
                <p className="text-sm text-slate-400">Operation</p>
                <p className="font-semibold text-white">
                  {formatOperationLabel(productionModal.operation.operation_number)} - {productionModal.operation.operation_name}
                </p>
                <p className="text-sm text-slate-400 mt-1">
                  {productionModal.operation.work_order_number} &middot; {productionModal.operation.part_number}
                </p>
                <p className="mt-2 text-xs text-slate-400">
                  Completed {productionModal.operation.quantity_complete} / {productionModal.operation.quantity_ordered}
                </p>
              </div>

              {/* Mode toggle: additive report vs. self-service over-count
                  correction (reduce-production). Removing is a miscount fix, not
                  scrap — the server bounds it to what THIS operator recorded on
                  their own open clock-in and refuses once the op/WO is complete. */}
              <div className="grid grid-cols-2 gap-2" role="group" aria-label="Quantity adjustment mode">
                <button
                  type="button"
                  aria-pressed={productionData.mode === 'add'}
                  onClick={() => {
                    setProductionError(null);
                    setProductionData((prev) => ({ ...prev, mode: 'add' }));
                  }}
                  className={`min-h-11 rounded-sm border px-3 text-sm font-semibold transition ${
                    productionData.mode === 'add'
                      ? 'border-emerald-500 bg-emerald-500/15 text-emerald-300'
                      : 'border-slate-700 text-slate-400 hover:border-slate-500'
                  }`}
                >
                  Add completed
                </button>
                <button
                  type="button"
                  aria-pressed={productionData.mode === 'remove'}
                  onClick={() => {
                    setProductionError(null);
                    setProductionData((prev) => ({ ...prev, mode: 'remove' }));
                  }}
                  className={`min-h-11 rounded-sm border px-3 text-sm font-semibold transition ${
                    productionData.mode === 'remove'
                      ? 'border-amber-500 bg-amber-500/15 text-amber-300'
                      : 'border-slate-700 text-slate-400 hover:border-slate-500'
                  }`}
                >
                  Correct over-count
                </button>
              </div>

              {productionData.mode === 'add' && (
              <>
              <div>
                <label htmlFor="shopfloor-prod-good-parts" className="label">Good parts to add</label>
                <input
                  id="shopfloor-prod-good-parts"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={productionData.quantity_complete_delta}
                  onChange={(e) => setProductionData({ ...productionData, quantity_complete_delta: Number(e.target.value) || 0 })}
                  className="input h-14 text-center text-2xl font-bold"
                  aria-label="Good parts to add"
                  autoFocus
                />
                <div className="mt-3 grid grid-cols-3 gap-2">
                  {[1, 5, 10].map((amount) => (
                    <button
                      key={amount}
                      type="button"
                      onClick={() => adjustProductionQuantity(amount)}
                      className="btn-secondary min-h-11 px-2 text-sm"
                    >
                      +{amount}
                    </button>
                  ))}
                  <button
                    type="button"
                    onClick={() => setProductionData({
                      ...productionData,
                      quantity_complete_delta: getRemainingQuantity(productionModal.operation),
                    })}
                    className="btn-secondary col-span-3 min-h-11 px-2 text-sm"
                  >
                    Remaining
                  </button>
                </div>
                <p className="mt-2 text-xs text-slate-400">
                  Remaining: {getRemainingQuantity(productionModal.operation)}
                </p>
              </div>

              <div>
                <label htmlFor="shopfloor-prod-scrap" className="label">Scrap to add</label>
                <input
                  id="shopfloor-prod-scrap"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={productionData.quantity_scrapped_delta}
                  onChange={(e) => setProductionData({ ...productionData, quantity_scrapped_delta: Number(e.target.value) || 0 })}
                  className="input h-12 text-center text-lg font-semibold"
                  aria-label="Scrap to add"
                />
              </div>

              {/* Scrap reason is required for traceability when scrap > 0 —
                  company scrap codes when defined, legacy SCRAP_REASONS
                  fallback otherwise (same columns the kiosk writes). */}
              {Number(productionData.quantity_scrapped_delta || 0) > 0 && (
                <ScrapReasonFields
                  codes={scrapCodes}
                  value={productionData.scrap}
                  onChange={(scrap) => setProductionData({ ...productionData, scrap })}
                />
              )}
              </>
              )}

              {productionData.mode === 'remove' && (
              <>
              <div className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200/90">
                Removes over-counted pieces you recorded on this operation — a miscount correction,
                not scrap. Approved labor or another operator&apos;s counts need a supervisor.
              </div>

              <div>
                <label htmlFor="shopfloor-reduce-qty" className="label">Parts to remove</label>
                <input
                  id="shopfloor-reduce-qty"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={productionData.remove_delta}
                  onChange={(e) => setProductionData({ ...productionData, remove_delta: Number(e.target.value) || 0 })}
                  className="input h-14 text-center text-2xl font-bold"
                  aria-label="Parts to remove"
                  autoFocus
                />
                <div className="mt-3 grid grid-cols-3 gap-2">
                  {[1, 5, 10].map((amount) => (
                    <button
                      key={amount}
                      type="button"
                      onClick={() => adjustRemoveQuantity(amount)}
                      className="btn-secondary min-h-11 px-2 text-sm"
                    >
                      +{amount}
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-xs text-slate-400">
                  Completed now: {productionModal.operation.quantity_complete} / {productionModal.operation.quantity_ordered}
                </p>
              </div>

              <div>
                <label htmlFor="shopfloor-reduce-reason" className="label">
                  Reason for correction{' '}
                  <span aria-hidden="true" className="text-fd-red">*</span>
                </label>
                <input
                  id="shopfloor-reduce-reason"
                  type="text"
                  maxLength={255}
                  value={productionData.remove_reason}
                  onChange={(e) => setProductionData({ ...productionData, remove_reason: e.target.value })}
                  className="input"
                  placeholder="e.g. double-scanned the tray"
                  aria-label="Reason for correction"
                  aria-required="true"
                />
                <p className="mt-1 text-xs text-slate-400">Recorded on the audit trail. Required.</p>
              </div>
              </>
              )}

              <div>
                <label htmlFor="shopfloor-prod-notes" className="label">Notes (optional)</label>
                <textarea
                  id="shopfloor-prod-notes"
                  value={productionData.notes}
                  onChange={(e) => setProductionData({ ...productionData, notes: e.target.value })}
                  className="input"
                  rows={3}
                  placeholder="Production notes, scrap details, or inspection notes..."
                  aria-label="Notes (optional)"
                />
              </div>

              {/* Server refusal, INLINE and verbatim — the primary display. A
                  toast alone sat under the Modal's z-[60] overlay and was
                  unreadable on the shop floor; this region sits right above the
                  confirm button and is announced via role="alert". */}
              {productionError && (
                <div
                  role="alert"
                  data-testid="shopfloor-production-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-base font-semibold text-red-300"
                >
                  {productionError}
                </div>
              )}
            </div>

            <div className="modal-footer">
              <Button variant="secondary" onClick={closeProductionModal}>
                Cancel
              </Button>
              {productionData.mode === 'remove' ? (
                <button
                  onClick={handleReduceProduction}
                  disabled={
                    actionLoading === productionModal.operation.id || productionSave.mutationsBlocked ||
                    Number(productionData.remove_delta || 0) <= 0 ||
                    productionData.remove_reason.trim().length === 0
                  }
                  className="btn-danger disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {actionLoading === productionModal.operation.id ? (
                    <ArrowPathIcon className="h-5 w-5 animate-spin" />
                  ) : (
                    <>
                      <MinusCircleIcon className="h-5 w-5 mr-2" />
                      Remove from Completed
                    </>
                  )}
                </button>
              ) : (
                <button
                  onClick={handleSaveProduction}
                  disabled={
                    actionLoading === productionModal.operation.id || productionSave.mutationsBlocked ||
                    (Number(productionData.quantity_complete_delta || 0) <= 0 &&
                      Number(productionData.quantity_scrapped_delta || 0) <= 0) ||
                    (Number(productionData.quantity_scrapped_delta || 0) > 0 &&
                      !isScrapSelectionComplete(scrapCodes, productionData.scrap))
                  }
                  className="btn-success disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {actionLoading === productionModal.operation.id ? (
                    <ArrowPathIcon className="h-5 w-5 animate-spin" />
                  ) : (
                    <>
                      <CheckCircleIcon className="h-5 w-5 mr-2" />
                      Add to Completed
                    </>
                  )}
                </button>
              )}
            </div>
          </>
        )}
      </Modal>

      {/* Check Out Modal */}
      <Modal
        open={checkOutModal !== null}
        onClose={closeCheckOutModal}
        size="md"
        padded={false}
        scroll={false}
      >
        {checkOutModal && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Check Out</h3>
              <button onClick={closeCheckOutModal} className="p-2 rounded-lg hover:bg-slate-800">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <div className="bg-slate-800/50 rounded-lg p-4">
                <p className="text-sm text-slate-400">Operation</p>
                <p className="font-semibold text-white">
                  {formatOperationLabel(checkOutModal.operation.operation_number)} - {checkOutModal.operation.operation_name}
                </p>
                <p className="text-sm text-slate-400 mt-1">
                  {checkOutModal.operation.work_order_number} &middot; {checkOutModal.operation.part_number}
                </p>
                <p className="mt-2 text-xs text-slate-400">
                  Started {formatCentralTime(checkOutModal.job.clock_in)} &middot; {getElapsedTime(checkOutModal.job.clock_in)}
                </p>
              </div>
              
              <div>
                <label htmlFor="shopfloor-checkout-good-parts" className="label">Additional good parts at checkout</label>
                <div className="flex items-center gap-2">
                  <input
                    id="shopfloor-checkout-good-parts"
                    type="number"
                    inputMode="decimal"
                    min={0}
                    value={checkOutData.quantity_produced}
                    onChange={(e) => setCheckOutData({ ...checkOutData, quantity_produced: Number(e.target.value) || 0 })}
                    className="input h-14 flex-1 text-center text-2xl font-bold"
                    aria-label="Additional good parts at checkout"
                    autoFocus
                  />
                </div>
                <div className="mt-3 grid grid-cols-3 gap-2">
                  {[1, 5, 10].map((amount) => (
                    <button
                      key={amount}
                      type="button"
                      onClick={() => adjustGoodQuantity(amount)}
                      className="btn-secondary min-h-11 px-2 text-sm"
                    >
                      +{amount}
                    </button>
                  ))}
                  <button
                    type="button"
                    onClick={() => setCheckOutData({
                      ...checkOutData,
                      quantity_produced: getRemainingQuantity(checkOutModal.operation),
                    })}
                    className="btn-secondary col-span-3 min-h-11 px-2 text-sm"
                  >
                    Remaining
                  </button>
                </div>
                <p className="mt-2 text-xs text-slate-400">
                  Use this only for parts not already added with +1 Complete. Remaining: {getRemainingQuantity(checkOutModal.operation)}
                </p>
              </div>

              <div>
                <label htmlFor="shopfloor-checkout-scrap" className="label">Scrap</label>
                <input
                  id="shopfloor-checkout-scrap"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={checkOutData.quantity_scrapped}
                  onChange={(e) => setCheckOutData({ ...checkOutData, quantity_scrapped: Number(e.target.value) || 0 })}
                  className="input h-12 text-center text-lg font-semibold"
                  aria-label="Scrap"
                />
              </div>

              {/* Scrap reason is required for traceability when scrap > 0 —
                  company scrap codes when defined, legacy SCRAP_REASONS
                  fallback otherwise (same columns the kiosk writes). */}
              {Number(checkOutData.quantity_scrapped || 0) > 0 && (
                <ScrapReasonFields
                  codes={scrapCodes}
                  value={checkOutData.scrap}
                  onChange={(scrap) => setCheckOutData({ ...checkOutData, scrap })}
                />
              )}

              <div>
                <label htmlFor="shopfloor-checkout-notes" className="label">Notes (optional)</label>
                <textarea
                  id="shopfloor-checkout-notes"
                  value={checkOutData.notes}
                  onChange={(e) => setCheckOutData({ ...checkOutData, notes: e.target.value })}
                  className="input"
                  rows={3}
                  placeholder="Issues, observations, or notes from this session..."
                  aria-label="Notes (optional)"
                />
              </div>
            </div>

            <div className="modal-footer">
              <Button variant="secondary" onClick={closeCheckOutModal}>
                Cancel
              </Button>
              <button
                onClick={handleClockOut}
                disabled={
                  actionLoading === checkOutModal.operation.id || productionSave.mutationsBlocked ||
                  checkOutData.quantity_produced < 0 ||
                  checkOutData.quantity_scrapped < 0 ||
                  (Number(checkOutData.quantity_scrapped || 0) > 0 &&
                    !isScrapSelectionComplete(scrapCodes, checkOutData.scrap))
                }
                className="btn-success disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {actionLoading === checkOutModal.operation.id ? (
                  <ArrowPathIcon className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <CheckCircleIcon className="h-5 w-5 mr-2" />
                    End time and save
                  </>
                )}
              </button>
            </div>
          </>
        )}
      </Modal>

      {/* Hold Modal — desktop holds file a structured WorkOrderBlocker
          (category from the shared HOLD_REASONS + optional note), mirroring the
          kiosk so traceability is preserved. */}
      <Modal
        open={holdModal !== null}
        onClose={closeHoldModal}
        size="md"
        padded={false}
        scroll={false}
      >
        {holdModal && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Place on Hold</h3>
              <button onClick={closeHoldModal} className="min-h-11 min-w-11 p-2 rounded-lg hover:bg-slate-800" aria-label="Close hold form">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <div className="bg-slate-800/50 rounded-lg p-4">
                <p className="text-sm text-slate-400">Operation</p>
                <p className="font-semibold text-white">
                  {formatOperationLabel(holdModal.operation_number)} - {holdModal.operation_name}
                </p>
                <p className="text-sm text-slate-400 mt-1">
                  {holdModal.work_order_number} &middot; {holdModal.part_number}
                </p>
              </div>

              <div>
                <span className="label">
                  Hold reason <span className="text-red-400">*</span>
                </span>
                {phone ? <div className="grid grid-cols-2 gap-2" role="group" aria-label="Hold reason">
                  {HOLD_REASONS.map(reason => <button key={reason.value} type="button" aria-pressed={holdData.category === reason.value}
                    onClick={() => setHoldData({ ...holdData, category: reason.value })}
                    className={`min-h-12 rounded-sm border px-2 py-2 text-sm font-semibold ${holdData.category === reason.value ? 'border-amber-400 bg-amber-500/15 text-amber-200' : 'border-slate-600 text-slate-200'}`}>
                    {reason.label}
                  </button>)}
                </div> : <SelectField
                  value={holdData.category}
                  onChange={(value) => setHoldData({ ...holdData, category: String(value) })}
                  options={HOLD_REASONS.map((r) => ({ value: r.value, label: r.label }))}
                  placeholder="Select a hold reason"
                  ariaLabel="Hold reason"
                />}
                {!holdData.category && (
                  <p className="mt-1 text-xs text-red-400">A reason is required to place a hold.</p>
                )}
              </div>

              <div>
                <label htmlFor="shopfloor-hold-note" className="label">Note (optional)</label>
                <textarea
                  id="shopfloor-hold-note"
                  value={holdData.note}
                  onChange={(e) => setHoldData({ ...holdData, note: e.target.value })}
                  className="input"
                  rows={3}
                  placeholder="Add context for the blocker (what's needed, who to notify)..."
                  aria-label="Note (optional)"
                />
              </div>
            </div>

            <div className="modal-footer">
              <Button variant="secondary" onClick={closeHoldModal}>
                Cancel
              </Button>
              <button
                onClick={handleConfirmHold}
                disabled={actionLoading === holdModal.id || productionSave.mutationsBlocked || !holdData.category}
                className="btn-warning disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {actionLoading === holdModal.id ? (
                  <ArrowPathIcon className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <PauseIcon className="h-5 w-5 mr-2" />
                    Place on Hold
                  </>
                )}
              </button>
            </div>
          </>
        )}
      </Modal>

      {/* Operation Details Modal */}
      <Modal
        open={detailsModal !== null}
        onClose={() => setDetailsModal(null)}
        size="2xl"
        padded={false}
        scroll={false}
      >
        {detailsModal && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Operation Details</h3>
              <button onClick={() => setDetailsModal(null)} className="p-2 rounded-lg hover:bg-slate-800">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-6 max-h-[70vh] overflow-y-auto">
              {/* Work Order Info */}
              <div className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
                <div className="flex justify-between items-start">
                  <div>
                    <p className="text-sm text-blue-300 font-medium">Work Order</p>
                    <p className="text-xl font-bold text-white">{detailsModal.work_order.work_order_number}</p>
                    <p className="text-slate-300">{detailsModal.work_order.part?.part_number} - {detailsModal.work_order.part?.name}</p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setDetailsModal(null);
                      navigate(`/work-orders/${detailsModal.work_order.id}`);
                    }}
                  >
                    Open Full WO
                  </Button>
                </div>
              </div>
              
              {/* Operation Info */}
              <div>
                <h4 className="font-semibold text-white mb-3">Current Operation</h4>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-sm text-slate-400">Operation</p>
                    <p className="font-medium">{formatOperationLabel(detailsModal.operation.operation_number)} - {detailsModal.operation.name}</p>
                  </div>
                  <div>
                    <p className="text-sm text-slate-400">Work Center</p>
                    <p className="font-medium">{detailsModal.work_center?.name || '—'}</p>
                  </div>
                  <div>
                    <p className="text-sm text-slate-400">Quantity</p>
                    <p className="font-medium">{detailsModal.operation.quantity_complete} / {detailsModal.operation.quantity_ordered}</p>
                  </div>
                  <div>
                    <p className="text-sm text-slate-400">Status</p>
                    <p className="font-medium capitalize">{detailsModal.operation.status.replace('_', ' ')}</p>
                  </div>
                </div>
              </div>
              
              <KioskJobNotes job={{
                work_order_notes: detailsModal.work_order.notes,
                work_order_special_instructions: detailsModal.work_order.special_instructions,
                operation_description: detailsModal.operation.description,
                operation_setup_instructions: detailsModal.operation.setup_instructions,
                operation_run_instructions: detailsModal.operation.run_instructions,
              }} size="sm" />

              {/* All Operations */}
              <div>
                <h4 className="font-semibold text-white mb-3">All Operations</h4>
                <div className="space-y-2">
                  {detailsModal.all_operations.map((op: any) => {
                    return (
                      <div 
                        key={op.id} 
                        className={`flex items-center justify-between gap-3 p-3 rounded-lg border transition-colors ${
                          op.is_current
                            ? 'border-blue-400/60 bg-blue-500/10 shadow-[inset_3px_0_0_rgba(96,165,250,0.9)]'
                            : 'border-transparent bg-slate-800/50'
                        }`}
                      >
                        <div className="flex min-w-0 items-center gap-3">
                          {/* No fixed width: the cell used to print a bare `10` and
                              was sized `w-12` (48px) for it. It now prints the full
                              `Op 100` -- or a free-text `Nest 12` -- which overruns
                              48px and collides with the operation name beside it on
                              the floor tablet. `whitespace-nowrap` keeps the label
                              on one line; the name next to it already truncates. */}
                          <span
                            className={`text-sm font-medium flex-shrink-0 whitespace-nowrap ${
                              op.is_current ? 'text-blue-200' : 'text-slate-400'
                            }`}
                          >
                            {formatOperationLabel(op.operation_number)}
                          </span>
                          <span className="font-medium text-slate-100 truncate">{op.name}</span>
                          <span className="text-xs text-slate-400 tabular-nums flex-shrink-0">
                            {op.quantity_complete} / {op.quantity_ordered}
                          </span>
                          {op.is_current && <span className="text-xs bg-blue-500/20 text-blue-200 border border-blue-400/30 px-2 py-0.5 rounded-full flex-shrink-0">Current</span>}
                        </div>
                        <span className={`px-2 py-0.5 rounded text-xs font-medium capitalize flex-shrink-0 ${statusColor(op.status)}`}>
                          {op.status.replace('_', ' ')}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
              
              {/* History */}
              {detailsModal.history && detailsModal.history.length > 0 && (
                <div>
                  <h4 className="font-semibold text-white mb-3">Recent History</h4>
                  <div className="space-y-2">
                    {detailsModal.history.map((h: any, i: number) => (
                      <div key={i} className="flex items-start gap-3 text-sm">
                        <span className="text-slate-500 w-32 flex-shrink-0">
                          {h.created_at ? formatCentralDateTime(h.created_at, { year: undefined }) : '—'}
                        </span>
                        <span className="text-slate-300">{h.details}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
            
            <div className="modal-footer">
              <Button variant="secondary" onClick={() => setDetailsModal(null)}>
                Close
              </Button>
            </div>
          </>
        )}
      </Modal>

      <Modal open={correctionReview !== null} onClose={() => setCorrectionReview(null)} size="lg" ariaLabel="Review quantity correction">
        <h2 className="text-lg font-semibold text-white">Review quantity correction</h2>
        <p className="mt-3 text-sm text-slate-300">The connection ended before we could confirm this removal. Review the history with your supervisor to establish whether it was recorded before entering another correction.</p>
        {productionSave.unconfirmedCorrection && <div className="my-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          <p>Remove {productionSave.unconfirmedCorrection.body.quantity_delta} complete</p>
          <p>Reason: {productionSave.unconfirmedCorrection.body.reason}</p>
          <p>Submitted {formatCentralDateTime(productionSave.unconfirmedCorrection.submittedAt)}</p>
        </div>}
        {correctionReview?.loading && <p role="status" className="my-4">Loading correction history…</p>}
        {correctionReview?.error && <ErrorState title="History unavailable" message={correctionReview.error} onRetry={() => void openCorrectionReview()} />}
        {correctionReview?.details && <div className="space-y-4">
          <p className="font-semibold">{correctionReview.details.work_order?.work_order_number} · {correctionReview.details.operation?.name}</p>
          <div className="space-y-3 rounded-lg bg-slate-800/50 p-3">
            <h3 className="font-semibold">Recent operation history</h3>
            {correctionReview.details.history?.length ? correctionReview.details.history.map((entry: any, index: number) => <div key={index} className="text-sm">
              <p className="text-xs text-slate-400">{entry.created_at ? formatCentralDateTime(entry.created_at) : 'Time unavailable'}</p>
              <p className="text-slate-200">{entry.details}</p>
            </div>) : <p className="text-sm text-slate-300">No recent history returned. Your supervisor must verify the outcome before continuing.</p>}
          </div>
          <p className="text-sm text-slate-300">Only recent events are shown. The current total alone does not confirm this correction. Finishing this review clears the held entry; it does not resend or undo the removal.</p>
          <label className="flex min-h-12 cursor-pointer items-start gap-3 text-sm text-slate-200">
            <input type="checkbox" className="mt-1 h-5 w-5 shrink-0" checked={correctionReviewed} onChange={event => setCorrectionReviewed(event.target.checked)} />
            I reviewed this correction with my supervisor and confirmed whether it was recorded.
          </label>
        </div>}
        <div className="mt-5 flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setCorrectionReview(null)}>Keep on hold</Button>
          <Button disabled={!correctionReviewed || !correctionReview?.details || !productionSave.online} onClick={() => void finishCorrectionReview()}>Finish review</Button>
        </div>
      </Modal>

      <style>{`
        @keyframes slide-in {
          from {
            transform: translateX(100%);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }
        .animate-slide-in {
          animation: slide-in 0.3s ease-out;
        }
      `}</style>
    </div>
  );
}
