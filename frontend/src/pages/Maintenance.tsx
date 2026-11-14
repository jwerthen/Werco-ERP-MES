import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { Modal } from '../components/ui/Modal';
import {
  EmptyState,
  ErrorState,
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
  Button,
  FormField,
  LoadingButton,
  ConfirmDialog,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { formatCentralDate, getCentralTodayISODate, isDateBeforeTodayInCentral } from '../utils/centralTime';
import { toDisplayString } from '../utils/apiError';
import {
  MAINTENANCE_FREQUENCY_LABELS,
  MAINTENANCE_PRIORITY_LABELS,
  MAINTENANCE_TYPE_LABELS,
  CANCELLABLE_STATUSES,
  STARTABLE_STATUSES,
  canPerformMaintenance,
  canPlanMaintenance,
  checklistPayload,
  frequencyText,
  targetText,
} from '../utils/maintenance';
import type {
  MaintenanceAsset,
  MaintenanceDashboard,
  MaintenanceSchedule,
  MaintenanceWorkOrder,
} from '../types/maintenance';
import {
  BLANK_TARGET,
  MaintenanceTarget,
  MaintenanceTargetField,
  hasTarget,
  targetPayload,
} from '../components/maintenance/MaintenanceTargetField';
import { MaintenanceAssetsPanel } from '../components/maintenance/MaintenanceAssetsPanel';
import { PmScheduleImportWizard } from '../components/maintenance/PmScheduleImportWizard';
import {
  ArrowUpTrayIcon,
  PlusIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  CalendarDaysIcon,
  PlayIcon,
  PauseIcon,
  NoSymbolIcon,
} from '@heroicons/react/24/outline';

type Tab = 'dashboard' | 'schedules' | 'work_orders' | 'assets';

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'schedules', label: 'Schedules' },
  { id: 'work_orders', label: 'Work Orders' },
  { id: 'assets', label: 'Assets' },
];
const isTab = (value: string | null): value is Tab => TABS.some(tab => tab.id === value);

// Priority is a page-specific scale (not a shared status), so it keeps its own
// colorMap. Status pills route through the central status-color source.
const priorityBadgeColors: Record<string, string> = {
  low: 'bg-slate-800/50 text-slate-300',
  medium: 'bg-blue-500/20 text-blue-400',
  high: 'bg-orange-500/20 text-orange-300',
  critical: 'bg-red-500/20 text-red-400',
};

const WO_STATUS_FILTERS: Array<[string, string]> = [
  ['scheduled', 'Scheduled'],
  ['overdue', 'Overdue'],
  ['in_progress', 'In Progress'],
  ['on_hold', 'On Hold'],
  ['completed', 'Completed'],
  ['cancelled', 'Cancelled'],
];
const OPEN_STATUSES = new Set(['scheduled', 'overdue', 'in_progress', 'on_hold']);

const BLANK_SCHEDULE = {
  name: '',
  maintenance_type: 'preventive',
  frequency: 'monthly',
  frequency_days: '',
  priority: 'medium',
  next_due_date: '',
  estimated_duration_hours: '1',
  requires_shutdown: false,
  description: '',
  checklist: '',
};
const BLANK_WO = {
  title: '',
  maintenance_type: 'preventive',
  priority: 'medium',
  scheduled_date: '',
  due_date: '',
  requires_shutdown: false,
  description: '',
};
const BLANK_COMPLETE = {
  notes: '',
  findings: '',
  parts_used: '',
  labor_cost: '',
  parts_cost: '',
  downtime_minutes: '',
  actual_duration_hours: '',
};

