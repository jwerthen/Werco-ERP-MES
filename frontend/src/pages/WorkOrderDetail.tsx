import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { User, WorkOrder, WorkOrderOperation, LaserNestInfo } from '../types';
import { WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerSeverity } from '../types/aiForward';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import LaserNestManualModal from '../components/laser/LaserNestManualModal';
import LaserNestImportWizard from '../components/laser/LaserNestImportWizard';
import LaserNestPdfPreview from '../components/laser/LaserNestPdfPreview';
import { CompleteWorkModal, CompleteWorkSubmit } from '../components/workorders/CompleteWorkModal';
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
import { EmptyState, ErrorState, useToast, statusColor, Button } from '../components/ui';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
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
  CalendarDaysIcon,
  FlagIcon,
  BuildingOffice2Icon,
  HashtagIcon,
  ClockIcon,
  ChartBarIcon,
  ClipboardDocumentCheckIcon,
  UserGroupIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';

const CURRENT_WORK_ORDER_STATUSES = ['released', 'in_progress', 'on_hold'];

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
  // Manual laser-nest manage actions are limited to admin/manager/supervisor —
  // the same trio the backend RBAC allows (routings:create maps to exactly that
  // set plus platform_admin).
  const canManageNests = hasPermission(user?.role, 'routings:create');
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [completingOpId, setCompletingOpId] = useState<number | null>(null);
  // Which operation's read-only "Process steps" evidence panel is expanded
  // (one at a time — it fetches the steps view on open).
  const [stepsOpenOpId, setStepsOpenOpId] = useState<number | null>(null);
  // Drives the CompleteWorkModal: either the work-order-level completion or a
  // specific operation. `null` = closed. The modal collects qty complete + qty
  // scrapped + a scrap reason (required when scrap > 0) before we call the API.
  const [completeTarget, setCompleteTarget] = useState<
    { kind: 'work_order' } | { kind: 'operation'; operation: WorkOrderOperation } | null
  >(null);
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
  const [userNameById, setUserNameById] = useState<Record<number, string>>({});
  const [activeUsersOnWorkOrder, setActiveUsersOnWorkOrder] = useState<ActiveShopUser[]>([]);
  // Batch ZIP import runs through the LaserNestImportWizard modal.
  const [nestImportWizardOpen, setNestImportWizardOpen] = useState(false);
  // Manual nest entry + per-nest PDF management.
  const [nestModalOpen, setNestModalOpen] = useState(false);
  const [nestModalTarget, setNestModalTarget] = useState<LaserNestInfo | null>(null);
  const [previewNestId, setPreviewNestId] = useState<number | null>(null);
  const [nestActionId, setNestActionId] = useState<number | null>(null);
  const [nestActionError, setNestActionError] = useState('');
  const nestAttachInputRef = useRef<HTMLInputElement | null>(null);
  const [nestAttachTargetId, setNestAttachTargetId] = useState<number | null>(null);
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

  const handleDelete = async () => {
    if (!workOrder) return;
    const isCurrent = CURRENT_WORK_ORDER_STATUSES.includes(workOrder.status);
    const message = isCurrent
      ? `Delete current work order ${workOrder.work_order_number}?\n\nThis removes it from active lists, scheduling, and shop floor queues while preserving the record for audit/restore.`
      : `Delete work order ${workOrder.work_order_number}?\n\nThis removes it from active lists while preserving the record for audit/restore.`;
    if (!window.confirm(message)) return;

    setDeleting(true);
    try {
      await api.deleteWorkOrder(workOrder.id);
      navigate('/work-orders');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete work order');
      setDeleting(false);
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
    const { quantityComplete, quantityScrapped, scrapReason } = values;
    if (completeTarget.kind === 'work_order') {
      setCompleting(true);
      try {
        const completeRes: unknown = await api.completeWorkOrder(
          workOrder!.id,
          quantityComplete,
          quantityScrapped,
          scrapReason
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

  // Called by the import wizard after a successful import. The wizard owns the
  // pick → preview → review → import flow; here we just close it and route to
  // the freshly-created child laser WO (or refresh in place if none came back).
  const handleNestPackageImported = (childWorkOrderId?: number) => {
    setNestImportWizardOpen(false);
    if (childWorkOrderId) {
      navigate(`/work-orders/${childWorkOrderId}`);
    } else {
      loadWorkOrder();
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

  const handleResolveBlocker = async (blocker: WorkOrderBlocker) => {
    const note = prompt(`Resolve blocker "${blocker.title}"?`, 'Resolved');
    if (note === null) return;
    setResolvingBlockerId(blocker.id);
    try {
      await api.resolveWorkOrderBlocker(blocker.id, note.trim() || undefined);
      await loadWorkOrder();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to resolve blocker');
    } finally {
      setResolvingBlockerId(null);
    }
  };

  // --- Manual laser nest handlers -----------------------------------------
  const openAddNestModal = () => {
    setNestModalTarget(null);
    setNestActionError('');
    setNestModalOpen(true);
  };

  const openEditNestModal = (nest: LaserNestInfo) => {
    setNestModalTarget(nest);
    setNestActionError('');
    setNestModalOpen(true);
  };

  // The modal calls this on every successful save. On a partial create (nest
  // persisted but its PDF failed to attach) it passes a non-fatal warning we
  // surface in the nest-action banner so the operator knows to retry via the
  // per-nest "Attach PDF" action.
  const handleNestSaved = async (warning?: string) => {
    setNestActionError(warning || '');
    await loadWorkOrder();
  };

  const handleDeleteNest = async (nest: LaserNestInfo) => {
    if (!window.confirm(`Delete laser nest ${nest.cnc_number || nest.nest_name}? This puts its operation on hold.`)) {
      return;
    }
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
  // thickness, sheet, runs, PDF actions) only for non-laser_cutting WOs. When it
  // is present we de-dup: the Operations table cell collapses to a compact
  // identifier + cross-link to the panel by stable nest id, rather than
  // repeating the same fields. For laser_cutting WOs (no package card) the table
  // cell keeps the full inline detail since there's nowhere else to show it.
  const nestPanelShown = workOrder.work_order_type !== 'laser_cutting' && laserNests.length > 0;

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
            <h1 className="text-2xl font-bold text-white">{workOrder.work_order_number}</h1>
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
          <Button
            variant="secondary"
            onClick={() => window.open(`/print/traveler/${workOrder.id}?autoprint=1`, '_blank')}
            className="flex items-center"
          >
            <PrinterIcon className="h-5 w-5 mr-2" />
            Print Traveler
          </Button>
          {isAdminView && (
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
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
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
          value={workOrder.due_date ? formatCentralDate(workOrder.due_date) : '-'}
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

      {/* Notes & Instructions — folded into a compact panel */}
      <CockpitPanel title="Notes & Instructions" bodyClassName="space-y-3 text-sm">
        <div>
          <p className="text-xs uppercase tracking-wide text-slate-400">Notes</p>
          <p className="mt-1 text-fd-body">{workOrder.notes || 'No notes'}</p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-wide text-slate-400">Special Instructions</p>
          <p className="mt-1 text-fd-body">{workOrder.special_instructions || 'No special instructions'}</p>
        </div>
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

      {workOrder.work_order_type !== 'laser_cutting' && (
        <div className="card card-compact">
          <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-3 mb-3">
            <div className="flex items-start gap-3 min-w-0">
              <ArrowUpTrayIcon className="h-5 w-5 text-fd-red mt-0.5 flex-shrink-0" />
              <div className="min-w-0">
                <h2 className="card-title">Laser Nest Package</h2>
                <p className="card-subtitle truncate">
                  Import a zipped Ermaksan folder or a server folder path to create the linked laser cutting work order.
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
                          </div>
                        </div>

                        {canManageNests && (
                          <div className="flex flex-wrap items-center gap-1.5">
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
                              onClick={() => openEditNestModal(nest)}
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
                    {(blocker.status === 'open' || blocker.status === 'acknowledged') && (
                      <button
                        onClick={() => handleResolveBlocker(blocker)}
                        disabled={resolvingBlockerId === blocker.id}
                        className="btn-success btn-sm"
                      >
                        {resolvingBlockerId === blocker.id ? 'Resolving...' : 'Resolve'}
                      </button>
                    )}
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
                    {op.operation_number || `Op ${op.sequence}`} - {op.name}
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
        <h2 className="card-title mb-3">Operations / Routing</h2>

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
                    const operationTarget = Number(op.laser_nest?.planned_runs || op.component_quantity || workOrder.quantity_ordered || 0);
                    
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
                            <button
                              type="button"
                              onClick={() => setStepsOpenOpId(stepsOpenOpId === op.id ? null : op.id)}
                              aria-expanded={stepsOpenOpId === op.id}
                              className="text-fd-cyan hover:text-cyan-300 text-sm font-medium"
                              title="Process steps evidence"
                            >
                              <ClipboardDocumentCheckIcon className="h-5 w-5 inline" /> Steps
                            </button>
                            {op.status !== 'complete' && workOrder.status !== 'draft' && (
                              <button
                                onClick={() => handleCompleteOperation(op)}
                                disabled={completingOpId === op.id}
                                className="text-green-600 hover:text-green-300 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                                title="Complete Operation"
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

      {materialReqs && !materialReqs.has_bom && (
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
    </div>
  );
}
