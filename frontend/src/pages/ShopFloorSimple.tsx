import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate, useLocation } from 'react-router-dom';
import api from '../services/api';
import { usePermissions } from '../hooks/usePermissions';
import { ActiveJob, LaserNestInfo } from '../types';
import { calculateDispatchScore } from '../utils/dispatchScore';
import {
  formatCentralDate,
  formatCentralDateTime,
  formatCentralTime,
  getDateSortValue,
  isDateBeforeTodayInCentral,
  isDateTodayInCentral,
} from '../utils/centralTime';
import {
  PlayIcon,
  CheckCircleIcon,
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
import { getKioskDept, getKioskWorkCenterCode, getKioskWorkCenterId } from '../utils/kiosk';
import { ScanResolveResult } from '../types/scan';

interface Operation {
  id: number;
  work_order_id: number;
  work_order_number: string;
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
}

interface WorkCenter {
  id: number;
  name: string;
  code: string;
}

interface Toast {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

const STATUS_COLORS: Record<string, { bg: string; text: string; dot: string }> = {
  pending: { bg: 'bg-slate-800', text: 'text-slate-300', dot: 'bg-slate-500' },
  ready: { bg: 'bg-blue-500/20', text: 'text-blue-400', dot: 'bg-blue-500/100' },
  in_progress: { bg: 'bg-amber-500/20', text: 'text-amber-400', dot: 'bg-amber-500/100' },
  complete: { bg: 'bg-green-500/20', text: 'text-green-400', dot: 'bg-green-500/100' },
  on_hold: { bg: 'bg-red-500/20', text: 'text-red-400', dot: 'bg-red-500/100' },
};

const WORK_CENTER_STORAGE_KEY = 'shop_floor_work_center_id';

const formatScanActions = (actions: string[]) => actions.map((action) => action.replace(/_/g, ' ')).join(', ');

export default function ShopFloorSimple() {
  const { can } = usePermissions();
  const navigate = useNavigate();
  const location = useLocation();
  const [operations, setOperations] = useState<Operation[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [activeJobs, setActiveJobs] = useState<ActiveJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // Filters
  const [workCenterId, _setWorkCenterId] = useState<number | ''>('');
  const workCenterIdRef = useRef<number | ''>('');
  const setWorkCenterId = useCallback((id: number | '') => {
    _setWorkCenterId(id);
    workCenterIdRef.current = id;
    if (id) {
      localStorage.setItem(WORK_CENTER_STORAGE_KEY, String(id));
    }
  }, []);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [dueTodayOnly, setDueTodayOnly] = useState(false);
  const [actionableOnly, setActionableOnly] = useState(false);
  
  // Modal states
  const [checkOutModal, setCheckOutModal] = useState<{ operation: Operation; job: ActiveJob } | null>(null);
  const [productionModal, setProductionModal] = useState<{ operation: Operation; job: ActiveJob } | null>(null);
  const [detailsModal, setDetailsModal] = useState<any | null>(null);
  const [checkOutData, setCheckOutData] = useState({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
  const [productionData, setProductionData] = useState({ quantity_complete_delta: 1, quantity_scrapped_delta: 0, notes: '' });
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  const [updatingPriorityWorkOrderId, setUpdatingPriorityWorkOrderId] = useState<number | null>(null);
  const [priorityReason, setPriorityReason] = useState('');
  const [showMobileFilters, setShowMobileFilters] = useState(false);
  const [showMobileCenters, setShowMobileCenters] = useState(false);
  const [showScanner, setShowScanner] = useState(false);
  const [scannerCode, setScannerCode] = useState('');
  // A0.4: row to spotlight after an OP:{id} scan (box or ?scan= deep link).
  const [highlightedOperationId, setHighlightedOperationId] = useState<number | null>(null);
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

  // Load data
  const loadOperations = useCallback(async () => {
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
        }));
      }

      setOperations(nextOperations);
    } catch (err) {
      console.error('Failed to load operations:', err);
      showToast('error', 'Failed to load operations');
    }
  }, [workCenterId, statusFilter, debouncedSearch, dueTodayOnly, workCenters]);

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
    } catch (err) {
      console.error('Failed to load dashboard counts:', err);
    }
  }, []);

  const loadActiveJobs = useCallback(async () => {
    try {
      const response = await api.getMyActiveJob();
      setActiveJobs(response.active_jobs || (response.active_job ? [response.active_job] : []));
    } catch (err) {
      console.error('Failed to load active jobs:', err);
    }
  }, []);

  const loadWorkCenters = useCallback(async () => {
    try {
      const response = await api.getWorkCenters();
      setWorkCenters(response || []);
      if (response && response.length > 0 && !workCenterIdRef.current) {
        const deptMatch = kioskParams.dept?.toLowerCase() || null;
        const matched = response.find((wc: WorkCenter) => {
          if (kioskParams.workCenterId && wc.id === kioskParams.workCenterId) return true;
          if (kioskParams.workCenterCode && wc.code.toLowerCase() === kioskParams.workCenterCode.toLowerCase()) return true;
          if (deptMatch) {
            return (
              wc.name.toLowerCase().includes(deptMatch) ||
              wc.code.toLowerCase().includes(deptMatch)
            );
          }
          return false;
        });
        if (matched) {
          setWorkCenterId(matched.id);
          setActionableOnly(true);
        } else {
          // Fall back to localStorage-saved work center
          const storedId = Number(localStorage.getItem(WORK_CENTER_STORAGE_KEY));
          const storedMatch = storedId ? response.find((wc: WorkCenter) => wc.id === storedId) : null;
          if (storedMatch) {
            setWorkCenterId(storedMatch.id);
            setActionableOnly(true);
          }
        }
      }
    } catch (err) {
      console.error('Failed to load work centers:', err);
    }
  }, [kioskParams.dept, kioskParams.workCenterCode, kioskParams.workCenterId, setWorkCenterId]);

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
  }, [workCenterId, statusFilter, debouncedSearch, dueTodayOnly, loadOperations, loading]);

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

  const showToast = useCallback((type: 'success' | 'error' | 'info', message: string) => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4000);
  }, []);

  const isOverdue = (dueDate: string | null) => {
    if (!dueDate) return false;
    return isDateBeforeTodayInCentral(dueDate);
  };

  const isDueToday = (dueDate: string | null) => {
    return Boolean(dueDate && isDateTodayInCentral(dueDate));
  };
  const canEditPriority = can('work_orders:edit');

  const getPriorityClasses = (priority: number) => {
    if (priority <= 2) return 'bg-red-500/20 text-red-400';
    if (priority <= 5) return 'bg-amber-500/20 text-amber-400';
    return 'bg-slate-800 text-slate-400';
  };

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
  const sortedVisibleOperations = useMemo(() => {
    return [...visibleOperations].sort((a, b) => {
      const aScore = calculateDispatchScore({
        priority: a.priority,
        dueDate: a.due_date || null,
        remainingHours: Math.max(0, Number(a.quantity_ordered || 0) - Number(a.quantity_complete || 0)),
        scheduledStart: null,
        status: a.status,
      });
      const bScore = calculateDispatchScore({
        priority: b.priority,
        dueDate: b.due_date || null,
        remainingHours: Math.max(0, Number(b.quantity_ordered || 0) - Number(b.quantity_complete || 0)),
        scheduledStart: null,
        status: b.status,
      });
      if (aScore !== bScore) return bScore - aScore;
      if (a.priority !== b.priority) return a.priority - b.priority;
      const aDue = getDateSortValue(a.due_date);
      const bDue = getDateSortValue(b.due_date);
      if (aDue !== bDue) return aDue - bDue;
      return a.work_order_number.localeCompare(b.work_order_number);
    });
  }, [visibleOperations]);
  const priorityFocusQueue = useMemo(() => {
    const ranked = [...sortedVisibleOperations]
      .filter((op) => ['pending', 'ready', 'in_progress', 'on_hold'].includes(op.status));
    return ranked.slice(0, 5);
  }, [sortedVisibleOperations]);

  const selectedWorkCenter = useMemo(
    () => workCenters.find((wc) => wc.id === workCenterId) || null,
    [workCenters, workCenterId]
  );
  const queuedOperationsAcrossCenters = useMemo(
    () => workCenterBuckets.reduce((total, bucket) => total + bucket.open + bucket.inProgress, 0),
    [workCenterBuckets]
  );

  const primaryActiveJob = useMemo(() => activeJobs[0] || null, [activeJobs]);

  const getActiveJobForOperation = useCallback(
    (operation: Operation) =>
      activeJobs.find((job) => {
        if (job.operation_id && job.operation_id === operation.id) return true;
        return (
          job.work_order_id === operation.work_order_id &&
          String(job.operation_number || '') === String(operation.operation_number || '')
        );
      }) || null,
    [activeJobs]
  );

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

  // Action handlers
  const handleCheckIn = async (operation: Operation) => {
    if (operation.can_check_in === false) {
      showToast('info', 'Previous work-center operations must be completed first');
      return;
    }

    setActionLoading(operation.id);
    try {
      if (operation.status === 'in_progress' || operation.status === 'on_hold') {
        if (!operation.work_center_id) {
          throw new Error('Operation is missing a work center');
        }
        if (operation.status === 'on_hold') {
          await api.resumeOperation(operation.id);
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
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || err.message || 'Failed to check in');
    } finally {
      setActionLoading(null);
    }
  };

  const handleOpenCheckOut = (operation: Operation, job: ActiveJob) => {
    setCheckOutModal({ operation, job });
    setCheckOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
  };

  const handleOpenProductionModal = (operation: Operation, job: ActiveJob) => {
    setProductionModal({ operation, job });
    setProductionData({ quantity_complete_delta: 1, quantity_scrapped_delta: 0, notes: '' });
  };

  const getOperationForActiveJob = (job: ActiveJob): Operation => {
    const matchingOperation = operations.find((op) => getActiveJobForOperation(op)?.time_entry_id === job.time_entry_id);
    if (matchingOperation) return matchingOperation;

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
      quantity_scrapped: 0,
      priority: 5,
      due_date: null,
      customer_name: null,
      customer_po: null,
      actual_start: job.clock_in,
      setup_instructions: null,
      run_instructions: null,
      requires_inspection: false,
      can_check_in: true,
      blocked_by_previous_operations: false,
    };
  };

  const handleOpenActiveJobCheckOut = (job: ActiveJob) => {
    handleOpenCheckOut(getOperationForActiveJob(job), job);
  };

  const closeCheckOutModal = () => {
    setCheckOutModal(null);
    setCheckOutData({ quantity_produced: 0, quantity_scrapped: 0, notes: '' });
  };

  const closeProductionModal = () => {
    setProductionModal(null);
    setProductionData({ quantity_complete_delta: 1, quantity_scrapped_delta: 0, notes: '' });
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

  const reportProduction = async (
    operation: Operation,
    quantityCompleteDelta: number,
    quantityScrappedDelta = 0,
    notes?: string,
    closeModal = false
  ) => {
    setActionLoading(operation.id);
    try {
      await api.reportOperationProduction(operation.id, {
        quantity_complete_delta: quantityCompleteDelta,
        quantity_scrapped_delta: quantityScrappedDelta,
        notes: notes || undefined,
      });
      const label = quantityCompleteDelta > 0
        ? `Added ${quantityCompleteDelta} complete part${quantityCompleteDelta === 1 ? '' : 's'}`
        : `Added ${quantityScrappedDelta} scrap`;
      showToast('success', label);
      if (closeModal) closeProductionModal();
      await Promise.all([loadOperations(), loadActiveJobs(), loadDashboardCounts()]);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to add completed quantity');
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
      true
    );
  };

  const handleCompleteOperation = async (operation: Operation) => {
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
    if (!checkOutModal) return;

    setActionLoading(checkOutModal.operation.id);
    try {
      await api.clockOut(checkOutModal.job.time_entry_id, {
        quantity_produced: Number(checkOutData.quantity_produced || 0),
        quantity_scrapped: Number(checkOutData.quantity_scrapped || 0),
        notes: checkOutData.notes || undefined,
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

      setShowScanner(false);
      setScannerCode('');

      if (resolved?.kind === 'operation') {
        const op = resolved.operation;
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
        setSearch(resolved.work_order.work_order_number);
        showToast('success', `Found ${resolved.work_order.work_order_number}`);
        scrollToOperations();
        return;
      }

      // kind 'employee' / 'unknown' (or resolver error): legacy behavior.
      const result = await api.scannerLookup(code);
      const nextSearch =
        result?.work_order?.work_order_number ||
        result?.work_order_number ||
        result?.part?.part_number ||
        result?.part_number ||
        code;
      setSearch(nextSearch);
      showToast('success', `Found ${nextSearch}`);
      scrollToOperations();
    } catch {
      setSearch(code);
      showToast('info', 'Showing scanned code in search');
    } finally {
      setActionLoading(null);
    }
  }, [openOperationDetails, scrollToOperations, showToast]);

  const handleScannerSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await resolveScan(scannerCode);
  };

  // Phone-scanned traveler op QRs open /shop-floor/operations?scan=OP:{id}
  // (kiosk mode included) — run the resolve flow once, then strip the param
  // via history replace so reloads don't re-scan.
  const scanParamHandledRef = useRef(false);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const scanCode = params.get('scan');
    if (!scanCode) {
      // Param gone (we stripped it, or plain navigation): re-arm so a LATER
      // client-side navigation to ?scan=... is handled. The ref still
      // suppresses strict-mode's double-invoke within one scan handling.
      scanParamHandledRef.current = false;
      return;
    }
    if (loading || scanParamHandledRef.current) return;
    scanParamHandledRef.current = true;
    params.delete('scan');
    navigate({ pathname: location.pathname, search: params.toString() }, { replace: true });
    resolveScan(scanCode);
  }, [loading, location.pathname, location.search, navigate, resolveScan]);

  // Let the operator take in the spotlighted row, then fade it.
  useEffect(() => {
    if (highlightedOperationId === null) return;
    const timer = setTimeout(() => setHighlightedOperationId(null), 8000);
    return () => clearTimeout(timer);
  }, [highlightedOperationId]);

  const handleViewDetails = async (operation: Operation) => {
    await openOperationDetails(operation.id);
  };

  const handleHold = async (operationId: number) => {
    setActionLoading(operationId);
    try {
      await api.holdOperation(operationId);
      showToast('info', 'Operation placed on hold');
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
      {/* Toast Notifications */}
      <div className="fixed top-4 left-4 right-4 z-50 space-y-2 md:left-auto">
        {toasts.map(toast => (
          <div
            key={toast.id}
            className={`px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 animate-slide-in ${
              toast.type === 'success' ? 'bg-green-600 text-white' :
              toast.type === 'error' ? 'bg-red-600 text-white' :
              'bg-blue-600 text-white'
            }`}
          >
            {toast.type === 'success' && <CheckCircleIcon className="h-5 w-5" />}
            {toast.type === 'error' && <ExclamationTriangleIcon className="h-5 w-5" />}
            {toast.type === 'info' && <ClockIcon className="h-5 w-5" />}
            <span className="font-medium">{toast.message}</span>
          </div>
        ))}
      </div>

      {/* Mobile Header */}
      <div className="md:hidden space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-bold text-white flex items-center gap-2">
              <WrenchScrewdriverIcon className="h-6 w-6 text-werco-600" />
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
        {primaryActiveJob && (
          <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-4 shadow-lg shadow-emerald-950/20">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-emerald-300">Checked in</p>
                <p className="mt-1 truncate text-base font-bold text-white">
                  {primaryActiveJob.work_order_number || 'Current job'} - {primaryActiveJob.operation_name || 'Operation'}
                </p>
                <p className="mt-1 text-xs text-emerald-100/80">
                  {primaryActiveJob.work_center_name || selectedWorkCenter?.name || 'Shop floor'} &middot; {getElapsedTime(primaryActiveJob.clock_in)}
                </p>
              </div>
              <button
                type="button"
                onClick={() => handleOpenActiveJobCheckOut(primaryActiveJob)}
                disabled={actionLoading !== null}
                className="btn-success min-h-11 shrink-0 px-4 text-sm"
              >
                Check Out
              </button>
            </div>
          </div>
        )}
        {showScanner && (
          <form onSubmit={handleScannerSubmit} className="card-compact space-y-3">
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Scan Traveler
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={scannerCode}
                onChange={(e) => setScannerCode(e.target.value)}
                className="input h-12 flex-1 text-base"
                placeholder="Scan or enter traveler code"
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
                <label className="text-xs font-medium text-slate-400 block mb-1">
                  Optional Priority Reason
                </label>
                <input
                  type="text"
                  value={priorityReason}
                  onChange={(e) => setPriorityReason(e.target.value)}
                  className="input text-sm"
                  maxLength={500}
                  placeholder="Applied to your next priority change"
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
            <WrenchScrewdriverIcon className="h-8 w-8 text-werco-600" />
            Shop Floor Operations
          </h1>
          <p className="page-subtitle">Check in and out of work order operations</p>
        </div>
        <div className="page-actions">
          <button 
            onClick={handleRefresh}
            disabled={refreshing}
            className="btn-secondary"
          >
            <ArrowPathIcon className={`h-5 w-5 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {primaryActiveJob && (
        <div className="hidden md:flex items-center justify-between gap-4 rounded-2xl border border-emerald-500/40 bg-emerald-500/10 px-5 py-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-emerald-300">You are checked into</p>
            <p className="mt-1 text-lg font-bold text-white">
              {primaryActiveJob.work_order_number || 'Current job'} - {primaryActiveJob.operation_name || 'Operation'}
            </p>
            <p className="mt-1 text-sm text-emerald-100/80">
              {primaryActiveJob.work_center_name || selectedWorkCenter?.name || 'Shop floor'} &middot; {getElapsedTime(primaryActiveJob.clock_in)}
            </p>
          </div>
          <button
            type="button"
            onClick={() => handleOpenActiveJobCheckOut(primaryActiveJob)}
            className="btn-success min-h-11 px-5"
          >
            Check Out
          </button>
        </div>
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
            <label className="text-xs font-medium text-slate-400 block mb-1">
              Optional Priority Reason
            </label>
            <input
              type="text"
              value={priorityReason}
              onChange={(e) => setPriorityReason(e.target.value)}
              className="input text-sm max-w-md"
              maxLength={500}
              placeholder="Applied to your next priority change"
            />
          </div>
        )}
      </div>

      {/* Work Cell Buckets */}
      <div className="space-y-4">
        <div className="md:hidden rounded-2xl border border-slate-700 bg-[#151b28] p-4">
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
              className="md:hidden text-sm font-semibold text-werco-700 hover:text-werco-800"
            >
              {showMobileCenters ? 'Hide Stations' : 'Change Station'}
            </button>
            <button
              type="button"
              onClick={() => focusOperations('')}
              className="hidden md:inline text-sm font-semibold text-werco-700 hover:text-werco-800"
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
        <div className={`${showMobileCenters ? 'grid' : 'hidden'} md:grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4`}>
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
                className={`relative text-left rounded-2xl border px-5 py-4 transition hover:shadow-md ${
                  workCenterId === bucket.id
                    ? 'border-werco-500 bg-werco-500/15 shadow-sm shadow-werco-500/20 ring-1 ring-werco-500/40'
                    : 'border-slate-700 bg-[#0b1118] hover:border-slate-500 hover:bg-slate-900/70'
                }`}
              >
                <div className="flex items-center gap-2 text-xs font-semibold">
                  <span className={`inline-flex items-center gap-2 rounded-full px-2.5 py-1 ${statusPill}`}>
                    <span className="h-2 w-2 rounded-full bg-current" />
                    {statusLabel}
                  </span>
                </div>
                <div className="mt-3 text-base font-semibold text-white">{bucket.name}</div>
                <div className="mt-1 text-xs text-slate-400">Work cell queue</div>
                <div className="mt-4 flex items-center gap-4 text-sm text-slate-400">
                  <span>Active: <span className="font-semibold text-slate-100">{bucket.inProgress}</span></span>
                  <span>Queue: <span className="font-semibold text-slate-100">{bucket.open}</span></span>
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
        <div className="md:hidden rounded-2xl border border-werco-500/30 bg-werco-500/10 p-4">
          {(() => {
            const op = priorityFocusQueue[0];
            const activeJob = getActiveJobForOperation(op);
            const overdue = isOverdue(op.due_date);
            return (
              <div className="space-y-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-werco-300">
                      Next Recommended Job
                    </p>
                    <p className="mt-1 text-lg font-bold text-white">{op.work_order_number}</p>
                    <p className="text-sm text-slate-300">
                      {op.operation_number} - {op.operation_name}
                    </p>
                  </div>
                  <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${getPriorityClasses(op.priority)}`}>
                    P{op.priority}
                  </span>
                </div>
                <div className="flex items-center justify-between text-sm text-slate-400">
                  <span>{op.part_number || 'No part'}</span>
                  <span className={overdue ? 'font-semibold text-red-400' : ''}>
                    {op.due_date ? `Due ${formatCentralDate(op.due_date, { year: undefined })}` : 'No due date'}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => activeJob ? handleOpenCheckOut(op, activeJob) : handleCheckIn(op)}
                  disabled={actionLoading === op.id}
                  className={`min-h-12 w-full ${activeJob ? 'btn-success' : 'btn-primary'}`}
                >
                  {actionLoading === op.id ? (
                    <ArrowPathIcon className="h-5 w-5 animate-spin" />
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

      {/* Priority Focus Queue */}
      {priorityFocusQueue.length > 0 && (
        <div className="card hidden md:block">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h2 className="text-lg font-semibold text-white">Most Important Next</h2>
              <p className="text-sm text-slate-400">Focus list ranked by overdue, priority, and due date</p>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-3">
            {priorityFocusQueue.map((op, idx) => {
              const overdue = isOverdue(op.due_date);
              return (
                <button
                  key={`focus-${op.id}`}
                  type="button"
                  onClick={() => handleViewDetails(op)}
                  className={`rounded-xl border p-3 text-left transition ${
                    overdue ? 'border-red-500/30 bg-red-500/10 hover:bg-red-500/20' : 'border-slate-700 bg-[#151b28] hover:bg-slate-800/50'
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-slate-400">#{idx + 1}</span>
                    <span className={`px-2 py-0.5 rounded text-xs font-semibold ${getPriorityClasses(op.priority)}`}>
                      P{op.priority}
                    </span>
                  </div>
                  <div className="text-sm font-semibold text-werco-700">{op.work_order_number}</div>
                  <div className="text-xs text-slate-400 truncate">{op.operation_number} - {op.operation_name}</div>
                  <div className={`mt-2 text-xs ${overdue ? 'text-red-600 font-medium' : 'text-slate-400'}`}>
                    {op.due_date ? `Due ${formatCentralDate(op.due_date, { year: undefined })}` : 'No due date'}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Operations Grid */}
      {sortedVisibleOperations.length === 0 ? (
        <div className="card text-center py-16">
          <CubeIcon className="h-12 w-12 text-slate-400 mx-auto mb-4" />
          <p className="text-slate-300 font-medium">
            {selectedWorkCenter ? `No operations found for ${selectedWorkCenter.name}` : 'No operations found'}
          </p>
          <p className="text-sm text-slate-400 mt-1">
            {selectedWorkCenter && queuedOperationsAcrossCenters > 0
              ? 'Other work centers have queued work. View all operations or choose a different station.'
              : 'Try adjusting your filters'}
          </p>
          {selectedWorkCenter && (
            <button
              type="button"
              onClick={() => focusOperations('')}
              className="btn-secondary mt-4"
            >
              View All Operations
            </button>
          )}
        </div>
      ) : (
        <div ref={operationsRef} className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4" data-tour="sf-operations">
          {sortedVisibleOperations.map(op => {
            const colors = STATUS_COLORS[op.status] || STATUS_COLORS.pending;
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
                data-testid={`shop-floor-op-${op.id}`}
                className={`card hover:shadow-lg transition-shadow ${overdue ? 'border-red-500/30 bg-red-500/10' : ''} ${
                  highlightedOperationId === op.id ? 'border-werco-500 ring-1 ring-werco-500/60' : ''
                }`}
              >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-werco-primary text-lg">{op.work_order_number}</span>
                      {overdue && (
                        <span className="px-2 py-0.5 bg-red-500/20 text-red-400 text-xs font-semibold rounded">
                          OVERDUE
                        </span>
                      )}
                    </div>
                    <p className="text-sm text-slate-400">{op.part_number}</p>
                  </div>
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold ${colors.bg} ${colors.text}`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${colors.dot}`}></span>
                    {op.status.replace('_', ' ')}
                  </span>
                </div>
                
                {/* Operation Info */}
                <div className="bg-slate-800/50 rounded-lg p-3 mb-3">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-semibold text-white">
                      {op.operation_number} - {op.operation_name}
                    </span>
                  </div>
                  <p className="text-sm text-slate-400">{op.part_name}</p>
                  {op.work_center_name && (
                    <p className="text-xs text-slate-400 mt-1">
                      Work Center: {op.work_center_name}
                    </p>
                  )}
                  {op.laser_nest && (
                    <div className="mt-2 rounded border border-red-500/20 bg-red-500/10 px-2 py-1.5 text-xs text-red-100">
                      <div className="font-semibold text-red-200">{op.laser_nest.nest_name}</div>
                      <div className="mt-1 grid grid-cols-1 gap-1 text-red-100/80">
                        <span>CNC: {op.laser_nest.cnc_file_name}</span>
                        <span>
                          Runs: {op.laser_nest.completed_runs} / {op.laser_nest.planned_runs}
                          {op.laser_nest.remaining_runs > 0 ? ` (${op.laser_nest.remaining_runs} left)` : ''}
                        </span>
                        {(op.laser_nest.material || op.laser_nest.thickness || op.laser_nest.sheet_size) && (
                          <span>
                            {[op.laser_nest.material, op.laser_nest.thickness, op.laser_nest.sheet_size].filter(Boolean).join(' • ')}
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
                
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
                          onClick={() => handleCompleteOperation(op)}
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
                            P{p}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${getPriorityClasses(op.priority)}`}>
                        P{op.priority}
                      </span>
                    )}
                  </div>
                </div>
                
                {/* Action Buttons */}
                <div className="flex flex-col sm:flex-row gap-2" data-tour="sf-complete">
                  {op.status === 'in_progress' && activeJob && !targetReached && (
                    <>
                      <button
                        onClick={() => reportProduction(op, 1)}
                        disabled={actionLoading === op.id || remainingQuantity <= 0}
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
                        More
                      </button>
                    </>
                  )}

                  {/* Check In Button */}
                  {showCheckIn && (
                    <button
                      onClick={() => handleCheckIn(op)}
                      disabled={actionLoading === op.id || !canCheckIn}
                      className="flex-1 btn-primary text-base sm:text-sm py-3 sm:py-2.5 w-full disabled:opacity-50 disabled:cursor-not-allowed"
                      title={canCheckIn ? 'Check in' : 'Waiting on a previous work center'}
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
                      onClick={() => handleHold(op.id)}
                      disabled={actionLoading === op.id}
                      className="btn-secondary text-sm py-2.5 px-3 w-full sm:w-auto"
                      title="Put on Hold"
                    >
                      <PauseIcon className="h-4 w-4" />
                      <span className="ml-1.5">Hold</span>
                    </button>
                  )}
                  
                  {/* Resume Button - visible when on hold */}
                  {op.status === 'on_hold' && (
                    <button
                      onClick={() => handleCheckIn(op)}
                      disabled={actionLoading === op.id}
                      className="flex-1 btn-primary text-base sm:text-sm py-3 sm:py-2.5 w-full"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <PlayIcon className="h-4 w-4 mr-1.5" />
                          <span>Check In</span>
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
              </div>
            );
          })}
        </div>
      )}

      {/* Add Production Modal */}
      {productionModal && createPortal((
        <div className="modal-overlay" onClick={closeProductionModal}>
          <div className="modal max-w-md" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Add Completed Quantity</h3>
              <button onClick={closeProductionModal} className="p-2 rounded-lg hover:bg-slate-800">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="modal-body space-y-4">
              <div className="bg-slate-800/50 rounded-lg p-4">
                <p className="text-sm text-slate-400">Operation</p>
                <p className="font-semibold text-white">
                  {productionModal.operation.operation_number} - {productionModal.operation.operation_name}
                </p>
                <p className="text-sm text-slate-400 mt-1">
                  {productionModal.operation.work_order_number} &middot; {productionModal.operation.part_number}
                </p>
                <p className="mt-2 text-xs text-slate-400">
                  Completed {productionModal.operation.quantity_complete} / {productionModal.operation.quantity_ordered}
                </p>
              </div>

              <div>
                <label className="label">Good parts to add</label>
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={productionData.quantity_complete_delta}
                  onChange={(e) => setProductionData({ ...productionData, quantity_complete_delta: Number(e.target.value) || 0 })}
                  className="input h-14 text-center text-2xl font-bold"
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
                <label className="label">Scrap to add</label>
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={productionData.quantity_scrapped_delta}
                  onChange={(e) => setProductionData({ ...productionData, quantity_scrapped_delta: Number(e.target.value) || 0 })}
                  className="input h-12 text-center text-lg font-semibold"
                />
              </div>

              <div>
                <label className="label">Notes (optional)</label>
                <textarea
                  value={productionData.notes}
                  onChange={(e) => setProductionData({ ...productionData, notes: e.target.value })}
                  className="input"
                  rows={3}
                  placeholder="Production notes, scrap details, or inspection notes..."
                />
              </div>
            </div>

            <div className="modal-footer">
              <button onClick={closeProductionModal} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={handleSaveProduction}
                disabled={
                  actionLoading === productionModal.operation.id ||
                  (Number(productionData.quantity_complete_delta || 0) <= 0 &&
                    Number(productionData.quantity_scrapped_delta || 0) <= 0)
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
            </div>
          </div>
        </div>
      ), document.body)}

      {/* Check Out Modal */}
      {checkOutModal && createPortal((
        <div className="modal-overlay" onClick={closeCheckOutModal}>
          <div className="modal max-w-md" onClick={e => e.stopPropagation()}>
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
                  {checkOutModal.operation.operation_number} - {checkOutModal.operation.operation_name}
                </p>
                <p className="text-sm text-slate-400 mt-1">
                  {checkOutModal.operation.work_order_number} &middot; {checkOutModal.operation.part_number}
                </p>
                <p className="mt-2 text-xs text-slate-400">
                  Started {formatCentralTime(checkOutModal.job.clock_in)} &middot; {getElapsedTime(checkOutModal.job.clock_in)}
                </p>
              </div>
              
              <div>
                <label className="label">Additional good parts at checkout</label>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    inputMode="decimal"
                    min={0}
                    value={checkOutData.quantity_produced}
                    onChange={(e) => setCheckOutData({ ...checkOutData, quantity_produced: Number(e.target.value) || 0 })}
                    className="input h-14 flex-1 text-center text-2xl font-bold"
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
                <label className="label">Scrap</label>
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  value={checkOutData.quantity_scrapped}
                  onChange={(e) => setCheckOutData({ ...checkOutData, quantity_scrapped: Number(e.target.value) || 0 })}
                  className="input h-12 text-center text-lg font-semibold"
                />
              </div>
              
              <div>
                <label className="label">Notes (optional)</label>
                <textarea
                  value={checkOutData.notes}
                  onChange={(e) => setCheckOutData({ ...checkOutData, notes: e.target.value })}
                  className="input"
                  rows={3}
                  placeholder="Issues, observations, or notes from this session..."
                />
              </div>
            </div>
            
            <div className="modal-footer">
              <button onClick={closeCheckOutModal} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={handleClockOut}
                disabled={
                  actionLoading === checkOutModal.operation.id ||
                  checkOutData.quantity_produced < 0 ||
                  checkOutData.quantity_scrapped < 0
                }
                className="btn-success"
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
          </div>
        </div>
      ), document.body)}

      {/* Operation Details Modal */}
      {detailsModal && createPortal((
        <div className="modal-overlay" onClick={() => setDetailsModal(null)}>
          <div className="modal max-w-2xl" onClick={e => e.stopPropagation()}>
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
                  <button
                    onClick={() => {
                      setDetailsModal(null);
                      navigate(`/work-orders/${detailsModal.work_order.id}`);
                    }}
                    className="btn-secondary btn-sm"
                  >
                    Open Full WO
                  </button>
                </div>
              </div>
              
              {/* Operation Info */}
              <div>
                <h4 className="font-semibold text-white mb-3">Current Operation</h4>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-sm text-slate-400">Operation</p>
                    <p className="font-medium">{detailsModal.operation.operation_number} - {detailsModal.operation.name}</p>
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
              
              {/* Instructions */}
              {(detailsModal.operation.setup_instructions || detailsModal.operation.run_instructions) && (
                <div>
                  <h4 className="font-semibold text-white mb-3">Work Instructions</h4>
                  {detailsModal.operation.setup_instructions && (
                    <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 mb-2">
                      <p className="text-sm font-medium text-amber-300">Setup Instructions</p>
                      <p className="text-sm text-amber-400 whitespace-pre-wrap">{detailsModal.operation.setup_instructions}</p>
                    </div>
                  )}
                  {detailsModal.operation.run_instructions && (
                    <div className="bg-blue-500/10 border border-blue-500/30 rounded-lg p-3">
                      <p className="text-sm font-medium text-blue-300">Run Instructions</p>
                      <p className="text-sm text-blue-400 whitespace-pre-wrap">{detailsModal.operation.run_instructions}</p>
                    </div>
                  )}
                </div>
              )}
              
              {/* All Operations */}
              <div>
                <h4 className="font-semibold text-white mb-3">All Operations</h4>
                <div className="space-y-2">
                  {detailsModal.all_operations.map((op: any) => {
                    const opColors = STATUS_COLORS[op.status] || STATUS_COLORS.pending;
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
                          <span className={`text-sm font-medium w-12 flex-shrink-0 ${op.is_current ? 'text-blue-200' : 'text-slate-400'}`}>
                            {op.operation_number}
                          </span>
                          <span className="font-medium text-slate-100 truncate">{op.name}</span>
                          <span className="text-xs text-slate-400 tabular-nums flex-shrink-0">
                            {op.quantity_complete} / {op.quantity_ordered}
                          </span>
                          {op.is_current && <span className="text-xs bg-blue-500/20 text-blue-200 border border-blue-400/30 px-2 py-0.5 rounded-full flex-shrink-0">Current</span>}
                        </div>
                        <span className={`px-2 py-0.5 rounded text-xs font-medium flex-shrink-0 ${opColors.bg} ${opColors.text}`}>
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
              <button onClick={() => setDetailsModal(null)} className="btn-secondary">
                Close
              </button>
            </div>
          </div>
        </div>
      ), document.body)}

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
