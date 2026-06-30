import React, { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import {
  TruckIcon,
  MagnifyingGlassIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  ClipboardDocumentCheckIcon,
  XMarkIcon,
  InboxArrowDownIcon,
  DocumentCheckIcon,
  ClockIcon,
  PrinterIcon,
} from '@heroicons/react/24/outline';
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
  statusColor,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';

interface POLine {
  line_id: number;
  line_number: number;
  part_id: number;
  part_number: string;
  part_name: string;
  quantity_ordered: number;
  quantity_received: number;
  quantity_remaining: number;
  unit_price: number;
  required_date: string | null;
  is_closed?: boolean;
  receipts?: {
    receipt_id: number;
    receipt_number: string;
    quantity_received: number;
    lot_number: string;
    status: string;
    received_at: string;
  }[];
}

interface PurchaseOrder {
  po_id: number;
  po_number: string;
  vendor_id: number;
  vendor_name: string;
  vendor_code: string;
  is_approved_vendor?: boolean;
  order_date: string | null;
  required_date: string | null;
  expected_date: string | null;
  status: string;
  notes?: string;
  lines: POLine[];
  total_lines: number;
}

interface Location {
  id: number;
  code: string;
  name: string;
}

interface ReceiveFormData {
  po_line_id: number;
  quantity_received: number;
  lot_number: string;
  serial_numbers: string;
  heat_number: string;
  cert_number: string;
  coc_attached: boolean;
  location_id: number | null;
  requires_inspection: boolean;
  packing_slip_number: string;
  carrier: string;
  tracking_number: string;
  notes: string;
  over_receive_approved: boolean;
}

interface InspectionQueueItem {
  receipt_id: number;
  receipt_number: string;
  po_number: string;
  vendor_name: string;
  part_number: string;
  part_name: string;
  quantity_received: number;
  lot_number: string;
  coc_attached: boolean;
  received_at: string;
  days_pending: number;
}

interface HistoryItem {
  receipt_id: number;
  receipt_number: string;
  po_number: string;
  part_number: string;
  quantity_received: number;
  quantity_accepted: number;
  quantity_rejected: number;
  lot_number: string;
  inspection_status?: string;
  status: string;
  received_at: string;
}

type TabType = 'receive' | 'queue' | 'history';

const QUEUE_DAYS_BADGE: Record<string, string> = {
  urgent: 'bg-red-500/20 text-red-300',
  warn: 'bg-amber-500/20 text-amber-300',
  ok: 'bg-green-500/20 text-emerald-300',
};

export default function ReceivingPage({ embedded }: { embedded?: boolean }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>(() => {
    const tab = searchParams.get('tab');
    return (tab === 'queue' || tab === 'history') ? tab : 'receive';
  });
  
  const { showToast } = useToast();

  const [openPOs, setOpenPOs] = useState<PurchaseOrder[]>([]);
  const [selectedPO, setSelectedPO] = useState<PurchaseOrder | null>(null);
  const [selectedLine, setSelectedLine] = useState<POLine | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [stats, setStats] = useState<any>(null);

  const [inspectionQueue, setInspectionQueue] = useState<InspectionQueueItem[]>([]);
  const [queueError, setQueueError] = useState(false);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [historyError, setHistoryError] = useState(false);
  
  const [showReceiveModal, setShowReceiveModal] = useState(false);
  const [showInspectModal, setShowInspectModal] = useState(false);
  const [selectedReceipt, setSelectedReceipt] = useState<any>(null);
  const [receiptDetail, setReceiptDetail] = useState<any>(null);
  
  const [formData, setFormData] = useState<ReceiveFormData>({
    po_line_id: 0,
    quantity_received: 0,
    lot_number: '',
    serial_numbers: '',
    heat_number: '',
    cert_number: '',
    coc_attached: false,
    location_id: null,
    requires_inspection: true,
    packing_slip_number: '',
    carrier: '',
    tracking_number: '',
    notes: '',
    over_receive_approved: false,
  });

  const [inspectionData, setInspectionData] = useState({
    quantity_accepted: 0,
    quantity_rejected: 0,
    inspection_method: 'visual',
    defect_type: '',
    inspection_notes: '',
  });

  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  // Receipt id offered for a one-click "Print label" on the success toast right
  // after a receipt is created.
  const [lastReceiptId, setLastReceiptId] = useState<number | null>(null);
  // Receipt currently being sent to the printer (disables that row's button).
  const [printingReceiptId, setPrintingReceiptId] = useState<number | null>(null);

  const { user } = useAuth();
  // Label printing is gated to the same roles that can receive (ADMIN / MANAGER
  // / SUPERVISOR) so the UI matches what the backend will allow.
  const canPrintLabel = !!user && ['admin', 'manager', 'supervisor'].includes(user.role);

  const isPartialLine = (line: POLine) => (
    !line.is_closed && line.quantity_received > 0 && line.quantity_remaining > 0
  );

  const applyReceiptToPO = (po: PurchaseOrder, lineId: number, qtyReceived: number): PurchaseOrder => {
    const updatedLines = po.lines.map((line) => {
      if (line.line_id !== lineId) {
        return line;
      }
      const newReceived = line.quantity_received + qtyReceived;
      const newRemaining = line.quantity_ordered - newReceived;
      return {
        ...line,
        quantity_received: newReceived,
        quantity_remaining: newRemaining,
        is_closed: line.is_closed || newRemaining <= 0,
      };
    });
    return {
      ...po,
      lines: updatedLines,
    };
  };

  const refreshSelectedPO = async (poId: number) => {
    try {
      const fullPO = await api.getPOForReceiving(poId);
      setSelectedPO(fullPO);
    } catch (err) {
      console.error('Failed to refresh PO details:', err);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    if (activeTab === 'queue') {
      loadInspectionQueue();
    } else if (activeTab === 'history') {
      loadHistory();
    }
  }, [activeTab]);

  const loadData = async () => {
    setLoadError(false);
    try {
      const [posRes, locsRes, statsRes] = await Promise.all([
        api.getOpenPOsForReceiving(),
        api.getReceivingLocations(),
        api.getReceivingStats(30)
      ]);
      setOpenPOs(posRes);
      setLocations(locsRes);
      setStats(statsRes);
    } catch (err) {
      console.error('Failed to load receiving data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const loadInspectionQueue = async () => {
    setQueueError(false);
    try {
      const data = await api.getInspectionQueue(30);
      setInspectionQueue(data);
    } catch (err) {
      console.error('Failed to load inspection queue:', err);
      setQueueError(true);
    }
  };

  const loadHistory = async () => {
    setHistoryError(false);
    try {
      const data = await api.getReceivingHistory(30);
      setHistory(data);
    } catch (err) {
      console.error('Failed to load history:', err);
      setHistoryError(true);
    }
  };

  const handleSelectPO = async (po: PurchaseOrder) => {
    try {
      const fullPO = await api.getPOForReceiving(po.po_id);
      setSelectedPO(fullPO);
    } catch (err) {
      console.error('Failed to load PO details:', err);
      showToast('error', 'Failed to load PO details');
    }
  };

  const handleSelectLine = (line: POLine) => {
    setSelectedLine(line);
    setFormData({
      ...formData,
      po_line_id: line.line_id,
      quantity_received: line.quantity_remaining,
    });
    setShowReceiveModal(true);
  };

  const handleReceive = async () => {
    setError('');
    
    if (!formData.lot_number.trim()) {
      setError('Lot number is required for AS9100D traceability');
      return;
    }
    
    if (formData.quantity_received <= 0) {
      setError('Quantity must be greater than 0');
      return;
    }

    if (selectedLine && formData.quantity_received > selectedLine.quantity_remaining && !formData.over_receive_approved) {
      setError(`Quantity exceeds remaining (${selectedLine.quantity_remaining}). Check "Approve Over-Receipt" to proceed.`);
      return;
    }

    const receivedQty = formData.quantity_received;
    const selectedLineId = selectedLine?.line_id;
    const selectedPOId = selectedPO?.po_id;

    try {
      const receipt = await api.receiveNewMaterial({
        ...formData,
        location_id: formData.location_id || undefined,
      });
      setSuccess('Material received successfully');
      // Offer a one-click label print/reprint on the success toast.
      setLastReceiptId(receipt?.id ?? null);
      if (selectedPO && selectedLine && selectedLineId !== undefined) {
        setSelectedPO((prev) => (prev ? applyReceiptToPO(prev, selectedLineId, receivedQty) : prev));
      }
      setShowReceiveModal(false);
      setSelectedLine(null);
      loadData();

      if (selectedPOId !== undefined) {
        refreshSelectedPO(selectedPOId);
      }

      if (formData.requires_inspection) {
        loadInspectionQueue();
      }

      // Keep the toast (and its Print label action) up a touch longer.
      setTimeout(() => { setSuccess(''); setLastReceiptId(null); }, 8000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to receive material');
    }
  };

  // Manually (re)print the 4x6 thermal receiving label for a receipt. Surfaces
  // the server message on success, and gives a friendly hint when the print
  // egress kill switch is OFF (HTTP 409) so an admin knows where to enable it.
  const handlePrintLabel = async (receiptId: number) => {
    if (!receiptId) return;
    setError('');
    setPrintingReceiptId(receiptId);
    try {
      const result = await api.printReceiptLabel(receiptId);
      setSuccess(result?.message || 'Label sent to printer');
      setTimeout(() => setSuccess(''), 3000);
    } catch (err: any) {
      const status = err.response?.status;
      if (status === 409) {
        setError("Label printing isn't enabled — an admin can configure it in print settings.");
      } else {
        setError(err.response?.data?.detail || 'Failed to print label');
      }
    } finally {
      setPrintingReceiptId(null);
    }
  };

  const handleOpenInspection = async (receipt: InspectionQueueItem) => {
    try {
      const detail = await api.getReceiptDetail(receipt.receipt_id);
      setReceiptDetail(detail);
      setSelectedReceipt(receipt);
      setInspectionData({
        quantity_accepted: receipt.quantity_received,
        quantity_rejected: 0,
        inspection_method: 'visual',
        defect_type: '',
        inspection_notes: '',
      });
      setShowInspectModal(true);
    } catch (err) {
      console.error('Failed to load receipt details:', err);
      showToast('error', 'Failed to load receipt details');
    }
  };

  const handleInspect = async () => {
    setError('');
    
    const total = inspectionData.quantity_accepted + inspectionData.quantity_rejected;
    if (total > (selectedReceipt?.quantity_received || 0)) {
      setError('Total cannot exceed received quantity');
      return;
    }
    
    if (inspectionData.quantity_rejected > 0 && !inspectionData.defect_type) {
      setError('Defect type is required when rejecting material');
      return;
    }
    
    if (inspectionData.quantity_rejected > 0 && !inspectionData.inspection_notes) {
      setError('Notes are required when rejecting material');
      return;
    }

    try {
      const result = await api.inspectReceiptNew(selectedReceipt.receipt_id, {
        ...inspectionData,
        defect_type: inspectionData.quantity_rejected > 0 ? inspectionData.defect_type : undefined,
      });
      
      let message = 'Inspection completed';
      if (result.inventory_created) {
        message += ` - ${inspectionData.quantity_accepted} added to inventory`;
      }
      if (result.ncr_created) {
        message += ` - NCR ${result.ncr_number} created for ${inspectionData.quantity_rejected} rejected`;
      }
      
      setSuccess(message);
      setShowInspectModal(false);
      setSelectedReceipt(null);
      setReceiptDetail(null);
      loadInspectionQueue();
      loadData();
      
      setTimeout(() => setSuccess(''), 5000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to complete inspection');
    }
  };

  const defectTypes = [
    { value: 'dimensional', label: 'Dimensional' },
    { value: 'cosmetic', label: 'Cosmetic' },
    { value: 'material', label: 'Material' },
    { value: 'documentation', label: 'Documentation' },
    { value: 'functional', label: 'Functional' },
    { value: 'contamination', label: 'Contamination' },
    { value: 'packaging', label: 'Packaging' },
    { value: 'other', label: 'Other' },
  ];

  const inspectionMethods = [
    { value: 'visual', label: 'Visual Inspection' },
    { value: 'dimensional', label: 'Dimensional Measurement' },
    { value: 'functional', label: 'Functional Test' },
    { value: 'documentation_review', label: 'Documentation Review' },
    { value: 'sampling', label: 'Sampling' },
    { value: 'non_destructive', label: 'Non-Destructive Testing' },
    { value: 'destructive', label: 'Destructive Testing' },
  ];

  const renderDaysBadge = (days: number) => {
    const tone = days > 3 ? 'urgent' : days > 1 ? 'warn' : 'ok';
    return (
      <span className={`px-2 py-1 rounded text-xs font-medium ${QUEUE_DAYS_BADGE[tone]}`}>
        {days}d
      </span>
    );
  };

  const queueColumns = useMemo<Array<DataTableColumn<InspectionQueueItem>>>(() => [
    {
      key: 'receipt_number',
      header: 'Receipt',
      sortable: true,
      className: 'font-mono',
      accessor: (item) => item.receipt_number,
    },
    {
      key: 'po_number',
      header: 'PO / Vendor',
      sortable: true,
      accessor: (item) => item.po_number,
      csv: (item) => `${item.po_number} / ${item.vendor_name}`,
      render: (item) => (
        <div>
          <p className="font-medium">{item.po_number}</p>
          <p className="text-xs text-slate-400">{item.vendor_name}</p>
        </div>
      ),
    },
    {
      key: 'part_number',
      header: 'Part',
      sortable: true,
      accessor: (item) => item.part_number,
      csv: (item) => `${item.part_number} ${item.part_name}`.trim(),
      render: (item) => (
        <div>
          <p className="font-mono">{item.part_number}</p>
          <p className="text-xs text-slate-400 truncate max-w-[200px]">{item.part_name}</p>
        </div>
      ),
    },
    {
      key: 'quantity_received',
      header: 'Qty',
      sortable: true,
      align: 'right',
      className: 'font-medium',
      accessor: (item) => item.quantity_received,
    },
    {
      key: 'lot_number',
      header: 'Lot #',
      sortable: true,
      className: 'font-mono',
      accessor: (item) => item.lot_number,
    },
    {
      key: 'coc_attached',
      header: 'CoC',
      align: 'center',
      accessor: (item) => (item.coc_attached ? 'Yes' : 'No'),
      render: (item) =>
        item.coc_attached ? (
          <CheckCircleIcon className="h-5 w-5 text-green-500 mx-auto" />
        ) : (
          <span className="text-gray-300">-</span>
        ),
    },
    {
      key: 'received_at',
      header: 'Received',
      sortable: true,
      accessor: (item) => item.received_at,
      render: (item) =>
        formatCentralDateTime(item.received_at, {
          year: undefined,
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        }),
    },
    {
      key: 'days_pending',
      header: 'Days',
      sortable: true,
      align: 'center',
      accessor: (item) => item.days_pending,
      render: (item) => renderDaysBadge(item.days_pending),
    },
    {
      key: 'actions',
      header: 'Action',
      align: 'right',
      render: (item) => (
        <div className="flex items-center justify-end gap-2">
          {canPrintLabel && (
            <button
              onClick={(e) => { e.stopPropagation(); handlePrintLabel(item.receipt_id); }}
              disabled={printingReceiptId === item.receipt_id}
              title="Print 4×6 receiving label"
              className="inline-flex items-center gap-1.5 border border-slate-600 text-slate-200 hover:border-werco-primary hover:text-werco-primary text-sm px-3 py-1 transition-colors disabled:opacity-50"
            >
              <PrinterIcon className="h-4 w-4" />
              {printingReceiptId === item.receipt_id ? 'Printing…' : 'Label'}
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); handleOpenInspection(item); }}
            className="btn-primary text-sm px-3 py-1"
          >
            Inspect
          </button>
        </div>
      ),
    },
  ], [canPrintLabel, printingReceiptId]);

  const renderQueueCard = (item: InspectionQueueItem) => (
    <MobileDataCard
      key={item.receipt_id}
      title={item.receipt_number}
      subtitle={`${item.po_number} · ${item.vendor_name}`}
      badge={renderDaysBadge(item.days_pending)}
      highlight={item.days_pending > 3}
      fields={[
        { label: 'Part', value: item.part_number, fullWidth: true },
        { label: 'Qty', value: item.quantity_received },
        { label: 'Lot #', value: <span className="font-mono">{item.lot_number}</span> },
        {
          label: 'CoC',
          value: item.coc_attached ? (
            <CheckCircleIcon className="h-5 w-5 text-green-500" />
          ) : (
            <span className="text-gray-300">-</span>
          ),
        },
        {
          label: 'Received',
          value: formatCentralDateTime(item.received_at, {
            year: undefined,
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          }),
        },
      ]}
      actions={
        <>
          {canPrintLabel && (
            <button
              onClick={() => handlePrintLabel(item.receipt_id)}
              disabled={printingReceiptId === item.receipt_id}
              className="inline-flex items-center gap-1.5 border border-slate-600 text-slate-200 hover:border-werco-primary hover:text-werco-primary text-sm px-3 py-1 transition-colors disabled:opacity-50"
            >
              <PrinterIcon className="h-4 w-4" />
              {printingReceiptId === item.receipt_id ? 'Printing…' : 'Label'}
            </button>
          )}
          <button
            onClick={() => handleOpenInspection(item)}
            className="btn-primary text-sm px-3 py-1"
          >
            Inspect
          </button>
        </>
      }
    />
  );

  const historyColumns = useMemo<Array<DataTableColumn<HistoryItem>>>(() => [
    {
      key: 'receipt_number',
      header: 'Receipt',
      sortable: true,
      className: 'font-mono',
      accessor: (item) => item.receipt_number,
    },
    {
      key: 'po_number',
      header: 'PO',
      sortable: true,
      accessor: (item) => item.po_number,
    },
    {
      key: 'part_number',
      header: 'Part',
      sortable: true,
      className: 'font-mono',
      accessor: (item) => item.part_number,
    },
    {
      key: 'quantity_received',
      header: "Recv'd",
      sortable: true,
      align: 'right',
      accessor: (item) => item.quantity_received,
    },
    {
      key: 'quantity_accepted',
      header: 'Accepted',
      sortable: true,
      align: 'right',
      className: 'text-green-600',
      accessor: (item) => item.quantity_accepted,
    },
    {
      key: 'quantity_rejected',
      header: 'Rejected',
      sortable: true,
      align: 'right',
      className: 'text-red-600',
      accessor: (item) => item.quantity_rejected ?? 0,
      render: (item) => item.quantity_rejected || '-',
    },
    {
      key: 'lot_number',
      header: 'Lot #',
      sortable: true,
      className: 'font-mono',
      accessor: (item) => item.lot_number,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      align: 'center',
      accessor: (item) => item.inspection_status || item.status,
      render: (item) => (
        <StatusBadge status={item.inspection_status || item.status} />
      ),
    },
    {
      key: 'received_at',
      header: 'Date',
      sortable: true,
      accessor: (item) => item.received_at,
      render: (item) => formatCentralDate(item.received_at),
    },
  ], []);

  const renderHistoryCard = (item: HistoryItem) => (
    <MobileDataCard
      key={item.receipt_id}
      title={item.receipt_number}
      subtitle={`${item.po_number} · ${item.part_number}`}
      badge={
        <StatusBadge status={item.inspection_status || item.status} />
      }
      fields={[
        { label: "Recv'd", value: item.quantity_received },
        { label: 'Lot #', value: <span className="font-mono">{item.lot_number}</span> },
        { label: 'Accepted', value: <span className="text-green-600">{item.quantity_accepted}</span> },
        { label: 'Rejected', value: <span className="text-red-600">{item.quantity_rejected || '-'}</span> },
        { label: 'Date', value: formatCentralDate(item.received_at), fullWidth: true },
      ]}
    />
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-4 border-werco-primary border-t-transparent"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      {!embedded && (
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-3xl font-bold text-white">Receiving & Inspection</h1>
            <p className="text-slate-400 mt-1">AS9100D compliant receiving and inspection workflow</p>
          </div>
        </div>
      )}

      {/* Success/Error Messages */}
      {success && (
        <div className="bg-green-500/10 border border-green-500/30 rounded-xl p-4 flex items-center gap-3">
          <CheckCircleIcon className="h-5 w-5 text-green-600" />
          <span className="text-emerald-300">{success}</span>
          {canPrintLabel && lastReceiptId !== null && (
            <button
              onClick={() => handlePrintLabel(lastReceiptId)}
              disabled={printingReceiptId === lastReceiptId}
              className="ml-auto inline-flex items-center gap-1.5 border border-werco-primary/60 text-werco-primary hover:bg-werco-primary/10 text-sm font-medium px-3 py-1 transition-colors disabled:opacity-50"
            >
              <PrinterIcon className="h-4 w-4" />
              {printingReceiptId === lastReceiptId ? 'Printing…' : 'Print label'}
            </button>
          )}
        </div>
      )}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
          <span className="text-red-300">{error}</span>
          <button onClick={() => setError('')} className="ml-auto">
            <XMarkIcon className="h-5 w-5 text-red-600" />
          </button>
        </div>
      )}

      {/* Stats Cards */}
      {stats && (
        <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          <MiniStat
            icon={ClockIcon}
            iconBg="bg-fd-amber/15"
            iconColor="text-fd-amber"
            label="Pending Inspection"
            value={stats.pending_inspection}
            valueColor="text-fd-amber"
          />
          <MiniStat
            icon={InboxArrowDownIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Received (30d)"
            value={stats.receipts_in_period}
            valueColor="text-fd-blue"
          />
          <MiniStat
            icon={DocumentCheckIcon}
            iconBg="bg-fd-green/15"
            iconColor="text-fd-green"
            label="Acceptance Rate"
            value={`${stats.acceptance_rate}%`}
            valueColor="text-fd-green"
          />
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg="bg-fd-red/15"
            iconColor="text-fd-red"
            label="Rejections (30d)"
            value={stats.rejections_in_period}
            valueColor="text-fd-red"
          />
        </MiniStatStrip>
      )}

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="-mb-px flex space-x-8">
          {[
            { id: 'receive', label: 'Receive Material', icon: TruckIcon },
            { id: 'queue', label: 'Inspection Queue', icon: ClipboardDocumentCheckIcon, count: stats?.pending_inspection },
            { id: 'history', label: 'History', icon: ClockIcon },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => {
                setActiveTab(tab.id as TabType);
                setSearchParams({ tab: tab.id });
              }}
              className={`flex items-center gap-2 py-4 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-slate-400 hover:text-slate-300'
              }`}
            >
              <tab.icon className="h-5 w-5" />
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span className="ml-1 px-2 py-0.5 rounded-full text-xs bg-amber-500/20 text-amber-300">
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <div className="card">
        {/* RECEIVE TAB */}
        {activeTab === 'receive' && (
          <div className="space-y-4">
            {/* PO Selection Row */}
            <div className="grid grid-cols-3 gap-4" style={{ minHeight: '500px' }}>
              <div className="col-span-1 flex flex-col">
                <h2 className="text-lg font-semibold mb-3">Open Purchase Orders</h2>
                <div className="space-y-2 flex-1 overflow-y-auto pr-2">
                  {loadError ? (
                    <ErrorState
                      message="Could not load open purchase orders."
                      onRetry={loadData}
                    />
                  ) : openPOs.length === 0 ? (
                    <EmptyState
                      icon={InboxArrowDownIcon}
                      title="No open POs"
                      description="Open purchase orders awaiting receipt will appear here."
                    />
                  ) : (
                    openPOs.map((po) => (
                      <button
                        type="button"
                        key={po.po_id}
                        onClick={() => handleSelectPO(po)}
                        className={`w-full text-left p-3 rounded-xl border-2 cursor-pointer transition-all ${
                          selectedPO?.po_id === po.po_id
                            ? 'border-werco-primary bg-werco-500/10'
                            : 'border-slate-700 hover:border-slate-600 hover:bg-slate-800'
                        }`}
                      >
                        <div className="flex justify-between items-start">
                          <div>
                            <p className="font-semibold text-werco-primary">{po.po_number}</p>
                            <p className="text-sm text-slate-400">{po.vendor_name}</p>
                          </div>
                          <span className="px-2 py-1 rounded text-xs font-medium bg-blue-500/20 text-blue-300">
                            {po.total_lines} line{po.total_lines !== 1 ? 's' : ''}
                          </span>
                        </div>
                        {po.required_date && (
                          <p className="text-xs text-slate-400 mt-1">
                            Required: {formatCentralDate(po.required_date)}
                          </p>
                        )}
                      </button>
                    ))
                  )}
                </div>
              </div>

              {/* PO Detail Panel */}
              <div className="col-span-2 flex flex-col">
                {selectedPO ? (
                  <div className="bg-slate-800 rounded-xl p-4 flex flex-col flex-1 overflow-hidden">
                    {/* Compact PO Header */}
                    <div className="flex items-center justify-between gap-4 mb-3 flex-shrink-0">
                      <div className="flex items-center gap-4 min-w-0">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <h3 className="text-lg font-bold text-werco-primary">{selectedPO.po_number}</h3>
                            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${statusColor(selectedPO.status)}`}>
                              {selectedPO.status.charAt(0).toUpperCase() + selectedPO.status.slice(1)}
                            </span>
                            {selectedPO.is_approved_vendor && (
                              <span className="text-xs text-green-600 font-medium">✓ Approved</span>
                            )}
                          </div>
                          <p className="text-sm text-slate-400 truncate">
                            {selectedPO.vendor_name}
                            {selectedPO.vendor_code && <span className="text-slate-400 ml-1">({selectedPO.vendor_code})</span>}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-4 text-xs text-slate-400 flex-shrink-0">
                        {selectedPO.order_date && (
                          <div className="text-center">
                            <p className="text-slate-400">Ordered</p>
                            <p className="font-medium text-slate-300">{formatCentralDate(selectedPO.order_date)}</p>
                          </div>
                        )}
                        {selectedPO.required_date && (
                          <div className="text-center">
                            <p className="text-slate-400">Required</p>
                            <p className="font-medium text-slate-300">{formatCentralDate(selectedPO.required_date)}</p>
                          </div>
                        )}
                        {selectedPO.expected_date && (
                          <div className="text-center">
                            <p className="text-slate-400">Expected</p>
                            <p className="font-medium text-slate-300">{formatCentralDate(selectedPO.expected_date)}</p>
                          </div>
                        )}
                      </div>
                    </div>

                    {selectedPO.notes && (
                      <div className="mb-3 px-3 py-2 bg-fd-panel rounded-lg border border-slate-700 text-sm text-slate-400 flex-shrink-0">
                        <span className="text-slate-400 font-medium">Notes: </span>{selectedPO.notes}
                      </div>
                    )}

                    {/* Lines Table — scrollable */}
                    <div className="flex-1 overflow-y-auto bg-fd-panel rounded-lg border border-slate-700">
                      <table className="w-full divide-y divide-slate-700">
                        <thead className="bg-slate-800/50 sticky top-0">
                          <tr>
                            <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase w-10">#</th>
                            <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase">Part</th>
                            <th className="px-3 py-2 text-right text-xs font-semibold text-slate-400 uppercase w-16">Ord</th>
                            <th className="px-3 py-2 text-right text-xs font-semibold text-slate-400 uppercase w-16">Recv</th>
                            <th className="px-3 py-2 text-right text-xs font-semibold text-slate-400 uppercase w-16">Rem</th>
                            <th className="px-3 py-2 text-right text-xs font-semibold text-slate-400 uppercase w-20">Unit $</th>
                            <th className="px-3 py-2 text-center text-xs font-semibold text-slate-400 uppercase w-20">Status</th>
                            <th className="px-3 py-2 text-center text-xs font-semibold text-slate-400 uppercase w-20"></th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-700">
                          {selectedPO.lines?.map((line: any) => (
                            <tr
                              key={line.line_id}
                              className={`${
                                line.is_closed
                                  ? 'bg-slate-800 text-slate-400'
                                  : isPartialLine(line)
                                    ? 'bg-amber-500/10 hover:bg-amber-500/20'
                                    : 'hover:bg-slate-800'
                              }`}
                            >
                              <td className="px-3 py-2 text-sm text-center">{line.line_number}</td>
                              <td className="px-3 py-2">
                                <span className="font-mono font-semibold text-sm">{line.part_number}</span>
                                <span className="text-xs text-slate-400 ml-2 hidden xl:inline">{line.part_name}</span>
                              </td>
                              <td className="px-3 py-2 text-right text-sm">{line.quantity_ordered}</td>
                              <td className="px-3 py-2 text-right text-sm">{line.quantity_received}</td>
                              <td className="px-3 py-2 text-right text-sm font-semibold">
                                {line.is_closed ? (
                                  <span className="text-slate-400">-</span>
                                ) : (
                                  <span className={line.quantity_remaining > 0 ? 'text-amber-600' : 'text-green-600'}>
                                    {line.quantity_remaining}
                                  </span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-right text-sm">${(line.unit_price || 0).toFixed(2)}</td>
                              <td className="px-3 py-2 text-center">
                                {line.is_closed ? (
                                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-emerald-300">Done</span>
                                ) : line.quantity_received > 0 ? (
                                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-amber-500/20 text-amber-300">Partial</span>
                                ) : (
                                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-500/20 text-blue-300">Open</span>
                                )}
                              </td>
                              <td className="px-3 py-2 text-center">
                                {!line.is_closed && line.quantity_remaining > 0 && (
                                  <button
                                    onClick={() => handleSelectLine(line)}
                                    className="btn-primary text-xs px-3 py-1"
                                  >
                                    Receive
                                  </button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                        <tfoot className="bg-slate-800/50">
                          <tr>
                            <td colSpan={5} className="px-3 py-2 text-right text-sm font-semibold">PO Total:</td>
                            <td className="px-3 py-2 text-right text-sm font-bold">
                              ${selectedPO.lines?.reduce((sum: number, l: any) => sum + ((l.unit_price || 0) * (l.quantity_ordered || 0)), 0).toFixed(2)}
                            </td>
                            <td colSpan={2}></td>
                          </tr>
                        </tfoot>
                      </table>
                    </div>

                    {/* Receipt History for this PO — collapsible */}
                    {selectedPO.lines?.some((l: any) => l.receipts?.length > 0) && (
                      <details className="mt-3 flex-shrink-0">
                        <summary className="text-sm font-semibold text-slate-400 cursor-pointer hover:text-slate-100">
                          Receipt History ({selectedPO.lines?.reduce((c: number, l: any) => c + (l.receipts?.length || 0), 0)} receipts)
                        </summary>
                        <div className="mt-2 bg-fd-panel rounded-lg border border-slate-700 overflow-x-auto max-h-48 overflow-y-auto">
                          <table className="w-full divide-y divide-slate-700">
                            <thead className="bg-slate-800 sticky top-0">
                              <tr>
                                <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase">Receipt #</th>
                                <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase">Part</th>
                                <th className="px-3 py-2 text-right text-xs font-semibold text-slate-400 uppercase">Qty</th>
                                <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase">Lot #</th>
                                <th className="px-3 py-2 text-center text-xs font-semibold text-slate-400 uppercase">Status</th>
                                <th className="px-3 py-2 text-left text-xs font-semibold text-slate-400 uppercase">Date</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-700">
                              {selectedPO.lines?.flatMap((l: any) =>
                                (l.receipts || []).map((r: any) => (
                                  <tr key={r.receipt_id} className="hover:bg-slate-800">
                                    <td className="px-3 py-1.5 font-mono text-sm">{r.receipt_number}</td>
                                    <td className="px-3 py-1.5 font-mono text-sm">{l.part_number}</td>
                                    <td className="px-3 py-1.5 text-right text-sm font-medium">{r.quantity_received}</td>
                                    <td className="px-3 py-1.5 font-mono text-sm">{r.lot_number}</td>
                                    <td className="px-3 py-1.5 text-center">
                                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${statusColor(r.status)}`}>
                                        {r.status.replace(/_/g, ' ')}
                                      </span>
                                    </td>
                                    <td className="px-3 py-1.5 text-sm text-slate-400">
                                      {r.received_at ? formatCentralDate(r.received_at) : '-'}
                                    </td>
                                  </tr>
                                ))
                              )}
                            </tbody>
                          </table>
                        </div>
                      </details>
                    )}
                  </div>
                ) : (
                  <div className="bg-slate-800 rounded-xl p-8 flex-1 flex flex-col items-center justify-center text-slate-400">
                    <MagnifyingGlassIcon className="h-16 w-16 mb-4" />
                    <p className="text-lg">Select a purchase order to view details</p>
                    <p className="text-sm mt-1">Click on a PO from the list to see lines and receive material</p>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* INSPECTION QUEUE TAB */}
        {activeTab === 'queue' && (
          <div>
            <h2 className="text-lg font-semibold mb-4">Items Pending Inspection</h2>
            <DataTable
              columns={queueColumns}
              data={inspectionQueue}
              rowKey={(item) => item.receipt_id}
              defaultSort={{ key: 'days_pending', dir: 'desc' }}
              pageSize={25}
              csvExport={{ filename: 'inspection-queue' }}
              error={queueError}
              onRetry={loadInspectionQueue}
              empty={{
                icon: ClipboardDocumentCheckIcon,
                title: 'No items pending inspection',
                description: 'Received material requiring inspection will appear here.',
              }}
              mobileCards={renderQueueCard}
            />
          </div>
        )}

        {/* HISTORY TAB */}
        {activeTab === 'history' && (
          <div>
            <h2 className="text-lg font-semibold mb-4">Receiving History (Last 30 Days)</h2>
            <DataTable
              columns={historyColumns}
              data={history}
              rowKey={(item) => item.receipt_id}
              defaultSort={{ key: 'received_at', dir: 'desc' }}
              pageSize={25}
              csvExport={{ filename: 'receiving-history' }}
              error={historyError}
              onRetry={loadHistory}
              empty={{
                icon: ClockIcon,
                title: 'No receiving history',
                description: 'Receipts from the last 30 days will appear here.',
              }}
              mobileCards={renderHistoryCard}
            />
          </div>
        )}
      </div>

      {/* RECEIVE MODAL */}
      <Modal
        open={showReceiveModal && !!selectedLine}
        onClose={() => setShowReceiveModal(false)}
        size="2xl"
        closeOnBackdrop={false}
      >
        {selectedLine && (
          <>
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-xl font-bold">Receive Material</h2>
              <button onClick={() => setShowReceiveModal(false)}>
                <XMarkIcon className="h-6 w-6 text-slate-400 hover:text-slate-400" />
              </button>
            </div>

            {/* Part Info */}
            <div className="bg-slate-800 rounded-xl p-4 mb-6">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <p className="text-sm text-slate-400">Part Number</p>
                  <p className="font-mono font-semibold">{selectedLine.part_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Part Name</p>
                  <p>{selectedLine.part_name}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">PO Number</p>
                  <p className="font-semibold">{selectedPO?.po_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Remaining to Receive</p>
                  <p className="font-semibold text-amber-600">{selectedLine.quantity_remaining}</p>
                </div>
              </div>
            </div>

            {/* Form */}
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Quantity Received" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      value={formData.quantity_received}
                      onChange={(e) => setFormData({ ...formData, quantity_received: parseFloat(e.target.value) || 0 })}
                      className="input w-full"
                      min="0"
                      step="1"
                    />
                  )}
                </FormField>
                <FormField label="Lot Number" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.lot_number}
                      onChange={(e) => setFormData({ ...formData, lot_number: e.target.value })}
                      className="input w-full"
                      placeholder="Required for traceability"
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Heat Number" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.heat_number}
                      onChange={(e) => setFormData({ ...formData, heat_number: e.target.value })}
                      className="input w-full"
                      placeholder="For metals"
                    />
                  )}
                </FormField>
                <FormField label="Cert Number" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.cert_number}
                      onChange={(e) => setFormData({ ...formData, cert_number: e.target.value })}
                      className="input w-full"
                      placeholder="Certificate of conformance"
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Location" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <select
                      {...field}
                      value={formData.location_id || ''}
                      onChange={(e) => setFormData({ ...formData, location_id: e.target.value ? parseInt(e.target.value) : null })}
                      className="input w-full"
                    >
                      <option value="">Select location</option>
                      {locations.map((loc) => (
                        <option key={loc.id} value={loc.id}>{loc.code} - {loc.name}</option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label="Packing Slip #" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.packing_slip_number}
                      onChange={(e) => setFormData({ ...formData, packing_slip_number: e.target.value })}
                      className="input w-full"
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Carrier" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.carrier}
                      onChange={(e) => setFormData({ ...formData, carrier: e.target.value })}
                      className="input w-full"
                      placeholder="e.g., UPS, FedEx"
                    />
                  )}
                </FormField>
                <FormField label="Tracking Number" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.tracking_number}
                      onChange={(e) => setFormData({ ...formData, tracking_number: e.target.value })}
                      className="input w-full"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Notes" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <textarea
                    {...field}
                    value={formData.notes}
                    onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                    className="input w-full"
                    rows={2}
                  />
                )}
              </FormField>

              <div className="flex gap-6">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={formData.requires_inspection}
                    onChange={(e) => setFormData({ ...formData, requires_inspection: e.target.checked })}
                    className="rounded border-slate-600"
                  />
                  <span className="text-sm">Requires Inspection</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={formData.coc_attached}
                    onChange={(e) => setFormData({ ...formData, coc_attached: e.target.checked })}
                    className="rounded border-slate-600"
                  />
                  <span className="text-sm">CoC Attached</span>
                </label>
              </div>

              {formData.quantity_received > selectedLine.quantity_remaining && (
                <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={formData.over_receive_approved}
                      onChange={(e) => setFormData({ ...formData, over_receive_approved: e.target.checked })}
                      className="rounded border-amber-500/40"
                    />
                    <span className="text-sm text-amber-300">
                      <strong>Approve Over-Receipt:</strong> Receiving {formData.quantity_received - selectedLine.quantity_remaining} more than remaining quantity
                    </span>
                  </label>
                </div>
              )}
            </div>

            <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
              <Button variant="secondary" className="px-6" onClick={() => setShowReceiveModal(false)}>
                Cancel
              </Button>
              <Button className="px-6" onClick={handleReceive}>
                Receive Material
              </Button>
            </div>
          </>
        )}
      </Modal>

      {/* INSPECT MODAL */}
      <Modal
        open={showInspectModal && !!selectedReceipt && !!receiptDetail}
        onClose={() => setShowInspectModal(false)}
        size="2xl"
        closeOnBackdrop={false}
      >
        {selectedReceipt && receiptDetail && (
          <>
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-xl font-bold">Inspect Receipt</h2>
              <button onClick={() => setShowInspectModal(false)}>
                <XMarkIcon className="h-6 w-6 text-slate-400 hover:text-slate-400" />
              </button>
            </div>

            {/* Receipt Info */}
            <div className="bg-slate-800 rounded-xl p-4 mb-6">
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <p className="text-sm text-slate-400">Receipt #</p>
                  <p className="font-mono font-semibold">{receiptDetail.receipt_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">PO #</p>
                  <p className="font-semibold">{receiptDetail.po_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Vendor</p>
                  <p>{receiptDetail.vendor_name}</p>
                  {receiptDetail.is_approved_vendor && (
                    <span className="text-xs text-green-600">✓ Approved Vendor</span>
                  )}
                </div>
                <div>
                  <p className="text-sm text-slate-400">Part Number</p>
                  <p className="font-mono">{receiptDetail.part_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Part Name</p>
                  <p>{receiptDetail.part_name}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Lot Number</p>
                  <p className="font-mono">{receiptDetail.lot_number}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Qty Received</p>
                  <p className="text-xl font-bold">{receiptDetail.quantity_received}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">Cert #</p>
                  <p>{receiptDetail.cert_number || '-'}</p>
                </div>
                <div>
                  <p className="text-sm text-slate-400">CoC</p>
                  <p>{receiptDetail.coc_attached ? '✓ Attached' : 'Not attached'}</p>
                </div>
              </div>
            </div>

            {/* Inspection Form */}
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField
                  label={<>Quantity Accepted <span className="text-green-600">✓</span></>}
                  labelClassName="block text-sm font-medium text-slate-300 mb-1"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      value={inspectionData.quantity_accepted}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setInspectionData({
                          ...inspectionData,
                          quantity_accepted: val,
                          quantity_rejected: Math.max(0, receiptDetail.quantity_received - val)
                        });
                      }}
                      className="input w-full"
                      min="0"
                      max={receiptDetail.quantity_received}
                    />
                  )}
                </FormField>
                <FormField
                  label={<>Quantity Rejected <span className="text-red-600">✗</span></>}
                  labelClassName="block text-sm font-medium text-slate-300 mb-1"
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      value={inspectionData.quantity_rejected}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setInspectionData({
                          ...inspectionData,
                          quantity_rejected: val,
                          quantity_accepted: Math.max(0, receiptDetail.quantity_received - val)
                        });
                      }}
                      className="input w-full"
                      min="0"
                      max={receiptDetail.quantity_received}
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Inspection Method" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <select
                    {...field}
                    value={inspectionData.inspection_method}
                    onChange={(e) => setInspectionData({ ...inspectionData, inspection_method: e.target.value })}
                    className="input w-full"
                  >
                    {inspectionMethods.map((m) => (
                      <option key={m.value} value={m.value}>{m.label}</option>
                    ))}
                  </select>
                )}
              </FormField>

              {inspectionData.quantity_rejected > 0 && (
                <>
                  <FormField label="Defect Type" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                    {(field) => (
                      <select
                        {...field}
                        value={inspectionData.defect_type}
                        onChange={(e) => setInspectionData({ ...inspectionData, defect_type: e.target.value })}
                        className="input w-full"
                      >
                        <option value="">Select defect type</option>
                        {defectTypes.map((d) => (
                          <option key={d.value} value={d.value}>{d.label}</option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4">
                    <p className="text-sm text-red-300 mb-2">
                      <strong>Note:</strong> An NCR will be auto-created for the rejected quantity ({inspectionData.quantity_rejected})
                    </p>
                  </div>
                </>
              )}

              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">
                  Inspection Notes {inspectionData.quantity_rejected > 0 && <span className="text-red-500">*</span>}
                </label>
                <textarea
                  value={inspectionData.inspection_notes}
                  onChange={(e) => setInspectionData({ ...inspectionData, inspection_notes: e.target.value })}
                  className="input w-full"
                  rows={3}
                  placeholder={inspectionData.quantity_rejected > 0 ? 'Required - describe the non-conformance' : 'Optional notes'}
                />
              </div>

              {/* Result Preview */}
              <div className="bg-blue-500/10 border border-blue-500/30 rounded-xl p-4">
                <p className="text-sm font-medium text-blue-300 mb-2">Inspection Result Preview:</p>
                <div className="flex gap-4 text-sm">
                  {inspectionData.quantity_accepted === receiptDetail.quantity_received && (
                    <span className="text-green-600 font-semibold">✓ Full Pass - Add to Inventory</span>
                  )}
                  {inspectionData.quantity_rejected === receiptDetail.quantity_received && (
                    <span className="text-red-600 font-semibold">✗ Full Reject - Create NCR</span>
                  )}
                  {inspectionData.quantity_accepted > 0 && inspectionData.quantity_rejected > 0 && (
                    <span className="text-amber-600 font-semibold">
                      ⚠ Partial - {inspectionData.quantity_accepted} to Inventory, {inspectionData.quantity_rejected} to NCR
                    </span>
                  )}
                </div>
              </div>
            </div>

            <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
              <Button variant="secondary" className="px-6" onClick={() => setShowInspectModal(false)}>
                Cancel
              </Button>
              <Button className="px-6" onClick={handleInspect}>
                Complete Inspection
              </Button>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