/** `iso` (YYYY-MM-DD) plus `days`, as YYYY-MM-DD. Noon UTC keeps it off any DST edge. */
const addDaysISO = (iso: string, days: number) => {
  const date = new Date(`${iso}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
};

const dueClass = (value?: string | null) =>
  value && isDateBeforeTodayInCentral(value) ? 'text-red-400 font-medium' : '';

const TargetCell = ({ row }: { row: MaintenanceSchedule | MaintenanceWorkOrder }) => (
  <div>
    <div>{targetText(row)}</div>
    {row.target_type === 'asset' && <div className="text-[10px] uppercase tracking-wide text-slate-500">Asset</div>}
  </div>
);

const errorText = (err: any, fallback: string) => toDisplayString(err?.response?.data?.detail) || fallback;

export default function Maintenance() {
  const { showToast } = useToast();
  const { user } = useAuth();
  const canPlan = canPlanMaintenance(user);
  const canPerform = canPerformMaintenance(user);
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTab = searchParams.get('tab');
  const [activeTab, setActiveTabState] = useState<Tab>(isTab(urlTab) ? urlTab : 'dashboard');
  // The first load picks Schedules when nothing is upcoming — once, and never over a tab the
  // URL or the user already chose.
  const initialTabChosen = useRef(isTab(urlTab));
  const setActiveTab = (tab: Tab) => {
    initialTabChosen.current = true;
    setActiveTabState(tab);
    const next = new URLSearchParams(searchParams);
    next.set('tab', tab);
    setSearchParams(next, { replace: true });
  };

  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [dashboard, setDashboard] = useState<MaintenanceDashboard | null>(null);
  const [schedules, setSchedules] = useState<MaintenanceSchedule[]>([]);
  const [workOrders, setWorkOrders] = useState<MaintenanceWorkOrder[]>([]);
  const [assets, setAssets] = useState<MaintenanceAsset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [statusFilter, setStatusFilter] = useState('');
  const [showCreateScheduleModal, setShowCreateScheduleModal] = useState(false);
  const [showCreateWOModal, setShowCreateWOModal] = useState(false);
  const [showCompleteModal, setShowCompleteModal] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [selectedWO, setSelectedWO] = useState<MaintenanceWorkOrder | null>(null);
  const [cancelTarget, setCancelTarget] = useState<MaintenanceWorkOrder | null>(null);

  const [scheduleForm, setScheduleForm] = useState(BLANK_SCHEDULE);
  const [scheduleTarget, setScheduleTarget] = useState<MaintenanceTarget>(BLANK_TARGET);
  const [woForm, setWoForm] = useState(BLANK_WO);
  const [woTarget, setWoTarget] = useState<MaintenanceTarget>(BLANK_TARGET);
  const [completeForm, setCompleteForm] = useState(BLANK_COMPLETE);

  const formDirty =
    (showCreateScheduleModal &&
      (JSON.stringify(scheduleForm) !== JSON.stringify(BLANK_SCHEDULE) || hasTarget(scheduleTarget))) ||
    (showCreateWOModal && (JSON.stringify(woForm) !== JSON.stringify(BLANK_WO) || hasTarget(woTarget))) ||
    (showCompleteModal && JSON.stringify(completeForm) !== JSON.stringify(BLANK_COMPLETE));
  const { confirmDiscard, markSaved } = useUnsavedChanges(formDirty);

  // Equipment assets feed the target pickers only, so a failure here (an older API during
  // a rolling deploy) must never take the page down — the pickers just offer none.
  const loadAssets = useCallback(() => {
    Promise.resolve()
      .then(() => api.getMaintenanceAssets())
      .then(rows => setAssets(Array.isArray(rows) ? rows : []))
      .catch(() => setAssets([]));
  }, []);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError('');
      const [dashData, schedData, woData] = await Promise.all([
        api.getMaintenanceDashboard(),
        api.getMaintenanceSchedules({}),
        api.getMaintenanceWorkOrders({}),
      ]);
      const nextSchedules: MaintenanceSchedule[] = Array.isArray(schedData) ? schedData : [];
      const nextWOs: MaintenanceWorkOrder[] = Array.isArray(woData) ? woData : [];
      setDashboard(dashData ?? null);
      setSchedules(nextSchedules);
      setWorkOrders(nextWOs);
      if (!initialTabChosen.current) {
        initialTabChosen.current = true;
        const nothingUpcoming =
          !nextSchedules.some(schedule => schedule.is_active) && !nextWOs.some(wo => OPEN_STATUSES.has(wo.status));
        if (nothingUpcoming) setActiveTabState('schedules');
      }
    } catch (err: any) {
      setError(errorText(err, 'Failed to load data'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    loadAssets();
  }, [loadData, loadAssets]);

  const upcoming = useMemo(() => {
    const horizon = addDaysISO(getCentralTodayISODate(), 30);
    return schedules
      .filter(schedule => schedule.is_active && schedule.next_due_date && schedule.next_due_date <= horizon)
      .sort((a, b) => String(a.next_due_date).localeCompare(String(b.next_due_date)))
      .slice(0, 9);
  }, [schedules]);

  const filteredWOs = useMemo(() => {
    return workOrders.filter(wo => {
      if (statusFilter && wo.status !== statusFilter) return false;
      if (debouncedSearch) {
        const needle = debouncedSearch.toLowerCase();
        return [wo.title, wo.wo_number, wo.target_code, wo.target_name]
          .filter(Boolean)
          .some(value => String(value).toLowerCase().includes(needle));
      }
      return true;
    });
  }, [workOrders, statusFilter, debouncedSearch]);

  const openScheduleModal = () => {
    setActionError('');
    setShowCreateScheduleModal(true);
  };
  const openWOModal = () => {
    setActionError('');
    setShowCreateWOModal(true);
  };
  const closeScheduleModal = () => {
    if (actionBusy || !confirmDiscard()) return;
    setShowCreateScheduleModal(false);
    setScheduleForm(BLANK_SCHEDULE);
    setScheduleTarget(BLANK_TARGET);
  };
  const closeWOModal = () => {
    if (actionBusy || !confirmDiscard()) return;
    setShowCreateWOModal(false);
    setWoForm(BLANK_WO);
    setWoTarget(BLANK_TARGET);
  };
  const closeCompleteModal = () => {
    if (actionBusy || !confirmDiscard()) return;
    setShowCompleteModal(false);
    setCompleteForm(BLANK_COMPLETE);
  };

  const isCustom = scheduleForm.frequency === 'custom';
  const scheduleReady =
    !!scheduleForm.name.trim() &&
    hasTarget(scheduleTarget) &&
    (!isCustom || Number.parseInt(scheduleForm.frequency_days, 10) > 0);

  const handleCreateSchedule = async () => {
    if (actionBusy || !scheduleReady) return;
    setActionBusy(true);
    setActionError('');
    try {
      const hours = Number.parseFloat(scheduleForm.estimated_duration_hours);
      await api.createMaintenanceSchedule({
        ...targetPayload(scheduleTarget),
        name: scheduleForm.name.trim(),
        maintenance_type: scheduleForm.maintenance_type,
        frequency: scheduleForm.frequency,
        // The server reads frequency_days only for a custom cadence (and requires it there).
        frequency_days: isCustom ? Number.parseInt(scheduleForm.frequency_days, 10) : undefined,
        priority: scheduleForm.priority,
        next_due_date: scheduleForm.next_due_date || undefined,
        estimated_duration_hours: Number.isFinite(hours) && hours >= 0 ? hours : 1,
        requires_shutdown: scheduleForm.requires_shutdown,
        description: scheduleForm.description.trim() || undefined,
        // Stored as the JSON array of strings the model documents; typed one item per line.
        checklist: checklistPayload(scheduleForm.checklist) ?? undefined,
      });
      markSaved();
      setShowCreateScheduleModal(false);
      setScheduleForm(BLANK_SCHEDULE);
      setScheduleTarget(BLANK_TARGET);
      showToast('success', 'Maintenance schedule created');
      void loadData();
    } catch (err: any) {
      const message = errorText(err, 'Unable to save. Check your entries and try again.');
      setActionError(message);
      showToast('error', message);
    } finally {
      setActionBusy(false);
    }
  };

  const woReady = !!woForm.title.trim() && hasTarget(woTarget);

  const handleCreateWO = async () => {
    if (actionBusy || !woReady) return;
    setActionBusy(true);
    setActionError('');
    try {
      await api.createMaintenanceWorkOrder({
        ...targetPayload(woTarget),
        title: woForm.title.trim(),
        maintenance_type: woForm.maintenance_type,
        priority: woForm.priority,
        scheduled_date: woForm.scheduled_date || undefined,
        due_date: woForm.due_date || undefined,
        requires_shutdown: woForm.requires_shutdown,
        description: woForm.description.trim() || undefined,
      });
      markSaved();
      setShowCreateWOModal(false);
      setWoForm(BLANK_WO);
      setWoTarget(BLANK_TARGET);
      showToast('success', 'Maintenance work order created');
      void loadData();
    } catch (err: any) {
      const message = errorText(err, 'Unable to save. Check your entries and try again.');
      setActionError(message);
      showToast('error', message);
    } finally {
      setActionBusy(false);
    }
  };

  const handleStart = useCallback(
    async (wo: MaintenanceWorkOrder) => {
      if (actionBusy) return;
      setActionBusy(true);
      try {
        await api.startMaintenanceWorkOrder(wo.id);
        showToast(
          'success',
          wo.requires_shutdown && wo.target_type === 'work_center'
            ? `${wo.wo_number} started — downtime is recorded on ${wo.target_code ?? 'the machine'} until it is completed`
            : `${wo.wo_number} started`
        );
        void loadData();
      } catch (err: any) {
        showToast('error', errorText(err, 'Failed to start'));
      } finally {
        setActionBusy(false);
      }
    },
    [actionBusy, loadData, showToast]
  );

  // Hold / cancel go through the work-order PUT (planning roles). Server-gated, so not
  // optimistic: the row changes only when the server says it did.
  const handleSetStatus = useCallback(
    async (wo: MaintenanceWorkOrder, status: 'on_hold' | 'cancelled') => {
      if (actionBusy) return false;
      setActionBusy(true);
      try {
        await api.updateMaintenanceWorkOrder(wo.id, { status });
        showToast('success', `${wo.wo_number} ${status === 'on_hold' ? 'put on hold' : 'cancelled'}`);
        void loadData();
        return true;
      } catch (err: any) {
        showToast('error', errorText(err, status === 'on_hold' ? 'Failed to put on hold' : 'Failed to cancel'));
        return false;
      } finally {
        setActionBusy(false);
      }
    },
    [actionBusy, loadData, showToast]
  );

  const confirmCancel = async () => {
    if (!cancelTarget) return;
    if (await handleSetStatus(cancelTarget, 'cancelled')) setCancelTarget(null);
  };

  const handleComplete = async () => {
    if (!selectedWO || actionBusy) return;
    setActionBusy(true);
    setActionError('');
    const number = (value: string) => {
      const parsed = Number.parseFloat(value);
      return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
    };
    try {
      await api.completeMaintenanceWorkOrder(selectedWO.id, {
        notes: completeForm.notes.trim() || undefined,
        findings: completeForm.findings.trim() || undefined,
        parts_used: completeForm.parts_used.trim() || undefined,
        labor_cost: number(completeForm.labor_cost),
        parts_cost: number(completeForm.parts_cost),
        // Left blank (0), the server fills it from the downtime this work order recorded.
        downtime_minutes: number(completeForm.downtime_minutes),
        actual_duration_hours: number(completeForm.actual_duration_hours),
      });
      markSaved();
      setShowCompleteModal(false);
      setCompleteForm(BLANK_COMPLETE);
      showToast('success', `${selectedWO.wo_number} completed`);
      void loadData();
    } catch (err: any) {
      const message = errorText(err, 'Unable to save. Check your entries and try again.');
      setActionError(message);
      showToast('error', message);
    } finally {
      setActionBusy(false);
    }
  };

  const woRowActions = useCallback(
    (wo: MaintenanceWorkOrder) =>
      canPerform ? (
        <div className="flex gap-1">
          {STARTABLE_STATUSES.has(wo.status) && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                void handleStart(wo);
              }}
              disabled={actionBusy}
              className="text-xs px-2 py-1 bg-blue-600 text-white rounded-sm hover:bg-blue-700 disabled:opacity-60"
              title="Start"
              aria-label={`Start work order ${wo.wo_number}`}
            >
              <PlayIcon className="w-4 h-4" aria-hidden="true" />
            </button>
          )}
          {wo.status === 'in_progress' && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                setSelectedWO(wo);
                setActionError('');
                setShowCompleteModal(true);
              }}
              className="text-xs px-2 py-1 bg-green-600 text-white rounded-sm hover:bg-green-700"
              title="Complete"
              aria-label={`Complete work order ${wo.wo_number}`}
            >
              <CheckCircleIcon className="w-4 h-4" aria-hidden="true" />
            </button>
          )}
          {canPlan && wo.status === 'in_progress' && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                void handleSetStatus(wo, 'on_hold');
              }}
              disabled={actionBusy}
              className="text-xs px-2 py-1 bg-amber-600 text-white rounded-sm hover:bg-amber-700 disabled:opacity-60"
              title="Put on hold"
              aria-label={`Put work order ${wo.wo_number} on hold`}
            >
              <PauseIcon className="w-4 h-4" aria-hidden="true" />
            </button>
          )}
          {canPlan && CANCELLABLE_STATUSES.has(wo.status) && (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                setCancelTarget(wo);
              }}
              disabled={actionBusy}
              className="text-xs px-2 py-1 bg-slate-600 text-white rounded-sm hover:bg-red-700 disabled:opacity-60"
              title="Cancel work order"
              aria-label={`Cancel work order ${wo.wo_number}`}
            >
              <NoSymbolIcon className="w-4 h-4" aria-hidden="true" />
            </button>
          )}
        </div>
      ) : null,
    [actionBusy, canPerform, canPlan, handleSetStatus, handleStart]
  );

  // ---- Work Orders tab columns ----
  const woColumns = useMemo<Array<DataTableColumn<MaintenanceWorkOrder>>>(
    () => [
      {
        key: 'wo_number',
        header: 'WO #',
        sortable: true,
        accessor: wo => wo.wo_number,
        render: wo => <span className="font-mono text-xs">{wo.wo_number}</span>,
      },
      {
        key: 'title',
        header: 'Title',
        sortable: true,
        className: 'font-medium',
        accessor: wo => wo.title,
        render: wo => (
          <div>
            <div>{wo.title}</div>
            {wo.requires_shutdown && (
              <div className="text-[10px] uppercase tracking-wide text-amber-300">Requires shutdown</div>
            )}
          </div>
        ),
      },
      {
        key: 'target',
        header: 'Target',
        sortable: true,
        accessor: wo => targetText(wo),
        render: wo => <TargetCell row={wo} />,
      },
      {
        key: 'type',
        header: 'Type',
        sortable: true,
        className: 'capitalize',
        accessor: wo => wo.maintenance_type,
      },
      {
        key: 'priority',
        header: 'Priority',
        sortable: true,
        accessor: wo => wo.priority,
        render: wo => <StatusBadge status={wo.priority} colorMap={priorityBadgeColors} />,
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: wo => wo.status,
        render: wo => <StatusBadge status={wo.status} />,
      },
      {
        key: 'scheduled',
        header: 'Scheduled',
        sortable: true,
        accessor: wo => wo.scheduled_date || '',
        csv: wo => (wo.scheduled_date ? formatCentralDate(wo.scheduled_date) : ''),
        render: wo => (wo.scheduled_date ? formatCentralDate(wo.scheduled_date) : '-'),
      },
      {
        key: 'due',
        header: 'Due',
        sortable: true,
        accessor: wo => wo.due_date || '',
        csv: wo => (wo.due_date ? formatCentralDate(wo.due_date) : ''),
        render: wo =>
          wo.due_date ? (
            <span className={OPEN_STATUSES.has(wo.status) ? dueClass(wo.due_date) : ''}>
              {formatCentralDate(wo.due_date)}
            </span>
          ) : (
            '-'
          ),
      },
      ...(canPerform ? [{ key: 'actions', header: 'Actions', render: woRowActions }] : []),
    ],
    [canPerform, woRowActions]
  );

  const renderWOCard = useCallback(
    (wo: MaintenanceWorkOrder) => (
      <MobileDataCard
        title={wo.title}
        subtitle={`${wo.wo_number} · ${targetText(wo)}`}
        badge={<StatusBadge status={wo.status} />}
        fields={[
          { label: 'Type', value: <span className="capitalize">{wo.maintenance_type}</span> },
          { label: 'Priority', value: <StatusBadge status={wo.priority} colorMap={priorityBadgeColors} /> },
          { label: 'Scheduled', value: wo.scheduled_date ? formatCentralDate(wo.scheduled_date) : '-' },
          { label: 'Due', value: wo.due_date ? formatCentralDate(wo.due_date) : '-' },
        ]}
        actions={
          canPerform && (STARTABLE_STATUSES.has(wo.status) || wo.status === 'in_progress')
            ? (woRowActions(wo) ?? undefined)
            : undefined
        }
      />
    ),
    [canPerform, woRowActions]
  );

  // ---- Schedules tab columns ----
  const scheduleColumns = useMemo<Array<DataTableColumn<MaintenanceSchedule>>>(
    () => [
      {
        key: 'name',
        header: 'Schedule',
        sortable: true,
        className: 'font-medium',
        accessor: s => s.name,
        render: s => (
          <div>
            <div>{s.name}</div>
            {s.requires_shutdown && (
              <div className="text-[10px] uppercase tracking-wide text-amber-300">Requires shutdown</div>
            )}
          </div>
        ),
      },
      {
        key: 'target',
        header: 'Target',
        sortable: true,
        accessor: s => targetText(s),
        render: s => <TargetCell row={s} />,
      },
      {
        key: 'type',
        header: 'Type',
        sortable: true,
        className: 'capitalize',
        accessor: s => s.maintenance_type,
      },
      {
        key: 'frequency',
        header: 'Frequency',
        sortable: true,
        accessor: s => frequencyText(s.frequency, s.frequency_days),
      },
      {
        key: 'checklist',
        header: 'Checklist',
        accessor: s => (s.checklist_items ?? []).join('; '),
        render: s => {
          const items = s.checklist_items ?? [];
          if (items.length === 0) return <span className="text-slate-500">—</span>;
          return (
            <span className="block max-w-xs truncate text-xs text-slate-300" title={items.join('\n')}>
              {items.length} item{items.length === 1 ? '' : 's'}: {items.join('; ')}
            </span>
          );
        },
      },
      {
        key: 'duration',
        header: 'Est. Duration',
        sortable: true,
        align: 'right',
        accessor: s => s.estimated_duration_hours,
        csv: s => `${s.estimated_duration_hours}h`,
        render: s => `${s.estimated_duration_hours}h`,
      },
      {
        key: 'last_completed',
        header: 'Last Completed',
        sortable: true,
        accessor: s => s.last_completed_date || '',
        csv: s => (s.last_completed_date ? formatCentralDate(s.last_completed_date) : ''),
        render: s => (s.last_completed_date ? formatCentralDate(s.last_completed_date) : '-'),
      },
      {
        key: 'next_due',
        header: 'Next Due',
        sortable: true,
        accessor: s => s.next_due_date || '',
        csv: s => (s.next_due_date ? formatCentralDate(s.next_due_date) : ''),
        render: s =>
          s.next_due_date ? (
            <span className={dueClass(s.next_due_date)}>{formatCentralDate(s.next_due_date)}</span>
          ) : (
            '-'
          ),
      },
      {
        key: 'active',
        header: 'Active',
        sortable: true,
        accessor: s => (s.is_active ? 1 : 0),
        csv: s => (s.is_active ? 'Yes' : 'No'),
        render: s =>
          s.is_active ? (
            <CheckCircleIcon className="w-5 h-5 text-green-500" aria-label="Active" />
          ) : (
            <XMarkIcon className="w-5 h-5 text-slate-400" aria-label="Inactive" />
          ),
      },
    ],
    []
  );

  const renderScheduleCard = useCallback(
    (s: MaintenanceSchedule) => (
      <MobileDataCard
        title={s.name}
        subtitle={targetText(s)}
        badge={
          <StatusBadge
            status={s.is_active ? 'active' : 'inactive'}
            colorMap={{ active: 'bg-green-500/20 text-green-300', inactive: 'bg-slate-800/50 text-slate-400' }}
          />
        }
        fields={[
          { label: 'Type', value: <span className="capitalize">{s.maintenance_type}</span> },
          { label: 'Frequency', value: frequencyText(s.frequency, s.frequency_days) },
          { label: 'Est. Duration', value: `${s.estimated_duration_hours}h` },
          {
            label: 'Next Due',
            value: s.next_due_date ? (
              <span className={dueClass(s.next_due_date)}>{formatCentralDate(s.next_due_date)}</span>
            ) : (
              '-'
            ),
          },
          ...((s.checklist_items ?? []).length
            ? [{ label: 'Checklist', value: (s.checklist_items ?? []).join('; '), fullWidth: true }]
            : []),
        ]}
      />
    ),
    []
  );

  if (loading && !dashboard) {
    return (
      <div className="p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/4" />
          <div className="grid grid-cols-4 gap-4">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-24 bg-gray-200 rounded" />
            ))}
          </div>
          <div className="h-64 bg-gray-200 rounded" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <ErrorState message={error} onRetry={loadData} />
      </div>
    );
  }

  const planButtons = (
    <div className="flex flex-wrap justify-center gap-2">
      <Button variant="secondary" onClick={openScheduleModal}>
        New Schedule
      </Button>
      <Button onClick={openWOModal}>New Work Order</Button>
    </div>
  );

  const actionErrorBanner = actionError ? (
    <p role="alert" className="text-red-300 p-3">
      {actionError}
    </p>
  ) : null;

  return (
    <div className="p-6 space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Preventive Maintenance</h1>
        {canPlan && (
          <div className="flex gap-2">
            <Button variant="secondary" onClick={openScheduleModal} className="inline-flex items-center">
              <CalendarDaysIcon className="w-4 h-4 mr-1" />
              New Schedule
            </Button>
            <Button onClick={openWOModal} className="inline-flex items-center">
              <PlusIcon className="w-5 h-5 mr-2" />
              New Work Order
            </Button>
          </div>
        )}
      </div>

      {/* KPI strip — compact instrument-panel tiles (keys of GET /maintenance/dashboard) */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={CalendarDaysIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-blue-400"
          label="Due This Week"
          value={dashboard?.due_this_week ?? 0}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={dashboard?.overdue_count ? 'bg-red-500/20' : 'bg-fd-green/15'}
          iconColor={dashboard?.overdue_count ? 'text-red-400' : 'text-fd-green'}
          label="Overdue"
          value={dashboard?.overdue_count ?? 0}
          valueColor={dashboard?.overdue_count ? 'text-red-400' : undefined}
        />
        <MiniStat
          icon={PlayIcon}
          iconBg="bg-orange-500/20"
          iconColor="text-orange-400"
          label="In Progress"
          value={dashboard?.in_progress ?? 0}
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Completed This Month"
          value={dashboard?.completed_this_month ?? 0}
          valueColor="text-green-400"
        />
      </MiniStatStrip>

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="flex -mb-px space-x-6" aria-label="Maintenance sections">
          {TABS.map(tab => (
            <button
              key={tab.id}
              type="button"
              aria-current={activeTab === tab.id ? 'page' : undefined}
              onClick={() => setActiveTab(tab.id)}
              className={`py-3 px-1 border-b-2 text-sm font-medium ${activeTab === tab.id ? 'border-blue-500 text-blue-400' : 'border-transparent text-slate-400 hover:text-slate-300'}`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Dashboard Tab */}
      {activeTab === 'dashboard' && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">Upcoming Maintenance</h3>
          {upcoming.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {upcoming.map(schedule => (
                <div
                  key={schedule.id}
                  className="bg-fd-panel rounded-sm border border-slate-700 border-l-4 border-l-blue-400 p-3"
                >
                  <div className="font-medium">{schedule.name}</div>
                  <div className="text-sm text-slate-400 mt-1">{targetText(schedule)}</div>
                  <div className="text-sm text-slate-400 mt-1">
                    Due:{' '}
                    <span className={dueClass(schedule.next_due_date)}>
                      {schedule.next_due_date ? formatCentralDate(schedule.next_due_date) : '-'}
                    </span>
                  </div>
                  <div className="mt-2 flex gap-2 text-xs">
                    <span className="px-2 py-0.5 rounded-sm bg-blue-500/20 text-blue-300 capitalize">
                      {schedule.maintenance_type}
                    </span>
                    <span className="px-2 py-0.5 rounded-sm bg-slate-800/50 text-slate-300">
                      {frequencyText(schedule.frequency, schedule.frequency_days)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon={CalendarDaysIcon}
              title="No upcoming maintenance"
              description="Active schedules due in the next 30 days will appear here."
              action={canPlan ? planButtons : undefined}
            />
          )}

          <h3 className="text-lg font-semibold mt-6">Recent Work Orders</h3>
          <div className="bg-fd-panel rounded-sm border border-slate-700 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
                <tr>
                  <th className="px-4 py-3">WO #</th>
                  <th className="px-4 py-3">Title</th>
                  <th className="px-4 py-3">Target</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Priority</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Date</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700">
                {workOrders.slice(0, 10).map(wo => (
                  <tr key={wo.id} className="hover:bg-slate-800">
                    <td className="px-4 py-3 font-mono text-xs">{wo.wo_number}</td>
                    <td className="px-4 py-3 font-medium">{wo.title}</td>
                    <td className="px-4 py-3">{targetText(wo)}</td>
                    <td className="px-4 py-3 capitalize">{wo.maintenance_type}</td>
                    <td className="px-4 py-3">
                      <StatusBadge status={wo.priority} colorMap={priorityBadgeColors} />
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={wo.status} />
                    </td>
                    <td className="px-4 py-3">
                      {wo.scheduled_date ? formatCentralDate(wo.scheduled_date) : formatCentralDate(wo.created_at)}
                    </td>
                  </tr>
                ))}
                {workOrders.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-4 py-6 text-center text-slate-500">
                      No maintenance work orders yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {(dashboard?.work_center_metrics?.length ?? 0) > 0 && (
            <>
              <h3 className="text-lg font-semibold mt-6">Work-Center Reliability (last 90 days)</h3>
              <p className="text-xs text-slate-500">Production work centers only; equipment assets are not counted.</p>
              <div className="bg-fd-panel rounded-sm border border-slate-700 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
                    <tr>
                      <th className="px-4 py-3">Work Center</th>
                      <th className="px-4 py-3 text-right">Completed WOs</th>
                      <th className="px-4 py-3 text-right">MTBF (h)</th>
                      <th className="px-4 py-3 text-right">MTTR (h)</th>
                      <th className="px-4 py-3 text-right">Downtime (min)</th>
                      <th className="px-4 py-3 text-right">Cost</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700">
                    {dashboard?.work_center_metrics.map(metric => (
                      <tr key={metric.work_center_id}>
                        <td className="px-4 py-2">
                          <span className="font-mono text-xs text-werco-navy-300">{metric.work_center_code}</span>{' '}
                          {metric.work_center_name}
                        </td>
                        <td className="px-4 py-2 text-right">{metric.wo_count}</td>
                        <td className="px-4 py-2 text-right">{metric.mtbf_hours}</td>
                        <td className="px-4 py-2 text-right">{metric.mttr_hours}</td>
                        <td className="px-4 py-2 text-right">{metric.total_downtime_minutes}</td>
                        <td className="px-4 py-2 text-right">${metric.total_cost.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {/* Schedules Tab */}
      {activeTab === 'schedules' && (
        <DataTable
          columns={scheduleColumns}
          data={schedules}
          rowKey={s => s.id}
          defaultSort={{ key: 'next_due', dir: 'asc' }}
          pageSize={25}
          csvExport={{ filename: 'maintenance-schedules' }}
          mobileCards={renderScheduleCard}
          toolbarStart={
            canPlan ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setShowImport(true)}
                className="inline-flex items-center"
              >
                <ArrowUpTrayIcon className="w-4 h-4 mr-1" aria-hidden="true" />
                Import PM Schedules
              </Button>
            ) : undefined
          }
          empty={{
            icon: CalendarDaysIcon,
            title: 'No schedules configured',
            description: canPlan
              ? 'Create a maintenance schedule, or import the PM sheet, to track recurring upkeep.'
              : 'Maintenance schedules will appear here once a planner creates them.',
            action: canPlan ? { label: 'New Schedule', onClick: openScheduleModal } : undefined,
          }}
        />
      )}

      {/* Work Orders Tab */}
      {activeTab === 'work_orders' && (
        <div className="space-y-4">
          <div className="flex gap-3 flex-wrap">
            <div className="relative flex-1 min-w-[200px]">
              <MagnifyingGlassIcon className="absolute left-3 top-2.5 w-5 h-5 text-slate-400" />
              <input
                type="text"
                placeholder="Search WO #, title, target…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                aria-label="Search work orders"
                className="w-full pl-10 pr-4 py-2 border border-slate-600 rounded-sm focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <select
              value={statusFilter}
              onChange={e => setStatusFilter(e.target.value)}
              aria-label="Filter by status"
              className="px-3 py-2 border border-slate-600 rounded-sm"
            >
              <option value="">All Statuses</option>
              {WO_STATUS_FILTERS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          <DataTable
            columns={woColumns}
            data={filteredWOs}
            rowKey={wo => wo.id}
            defaultSort={{ key: 'scheduled', dir: 'desc' }}
            pageSize={25}
            csvExport={{ filename: 'maintenance-work-orders' }}
            mobileCards={renderWOCard}
            empty={{
              icon: PlusIcon,
              title: 'No work orders found',
              description:
                search || statusFilter
                  ? 'No maintenance work orders match your search or filter.'
                  : 'Create a maintenance work order to get started.',
              action:
                search || statusFilter || !canPlan ? undefined : { label: 'New Work Order', onClick: openWOModal },
            }}
          />
        </div>
      )}

      {/* Equipment assets Tab */}
      {activeTab === 'assets' && <MaintenanceAssetsPanel canEdit={canPlan} onChanged={loadAssets} />}

      {showImport && (
        <PmScheduleImportWizard
          onClose={() => setShowImport(false)}
          onCommitted={() => {
            void loadData();
            loadAssets();
          }}
        />
      )}

      {/* Create Schedule Modal */}
      <Modal
        open={showCreateScheduleModal}
        onClose={closeScheduleModal}
        size="lg"
        scroll={false}
        padded={false}
        closeOnBackdrop={false}
      >
        {actionErrorBanner}
        <div className="flex justify-between items-center p-4 border-b border-slate-700">
          <h3 className="text-lg font-semibold">New Maintenance Schedule</h3>
          <button type="button" onClick={closeScheduleModal} aria-label="Close dialog">
            <XMarkIcon className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
          <FormField label="Name" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <input
                {...field}
                type="text"
                maxLength={255}
                required
                placeholder="e.g. CNC-01 Monthly PM"
                value={scheduleForm.name}
                onChange={e => setScheduleForm(f => ({ ...f, name: e.target.value }))}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <MaintenanceTargetField
            name="schedule-target"
            value={scheduleTarget}
            onChange={setScheduleTarget}
            assets={assets}
          />
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Type" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <select
                  {...field}
                  value={scheduleForm.maintenance_type}
                  onChange={e => setScheduleForm(f => ({ ...f, maintenance_type: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                >
                  {Object.entries(MAINTENANCE_TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Priority" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <select
                  {...field}
                  value={scheduleForm.priority}
                  onChange={e => setScheduleForm(f => ({ ...f, priority: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                >
                  {Object.entries(MAINTENANCE_PRIORITY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Frequency" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <select
                  {...field}
                  value={scheduleForm.frequency}
                  onChange={e => setScheduleForm(f => ({ ...f, frequency: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                >
                  {Object.entries(MAINTENANCE_FREQUENCY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {isCustom ? (
              <FormField label="Every N days" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="number"
                    min={1}
                    max={3650}
                    step={1}
                    required
                    value={scheduleForm.frequency_days}
                    onChange={e => setScheduleForm(f => ({ ...f, frequency_days: e.target.value }))}
                    className="w-full px-3 py-2 border rounded-sm"
                  />
                )}
              </FormField>
            ) : (
              <FormField label="Next Due Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={scheduleForm.next_due_date}
                    onChange={e => setScheduleForm(f => ({ ...f, next_due_date: e.target.value }))}
                    className="w-full px-3 py-2 border rounded-sm"
                  />
                )}
              </FormField>
            )}
            {isCustom && (
              <FormField label="Next Due Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={scheduleForm.next_due_date}
                    onChange={e => setScheduleForm(f => ({ ...f, next_due_date: e.target.value }))}
                    className="w-full px-3 py-2 border rounded-sm"
                  />
                )}
              </FormField>
            )}
            <FormField label="Est. Duration (hours)" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <input
                  {...field}
                  type="number"
                  min={0}
                  step="0.5"
                  value={scheduleForm.estimated_duration_hours}
                  onChange={e => setScheduleForm(f => ({ ...f, estimated_duration_hours: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={scheduleForm.requires_shutdown}
              onChange={e => setScheduleForm(f => ({ ...f, requires_shutdown: e.target.checked }))}
            />
            <span>Requires machine shutdown</span>
          </label>
          <FormField label="Description" labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <textarea
                {...field}
                value={scheduleForm.description}
                onChange={e => setScheduleForm(f => ({ ...f, description: e.target.value }))}
                rows={2}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <FormField
            label="Checklist"
            help="One item per line, in the order they are done."
            labelClassName="block text-sm font-medium text-slate-300 mb-1"
          >
            {field => (
              <textarea
                {...field}
                value={scheduleForm.checklist}
                onChange={e => setScheduleForm(f => ({ ...f, checklist: e.target.value }))}
                rows={4}
                className="w-full px-3 py-2 border rounded-sm"
                placeholder={'Lubricate ways\nCheck coolant level'}
              />
            )}
          </FormField>
        </div>
        <div className="flex justify-end gap-2 p-4 border-t border-slate-700">
          <Button variant="secondary" onClick={closeScheduleModal} disabled={actionBusy}>
            Cancel
          </Button>
          <LoadingButton
            onClick={handleCreateSchedule}
            loading={actionBusy}
            loadingText="Saving…"
            disabled={!scheduleReady}
          >
            Create
          </LoadingButton>
        </div>
      </Modal>

      {/* Create Work Order Modal */}
      <Modal
        open={showCreateWOModal}
        onClose={closeWOModal}
        size="lg"
        scroll={false}
        padded={false}
        closeOnBackdrop={false}
      >
        {actionErrorBanner}
        <div className="flex justify-between items-center p-4 border-b border-slate-700">
          <h3 className="text-lg font-semibold">New Maintenance Work Order</h3>
          <button type="button" onClick={closeWOModal} aria-label="Close dialog">
            <XMarkIcon className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
          <FormField label="Title" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <input
                {...field}
                type="text"
                maxLength={255}
                required
                value={woForm.title}
                onChange={e => setWoForm(f => ({ ...f, title: e.target.value }))}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <MaintenanceTargetField name="wo-target" value={woTarget} onChange={setWoTarget} assets={assets} />
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Type" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <select
                  {...field}
                  value={woForm.maintenance_type}
                  onChange={e => setWoForm(f => ({ ...f, maintenance_type: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                >
                  {Object.entries(MAINTENANCE_TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Priority" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <select
                  {...field}
                  value={woForm.priority}
                  onChange={e => setWoForm(f => ({ ...f, priority: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                >
                  {Object.entries(MAINTENANCE_PRIORITY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Scheduled Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <input
                  {...field}
                  type="date"
                  value={woForm.scheduled_date}
                  onChange={e => setWoForm(f => ({ ...f, scheduled_date: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
            <FormField label="Due Date" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <input
                  {...field}
                  type="date"
                  value={woForm.due_date}
                  onChange={e => setWoForm(f => ({ ...f, due_date: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
          </div>
          <div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={woForm.requires_shutdown}
                onChange={e => setWoForm(f => ({ ...f, requires_shutdown: e.target.checked }))}
                aria-describedby="wo-shutdown-help"
              />
              <span>Requires machine shutdown</span>
            </label>
            <p id="wo-shutdown-help" className="mt-1 text-xs text-slate-500">
              {woTarget.targetType === 'asset'
                ? 'Equipment assets never record machine downtime.'
                : 'Starting it records downtime on the work center until it is completed or cancelled — planned for preventive / predictive work, unplanned (an OEE loss) for corrective / emergency.'}
            </p>
          </div>
          <FormField label="Description" labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <textarea
                {...field}
                value={woForm.description}
                onChange={e => setWoForm(f => ({ ...f, description: e.target.value }))}
                rows={3}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
        </div>
        <div className="flex justify-end gap-2 p-4 border-t border-slate-700">
          <Button variant="secondary" onClick={closeWOModal} disabled={actionBusy}>
            Cancel
          </Button>
          <LoadingButton onClick={handleCreateWO} loading={actionBusy} loadingText="Saving…" disabled={!woReady}>
            Create
          </LoadingButton>
        </div>
      </Modal>

      {/* Complete Work Order Modal */}
      <ConfirmDialog
        open={!!cancelTarget}
        title={cancelTarget ? `Cancel ${cancelTarget.wo_number}?` : 'Cancel work order?'}
        message={
          cancelTarget?.status === 'in_progress' || cancelTarget?.status === 'on_hold'
            ? 'The work order is closed without being completed. Downtime it is recording on the machine ends — unless another maintenance work order still has the machine shut down, or the downtime was an operator’s breakdown report, which stays open.'
            : 'The work order is closed without being completed.'
        }
        confirmLabel="Cancel work order"
        cancelLabel="Keep it"
        variant="danger"
        pending={actionBusy}
        onConfirm={() => void confirmCancel()}
        onCancel={() => setCancelTarget(null)}
      />

      <Modal
        open={showCompleteModal && !!selectedWO}
        onClose={closeCompleteModal}
        size="md"
        scroll={false}
        padded={false}
        closeOnBackdrop={false}
      >
        {actionErrorBanner}
        <div className="flex justify-between items-center p-4 border-b border-slate-700">
          <h3 className="text-lg font-semibold">Complete: {selectedWO?.title}</h3>
          <button type="button" onClick={closeCompleteModal} aria-label="Close dialog">
            <XMarkIcon className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
          <FormField label="Completion Notes" labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <textarea
                {...field}
                value={completeForm.notes}
                onChange={e => setCompleteForm(f => ({ ...f, notes: e.target.value }))}
                rows={2}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <FormField label="Findings" labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <textarea
                {...field}
                value={completeForm.findings}
                onChange={e => setCompleteForm(f => ({ ...f, findings: e.target.value }))}
                rows={2}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <FormField label="Parts Used" labelClassName="block text-sm font-medium text-slate-300 mb-1">
            {field => (
              <input
                {...field}
                type="text"
                value={completeForm.parts_used}
                onChange={e => setCompleteForm(f => ({ ...f, parts_used: e.target.value }))}
                className="w-full px-3 py-2 border rounded-sm"
              />
            )}
          </FormField>
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Labor Cost ($)" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <input
                  {...field}
                  type="number"
                  min={0}
                  step="0.01"
                  value={completeForm.labor_cost}
                  onChange={e => setCompleteForm(f => ({ ...f, labor_cost: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
            <FormField label="Parts Cost ($)" labelClassName="block text-sm font-medium text-slate-300 mb-1">
              {field => (
                <input
                  {...field}
                  type="number"
                  min={0}
                  step="0.01"
                  value={completeForm.parts_cost}
                  onChange={e => setCompleteForm(f => ({ ...f, parts_cost: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
            <FormField
              label="Downtime (minutes)"
              help={
                selectedWO?.requires_shutdown && selectedWO?.target_type === 'work_center'
                  ? 'Leave blank to use the downtime recorded on the machine.'
                  : undefined
              }
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {field => (
                <input
                  {...field}
                  type="number"
                  min={0}
                  step="1"
                  value={completeForm.downtime_minutes}
                  onChange={e => setCompleteForm(f => ({ ...f, downtime_minutes: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
            <FormField
              label="Actual Hours"
              help="Leave blank to use the time since it was started."
              labelClassName="block text-sm font-medium text-slate-300 mb-1"
            >
              {field => (
                <input
                  {...field}
                  type="number"
                  min={0}
                  step="0.25"
                  value={completeForm.actual_duration_hours}
                  onChange={e => setCompleteForm(f => ({ ...f, actual_duration_hours: e.target.value }))}
                  className="w-full px-3 py-2 border rounded-sm"
                />
              )}
            </FormField>
          </div>
        </div>
        <div className="flex justify-end gap-2 p-4 border-t border-slate-700">
          <Button variant="secondary" onClick={closeCompleteModal} disabled={actionBusy}>
            Cancel
          </Button>
          <LoadingButton onClick={handleComplete} loading={actionBusy} loadingText="Saving…">
            Complete
          </LoadingButton>
        </div>
      </Modal>
    </div>
  );
}
