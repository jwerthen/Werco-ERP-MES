import React, { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { WorkOrderSummary, WorkOrderStatus } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import {
  formatCentralDate,
  getCentralDateStamp,
  isDateBeforeTodayInCentral,
  isDateTodayInCentral,
} from '../utils/centralTime';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  Squares2X2Icon,
  ListBulletIcon,
  ChevronRightIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  TrashIcon,
  CheckCircleIcon,
  ArrowUpTrayIcon,
  DocumentDuplicateIcon,
  PencilSquareIcon,
  CheckIcon,
  XMarkIcon,
  BookmarkSquareIcon,
} from '@heroicons/react/24/outline';
import { SkeletonTable, SkeletonCard } from '../components/ui/Skeleton';
import { ConfirmDialog, EmptyState, ErrorState, useToast, DataTable, DataTableColumn, StatusBadge, Button, UnitBadge } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { useOptimisticMutation } from '../hooks/useOptimisticMutation';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import LaserNestImportWizard from '../components/laser/LaserNestImportWizard';
import DuplicateWorkOrderModal from '../components/workorders/DuplicateWorkOrderModal';
import SaveAsTemplateModal from '../components/workorders/SaveAsTemplateModal';
import WorkOrderTemplatesPanel from '../components/workorders/WorkOrderTemplatesPanel';

const priorityConfig: Record<number, { bg: string; text: string; label: string }> = {
  1: { bg: 'bg-red-500/20', text: 'text-red-400', label: 'Critical' },
  2: { bg: 'bg-red-500/10', text: 'text-red-600', label: 'High' },
  3: { bg: 'bg-amber-500/10', text: 'text-amber-400', label: 'Medium' },
  4: { bg: 'bg-blue-500/10', text: 'text-blue-600', label: 'Normal' },
  5: { bg: 'bg-surface-100', text: 'text-surface-600', label: 'Low' },
};

const EXCLUDED_PART_TYPES = ['purchased', 'hardware', 'raw_material'];
// A finished job's due date is its promise date, and OTD scores against
// `coalesce(must_ship_by, due_date)` -- moving it retroactively rewrites a delivery
// result that is already recorded. `PUT /work-orders/{id}` REFUSES that change with
// a 409, so this is the matching client gate, not the enforcement: hiding the pencil
// here (and on the detail page) keeps the UI from offering an edit the server will
// reject. Also the list the overdue badge and the overdue stat suppress on.
const TERMINAL_WO_STATUSES = ['complete', 'closed', 'cancelled'];
const CURRENT_WORK_ORDER_STATUSES = ['released', 'in_progress', 'on_hold'];

type GroupBy = 'none' | 'customer' | 'part' | 'status';

const statusOptions: { value: string; label: string }[] = [
  { value: '', label: 'All Active' },
  { value: 'draft', label: 'Draft' },
  { value: 'released', label: 'Released' },
  { value: 'in_progress', label: 'In Progress' },
  { value: 'on_hold', label: 'On Hold' },
  { value: 'complete', label: 'Complete' },
  { value: 'closed', label: 'Closed' },
];

const groupOptions: { value: GroupBy; label: string }[] = [
  { value: 'none', label: 'No Grouping' },
  { value: 'customer', label: 'By Customer' },
  { value: 'part', label: 'By Part' },
  { value: 'status', label: 'By Status' },
];

const formatStatusLabel = (status: string) => status.replace('_', ' ');

// Sanitize a group name into a per-group CSV filename slug: lowercase, runs of
// non-alphanumerics collapse to a single dash, edge dashes trimmed.
const groupCsvSlug = (name: string) =>
  name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'group';

const getWorkOrderProgress = (wo: WorkOrderSummary) => {
  const operationCount = Number(wo.operation_count || 0);
  if (operationCount > 0 && wo.operation_progress_percent !== undefined) {
    return {
      percent: Math.min(100, Math.max(0, Number(wo.operation_progress_percent || 0))),
      label: `${Number(wo.operations_complete || 0)}/${operationCount} ops`,
      title: 'Progress',
    };
  }

  const ordered = Number(wo.quantity_ordered || 0);
  const complete = Number(wo.quantity_complete || 0);
  return {
    percent: ordered > 0 ? Math.min(100, Math.max(0, (complete / ordered) * 100)) : 0,
    label: `${complete}/${ordered}`,
    title: 'Quantity',
  };
};

// Cell renderers — shared by the flat and grouped DataTable views.
function StatusCell({ status }: { status: WorkOrderStatus }) {
  return <StatusBadge status={status} />;
}

function PriorityCell({ priority }: { priority: number }) {
  const cfg = priorityConfig[priority] || priorityConfig[4];
  return <span className={`badge ${cfg.bg} ${cfg.text}`}>P{priority}</span>;
}

function ProgressCell({ wo }: { wo: WorkOrderSummary }) {
  const progress = getWorkOrderProgress(wo);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-surface-200 rounded-full overflow-hidden w-20">
        <div
          className="h-full bg-werco-500 rounded-full transition-all"
          style={{ width: `${progress.percent}%` }}
        />
      </div>
      <span className="text-sm font-medium text-surface-700 tabular-nums">{progress.label}</span>
    </div>
  );
}

// Open inline due-date editor: which row, the draft, and the value the row showed
// when the editor opened. The baseline is the lost-update guard -- see
// `handleDueDateSave` for why `version` cannot carry it on this page.
interface DueDateEditState {
  id: number;
  draft: string;
  baseline: string;
}

interface DueDateCellOptions {
  edit?: DueDateEditState | null;
  saving?: boolean;
  onStartEdit?: (wo: WorkOrderSummary) => void;
  onChangeDraft?: (value: string) => void;
  onSave?: (wo: WorkOrderSummary) => void;
  onCancelEdit?: () => void;
}

