import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  PlusIcon,
  TruckIcon,
  CheckCircleIcon,
  ClipboardDocumentCheckIcon,
  BuildingOfficeIcon,
} from '@heroicons/react/24/outline';

interface Vendor {
  id: number;
  code: string;
  name: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  is_approved: boolean;
  is_as9100_certified: boolean;
  is_iso9001_certified: boolean;
  lead_time_days: number;
}

interface PurchaseOrder {
  id: number;
  po_number: string;
  vendor_id: number;
  vendor_name?: string;
  status: string;
  order_date?: string;
  required_date?: string;
  total: number;
  line_count: number;
}

interface ReceivingQueueItem {
  po_line_id: number;
  po_number: string;
  po_id: number;
  vendor_name: string;
  part_number: string;
  part_name: string;
  quantity_ordered: number;
  quantity_received: number;
  quantity_remaining: number;
  required_date?: string;
  line_number: number;
}

interface PendingInspection {
  receipt_id: number;
  receipt_number: string;
  po_number: string;
  vendor_name: string;
  part_number: string;
  part_name: string;
  quantity_received: number;
  lot_number?: string;
  cert_number?: string;
  received_at: string;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
}

interface Location {
  id: number;
  code: string;
  name: string;
}

type TabType = 'receiving' | 'orders' | 'vendors' | 'inspection';

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  pending_approval: 'bg-yellow-100 text-yellow-800',
  approved: 'bg-blue-100 text-blue-800',
  sent: 'bg-indigo-100 text-indigo-800',
  partial: 'bg-orange-100 text-orange-800',
  received: 'bg-green-100 text-green-800',
  closed: 'bg-gray-100 text-gray-600',
  cancelled: 'bg-red-100 text-red-800',
};

