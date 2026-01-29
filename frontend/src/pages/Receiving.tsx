import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { format } from 'date-fns';
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
} from '@heroicons/react/24/outline';

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

type TabType = 'receive' | 'queue' | 'history';

export default function ReceivingPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>(() => {
    const tab = searchParams.get('tab');
    return (tab === 'queue' || tab === 'history') ? tab : 'receive';
  });
  
  const [openPOs, setOpenPOs] = useState<PurchaseOrder[]>([]);
  const [selectedPO, setSelectedPO] = useState<PurchaseOrder | null>(null);
  const [selectedLine, setSelectedLine] = useState<POLine | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState<any>(null);
  
  const [inspectionQueue, setInspectionQueue] = useState<any[]>([]);
  const [history, setHistory] = useState<any[]>([]);
  
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
    } finally {
      setLoading(false);
    }
  };

  const loadInspectionQueue = async () => {
    try {
      const data = await api.getInspectionQueue(30);
      setInspectionQueue(data);
    } catch (err) {
      console.error('Failed to load inspection queue:', err);
    }
  };

  const loadHistory = async () => {
    try {
      const data = await api.getReceivingHistory(30);
      setHistory(data);
    } catch (err) {
      console.error('Failed to load history:', err);
    }
  };

  const handleSelectPO = async (po: PurchaseOrder) => {
    try {
      const fullPO = await api.getPOForReceiving(po.po_id);
      setSelectedPO(fullPO);
    } catch (err) {
      console.error('Failed to load PO details:', err);
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
      await api.receiveNewMaterial({
        ...formData,
        location_id: formData.location_id || undefined,
      });
      setSuccess('Material received successfully');
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
      
      setTimeout(() => setSuccess(''), 3000);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to receive material');
    }
  };

  const handleOpenInspection = async (receipt: any) => {
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
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Receiving & Inspection</h1>
          <p className="text-gray-500 mt-1">AS9100D compliant receiving and inspection workflow</p>
        </div>
      </div>

      {/* Success/Error Messages */}
      {success && (
        <div className="bg-green-50 border border-green-200 rounded-xl p-4 flex items-center gap-3">
          <CheckCircleIcon className="h-5 w-5 text-green-600" />
          <span className="text-green-800">{success}</span>
        </div>
      )}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
          <span className="text-red-800">{error}</span>
          <button onClick={() => setError('')} className="ml-auto">
            <XMarkIcon className="h-5 w-5 text-red-600" />
          </button>
        </div>
      )}

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          <div className="stat-card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-500">Pending Inspection</p>
                <p className="text-2xl font-bold text-amber-600">{stats.pending_inspection}</p>
              </div>
              <ClockIcon className="h-10 w-10 text-amber-200" />
            </div>
          </div>
          <div className="stat-card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-500">Received (30d)</p>
                <p className="text-2xl font-bold text-werco-primary">{stats.receipts_in_period}</p>
              </div>
              <InboxArrowDownIcon className="h-10 w-10 text-werco-100" />
            </div>
          </div>
          <div className="stat-card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-500">Acceptance Rate</p>
                <p className="text-2xl font-bold text-green-600">{stats.acceptance_rate}%</p>
              </div>
              <DocumentCheckIcon className="h-10 w-10 text-green-200" />
            </div>
          </div>
          <div className="stat-card">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-500">Rejections (30d)</p>
                <p className="text-2xl font-bold text-red-600">{stats.rejections_in_period}</p>
              </div>
              <ExclamationTriangleIcon className="h-10 w-10 text-red-200" />
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-gray-200">
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
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              <tab.icon className="h-5 w-5" />
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span className="ml-1 px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-800">
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
          <div className="space-y-6">
            {/* PO Selection Row */}
            <div className="grid grid-cols-3 gap-4">
              <div className="col-span-1">
                <h2 className="text-lg font-semibold mb-3">Open Purchase Orders</h2>
                <div className="space-y-2 max-h-[300px] overflow-y-auto pr-2">
                  {openPOs.length === 0 ? (
                    <p className="text-gray-500 text-center py-8">No open POs awaiting receipt</p>
                  ) : (
                    openPOs.map((po) => (
                      <div
                        key={po.po_id}
                        onClick={() => handleSelectPO(po)}
                        className={`p-3 rounded-xl border-2 cursor-pointer transition-all ${
                          selectedPO?.po_id === po.po_id
                            ? 'border-werco-primary bg-werco-50'
                            : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'
                        }`}
                      >
                        <div className="flex justify-between items-start">
                          <div>
                            <p className="font-semibold text-werco-primary">{po.po_number}</p>
                            <p className="text-sm text-gray-600">{po.vendor_name}</p>
                          </div>
                          <span className="px-2 py-1 rounded text-xs font-medium bg-blue-100 text-blue-800">
                            {po.total_lines} line{po.total_lines !== 1 ? 's' : ''}
                          </span>
                        </div>
                        {po.required_date && (
                          <p className="text-xs text-gray-500 mt-1">
                            Required: {format(new Date(po.required_date), 'MMM d, yyyy')}
                          </p>
                        )}
                      </div>
                    ))
                  )}
                </div>
              </div>

              {/* PO Detail Panel */}
              <div className="col-span-2">
                {selectedPO ? (
                  <div className="bg-gray-50 rounded-xl p-4 h-full">
                    <div className="flex justify-between items-start mb-4">
                      <div>
                        <h3 className="text-xl font-bold text-werco-primary">{selectedPO.po_number}</h3>
                        <p className="text-gray-600">{selectedPO.vendor_name}</p>
                        {selectedPO.vendor_code && (
                          <p className="text-sm text-gray-500">Vendor Code: {selectedPO.vendor_code}</p>
                        )}
                      </div>
                      <div className="text-right">
                        <span className={`px-3 py-1 rounded-full text-sm font-medium ${
                          selectedPO.status === 'sent' ? 'bg-blue-100 text-blue-800' :
                          selectedPO.status === 'partial' ? 'bg-amber-100 text-amber-800' :
                          'bg-gray-100 text-gray-800'
                        }`}>
                          {selectedPO.status.charAt(0).toUpperCase() + selectedPO.status.slice(1)}
                        </span>
                        {selectedPO.is_approved_vendor && (
                          <p className="text-xs text-green-600 mt-1">✓ Approved Vendor</p>
                        )}
                      </div>
                    </div>
                    
                    <div className="grid grid-cols-4 gap-4 mb-4 text-sm">
                      <div>
                        <p className="text-gray-500">Order Date</p>
                        <p className="font-medium">
                          {selectedPO.order_date ? format(new Date(selectedPO.order_date), 'MMM d, yyyy') : '-'}
                        </p>
                      </div>
                      <div>
                        <p className="text-gray-500">Required Date</p>
                        <p className="font-medium">
                          {selectedPO.required_date ? format(new Date(selectedPO.required_date), 'MMM d, yyyy') : '-'}
                        </p>
                      </div>
                      <div>
                        <p className="text-gray-500">Expected Date</p>
                        <p className="font-medium">
                          {selectedPO.expected_date ? format(new Date(selectedPO.expected_date), 'MMM d, yyyy') : '-'}
                        </p>
                      </div>
                      <div>
                        <p className="text-gray-500">Total Lines</p>
                        <p className="font-medium">{selectedPO.lines?.length || 0}</p>
                      </div>
                    </div>

                    {selectedPO.notes && (
                      <div className="mb-4 p-3 bg-white rounded-lg border border-gray-200">
                        <p className="text-xs text-gray-500 mb-1">PO Notes</p>
                        <p className="text-sm">{selectedPO.notes}</p>
                      </div>
                    )}

                    {/* All Lines Table */}
                    <div className="bg-white rounded-lg border border-gray-200 overflow-x-auto">
                      <table className="w-full divide-y divide-gray-200" style={{ minWidth: '700px' }}>
                        <thead className="bg-gray-100">
                          <tr>
                            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-16">Line</th>
                            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase whitespace-nowrap">Part Number</th>
                            <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">Part Name</th>
                            <th className="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Ordered</th>
                            <th className="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Received</th>
                            <th className="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Remaining</th>
                            <th className="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Unit $</th>
                            <th className="px-4 py-3 text-right text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Line Total</th>
                            <th className="px-4 py-3 text-center text-xs font-semibold text-gray-600 uppercase whitespace-nowrap w-24">Status</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-200">
                          {selectedPO.lines?.map((line: any) => (
                            <tr
                              key={line.line_id}
                              className={`${
                                line.is_closed
                                  ? 'bg-gray-50 text-gray-400'
                                  : isPartialLine(line)
                                    ? 'bg-amber-50 hover:bg-amber-100'
                                    : 'hover:bg-gray-50'
                              }`}
                            >
                              <td className="px-4 py-3 text-sm font-medium text-center">{line.line_number}</td>
                              <td className="px-4 py-3">
                                <span className="font-mono font-semibold text-sm">{line.part_number}</span>
                              </td>
                              <td className="px-4 py-3 text-sm text-gray-700">{line.part_name}</td>
                              <td className="px-4 py-3 text-right text-sm font-medium">{line.quantity_ordered}</td>
                              <td className="px-4 py-3 text-right text-sm">{line.quantity_received}</td>
                              <td className="px-4 py-3 text-right text-sm font-semibold">
                                {line.is_closed ? (
                                  <span className="text-gray-400">-</span>
                                ) : (
                                  <span className={line.quantity_remaining > 0 ? 'text-amber-600' : 'text-green-600'}>
                                    {line.quantity_remaining}
                                  </span>
                                )}
                              </td>
                              <td className="px-4 py-3 text-right text-sm">${(line.unit_price || 0).toFixed(2)}</td>
                              <td className="px-4 py-3 text-right text-sm font-medium">${((line.unit_price || 0) * (line.quantity_ordered || 0)).toFixed(2)}</td>
                              <td className="px-4 py-3 text-center">
                                {line.is_closed ? (
                                  <span className="px-2 py-1 rounded text-xs font-medium bg-green-100 text-green-800">Complete</span>
                                ) : line.quantity_received > 0 ? (
                                  <span className="px-2 py-1 rounded text-xs font-medium bg-amber-100 text-amber-800">Partial</span>
                                ) : (
                                  <span className="px-2 py-1 rounded text-xs font-medium bg-blue-100 text-blue-800">Open</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                        <tfoot className="bg-gray-100">
                          <tr>
                            <td colSpan={7} className="px-4 py-3 text-right text-sm font-semibold">PO Total:</td>
                            <td className="px-4 py-3 text-right text-sm font-bold">
                              ${selectedPO.lines?.reduce((sum: number, l: any) => sum + ((l.unit_price || 0) * (l.quantity_ordered || 0)), 0).toFixed(2)}
                            </td>
                            <td></td>
                          </tr>
                        </tfoot>
                      </table>
                    </div>

                    {/* Receipt History for this PO */}
                    {selectedPO.lines?.some((l: any) => l.receipts?.length > 0) && (
                      <div className="mt-4">
                        <p className="text-sm font-semibold text-gray-700 mb-2">Receipt History</p>
                        <div className="bg-white rounded-lg border border-gray-200 overflow-x-auto">
                          <table className="w-full divide-y divide-gray-200" style={{ minWidth: '600px' }}>
                            <thead className="bg-gray-50">
                              <tr>
                                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Receipt #</th>
                                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Part</th>
                                <th className="px-4 py-2 text-right text-xs font-semibold text-gray-600 uppercase">Qty</th>
                                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Lot #</th>
                                <th className="px-4 py-2 text-center text-xs font-semibold text-gray-600 uppercase">Status</th>
                                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Date</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-gray-200">
                              {selectedPO.lines?.flatMap((l: any) => 
                                (l.receipts || []).map((r: any) => (
                                  <tr key={r.receipt_id} className="hover:bg-gray-50">
                                    <td className="px-4 py-2 font-mono text-sm">{r.receipt_number}</td>
                                    <td className="px-4 py-2 font-mono text-sm">{l.part_number}</td>
                                    <td className="px-4 py-2 text-right text-sm font-medium">{r.quantity_received}</td>
                                    <td className="px-4 py-2 font-mono text-sm">{r.lot_number}</td>
                                    <td className="px-4 py-2 text-center">
                                      <span className={`px-2 py-1 rounded text-xs font-medium ${
                                        r.status === 'accepted' ? 'bg-green-100 text-green-800' :
                                        r.status === 'pending_inspection' ? 'bg-amber-100 text-amber-800' :
                                        r.status === 'rejected' ? 'bg-red-100 text-red-800' :
                                        'bg-gray-100 text-gray-800'
                                      }`}>
                                        {r.status.replace(/_/g, ' ')}
                                      </span>
                                    </td>
                                    <td className="px-4 py-2 text-sm text-gray-600">
                                      {r.received_at ? format(new Date(r.received_at), 'MMM d, yyyy') : '-'}
                                    </td>
                                  </tr>
                                ))
                              )}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="bg-gray-50 rounded-xl p-8 h-full flex flex-col items-center justify-center text-gray-400">
                    <MagnifyingGlassIcon className="h-16 w-16 mb-4" />
                    <p className="text-lg">Select a purchase order to view details</p>
                    <p className="text-sm mt-1">Click on a PO from the list to see full order information</p>
                  </div>
                )}
              </div>
            </div>

            {/* Lines Available to Receive */}
            {selectedPO && (
              <div>
                <h2 className="text-lg font-semibold mb-3">Lines Available to Receive</h2>
                <div className="grid grid-cols-2 gap-4">
                  {selectedPO.lines?.filter((l: any) => l.quantity_remaining > 0 && !l.is_closed).map((line: any) => (
                    <div
                      key={line.line_id}
                      className={`p-4 rounded-xl border-2 transition-all ${
                        isPartialLine(line)
                          ? 'border-amber-300 bg-amber-50 hover:border-amber-400'
                          : 'border-gray-200 hover:border-werco-primary hover:bg-werco-50'
                      }`}
                    >
                      <div className="flex justify-between items-start mb-3">
                        <div>
                          <p className="font-mono font-bold text-lg">{line.part_number}</p>
                          <p className="text-sm text-gray-600">{line.part_name}</p>
                        </div>
                        <button
                          onClick={() => handleSelectLine(line)}
                          className="btn-primary px-6 py-2"
                        >
                          Receive
                        </button>
                      </div>
                      <div className="grid grid-cols-4 gap-4 text-sm">
                        <div>
                          <p className="text-gray-500">Ordered</p>
                          <p className="font-semibold text-lg">{line.quantity_ordered}</p>
                        </div>
                        <div>
                          <p className="text-gray-500">Received</p>
                          <p className="font-semibold text-lg">{line.quantity_received}</p>
                        </div>
                        <div>
                          <p className="text-gray-500">Remaining</p>
                          <p className="font-bold text-lg text-amber-600">{line.quantity_remaining}</p>
                        </div>
                        <div>
                          <p className="text-gray-500">Unit Price</p>
                          <p className="font-semibold text-lg">${line.unit_price?.toFixed(2)}</p>
                        </div>
                      </div>
                      {line.required_date && (
                        <p className="text-xs text-gray-500 mt-2">
                          Required: {format(new Date(line.required_date), 'MMM d, yyyy')}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
                {selectedPO.lines?.filter((l: any) => l.quantity_remaining > 0 && !l.is_closed).length === 0 && (
                  <div className="text-center py-8 text-gray-500 bg-gray-50 rounded-xl">
                    <CheckCircleIcon className="h-12 w-12 mx-auto mb-2 text-green-500" />
                    <p>All lines have been fully received</p>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* INSPECTION QUEUE TAB */}
        {activeTab === 'queue' && (
          <div>
            <h2 className="text-lg font-semibold mb-4">Items Pending Inspection</h2>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Receipt</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">PO / Vendor</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Lot #</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">CoC</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Received</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Days</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Action</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {inspectionQueue.map((item) => (
                    <tr key={item.receipt_id} className={`hover:bg-gray-50 ${item.days_pending > 3 ? 'bg-amber-50' : ''}`}>
                      <td className="px-4 py-3 font-mono text-sm">{item.receipt_number}</td>
                      <td className="px-4 py-3">
                        <p className="font-medium text-sm">{item.po_number}</p>
                        <p className="text-xs text-gray-500">{item.vendor_name}</p>
                      </td>
                      <td className="px-4 py-3">
                        <p className="font-mono text-sm">{item.part_number}</p>
                        <p className="text-xs text-gray-500 truncate max-w-[200px]">{item.part_name}</p>
                      </td>
                      <td className="px-4 py-3 text-right font-medium">{item.quantity_received}</td>
                      <td className="px-4 py-3 font-mono text-sm">{item.lot_number}</td>
                      <td className="px-4 py-3 text-center">
                        {item.coc_attached ? (
                          <CheckCircleIcon className="h-5 w-5 text-green-500 mx-auto" />
                        ) : (
                          <span className="text-gray-300">-</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {format(new Date(item.received_at), 'MMM d, HH:mm')}
                      </td>
                      <td className="px-4 py-3 text-center">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                          item.days_pending > 3 ? 'bg-red-100 text-red-800' :
                          item.days_pending > 1 ? 'bg-amber-100 text-amber-800' :
                          'bg-green-100 text-green-800'
                        }`}>
                          {item.days_pending}d
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => handleOpenInspection(item)}
                          className="btn-primary text-sm px-3 py-1"
                        >
                          Inspect
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {inspectionQueue.length === 0 && (
                <p className="text-center text-gray-500 py-8">No items pending inspection</p>
              )}
            </div>
          </div>
        )}

        {/* HISTORY TAB */}
        {activeTab === 'history' && (
          <div>
            <h2 className="text-lg font-semibold mb-4">Receiving History (Last 30 Days)</h2>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Receipt</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">PO</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Recv'd</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Accepted</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Rejected</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Lot #</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {history.map((item) => (
                    <tr key={item.receipt_id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-mono text-sm">{item.receipt_number}</td>
                      <td className="px-4 py-3 text-sm">{item.po_number}</td>
                      <td className="px-4 py-3">
                        <p className="font-mono text-sm">{item.part_number}</p>
                      </td>
                      <td className="px-4 py-3 text-right">{item.quantity_received}</td>
                      <td className="px-4 py-3 text-right text-green-600">{item.quantity_accepted}</td>
                      <td className="px-4 py-3 text-right text-red-600">{item.quantity_rejected || '-'}</td>
                      <td className="px-4 py-3 font-mono text-sm">{item.lot_number}</td>
                      <td className="px-4 py-3 text-center">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                          item.inspection_status === 'passed' ? 'bg-green-100 text-green-800' :
                          item.inspection_status === 'failed' ? 'bg-red-100 text-red-800' :
                          item.inspection_status === 'partial' ? 'bg-amber-100 text-amber-800' :
                          'bg-gray-100 text-gray-800'
                        }`}>
                          {item.inspection_status?.replace(/_/g, ' ') || item.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm">
                        {format(new Date(item.received_at), 'MMM d, yyyy')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {history.length === 0 && (
                <p className="text-center text-gray-500 py-8">No receiving history</p>
              )}
            </div>
          </div>
        )}
      </div>

      {/* RECEIVE MODAL */}
      {showReceiveModal && selectedLine && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-xl font-bold">Receive Material</h2>
              <button onClick={() => setShowReceiveModal(false)}>
                <XMarkIcon className="h-6 w-6 text-gray-400 hover:text-gray-600" />
              </button>
            </div>

            {/* Part Info */}
            <div className="bg-gray-50 rounded-xl p-4 mb-6">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <p className="text-sm text-gray-500">Part Number</p>
                  <p className="font-mono font-semibold">{selectedLine.part_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Part Name</p>
                  <p>{selectedLine.part_name}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">PO Number</p>
                  <p className="font-semibold">{selectedPO?.po_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Remaining to Receive</p>
                  <p className="font-semibold text-amber-600">{selectedLine.quantity_remaining}</p>
                </div>
              </div>
            </div>

            {/* Form */}
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Quantity Received <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="number"
                    value={formData.quantity_received}
                    onChange={(e) => setFormData({ ...formData, quantity_received: parseFloat(e.target.value) || 0 })}
                    className="input w-full"
                    min="0"
                    step="1"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Lot Number <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="text"
                    value={formData.lot_number}
                    onChange={(e) => setFormData({ ...formData, lot_number: e.target.value })}
                    className="input w-full"
                    placeholder="Required for traceability"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Heat Number</label>
                  <input
                    type="text"
                    value={formData.heat_number}
                    onChange={(e) => setFormData({ ...formData, heat_number: e.target.value })}
                    className="input w-full"
                    placeholder="For metals"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Cert Number</label>
                  <input
                    type="text"
                    value={formData.cert_number}
                    onChange={(e) => setFormData({ ...formData, cert_number: e.target.value })}
                    className="input w-full"
                    placeholder="Certificate of conformance"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Location</label>
                  <select
                    value={formData.location_id || ''}
                    onChange={(e) => setFormData({ ...formData, location_id: e.target.value ? parseInt(e.target.value) : null })}
                    className="input w-full"
                  >
                    <option value="">Select location</option>
                    {locations.map((loc) => (
                      <option key={loc.id} value={loc.id}>{loc.code} - {loc.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Packing Slip #</label>
                  <input
                    type="text"
                    value={formData.packing_slip_number}
                    onChange={(e) => setFormData({ ...formData, packing_slip_number: e.target.value })}
                    className="input w-full"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Carrier</label>
                  <input
                    type="text"
                    value={formData.carrier}
                    onChange={(e) => setFormData({ ...formData, carrier: e.target.value })}
                    className="input w-full"
                    placeholder="e.g., UPS, FedEx"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Tracking Number</label>
                  <input
                    type="text"
                    value={formData.tracking_number}
                    onChange={(e) => setFormData({ ...formData, tracking_number: e.target.value })}
                    className="input w-full"
                  />
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Notes</label>
                <textarea
                  value={formData.notes}
                  onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                  className="input w-full"
                  rows={2}
                />
              </div>

              <div className="flex gap-6">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={formData.requires_inspection}
                    onChange={(e) => setFormData({ ...formData, requires_inspection: e.target.checked })}
                    className="rounded border-gray-300"
                  />
                  <span className="text-sm">Requires Inspection</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={formData.coc_attached}
                    onChange={(e) => setFormData({ ...formData, coc_attached: e.target.checked })}
                    className="rounded border-gray-300"
                  />
                  <span className="text-sm">CoC Attached</span>
                </label>
              </div>

              {formData.quantity_received > selectedLine.quantity_remaining && (
                <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={formData.over_receive_approved}
                      onChange={(e) => setFormData({ ...formData, over_receive_approved: e.target.checked })}
                      className="rounded border-amber-300"
                    />
                    <span className="text-sm text-amber-800">
                      <strong>Approve Over-Receipt:</strong> Receiving {formData.quantity_received - selectedLine.quantity_remaining} more than remaining quantity
                    </span>
                  </label>
                </div>
              )}
            </div>

            <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
              <button onClick={() => setShowReceiveModal(false)} className="btn-secondary px-6">
                Cancel
              </button>
              <button onClick={handleReceive} className="btn-primary px-6">
                Receive Material
              </button>
            </div>
          </div>
        </div>
      )}

      {/* INSPECT MODAL */}
      {showInspectModal && selectedReceipt && receiptDetail && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-xl font-bold">Inspect Receipt</h2>
              <button onClick={() => setShowInspectModal(false)}>
                <XMarkIcon className="h-6 w-6 text-gray-400 hover:text-gray-600" />
              </button>
            </div>

            {/* Receipt Info */}
            <div className="bg-gray-50 rounded-xl p-4 mb-6">
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <p className="text-sm text-gray-500">Receipt #</p>
                  <p className="font-mono font-semibold">{receiptDetail.receipt_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">PO #</p>
                  <p className="font-semibold">{receiptDetail.po_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Vendor</p>
                  <p>{receiptDetail.vendor_name}</p>
                  {receiptDetail.is_approved_vendor && (
                    <span className="text-xs text-green-600">✓ Approved Vendor</span>
                  )}
                </div>
                <div>
                  <p className="text-sm text-gray-500">Part Number</p>
                  <p className="font-mono">{receiptDetail.part_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Part Name</p>
                  <p>{receiptDetail.part_name}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Lot Number</p>
                  <p className="font-mono">{receiptDetail.lot_number}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Qty Received</p>
                  <p className="text-xl font-bold">{receiptDetail.quantity_received}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">Cert #</p>
                  <p>{receiptDetail.cert_number || '-'}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-500">CoC</p>
                  <p>{receiptDetail.coc_attached ? '✓ Attached' : 'Not attached'}</p>
                </div>
              </div>
            </div>

            {/* Inspection Form */}
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Quantity Accepted <span className="text-green-600">✓</span>
                  </label>
                  <input
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
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Quantity Rejected <span className="text-red-600">✗</span>
                  </label>
                  <input
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
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Inspection Method <span className="text-red-500">*</span>
                </label>
                <select
                  value={inspectionData.inspection_method}
                  onChange={(e) => setInspectionData({ ...inspectionData, inspection_method: e.target.value })}
                  className="input w-full"
                >
                  {inspectionMethods.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              </div>

              {inspectionData.quantity_rejected > 0 && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      Defect Type <span className="text-red-500">*</span>
                    </label>
                    <select
                      value={inspectionData.defect_type}
                      onChange={(e) => setInspectionData({ ...inspectionData, defect_type: e.target.value })}
                      className="input w-full"
                    >
                      <option value="">Select defect type</option>
                      {defectTypes.map((d) => (
                        <option key={d.value} value={d.value}>{d.label}</option>
                      ))}
                    </select>
                  </div>
                  <div className="bg-red-50 border border-red-200 rounded-xl p-4">
                    <p className="text-sm text-red-800 mb-2">
                      <strong>Note:</strong> An NCR will be auto-created for the rejected quantity ({inspectionData.quantity_rejected})
                    </p>
                  </div>
                </>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
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
              <div className="bg-blue-50 border border-blue-200 rounded-xl p-4">
                <p className="text-sm font-medium text-blue-800 mb-2">Inspection Result Preview:</p>
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
              <button onClick={() => setShowInspectModal(false)} className="btn-secondary px-6">
                Cancel
              </button>
              <button onClick={handleInspect} className="btn-primary px-6">
                Complete Inspection
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