function DueDateCell({ wo, options }: { wo: WorkOrderSummary; options?: DueDateCellOptions }) {
  const overdue = isWorkOrderOverdue(wo);
  const { edit, saving, onStartEdit, onChangeDraft, onSave, onCancelEdit } = options ?? {};
  const editing = Boolean(edit && edit.id === wo.id);
  // The pencil appears only for a caller that passed the handlers (i.e. a user
  // who holds work_orders:edit) and only on a job that has not finished.
  const editable = Boolean(onStartEdit) && !TERMINAL_WO_STATUSES.includes(wo.status);

  if (editing && edit) {
    return (
      // The row is click-through to the detail page; every control in here has to
      // stop that, or picking a date navigates away mid-edit. Click only — DataTable
      // rows carry no keyboard handler, so a keydown guard here would assert a hazard
      // that does not exist.
      <div className="flex items-center gap-1" role="presentation" onClick={(e) => e.stopPropagation()}>
        <input
          type="date"
          value={edit.draft}
          autoFocus
          disabled={saving}
          aria-label={`Due date for ${wo.work_order_number}`}
          onChange={(e) => onChangeDraft?.(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              // A native date input reports '' the moment a complete value is edited
              // back to an incomplete one — retyping the month empties it in passing.
              // Enter at that instant would CLEAR a promise date and toast success.
              // Clearing stays available, deliberately, through the explicit ✓.
              if (edit.draft === '' && edit.baseline !== '') return;
              onSave?.(wo);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              onCancelEdit?.();
            }
          }}
          className="input input-sm w-36 disabled:opacity-50"
        />
        <button
          type="button"
          onClick={() => onSave?.(wo)}
          disabled={saving}
          aria-label={`Save due date for ${wo.work_order_number}`}
          className="p-1 rounded text-emerald-600 hover:bg-emerald-500/10 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <CheckIcon className="h-4 w-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={() => onCancelEdit?.()}
          disabled={saving}
          aria-label={`Cancel due date edit for ${wo.work_order_number}`}
          className="p-1 rounded text-surface-400 hover:text-red-600 hover:bg-red-500/10 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <XMarkIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    );
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`text-sm font-medium ${overdue ? 'text-red-600' : 'text-surface-700'}`}>
        {wo.due_date ? formatCentralDate(wo.due_date) : '—'}
      </span>
      {overdue && <span className="badge badge-danger text-[10px] py-0.5">OVERDUE</span>}
      {editable && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onStartEdit?.(wo);
          }}
          aria-label={`Edit due date for ${wo.work_order_number}`}
          className="p-1 rounded text-surface-400 hover:text-werco-600 hover:bg-werco-50"
        >
          <PencilSquareIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    </span>
  );
}