export default function Purchasing() {
  const [activeTab, setActiveTab] = useState<TabType>('orders');
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [purchaseOrders, setPurchaseOrders] = useState<PurchaseOrder[]>([]);
  const [receivingQueue, setReceivingQueue] = useState<ReceivingQueueItem[]>([]);
  const [pendingInspection, setPendingInspection] = useState<PendingInspection[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [locations, setLocations] = useState<Location[]>([]);
  const [loading, setLoading] = useState(true);
  const [poSearch, setPoSearch] = useState('');

  const [showPOModal, setShowPOModal] = useState(false);
  const [showVendorModal, setShowVendorModal] = useState(false);
  const [showReceiveModal, setShowReceiveModal] = useState(false);
  const [showInspectModal, setShowInspectModal] = useState(false);
  const [showAddPartModal, setShowAddPartModal] = useState(false);
  const [addPartForLineIndex, setAddPartForLineIndex] = useState<number | null>(null);
  const [selectedReceiveItem, setSelectedReceiveItem] = useState<ReceivingQueueItem | null>(null);
  const [selectedInspectItem, setSelectedInspectItem] = useState<PendingInspection | null>(null);

  const [newPO, setNewPO] = useState({
    vendor_id: 0,
    required_date: '',
    notes: '',
    lines: [] as Array<{ part_id: number; quantity_ordered: number; unit_price: number }>
  });

  const [newVendor, setNewVendor] = useState({
    code: '',
    name: '',
    contact_name: '',
    email: '',
    phone: '',
    is_approved: false,
    lead_time_days: 14
  });

  const [receiveForm, setReceiveForm] = useState({
    quantity_received: 0,
    lot_number: '',
    cert_number: '',
    location_id: 0,
    requires_inspection: true,
    packing_slip_number: '',
    notes: ''
  });

  const [newPart, setNewPart] = useState({
    part_number: '',
    name: '',
    description: '',
    part_type: 'purchased',
    unit_of_measure: 'EA',
    unit_cost: 0
  });

  const [inspectForm, setInspectForm] = useState({
    quantity_accepted: 0,
    quantity_rejected: 0,
    status: 'accepted',
    inspection_notes: ''
  });

  const isPartialReceivingItem = (item: ReceivingQueueItem) => (
    item.quantity_received > 0 && item.quantity_remaining > 0
  );

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [vendorsRes, posRes, queueRes, inspectRes, partsRes, locsRes] = await Promise.all([
        api.getVendors(),
        api.getPurchaseOrders(),
        api.getReceivingQueue(),
        api.getPendingInspection(),
        api.getParts({ active_only: true }),
        api.getInventoryLocations()
      ]);
      setVendors(vendorsRes);
      setPurchaseOrders(posRes);
      setReceivingQueue(queueRes);
      setPendingInspection(inspectRes);
      setParts(partsRes);
      setLocations(locsRes);
    } catch (err) {
      console.error('Failed to load purchasing data:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleCreatePO = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPO.lines.length === 0) {
      alert('Please add at least one line item');
      return;
    }
    try {
      await api.createPurchaseOrder(newPO);
      setShowPOModal(false);
      setNewPO({ vendor_id: 0, required_date: '', notes: '', lines: [] });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create PO');
    }
  };

  const handleSendPO = async (poId: number) => {
    if (!window.confirm('Send this PO to vendor?')) return;
    try {
      await api.sendPurchaseOrder(poId);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to send PO');
    }
  };

  const handlePrintPO = (poId: number) => {
    window.open(`/print/purchase-order/${poId}?autoprint=1`, '_blank');
  };

  const handleCreateVendor = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createVendor(newVendor);
      setShowVendorModal(false);
      setNewVendor({ code: '', name: '', contact_name: '', email: '', phone: '', is_approved: false, lead_time_days: 14 });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create vendor');
    }
  };

  const openAddPartModal = (lineIndex: number) => {
    setAddPartForLineIndex(lineIndex);
    setNewPart({
      part_number: '',
      name: '',
      description: '',
      part_type: 'purchased',
      unit_of_measure: 'EA',
      unit_cost: 0
    });
    setShowAddPartModal(true);
  };

  const handleCreatePart = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const createdPart = await api.createPart(newPart);
      // Add to local parts list
      setParts([...parts, createdPart]);
      // Update the PO line with the new part
      if (addPartForLineIndex !== null) {
        const lines = [...newPO.lines];
        lines[addPartForLineIndex] = { 
          ...lines[addPartForLineIndex], 
          part_id: createdPart.id,
          unit_price: newPart.unit_cost
        };
        setNewPO({ ...newPO, lines });
      }
      setShowAddPartModal(false);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create part');
    }
  };

  const openReceiveModal = (item: ReceivingQueueItem) => {
    setSelectedReceiveItem(item);
    setReceiveForm({
      quantity_received: item.quantity_remaining,
      lot_number: '',
      cert_number: '',
      location_id: 0,
      requires_inspection: true,
      packing_slip_number: '',
      notes: ''
    });
    setShowReceiveModal(true);
  };

  const handleReceive = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedReceiveItem) return;
    const receivedQty = receiveForm.quantity_received;
    const selectedLineId = selectedReceiveItem.po_line_id;
    try {
      await api.receiveMaterial({
        po_line_id: selectedReceiveItem.po_line_id,
        ...receiveForm,
        location_id: receiveForm.location_id || null
      });
      setReceivingQueue((prev) => prev
        .map((item) => {
          if (item.po_line_id !== selectedLineId) {
            return item;
          }
          const newReceived = item.quantity_received + receivedQty;
          const newRemaining = Math.max(item.quantity_remaining - receivedQty, 0);
          return {
            ...item,
            quantity_received: newReceived,
            quantity_remaining: newRemaining,
          };
        })
        .filter((item) => item.quantity_remaining > 0)
      );
      setShowReceiveModal(false);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to receive material');
    }
  };

  const openInspectModal = (item: PendingInspection) => {
    setSelectedInspectItem(item);
    setInspectForm({
      quantity_accepted: item.quantity_received,
      quantity_rejected: 0,
      status: 'accepted',
      inspection_notes: ''
    });
    setShowInspectModal(true);
  };

  const handleInspect = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedInspectItem) return;
    try {
      await api.inspectReceipt(selectedInspectItem.receipt_id, inspectForm);
      setShowInspectModal(false);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to complete inspection');
    }
  };

  const addPOLine = () => {
    setNewPO({
      ...newPO,
      lines: [...newPO.lines, { part_id: 0, quantity_ordered: 1, unit_price: 0 }]
    });
  };

  const updatePOLine = (index: number, field: string, value: any) => {
    const lines = [...newPO.lines];
    lines[index] = { ...lines[index], [field]: value };
    setNewPO({ ...newPO, lines });
  };

  const removePOLine = (index: number) => {
    setNewPO({ ...newPO, lines: newPO.lines.filter((_, i) => i !== index) });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Purchasing & Receiving</h1>
        <div className="flex gap-2">
          <button onClick={() => setShowVendorModal(true)} className="btn-secondary flex items-center">
            <BuildingOfficeIcon className="h-5 w-5 mr-2" />
            New Vendor
          </button>
          <button onClick={() => setShowPOModal(true)} className="btn-primary flex items-center">
            <PlusIcon className="h-5 w-5 mr-2" />
            New PO
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="card bg-blue-50 border-blue-200">
          <div className="flex items-center">
            <TruckIcon className="h-8 w-8 text-blue-600 mr-3" />
            <div>
              <p className="text-sm text-blue-600">Awaiting Receipt</p>
              <p className="text-2xl font-bold text-blue-800">{receivingQueue.length}</p>
            </div>
          </div>
        </div>
        <div className="card bg-yellow-50 border-yellow-200">
          <div className="flex items-center">
            <ClipboardDocumentCheckIcon className="h-8 w-8 text-yellow-600 mr-3" />
            <div>
              <p className="text-sm text-yellow-600">Pending Inspection</p>
              <p className="text-2xl font-bold text-yellow-800">{pendingInspection.length}</p>
            </div>
          </div>
        </div>
        <div className="card bg-indigo-50 border-indigo-200">
          <div className="flex items-center">
            <div className="h-8 w-8 bg-indigo-600 rounded-full flex items-center justify-center text-white font-bold mr-3">
              PO
            </div>
            <div>
              <p className="text-sm text-indigo-600">Open POs</p>
              <p className="text-2xl font-bold text-indigo-800">{purchaseOrders.length}</p>
            </div>
          </div>
        </div>
        <div className="card bg-green-50 border-green-200">
          <div className="flex items-center">
            <CheckCircleIcon className="h-8 w-8 text-green-600 mr-3" />
            <div>
              <p className="text-sm text-green-600">Approved Vendors</p>
              <p className="text-2xl font-bold text-green-800">{vendors.filter(v => v.is_approved).length}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-8">
          {[
            { id: 'receiving', label: 'Receiving Queue', count: receivingQueue.length },
            { id: 'inspection', label: 'Pending Inspection', count: pendingInspection.length },
            { id: 'orders', label: 'Purchase Orders', count: purchaseOrders.length },
            { id: 'vendors', label: 'Vendors', count: vendors.length }
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`py-2 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {tab.label}
              {tab.count > 0 && (
                <span className={`ml-2 px-2 py-0.5 rounded-full text-xs ${
                  activeTab === tab.id ? 'bg-werco-primary text-white' : 'bg-gray-100'
                }`}>
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Receiving Queue Tab */}
      {activeTab === 'receiving' && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Material Awaiting Receipt</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">PO #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Ordered</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Received</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Remaining</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {receivingQueue.map((item) => (
                  <tr
                    key={item.po_line_id}
                    className={`${
                      isPartialReceivingItem(item)
                        ? 'bg-amber-50 hover:bg-amber-100'
                        : 'hover:bg-gray-50'
                    }`}
                  >
                    <td className="px-4 py-3 font-medium text-werco-primary">{item.po_number}</td>
                    <td className="px-4 py-3">{item.vendor_name}</td>
                    <td className="px-4 py-3">
                      <div className="font-medium">{item.part_number}</div>
                      <div className="text-sm text-gray-500">{item.part_name}</div>
                    </td>
                    <td className="px-4 py-3 text-right">{item.quantity_ordered}</td>
                    <td className="px-4 py-3 text-right">{item.quantity_received}</td>
                    <td className="px-4 py-3 text-right font-medium text-orange-600">{item.quantity_remaining}</td>
                    <td className="px-4 py-3">
                      {item.required_date ? format(new Date(item.required_date), 'MMM d') : '-'}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <button
                        onClick={() => openReceiveModal(item)}
                        className="btn-primary text-sm px-3 py-1"
                      >
                        Receive
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {receivingQueue.length === 0 && (
              <p className="text-center text-gray-500 py-8">No material awaiting receipt</p>
            )}
          </div>
        </div>
      )}

      {/* Pending Inspection Tab */}
      {activeTab === 'inspection' && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Pending Receiving Inspection</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Receipt #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">PO #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Lot #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Received</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {pendingInspection.map((item) => (
                  <tr key={item.receipt_id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-mono">{item.receipt_number}</td>
                    <td className="px-4 py-3">{item.po_number}</td>
                    <td className="px-4 py-3">{item.vendor_name}</td>
                    <td className="px-4 py-3">
                      <div className="font-medium">{item.part_number}</div>
                      <div className="text-sm text-gray-500">{item.part_name}</div>
                    </td>
                    <td className="px-4 py-3 text-right font-medium">{item.quantity_received}</td>
                    <td className="px-4 py-3 font-mono text-sm">{item.lot_number || '-'}</td>
                    <td className="px-4 py-3 text-sm">
                      {format(new Date(item.received_at), 'MMM d, h:mm a')}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <button
                        onClick={() => openInspectModal(item)}
                        className="btn-primary text-sm px-3 py-1"
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {pendingInspection.length === 0 && (
              <p className="text-center text-gray-500 py-8">No receipts pending inspection</p>
            )}
          </div>
        </div>
      )}

      {/* Purchase Orders Tab */}
      {activeTab === 'orders' && (
        <div className="card">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-4">
            <h2 className="text-lg font-semibold">Purchase Orders</h2>
            <input
              type="text"
              value={poSearch}
              onChange={(e) => setPoSearch(e.target.value)}
              className="input max-w-sm"
              placeholder="Search by PO # or vendor..."
            />
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">PO #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Order Date</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due Date</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Total</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Lines</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                  {purchaseOrders
                    .filter((po) => {
                      const term = poSearch.trim().toLowerCase();
                      if (!term) return true;
                      return (
                        po.po_number.toLowerCase().includes(term) ||
                        (po.vendor_name || '').toLowerCase().includes(term)
                      );
                    })
                    .map((po) => (
                  <tr key={po.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium text-werco-primary">{po.po_number}</td>
                    <td className="px-4 py-3">{po.vendor_name}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[po.status] || 'bg-gray-100'}`}>
                        {po.status.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {po.order_date ? format(new Date(po.order_date), 'MMM d, yyyy') : '-'}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {po.required_date ? format(new Date(po.required_date), 'MMM d, yyyy') : '-'}
                    </td>
                    <td className="px-4 py-3 text-right font-medium">
                      ${Number(po.total || 0).toFixed(2)}
                    </td>
                    <td className="px-4 py-3 text-center">{po.line_count}</td>
                    <td className="px-4 py-3 text-center">
                      <div className="flex items-center justify-center gap-3">
                        <button
                          onClick={() => handlePrintPO(po.id)}
                          className="text-surface-600 hover:text-werco-primary text-sm"
                        >
                          Print
                        </button>
                        {po.status === 'draft' && (
                          <button
                            onClick={() => handleSendPO(po.id)}
                            className="text-werco-primary hover:underline text-sm"
                          >
                            Send
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {purchaseOrders.length === 0 && (
              <p className="text-center text-gray-500 py-8">No purchase orders</p>
            )}
          </div>
        </div>
      )}

      {/* Vendors Tab */}
      {activeTab === 'vendors' && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Vendors</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Code</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Contact</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Approved</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">AS9100</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">ISO9001</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Lead Time</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {vendors.map((vendor) => (
                  <tr key={vendor.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-mono">{vendor.code}</td>
                    <td className="px-4 py-3 font-medium">{vendor.name}</td>
                    <td className="px-4 py-3">
                      <div>{vendor.contact_name}</div>
                      <div className="text-sm text-gray-500">{vendor.email}</div>
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_approved ? (
                        <CheckCircleIcon className="h-5 w-5 text-green-500 mx-auto" />
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_as9100_certified ? (
                        <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {vendor.is_iso9001_certified ? (
                        <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />
                      ) : (
                        <span className="text-gray-400">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right">{vendor.lead_time_days} days</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Create PO Modal */}
      {showPOModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold mb-4">Create Purchase Order</h3>
            <form onSubmit={handleCreatePO} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Vendor *</label>
                  <select
                    value={newPO.vendor_id}
                    onChange={(e) => setNewPO({ ...newPO, vendor_id: parseInt(e.target.value) })}
                    className="input"
                    required
                  >
                    <option value={0}>Select vendor...</option>
                    {vendors.filter(v => v.is_approved).map(v => (
                      <option key={v.id} value={v.id}>{v.code} - {v.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">Required Date</label>
                  <input
                    type="date"
                    value={newPO.required_date}
                    onChange={(e) => setNewPO({ ...newPO, required_date: e.target.value })}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <div className="flex justify-between items-center mb-2">
                  <label className="label">Line Items</label>
                  <button type="button" onClick={addPOLine} className="text-werco-primary text-sm hover:underline">
                    + Add Line
                  </button>
                </div>
                {newPO.lines.length > 0 && (
                  <div className="flex gap-2 mb-1 text-xs text-gray-500 font-medium">
                    <div className="flex-1">Part</div>
                    <div className="w-24">Quantity</div>
                    <div className="w-28">Unit Price ($)</div>
                    <div className="w-6"></div>
                  </div>
                )}
                {newPO.lines.map((line, idx) => (
                  <div key={idx} className="flex gap-2 mb-2 items-start">
                    <div className="flex-1">
                      <select
                        value={line.part_id}
                        onChange={(e) => updatePOLine(idx, 'part_id', parseInt(e.target.value))}
                        className="input text-sm"
                        required
                      >
                        <option value={0}>Select part...</option>
                        {parts.map(p => (
                          <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => openAddPartModal(idx)}
                        className="text-werco-primary text-xs hover:underline mt-1"
                      >
                        + New Part
                      </button>
                    </div>
                    <div className="w-24">
                      <input
                        type="number"
                        value={line.quantity_ordered}
                        onChange={(e) => updatePOLine(idx, 'quantity_ordered', parseFloat(e.target.value))}
                        className="input text-sm"
                        min={1}
                        required
                      />
                    </div>
                    <div className="w-28">
                      <input
                        type="number"
                        value={line.unit_price}
                        onChange={(e) => updatePOLine(idx, 'unit_price', parseFloat(e.target.value))}
                        className="input text-sm"
                        step={0.01}
                        min={0}
                        required
                      />
                    </div>
                    <button type="button" onClick={() => removePOLine(idx)} className="text-red-500 hover:text-red-700 mt-2">
                      &times;
                    </button>
                  </div>
                ))}
                {newPO.lines.length === 0 && (
                  <p className="text-gray-500 text-sm">Click "+ Add Line" to add items</p>
                )}
              </div>

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={newPO.notes}
                  onChange={(e) => setNewPO({ ...newPO, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowPOModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create PO</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Create Vendor Modal */}
      {showVendorModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create Vendor</h3>
            <form onSubmit={handleCreateVendor} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Code *</label>
                  <input
                    type="text"
                    value={newVendor.code}
                    onChange={(e) => setNewVendor({ ...newVendor, code: e.target.value })}
                    className="input"
                    placeholder="VND-001"
                    required
                  />
                </div>
                <div>
                  <label className="label">Lead Time (days)</label>
                  <input
                    type="number"
                    value={newVendor.lead_time_days}
                    onChange={(e) => setNewVendor({ ...newVendor, lead_time_days: parseInt(e.target.value) })}
                    className="input"
                    min={1}
                  />
                </div>
              </div>
              <div>
                <label className="label">Name *</label>
                <input
                  type="text"
                  value={newVendor.name}
                  onChange={(e) => setNewVendor({ ...newVendor, name: e.target.value })}
                  className="input"
                  required
                />
              </div>
              <div>
                <label className="label">Contact Name</label>
                <input
                  type="text"
                  value={newVendor.contact_name}
                  onChange={(e) => setNewVendor({ ...newVendor, contact_name: e.target.value })}
                  className="input"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Email</label>
                  <input
                    type="email"
                    value={newVendor.email}
                    onChange={(e) => setNewVendor({ ...newVendor, email: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Phone</label>
                  <input
                    type="text"
                    value={newVendor.phone}
                    onChange={(e) => setNewVendor({ ...newVendor, phone: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newVendor.is_approved}
                    onChange={(e) => setNewVendor({ ...newVendor, is_approved: e.target.checked })}
                    className="mr-2"
                  />
                  <span>Approved Vendor</span>
                </label>
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowVendorModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create Vendor</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Receive Material Modal */}
      {showReceiveModal && selectedReceiveItem && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Receive Material</h3>
            <div className="bg-gray-50 rounded p-3 mb-4">
              <p className="font-medium">{selectedReceiveItem.part_number}</p>
              <p className="text-sm text-gray-600">{selectedReceiveItem.part_name}</p>
              <p className="text-sm text-gray-500 mt-1">
                PO: {selectedReceiveItem.po_number} | Remaining: {selectedReceiveItem.quantity_remaining}
              </p>
            </div>
            <form onSubmit={handleReceive} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Quantity Received *</label>
                  <input
                    type="number"
                    value={receiveForm.quantity_received}
                    onChange={(e) => setReceiveForm({ ...receiveForm, quantity_received: parseFloat(e.target.value) })}
                    className="input"
                    min={0.01}
                    max={selectedReceiveItem.quantity_remaining}
                    step={0.01}
                    required
                  />
                </div>
                <div>
                  <label className="label">Location</label>
                  <select
                    value={receiveForm.location_id}
                    onChange={(e) => setReceiveForm({ ...receiveForm, location_id: parseInt(e.target.value) })}
                    className="input"
                  >
                    <option value={0}>Default (RECV-01)</option>
                    {locations.map(loc => (
                      <option key={loc.id} value={loc.id}>{loc.code} - {loc.name}</option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Lot Number</label>
                  <input
                    type="text"
                    value={receiveForm.lot_number}
                    onChange={(e) => setReceiveForm({ ...receiveForm, lot_number: e.target.value })}
                    className="input"
                    placeholder="For traceability"
                  />
                </div>
                <div>
                  <label className="label">Cert Number</label>
                  <input
                    type="text"
                    value={receiveForm.cert_number}
                    onChange={(e) => setReceiveForm({ ...receiveForm, cert_number: e.target.value })}
                    className="input"
                    placeholder="CoC #"
                  />
                </div>
              </div>
              <div>
                <label className="label">Packing Slip #</label>
                <input
                  type="text"
                  value={receiveForm.packing_slip_number}
                  onChange={(e) => setReceiveForm({ ...receiveForm, packing_slip_number: e.target.value })}
                  className="input"
                />
              </div>
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={receiveForm.requires_inspection}
                    onChange={(e) => setReceiveForm({ ...receiveForm, requires_inspection: e.target.checked })}
                    className="mr-2"
                  />
                  <span>Requires Receiving Inspection</span>
                </label>
                <p className="text-xs text-gray-500 ml-6">
                  If unchecked, material will be added directly to inventory
                </p>
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowReceiveModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Receive</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Inspection Modal */}
      {showInspectModal && selectedInspectItem && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Complete Inspection</h3>
            <div className="bg-gray-50 rounded p-3 mb-4">
              <p className="font-medium">{selectedInspectItem.part_number}</p>
              <p className="text-sm text-gray-600">{selectedInspectItem.part_name}</p>
              <p className="text-sm text-gray-500 mt-1">
                Receipt: {selectedInspectItem.receipt_number} | Qty: {selectedInspectItem.quantity_received}
                {selectedInspectItem.lot_number && ` | Lot: ${selectedInspectItem.lot_number}`}
              </p>
            </div>
            <form onSubmit={handleInspect} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Qty Accepted</label>
                  <input
                    type="number"
                    value={inspectForm.quantity_accepted}
                    onChange={(e) => setInspectForm({ ...inspectForm, quantity_accepted: parseFloat(e.target.value) })}
                    className="input"
                    min={0}
                    max={selectedInspectItem.quantity_received}
                    step={0.01}
                  />
                </div>
                <div>
                  <label className="label">Qty Rejected</label>
                  <input
                    type="number"
                    value={inspectForm.quantity_rejected}
                    onChange={(e) => setInspectForm({ ...inspectForm, quantity_rejected: parseFloat(e.target.value) })}
                    className="input"
                    min={0}
                    max={selectedInspectItem.quantity_received}
                    step={0.01}
                  />
                </div>
              </div>
              <div>
                <label className="label">Disposition</label>
                <select
                  value={inspectForm.status}
                  onChange={(e) => setInspectForm({ ...inspectForm, status: e.target.value })}
                  className="input"
                >
                  <option value="accepted">Accept - Add to Inventory</option>
                  <option value="rejected">Reject - Return to Vendor</option>
                  <option value="quarantine">Quarantine - Hold for Review</option>
                </select>
              </div>
              <div>
                <label className="label">Inspection Notes</label>
                <textarea
                  value={inspectForm.inspection_notes}
                  onChange={(e) => setInspectForm({ ...inspectForm, inspection_notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowInspectModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Complete Inspection</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Add New Part Modal */}
      {showAddPartModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Add New Part</h3>
            <form onSubmit={handleCreatePart} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number *</label>
                  <input
                    type="text"
                    value={newPart.part_number}
                    onChange={(e) => setNewPart({ ...newPart, part_number: e.target.value })}
                    className="input"
                    placeholder="e.g., RAW-001"
                    required
                  />
                </div>
                <div>
                  <label className="label">Type</label>
                  <select
                    value={newPart.part_type}
                    onChange={(e) => setNewPart({ ...newPart, part_type: e.target.value })}
                    className="input"
                  >
                    <option value="purchased">Purchased</option>
                    <option value="raw_material">Raw Material</option>
                    <option value="manufactured">Manufactured</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="label">Name *</label>
                <input
                  type="text"
                  value={newPart.name}
                  onChange={(e) => setNewPart({ ...newPart, name: e.target.value })}
                  className="input"
                  placeholder="Part description"
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Unit of Measure</label>
                  <select
                    value={newPart.unit_of_measure}
                    onChange={(e) => setNewPart({ ...newPart, unit_of_measure: e.target.value })}
                    className="input"
                  >
                    <option value="EA">Each (EA)</option>
                    <option value="FT">Feet (FT)</option>
                    <option value="IN">Inches (IN)</option>
                    <option value="LB">Pounds (LB)</option>
                    <option value="KG">Kilograms (KG)</option>
                    <option value="GAL">Gallons (GAL)</option>
                    <option value="SHT">Sheets (SHT)</option>
                    <option value="BOX">Box (BOX)</option>
                  </select>
                </div>
                <div>
                  <label className="label">Unit Cost ($)</label>
                  <input
                    type="number"
                    value={newPart.unit_cost}
                    onChange={(e) => setNewPart({ ...newPart, unit_cost: parseFloat(e.target.value) || 0 })}
                    className="input"
                    step={0.01}
                    min={0}
                  />
                </div>
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={newPart.description}
                  onChange={(e) => setNewPart({ ...newPart, description: e.target.value })}
                  className="input"
                  rows={2}
                  placeholder="Optional details"
                />
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowAddPartModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create Part</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