function RowActionsCell({
  wo,
  onDelete,
  onDuplicate,
  onSaveTemplate,
  onRelease,
  isReleasing,
  isDeleting,
}: {
  wo: WorkOrderSummary;
  onDelete?: (wo: WorkOrderSummary) => void;
  onDuplicate?: (wo: WorkOrderSummary) => void;
  onSaveTemplate?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  isReleasing: boolean;
  isDeleting: boolean;
}) {
  // Stop propagation so action clicks don't trigger the row click-through.
  return (
    <div
      className="flex items-center gap-1"
      role="presentation"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {onRelease && wo.status === 'draft' && (
        <button
          onClick={() => onRelease(wo)}
          disabled={isReleasing}
          className="p-2 rounded-lg text-emerald-600 hover:text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          title="Release"
          aria-label={`Release ${wo.work_order_number}`}
        >
          <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      {onDuplicate && (
        <button
          onClick={() => onDuplicate(wo)}
          className="p-2 rounded-lg text-surface-400 hover:text-werco-600 hover:bg-werco-50 transition-colors"
          title="Duplicate"
          aria-label={`Duplicate ${wo.work_order_number}`}
        >
          <DocumentDuplicateIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      {onSaveTemplate && (
        <button
          onClick={() => onSaveTemplate(wo)}
          className="p-2 rounded-lg text-surface-400 hover:text-werco-600 hover:bg-werco-50 transition-colors"
          title="Save as template"
          aria-label={`Save ${wo.work_order_number} as a template`}
        >
          <BookmarkSquareIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      {onDelete && (
        <button
          onClick={() => onDelete(wo)}
          disabled={isDeleting}
          className="p-2 rounded-lg text-surface-400 hover:text-red-600 hover:bg-red-500/10 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          title="Delete"
          aria-label={`Delete ${wo.work_order_number}`}
        >
          <TrashIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
      <Link
        to={`/work-orders/${wo.id}`}
        className="p-2 rounded-lg text-surface-400 hover:text-werco-600 hover:bg-werco-50 transition-colors"
        aria-label={`View ${wo.work_order_number}`}
      >
        <ChevronRightIcon className="h-5 w-5" aria-hidden="true" />
      </Link>
    </div>
  );
}

interface WorkOrderColumnOptions {
  hideColumn?: 'customer' | 'part';
  onDelete?: (wo: WorkOrderSummary) => void;
  onDuplicate?: (wo: WorkOrderSummary) => void;
  /** Catalog this row's plan under a name. Same gate as Duplicate. */
  onSaveTemplate?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  releasingIds?: Set<number>;
  deletePending?: boolean;
  dueDateEdit?: DueDateCellOptions;
}

function buildWorkOrderColumns({
  hideColumn,
  onDelete,
  onDuplicate,
  onSaveTemplate,
  onRelease,
  releasingIds,
  deletePending,
  dueDateEdit,
}: WorkOrderColumnOptions): Array<DataTableColumn<WorkOrderSummary>> {
  const cols: Array<DataTableColumn<WorkOrderSummary>> = [
    {
      key: 'work_order_number',
      header: 'Work Order',
      sortable: true,
      accessor: (wo) => wo.work_order_number,
      render: (wo) => (
        <div className="min-w-0">
          <Link
            to={`/work-orders/${wo.id}`}
            onClick={(e) => e.stopPropagation()}
            className="font-semibold text-werco-600 hover:text-werco-700 hover:underline"
          >
            {wo.work_order_number}
          </Link>
          {/* Renders nothing when the work order tracks no unit, so every other
              row keeps its pre-083 single-line height. The wrapper is what puts the
              badge on its own line — a caller `flex` cannot override the badge's
              own `inline-flex`. */}
          {wo.unit_number ? (
            <div className="mt-1">
              <UnitBadge unitNumber={wo.unit_number} size="sm" />
            </div>
          ) : null}
        </div>
      ),
    },
  ];

  if (hideColumn !== 'part') {
    cols.push({
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (wo) => wo.part_number ?? '',
      csv: (wo) => wo.part_number ?? '',
      render: (wo) => (
        <div>
          {/* Standalone laser nest WOs carry no part — label them instead of
              rendering blank cells. */}
          <p className="font-medium text-surface-900">
            {wo.part_number || (wo.work_order_type === 'laser_cutting' ? 'Nest package' : '—')}
          </p>
          <p className="text-sm text-surface-500 line-clamp-1">
            {wo.part_name || (!wo.part_number && wo.work_order_type === 'laser_cutting' ? 'Laser sheet runs' : '')}
          </p>
        </div>
      ),
    });
  }

  if (hideColumn !== 'customer') {
    cols.push({
      key: 'customer',
      header: 'Customer',
      sortable: true,
      accessor: (wo) => wo.customer_name ?? '',
      className: 'text-surface-600',
      render: (wo) => wo.customer_name || '—',
    });
  }

  cols.push(
    {
      key: 'progress',
      header: 'Progress',
      accessor: (wo) => getWorkOrderProgress(wo).percent,
      csv: (wo) => getWorkOrderProgress(wo).label,
      render: (wo) => <ProgressCell wo={wo} />,
    },
    {
      key: 'due_date',
      header: 'Due Date',
      sortable: true,
      accessor: (wo) => wo.due_date ?? '',
      csv: (wo) => (wo.due_date ? formatCentralDate(wo.due_date) : ''),
      render: (wo) => <DueDateCell wo={wo} options={dueDateEdit} />,
    },
    {
      key: 'priority',
      header: 'Priority',
      sortable: true,
      accessor: (wo) => wo.priority,
      render: (wo) => <PriorityCell priority={wo.priority} />,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (wo) => wo.status,
      render: (wo) => <StatusCell status={wo.status} />,
    },
    {
      key: 'actions',
      header: '',
      // Fits the widest row: Release (draft only) + Duplicate + Save as
      // template + Delete + View.
      className: 'w-44',
      render: (wo) => (
        <RowActionsCell
          wo={wo}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
          onSaveTemplate={onSaveTemplate}
          onRelease={onRelease}
          isReleasing={Boolean(releasingIds?.has(wo.id))}
          isDeleting={Boolean(deletePending)}
        />
      ),
    }
  );

  return cols;
}

export default function WorkOrders() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { showToast } = useToast();
  // Backend now permits admin AND manager (plus superuser) to soft-delete a WO.
  const canDeleteWorkOrders = user?.role === 'admin' || user?.role === 'manager' || !!user?.is_superuser;
  // Matches the backend RBAC on the nest endpoints (admin/manager/supervisor,
  // + platform_admin) — the same gate WorkOrderDetail uses for nest actions.
  const canImportNests = hasPermission(user?.role, 'routings:create');
  // Duplicate is require_role([ADMIN, MANAGER, SUPERVISOR]) on the backend —
  // the trio work_orders:edit maps to. A hidden control and a refused call have
  // to agree, so this mirrors WorkOrderDetail's gate exactly.
  // `work_orders:edit` maps to exactly the trio the backend's
  // require_role([ADMIN, MANAGER, SUPERVISOR]) admits (plus superuser/platform-admin
  // via require_role itself), so a hidden control and a refused call agree. Both the
  // Duplicate action and the inline due-date edit go through that tier.
  const canEditWorkOrders = hasPermission(user?.role, 'work_orders:edit') || !!user?.is_superuser;
  const canDuplicateWorkOrders = canEditWorkOrders;
  const canEditDueDate = canEditWorkOrders;
  const [nestWizardOpen, setNestWizardOpen] = useState(false);
  const [workOrders, setWorkOrders] = useState<WorkOrderSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  // Free-text search stays local state; only the debounced value drives the fetch.
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search.trim(), 250);

  // Structured filters live in the URL (the ProcessSheets idiom) so a filtered
  // view survives reload and can be shared/bookmarked. An ABSENT param means the
  // default (hideCOTS defaults ON — `cots=1` appears only when showing COTS;
  // groupBy defaults none — `group` appears only when grouping), so the default
  // state keeps a clean URL and existing bookmarks are unaffected. Rapid param
  // changes are handled by the loadRequestRef race guard below.
  const [searchParams, setSearchParams] = useSearchParams();
  const statusFilter = searchParams.get('status') ?? '';
  const customerFilter = searchParams.get('customer') ?? '';
  const hideCOTS = searchParams.get('cots') !== '1';
  const groupParam = searchParams.get('group');
  const groupBy: GroupBy =
    groupParam === 'customer' || groupParam === 'part' || groupParam === 'status' ? groupParam : 'none';

  // Copy-and-set setter: an empty value deletes the param (default = clean URL).
  const setFilterParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    setSearchParams(next);
  };
  const setStatusFilter = (value: string) => setFilterParam('status', value);
  // Templates is a TAB here, not a route: `/work-orders/templates` is matched by
  // App.tsx's `/work-orders/:id` route AND by routeMeta's WO-detail pattern, so a
  // real route there would resolve as a work order whose id is the word
  // "templates". The tab rides on `?tab=` through the SAME copy-and-set setter
  // every other filter uses, so switching tabs preserves the status / customer /
  // COTS / grouping params instead of wiping them.
  const setActiveTab = (value: 'orders' | 'templates') => setFilterParam('tab', value === 'orders' ? '' : value);
  const setCustomerFilter = (value: string) => setFilterParam('customer', value);
  const setHideCOTS = (hide: boolean) => setFilterParam('cots', hide ? '' : '1');
  const setGroupBy = (value: GroupBy) => setFilterParam('group', value === 'none' ? '' : value);

  // A deep link to ?tab=templates from someone WITHOUT work_orders:edit falls
  // back to the list: every template verb (reads included) is role-gated to the
  // trio that permission maps to, so showing the tab would only produce a 403.
  const activeTab: 'orders' | 'templates' =
    searchParams.get('tab') === 'templates' && canEditWorkOrders ? 'templates' : 'orders';

  const [releasingIds, setReleasingIds] = useState<Set<number>>(new Set());
  const [dueDateEdit, setDueDateEdit] = useState<DueDateEditState | null>(null);
  const [savingDueDate, setSavingDueDate] = useState(false);
  const [dueDateConflict, setDueDateConflict] = useState<{ wo: WorkOrderSummary; serverStamp: string } | null>(null);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const loadRequestRef = useRef(0);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, [user?.id]);

  const loadWorkOrders = useCallback(async () => {
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;

    try {
      const params: any = {};
      if (statusFilter) params.status = statusFilter;
      if (debouncedSearch) params.search = debouncedSearch;
      const response = await api.getWorkOrders(params);
      if (requestId !== loadRequestRef.current) return;
      setWorkOrders(response);
      setLoadError(false);
    } catch (err) {
      if (requestId !== loadRequestRef.current) return;
      console.error('Failed to load work orders:', err);
      setLoadError(true);
    } finally {
      if (requestId !== loadRequestRef.current) return;
      setLoading(false);
    }
  }, [statusFilter, debouncedSearch]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadWorkOrders();
    }, 600);
  }, [loadWorkOrders]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['work_order_update', 'dashboard_update', 'shop_floor_update'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadWorkOrders();
  }, [loadWorkOrders]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      loadWorkOrders();
    }, 30000);

    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        loadWorkOrders();
      }
    };

    const refreshOnFocus = () => {
      loadWorkOrders();
    };

    document.addEventListener('visibilitychange', refreshWhenVisible);
    window.addEventListener('focus', refreshOnFocus);

    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      window.removeEventListener('focus', refreshOnFocus);
    };
  }, [loadWorkOrders]);

  // Optimistic delete. Deleting a work order is a soft-delete that simply removes
  // an already-loaded row from active lists — the SAFE "record a decision on a
  // loaded row" shape, so we drop the row from the table synchronously and only
  // roll it back (re-inserting at its original index) on the rare server refusal,
  // surfacing the verbatim server detail via the hook's default error toast. The
  // per-call target (which row + where it sat) is threaded through run(ctx), so each
  // delete's rollback restores the row IT removed even if two rows are deleted in
  // quick succession (one hook instance serves every row's Delete control). (WO
  // *release* stays non-optimistic below — it is server-gated by readiness checks.)
  // Mirror the latest list so handleDelete can read the row's current index
  // without depending on (and re-creating) the callback on every list change.
  const workOrdersRef = useRef(workOrders);
  workOrdersRef.current = workOrders;

  const { run: runDelete, pending: deletePending } = useOptimisticMutation<unknown, { wo: WorkOrderSummary; index: number }>({
    applyOptimistic: ({ wo }) => {
      setWorkOrders((prev) => prev.filter((w) => w.id !== wo.id));
    },
    rollback: ({ wo, index }) => {
      setWorkOrders((prev) => {
        if (prev.some((w) => w.id === wo.id)) return prev;
        const next = [...prev];
        next.splice(Math.min(index, next.length), 0, wo);
        return next;
      });
    },
    mutate: ({ wo }) => api.deleteWorkOrder(wo.id),
    errorFallback: 'Failed to delete work order',
  });

  // The row Delete action opens the shared confirm dialog; the optimistic
  // mutation itself only fires from the dialog's confirm.
  const [deleteTarget, setDeleteTarget] = useState<WorkOrderSummary | null>(null);

  const handleDelete = useCallback((wo: WorkOrderSummary) => {
    setDeleteTarget(wo);
  }, []);

  // Duplicate opens the shared dialog; the write itself is server-gated and
  // stays non-optimistic (nothing is added to this list until the server
  // answers — and by then we have navigated to the new draft anyway).
  const [duplicateTarget, setDuplicateTarget] = useState<WorkOrderSummary | null>(null);

  const handleDuplicate = useCallback((wo: WorkOrderSummary) => {
    setDuplicateTarget(wo);
  }, []);

  // Save-as-template writes ONE row and points it at this work order — it copies
  // nothing now and changes nothing on the source, so there is no list state to
  // update and nowhere to navigate. The token below just re-reads the catalog if
  // the Templates tab is already mounted.
  const [saveTemplateTarget, setSaveTemplateTarget] = useState<WorkOrderSummary | null>(null);

  const handleSaveTemplate = useCallback((wo: WorkOrderSummary) => {
    setSaveTemplateTarget(wo);
  }, []);

  const handleConfirmDelete = useCallback(async () => {
    const wo = deleteTarget;
    if (!wo || deletePending) return;
    // Capture the row's current position; run(ctx) closes over it so the rollback
    // (on the rare server refusal) restores THIS row at its original index.
    const index = workOrdersRef.current.findIndex((w) => w.id === wo.id);
    // Stale-target guard: unlike window.confirm, the dialog doesn't block the
    // event loop, and the list background-refreshes (30s poll / WS / focus)
    // while it is open. If the target row is already gone — deleted by another
    // session — confirming must NOT fire the API: the server would refuse and
    // the optimistic rollback would re-insert a phantom row.
    if (index === -1) {
      setDeleteTarget(null);
      showToast('info', 'Work order was already removed');
      return;
    }
    try {
      await runDelete({ wo, index });
    } finally {
      setDeleteTarget(null);
    }
  }, [deleteTarget, deletePending, runDelete, showToast]);

  // Standalone nest import: the wizard created a fresh released laser WO (no
  // parent, no part) — route to it, mirroring WorkOrderDetail's handler.
  const handleNestPackageImported = useCallback((newWorkOrderId?: number) => {
    setNestWizardOpen(false);
    showToast('success', 'Nest package imported — laser work order created.');
    if (newWorkOrderId) {
      navigate(`/work-orders/${newWorkOrderId}`);
    } else {
      loadWorkOrders();
    }
  }, [navigate, loadWorkOrders, showToast]);

  const handleRelease = useCallback(async (wo: WorkOrderSummary) => {
    if (wo.status !== 'draft') return;
    setReleasingIds((prev) => new Set(prev).add(wo.id));
    try {
      await api.releaseWorkOrder(wo.id);
      loadWorkOrders();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release work order');
    } finally {
      setReleasingIds((prev) => {
        const next = new Set(prev);
        next.delete(wo.id);
        return next;
      });
    }
  }, [loadWorkOrders, showToast]);

  // --- Inline due-date edit ------------------------------------------------
  // A due date is a promise date feeding OTD (`must_ship_by || due_date`), so a
  // silently-lost reschedule is not cosmetic. Two guards, and BOTH are needed:
  //
  //  1. `version` — the server-side optimistic lock. Stops a write that landed
  //     between our last fetch and this PUT (409, nothing written).
  //  2. a local `baseline` — the value the row showed when the editor opened.
  //     This list refetches every 30s, on window focus, and on every work-order
  //     broadcast, so `version` refreshes UNDER an open editor and its 409 would
  //     never fire. Without the baseline, a concurrent reschedule is overwritten
  //     with a clean 200. The baseline turns that silent loss into a decision.
  //
  // Server-gated ⇒ NON-optimistic (the CLAUDE.md convention): the cell keeps
  // showing the server's date until the refetch confirms the new one.
  const dueDateStampOf = useCallback(
    (wo: WorkOrderSummary) => (wo.due_date ? getCentralDateStamp(wo.due_date) : ''),
    []
  );

  const handleStartDueDateEdit = useCallback((wo: WorkOrderSummary) => {
    // getCentralDateStamp passes date-only strings through verbatim and
    // normalizes datetimes to the Central calendar date — the `<input type="date">`
    // value has to be that stamp, never a raw ISO instant.
    const stamp = wo.due_date ? getCentralDateStamp(wo.due_date) : '';
    setDueDateEdit({ id: wo.id, draft: stamp, baseline: stamp });
  }, []);

  const handleCancelDueDateEdit = useCallback(() => {
    if (savingDueDate) return;
    setDueDateEdit(null);
  }, [savingDueDate]);

  const handleChangeDueDateDraft = useCallback((value: string) => {
    setDueDateEdit((prev) => (prev ? { ...prev, draft: value } : prev));
  }, []);

  const performDueDateSave = useCallback(
    async (wo: WorkOrderSummary, draft: string) => {
      setSavingDueDate(true);
      try {
        await api.updateWorkOrder(wo.id, { due_date: draft || null, version: wo.version ?? 0 });
        showToast(
          'success',
          draft
            ? `${wo.work_order_number} due ${formatCentralDate(draft)}`
            : `Due date cleared on ${wo.work_order_number}`
        );
        setDueDateEdit(null);
        // Awaited: `savingDueDate` must not clear until the fresh row has landed, or
        // an immediate retry re-sends the version we just superseded.
        await loadWorkOrders();
        return true;
      } catch (err: any) {
        const detail = err?.response?.data?.detail;
        showToast('error', typeof detail === 'string' && detail ? detail : 'Failed to update due date');
        if (err?.response?.status === 409) {
          // Stale version. Refetch so a retry carries a fresh one; the editor stays
          // open holding the draft. The baseline is deliberately left behind — if
          // that concurrent write touched the due date, the retry trips the guard
          // below and asks before overwriting it.
          await loadWorkOrders();
        }
        return false;
      } finally {
        setSavingDueDate(false);
      }
    },
    [loadWorkOrders, showToast]
  );

  const handleDueDateSave = useCallback(
    async (wo: WorkOrderSummary) => {
      if (savingDueDate || !dueDateEdit || dueDateEdit.id !== wo.id) return;
      const serverStamp = dueDateStampOf(wo);
      if (serverStamp !== dueDateEdit.baseline) {
        setDueDateConflict({ wo, serverStamp });
        return;
      }
      await performDueDateSave(wo, dueDateEdit.draft);
    },
    [savingDueDate, dueDateEdit, dueDateStampOf, performDueDateSave]
  );

  // Confirmed overwrite. Adopting the server's value as the new baseline is what
  // makes this confirmation cover exactly the change we showed them, so a retry
  // after a FAILED write does not re-ask about a change already approved.
  const handleDueDateConflictReplace = useCallback(async () => {
    if (!dueDateConflict || savingDueDate || !dueDateEdit) return;
    const { wo, serverStamp } = dueDateConflict;
    // Write against the LIVE row, not the snapshot taken when the dialog opened —
    // a poll landing while it is up would otherwise 409 a decision the user just
    // made, over a field nobody touched. Only adopt the live row if its due date
    // still matches what we ASKED about; if it moved again, keep the snapshot so
    // the stale version 409s rather than silently overwriting a change we never
    // showed them.
    const live = workOrders.find((row) => row.id === wo.id);
    const target = live && dueDateStampOf(live) === serverStamp ? live : wo;
    setDueDateEdit((prev) => (prev ? { ...prev, baseline: serverStamp } : prev));
    // Closed up front: `performDueDateSave` clears the editor on success, which
    // would otherwise leave a frame where the dialog is open and no longer pending.
    setDueDateConflict(null);
    await performDueDateSave(target, dueDateEdit.draft);
  }, [dueDateConflict, savingDueDate, dueDateEdit, workOrders, dueDateStampOf, performDueDateSave]);

  const dueDateCellOptions: DueDateCellOptions | undefined = useMemo(
    () =>
      canEditDueDate
        ? {
            edit: dueDateEdit,
            saving: savingDueDate,
            onStartEdit: handleStartDueDateEdit,
            onChangeDraft: handleChangeDueDateDraft,
            onSave: handleDueDateSave,
            onCancelEdit: handleCancelDueDateEdit,
          }
        : undefined,
    [
      canEditDueDate,
      dueDateEdit,
      savingDueDate,
      handleStartDueDateEdit,
      handleChangeDueDateDraft,
      handleDueDateSave,
      handleCancelDueDateEdit,
    ]
  );

  const workOrderColumns = useMemo(
    () =>
      buildWorkOrderColumns({
        onDelete: canDeleteWorkOrders ? handleDelete : undefined,
        onDuplicate: canDuplicateWorkOrders ? handleDuplicate : undefined,
        onSaveTemplate: canEditWorkOrders ? handleSaveTemplate : undefined,
        onRelease: handleRelease,
        releasingIds,
        deletePending,
        dueDateEdit: dueDateCellOptions,
      }),
    [
      canDeleteWorkOrders,
      canDuplicateWorkOrders,
      canEditWorkOrders,
      handleDelete,
      handleDuplicate,
      handleSaveTemplate,
      handleRelease,
      releasingIds,
      deletePending,
      dueDateCellOptions,
    ]
  );

  const customers = useMemo(() => {
    const unique = new Set(workOrders.map(wo => wo.customer_name).filter(Boolean));
    return Array.from(unique).sort() as string[];
  }, [workOrders]);

  const filteredWorkOrders = useMemo(() => {
    return workOrders.filter(wo => {
      if (hideCOTS && wo.part_type && EXCLUDED_PART_TYPES.includes(wo.part_type)) {
        return false;
      }
      if (customerFilter && wo.customer_name !== customerFilter) {
        return false;
      }
      return true;
    });
  }, [workOrders, customerFilter, hideCOTS]);

  // The row being edited can disappear from the table — a refetch (someone else
  // completed the job), or one of the CLIENT-side filters (customer, hide-COTS)
  // excluding it with no refetch at all. Watch `filteredWorkOrders`, i.e. what the
  // table actually renders: watching `workOrders` missed the filter case entirely,
  // leaving the conflict dialog up and "Replace with mine" clickable for a row the
  // list was no longer showing. Derived from `workOrders`, so it is never
  // transiently empty mid-refetch, and the editor cannot open before the first load.
  //
  // A `warning` toast, not silence: the save SUCCEEDED at nothing, and a scheduler
  // who typed a reschedule and looked away otherwise gets no signal it was dropped.
  useEffect(() => {
    if (!dueDateEdit || savingDueDate) return;
    if (!filteredWorkOrders.some((wo) => wo.id === dueDateEdit.id)) {
      setDueDateEdit(null);
      setDueDateConflict(null);
      showToast('warning', 'The work order you were rescheduling left the list — the date was not saved.');
    }
  }, [filteredWorkOrders, dueDateEdit, savingDueDate, showToast]);

  const groupedWorkOrders = useMemo(() => {
    if (groupBy === 'none') return null;
    
    const groups: Record<string, WorkOrderSummary[]> = {};
    filteredWorkOrders.forEach(wo => {
      let key: string;
      switch (groupBy) {
        case 'customer':
          key = wo.customer_name || 'No Customer';
          break;
        case 'part':
          key = wo.part_number || (wo.work_order_type === 'laser_cutting' ? 'Nest Packages' : 'No Part');
          break;
        case 'status':
          key = wo.status;
          break;
        default:
          key = 'Unknown';
      }
      if (!groups[key]) groups[key] = [];
      groups[key].push(wo);
    });
    
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredWorkOrders, groupBy]);

  // Stats
  const stats = useMemo(() => {
    const overdue = filteredWorkOrders.filter(wo => 
      wo.due_date && isDateBeforeTodayInCentral(wo.due_date) && !TERMINAL_WO_STATUSES.includes(wo.status)
    ).length;
    const inProgress = filteredWorkOrders.filter(wo => wo.status === 'in_progress').length;
    const dueToday = filteredWorkOrders.filter(wo => Boolean(wo.due_date && isDateTodayInCentral(wo.due_date))).length;
    return { overdue, inProgress, dueToday };
  }, [filteredWorkOrders]);

  // The page header and the tab strip are built here, ABOVE the work-order
  // loading gate below, and rendered by every branch. A deep link to
  // ?tab=templates used to land on the full-page skeleton — which only the WORK
  // ORDER fetch ever clears — leaving the tab it asked for unreachable.
  const pageHeader = (
    <div className="page-header mb-0">
      <div className="min-w-0">
        <h1 className="page-title">Work Orders</h1>
        <p className="page-subtitle">Manage and track manufacturing orders</p>
      </div>
      <div className="page-actions w-full sm:w-auto" data-tour="wo-create">
        {canImportNests && (
          <Button
            variant="secondary"
            onClick={() => setNestWizardOpen(true)}
            className="w-full sm:w-auto flex items-center justify-center"
          >
            <ArrowUpTrayIcon className="h-5 w-5 mr-2 flex-shrink-0" />
            Import Nest Package
          </Button>
        )}
        {canEditWorkOrders && (
          <Button
            variant="secondary"
            onClick={() => setActiveTab('templates')}
            className="w-full sm:w-auto flex items-center justify-center"
          >
            <BookmarkSquareIcon className="h-5 w-5 mr-2 flex-shrink-0" aria-hidden="true" />
            New from template
          </Button>
        )}
        <Link to="/work-orders/new" className="btn-primary w-full sm:w-auto">
          <PlusIcon className="h-5 w-5 mr-2 flex-shrink-0" />
          New Work Order
        </Link>
      </div>
    </div>
  );

  // Hand-rolled rather than the <Tabs> primitive, matching the neighbouring
  // Inventory / Purchasing / Warehouse pages.
  const tabStrip = canEditWorkOrders ? (
    <div className="border-b border-slate-700">
      <nav className="-mb-px flex space-x-8">
        {([
          { id: 'orders' as const, label: 'Work Orders' },
          { id: 'templates' as const, label: 'Templates' },
        ]).map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            aria-current={activeTab === tab.id ? 'page' : undefined}
            className={`py-4 px-1 border-b-2 font-medium text-sm ${
              activeTab === tab.id
                ? 'border-werco-primary text-werco-primary'
                : 'border-transparent text-slate-400 hover:text-slate-300'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>
    </div>
  ) : null;

  // Templates is its own view over its own endpoint, and it returns BEFORE the
  // work-order loading gate: the catalog must not wait on (or be hidden by) a
  // fetch it does not use.
  if (activeTab === 'templates') {
    return (
      <div className="space-y-5 sm:space-y-6">
        {pageHeader}
        {tabStrip}
        <WorkOrderTemplatesPanel
          // A template's output is a DRAFT nobody has reviewed, so the hand-off
          // is the same as Duplicate's: land on the new work order.
          onUsed={(result) => navigate(`/work-orders/${result.work_order.id}`)}
        />
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-5 sm:space-y-6">
        {pageHeader}
        {tabStrip}

        {/* Stats skeleton */}
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <SkeletonCard key={i} className="h-24" />
          ))}
        </div>
        
        {/* Table skeleton */}
        <div className="card overflow-hidden">
          <SkeletonTable rows={8} columns={8} />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5 sm:space-y-6">
      {pageHeader}
      {tabStrip}

      {/* Quick Stats */}
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 gap-2">
        <MiniStat
          label="Overdue"
          value={stats.overdue}
          icon={ExclamationTriangleIcon}
          iconBg={stats.overdue > 0 ? 'bg-red-500/20' : 'bg-fd-green/15'}
          iconColor={stats.overdue > 0 ? 'text-red-500' : 'text-fd-green'}
          valueColor={stats.overdue > 0 ? 'text-red-500' : undefined}
        />
        <MiniStat
          label="In Progress"
          value={stats.inProgress}
          icon={Squares2X2Icon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
        />
        <MiniStat
          label="Due Today"
          value={stats.dueToday}
          icon={ClockIcon}
          iconBg={stats.dueToday > 0 ? 'bg-amber-500/20' : 'bg-slate-800/50'}
          iconColor={stats.dueToday > 0 ? 'text-fd-amber' : 'text-slate-400'}
          valueColor={stats.dueToday > 0 ? 'text-fd-amber' : undefined}
        />
      </MiniStatStrip>

      {/* Filters */}
      <div className="card rounded-sm border-fd-line p-2.5 sm:p-3" data-tour="wo-filters">
        <div className="grid grid-cols-1 xs:grid-cols-2 lg:grid-cols-[minmax(18rem,1fr)_11rem_13rem_11rem] gap-2 sm:gap-3">
          {/* Search */}
          <div className="relative min-w-0 xs:col-span-2 lg:col-span-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-4 top-1/2 transform -translate-y-1/2 text-surface-400" />
            <input
              type="text"
              placeholder="Search by WO#, unit #, part, or customer..."
              aria-label="Search work orders"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-11"
            />
          </div>

          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input px-3 text-sm sm:px-4 sm:text-base"
            aria-label="Status filter"
          >
            {statusOptions.map(option => (
              <option key={option.value || 'all'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          {/* Customer Filter */}
          <select
            value={customerFilter}
            onChange={(e) => setCustomerFilter(e.target.value)}
            className="input px-3 text-sm sm:px-4 sm:text-base"
            aria-label="Customer filter"
          >
            <option value="">All Customers</option>
            {/* A URL-borne customer no loaded row carries must still render as the
                selected option — otherwise the select shows blank while the filter
                quietly hides every row. */}
            {customerFilter && !customers.includes(customerFilter) && (
              <option value={customerFilter}>{customerFilter}</option>
            )}
            {customers.map(c => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          {/* Group By */}
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
            className="input px-3 text-sm sm:px-4 sm:text-base xs:col-span-2 lg:col-span-1"
            aria-label="Group work orders"
          >
            {groupOptions.map(option => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        {/* Toggle + count row */}
        <div className="flex flex-col xs:flex-row xs:items-center xs:justify-between gap-2 mt-2.5 pt-2.5 border-t border-fd-line">
          <label className="flex items-center gap-2 cursor-pointer group">
            <input
              type="checkbox"
              checked={hideCOTS}
              onChange={(e) => setHideCOTS(e.target.checked)}
              className="checkbox"
              aria-label="Hide COTS/Hardware"
            />
            <span className="text-sm text-surface-600 group-hover:text-surface-900">Hide COTS/Hardware</span>
          </label>
          <span className="text-xs text-surface-500 tabular-nums xs:text-right">
            <span className="sm:hidden">
              <span className="font-semibold text-surface-700">{filteredWorkOrders.length}</span> of {workOrders.length} shown
            </span>
            <span className="hidden sm:inline">
              Showing <span className="font-semibold text-surface-700">{filteredWorkOrders.length}</span> of {workOrders.length} work orders
            </span>
          </span>
        </div>
      </div>

      {/* Work Orders List */}
      {loadError && workOrders.length === 0 ? (
        <ErrorState
          message="Could not load work orders."
          onRetry={loadWorkOrders}
        />
      ) : groupBy !== 'none' && groupedWorkOrders ? (
        // Grouped View
        <div className="space-y-4" data-tour="wo-list">
          {groupedWorkOrders.map(([groupName, orders]) => (
            <div key={groupName} className="card card-flush overflow-hidden">
              <div className="bg-surface-50 px-4 py-3 sm:px-6 sm:py-4 border-b border-surface-200">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-surface-900 capitalize">
                    {groupBy === 'status' ? formatStatusLabel(groupName) : groupName}
                  </h3>
                  <span className="badge badge-neutral">
                    {orders.length} order{orders.length !== 1 ? 's' : ''}
                  </span>
                </div>
              </div>
              <div className="hidden lg:block">
                <DataTable
                  columns={buildWorkOrderColumns({
                    hideColumn: groupBy === 'customer' ? 'customer' : groupBy === 'part' ? 'part' : undefined,
                    onDelete: canDeleteWorkOrders ? handleDelete : undefined,
                    onDuplicate: canDuplicateWorkOrders ? handleDuplicate : undefined,
                    onSaveTemplate: canEditWorkOrders ? handleSaveTemplate : undefined,
                    onRelease: handleRelease,
                    releasingIds,
                    deletePending,
                    dueDateEdit: dueDateCellOptions,
                  })}
                  data={orders}
                  rowKey={(wo) => wo.id}
                  onRowClick={(wo) => navigate(`/work-orders/${wo.id}`)}
                  className="border-0"
                  csvExport={{ filename: `work-orders-${groupCsvSlug(groupName)}` }}
                />
              </div>
              <WorkOrderMobileList
                workOrders={orders}
                onDelete={canDeleteWorkOrders ? handleDelete : undefined}
                onDuplicate={canDuplicateWorkOrders ? handleDuplicate : undefined}
                onSaveTemplate={canEditWorkOrders ? handleSaveTemplate : undefined}
                onRelease={handleRelease}
                releasingIds={releasingIds}
                deletePending={deletePending}
                className="lg:hidden p-3"
              />
            </div>
          ))}
          {filteredWorkOrders.length === 0 && <WorkOrdersEmptyState />}
        </div>
      ) : (
        // Flat Responsive View
        <div data-tour="wo-list">
          {filteredWorkOrders.length === 0 ? (
            <WorkOrdersEmptyState />
          ) : (
            <>
              <div className="hidden lg:block">
                <DataTable
                  columns={workOrderColumns}
                  data={filteredWorkOrders}
                  rowKey={(wo) => wo.id}
                  onRowClick={(wo) => navigate(`/work-orders/${wo.id}`)}
                  defaultSort={{ key: 'priority', dir: 'asc' }}
                  pageSize={25}
                  csvExport={{ filename: 'work-orders' }}
                />
              </div>

              <WorkOrderMobileList
                workOrders={filteredWorkOrders}
                onDelete={canDeleteWorkOrders ? handleDelete : undefined}
                onDuplicate={canDuplicateWorkOrders ? handleDuplicate : undefined}
                onSaveTemplate={canEditWorkOrders ? handleSaveTemplate : undefined}
                onRelease={handleRelease}
                releasingIds={releasingIds}
                deletePending={deletePending}
                className="lg:hidden"
              />
            </>
          )}
        </div>
      )}

      {canImportNests && (
        <LaserNestImportWizard
          open={nestWizardOpen}
          onClose={() => setNestWizardOpen(false)}
          onImported={handleNestPackageImported}
        />
      )}

      {/* Duplicate a work order's plan onto a new draft. On success we navigate
          to the new WO rather than refreshing this list — a draft that nobody
          reviews is worse than no copy at all. */}
      {canDuplicateWorkOrders && (
        <DuplicateWorkOrderModal
          open={duplicateTarget !== null}
          workOrder={duplicateTarget}
          // Answer nest-ness from the row we already have wherever we can. Left
          // undefined the dialog probes with `getWorkOrder`, and that endpoint
          // is not a plain read: it runs the operation-quantity reconcile and
          // can COMMIT writes against the SOURCE work order. Nests only ever
          // land on a laser_cutting work order (the auto-created child, the
          // target when it is already laser, or a standalone nest WO), so every
          // other row is a `false` we can state outright — no round trip, and
          // no write fired just because a planner opened a dialog.
          hasLaserNests={duplicateTarget?.work_order_type === 'laser_cutting' ? undefined : false}
          onClose={() => setDuplicateTarget(null)}
          onDuplicated={(result) => navigate(`/work-orders/${result.work_order.id}`)}
        />
      )}

      {/* Save this work order's plan under a name. Writes ONE row and points it
          at the work order: nothing is copied now, nothing on the source
          changes, and there is nowhere to navigate — so it stays on this page
          and only nudges the catalog to re-read. */}
      {canEditWorkOrders && (
        <SaveAsTemplateModal
          open={saveTemplateTarget !== null}
          workOrder={saveTemplateTarget}
          // Answered from the row we already hold — nests only ever land on a
          // laser_cutting work order, and unlike Duplicate this dialog has no
          // reason to probe: `getWorkOrder` runs the operation-quantity
          // reconcile and can COMMIT writes against the very work order this
          // dialog promises not to touch.
          hasLaserNests={saveTemplateTarget?.work_order_type === 'laser_cutting'}
          onClose={() => setSaveTemplateTarget(null)}
          // Nothing to refresh here. This dialog renders only on the ORDERS tab and
          // the templates panel only on the TEMPLATES tab, so the two are never
          // co-mounted: switching tabs remounts the panel, which fetches on mount.
          onSaved={() => setSaveTemplateTarget(null)}
        />
      )}

      {/* Delete work order confirm (soft delete; optimistic removal fires on confirm) */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete Work Order"
        message={
          deleteTarget
            ? CURRENT_WORK_ORDER_STATUSES.includes(deleteTarget.status)
              ? `Delete current work order ${deleteTarget.work_order_number}?\n\nThis removes it from active lists, scheduling, and shop floor queues while preserving the record for audit/restore.`
              : `Delete work order ${deleteTarget.work_order_number}?\n\nThis removes it from active lists while preserving the record for audit/restore.`
            : ''
        }
        confirmLabel="Delete"
        pending={deletePending}
        variant="danger"
        onConfirm={handleConfirmDelete}
        onCancel={() => {
          if (!deletePending) setDeleteTarget(null);
        }}
      />

      {/* Lost-update guard for the inline due-date editor. This list refetches on a
          timer, on focus, and on every broadcast, so the optimistic-lock `version`
          refreshes under an open editor and its 409 would never fire — this is what
          turns a silently-overwritten reschedule into a decision. See handleDueDateSave. */}
      <ConfirmDialog
        open={dueDateConflict !== null}
        title="Due date changed by someone else"
        message={
          dueDateConflict
            ? `Someone else changed the due date on ${dueDateConflict.wo.work_order_number} to ` +
              `${dueDateConflict.serverStamp ? formatCentralDate(dueDateConflict.serverStamp) : 'no date'} ` +
              'while you were editing.\n\n' +
              'Saving replaces their date with yours. Your draft is kept either way — cancel ' +
              'to keep theirs.'
            : ''
        }
        confirmLabel="Replace with mine"
        cancelLabel="Keep theirs"
        pending={savingDueDate}
        variant="warning"
        onConfirm={handleDueDateConflictReplace}
        onCancel={() => {
          if (!savingDueDate) setDueDateConflict(null);
        }}
      />
    </div>
  );
}

function WorkOrdersEmptyState() {
  return (
    <EmptyState
      icon={ListBulletIcon}
      title="No work orders found"
      description="Try adjusting your filters, or create a new work order to get started."
      action={
        <Link to="/work-orders/new" className="btn-primary">
          <PlusIcon className="h-5 w-5 mr-2 flex-shrink-0" />
          New Work Order
        </Link>
      }
    />
  );
}

function isWorkOrderOverdue(wo: WorkOrderSummary) {
  return Boolean(
    wo.due_date &&
    isDateBeforeTodayInCentral(wo.due_date) &&
    !TERMINAL_WO_STATUSES.includes(wo.status)
  );
}

interface WorkOrderMobileListProps {
  workOrders: WorkOrderSummary[];
  onDelete?: (wo: WorkOrderSummary) => void;
  onDuplicate?: (wo: WorkOrderSummary) => void;
  onSaveTemplate?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  releasingIds?: Set<number>;
  deletePending?: boolean;
  className?: string;
}

const WorkOrderMobileList = React.memo(function WorkOrderMobileList({ workOrders, onDelete, onDuplicate, onSaveTemplate, onRelease, releasingIds, deletePending, className = '' }: WorkOrderMobileListProps) {
  if (workOrders.length === 0) return null;

  return (
    <div className={`space-y-3 ${className}`}>
      {workOrders.map((wo) => (
        <WorkOrderMobileCard
          key={wo.id}
          workOrder={wo}
          onDelete={onDelete}
          onDuplicate={onDuplicate}
          onSaveTemplate={onSaveTemplate}
          onRelease={onRelease}
          isReleasing={Boolean(releasingIds?.has(wo.id))}
          isDeleting={Boolean(deletePending)}
        />
      ))}
    </div>
  );
});

interface WorkOrderMobileCardProps {
  workOrder: WorkOrderSummary;
  onDelete?: (wo: WorkOrderSummary) => void;
  onDuplicate?: (wo: WorkOrderSummary) => void;
  onSaveTemplate?: (wo: WorkOrderSummary) => void;
  onRelease?: (wo: WorkOrderSummary) => void;
  isReleasing?: boolean;
  isDeleting?: boolean;
}

const WorkOrderMobileCard = React.memo(function WorkOrderMobileCard({ workOrder: wo, onDelete, onDuplicate, onSaveTemplate, onRelease, isReleasing, isDeleting }: WorkOrderMobileCardProps) {
  const priority = priorityConfig[wo.priority] || priorityConfig[4];
  const overdue = isWorkOrderOverdue(wo);
  const canRelease = onRelease && wo.status === 'draft';
  const canDelete = Boolean(onDelete);
  const canDuplicate = Boolean(onDuplicate);
  const canSaveTemplate = Boolean(onSaveTemplate);
  const progress = getWorkOrderProgress(wo);

  return (
    <article className={`mobile-card ${overdue ? 'border-red-500/50 bg-red-500/5' : ''}`}>
      <div className="px-4 py-3 border-b border-slate-700/50 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link
            to={`/work-orders/${wo.id}`}
            className="block font-semibold text-werco-400 hover:text-werco-300 truncate"
          >
            {wo.work_order_number}
          </Link>
          {wo.unit_number ? (
            <div className="mt-1">
              <UnitBadge unitNumber={wo.unit_number} size="sm" />
            </div>
          ) : null}
          <p className="text-sm text-surface-500 truncate mt-0.5">{wo.customer_name || 'No Customer'}</p>
        </div>
        <StatusBadge status={wo.status} className="flex-shrink-0" />
      </div>

      <div className="px-4 py-3 space-y-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-surface-900 truncate">
            {wo.part_number || (wo.work_order_type === 'laser_cutting' ? 'Nest package' : 'No part number')}
          </p>
          <p className="text-sm text-surface-500 line-clamp-2">
            {wo.part_name ||
              (wo.work_order_type === 'laser_cutting' && !wo.part_number ? 'Laser sheet runs' : 'No part description')}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <p className="text-xs uppercase tracking-wide text-surface-500">Due</p>
            <p className={`text-sm font-semibold mt-0.5 ${overdue ? 'text-red-400' : 'text-surface-800'}`}>
              {wo.due_date ? formatCentralDate(wo.due_date) : 'No date'}
            </p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wide text-surface-500">Priority</p>
            <span className={`badge mt-1 ${priority.bg} ${priority.text}`}>P{wo.priority}</span>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-wide text-surface-500">{progress.title}</p>
            <p className="text-sm font-semibold text-surface-800 tabular-nums">
              {progress.label}
            </p>
          </div>
          <div className="mt-2 h-2 bg-surface-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-werco-500 rounded-full transition-all"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
        </div>
      </div>

      <div className="px-4 py-3 bg-slate-800/50 border-t border-slate-700/50 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {overdue && <span className="badge badge-danger">Overdue</span>}
        </div>
        <div className="flex items-center gap-2">
          {canRelease && (
            <button
              onClick={() => onRelease?.(wo)}
              disabled={isReleasing}
              className="btn-success btn-sm"
            >
              <CheckCircleIcon className="h-4 w-4 mr-1" />
              Release
            </button>
          )}
          {canDuplicate && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onDuplicate?.(wo)}
              aria-label={`Duplicate ${wo.work_order_number}`}
            >
              <DocumentDuplicateIcon className="h-4 w-4 mr-1" aria-hidden="true" />
              Duplicate
            </Button>
          )}
          {canSaveTemplate && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onSaveTemplate?.(wo)}
              aria-label={`Save ${wo.work_order_number} as a template`}
            >
              <BookmarkSquareIcon className="h-4 w-4 mr-1" aria-hidden="true" />
              Template
            </Button>
          )}
          {canDelete && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onDelete?.(wo)}
              disabled={isDeleting}
              className="text-red-300 hover:text-red-200"
            >
              <TrashIcon className="h-4 w-4 mr-1" />
              Delete
            </Button>
          )}
          <Link
            to={`/work-orders/${wo.id}`}
            className="btn-secondary btn-sm"
          >
            Details
            <ChevronRightIcon className="h-4 w-4 ml-1" />
          </Link>
        </div>
      </div>
    </article>
  );
});

