import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  TruckIcon,
  PaperAirplaneIcon,
  PrinterIcon,
} from '@heroicons/react/24/outline';

interface Shipment {
  id: number;
  shipment_number: string;
  work_order_id: number;
  work_order_number?: string;
  customer_name?: string;
  part_number?: string;
  status: string;
  ship_to_name?: string;
  carrier?: string;
  tracking_number?: string;
  quantity_shipped: number;
  ship_date?: string;
  created_at: string;
}

interface ReadyToShip {
  work_order_id: number;
  work_order_number: string;
  part_number?: string;
  part_name?: string;
  customer_name?: string;
  quantity_complete: number;
  due_date?: string;
}

const statusColors: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800',
  packed: 'bg-blue-100 text-blue-800',
  shipped: 'bg-green-100 text-green-800',
  delivered: 'bg-emerald-100 text-emerald-800',
  cancelled: 'bg-red-100 text-red-800',
};

export default function Shipping() {
  const [shipments, setShipments] = useState<Shipment[]>([]);
  const [readyToShip, setReadyToShip] = useState<ReadyToShip[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedWO, setSelectedWO] = useState<ReadyToShip | null>(null);

  const [shipForm, setShipForm] = useState({
    ship_to_name: '',
    carrier: '',
    quantity_shipped: 0,
    weight_lbs: 0,
    num_packages: 1,
    cert_of_conformance: true,
    packing_notes: ''
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [shipmentsRes, readyRes] = await Promise.all([
        api.getShipments(),
        api.getReadyToShip()
      ]);
      setShipments(shipmentsRes);
      setReadyToShip(readyRes);
    } catch (err) {
      console.error('Failed to load shipping data:', err);
    } finally {
      setLoading(false);
    }
  };

  const openCreateModal = (wo: ReadyToShip) => {
    setSelectedWO(wo);
    setShipForm({
      ship_to_name: wo.customer_name || '',
      carrier: '',
      quantity_shipped: wo.quantity_complete,
      weight_lbs: 0,
      num_packages: 1,
      cert_of_conformance: true,
      packing_notes: ''
    });
    setShowCreateModal(true);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedWO) return;

    try {
      await api.createShipment({
        work_order_id: selectedWO.work_order_id,
        ...shipForm
      });
      setShowCreateModal(false);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create shipment');
    }
  };

  const handleShip = async (shipmentId: number) => {
    const tracking = prompt('Enter tracking number (optional):');
    try {
      await api.markShipped(shipmentId, tracking || undefined);
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to mark as shipped');
    }
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
        <h1 className="text-2xl font-bold text-gray-900">Shipping</h1>
      </div>

      {/* Ready to Ship */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Ready to Ship ({readyToShip.length})</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {readyToShip.map((wo) => (
                <tr key={wo.work_order_id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-werco-primary">{wo.work_order_number}</td>
                  <td className="px-4 py-3">
                    <div className="font-medium">{wo.part_number}</div>
                    <div className="text-sm text-gray-500">{wo.part_name}</div>
                  </td>
                  <td className="px-4 py-3">{wo.customer_name || '-'}</td>
                  <td className="px-4 py-3 text-right font-medium">{wo.quantity_complete}</td>
                  <td className="px-4 py-3">
                    {wo.due_date ? format(new Date(wo.due_date), 'MMM d') : '-'}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <button
                      onClick={() => openCreateModal(wo)}
                      className="btn-primary text-sm px-3 py-1"
                    >
                      <TruckIcon className="h-4 w-4 inline mr-1" />
                      Create Shipment
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {readyToShip.length === 0 && (
            <p className="text-center text-gray-500 py-8">No work orders ready to ship</p>
          )}
        </div>
      </div>

      {/* Recent Shipments */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Recent Shipments</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Shipment #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Carrier</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Tracking</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {shipments.map((s) => (
                <tr key={s.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-mono">{s.shipment_number}</td>
                  <td className="px-4 py-3 font-medium">{s.work_order_number}</td>
                  <td className="px-4 py-3">{s.customer_name || s.ship_to_name || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusColors[s.status]}`}>
                      {s.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">{s.carrier || '-'}</td>
                  <td className="px-4 py-3 font-mono text-sm">{s.tracking_number || '-'}</td>
                  <td className="px-4 py-3 text-right">{s.quantity_shipped}</td>
                  <td className="px-4 py-3 text-center space-x-2">
                    {s.status === 'pending' && (
                      <button
                        onClick={() => handleShip(s.id)}
                        className="text-green-600 hover:text-green-800 text-sm"
                      >
                        <PaperAirplaneIcon className="h-5 w-5 inline" /> Ship
                      </button>
                    )}
                    <button
                      onClick={() => window.open(`/print/packing-slip/${s.id}`, '_blank')}
                      className="text-blue-600 hover:text-blue-800 text-sm"
                      title="Print Packing Slip"
                    >
                      <PrinterIcon className="h-5 w-5 inline" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {shipments.length === 0 && (
            <p className="text-center text-gray-500 py-8">No shipments yet</p>
          )}
        </div>
      </div>

      {/* Create Shipment Modal */}
      {showCreateModal && selectedWO && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create Shipment</h3>
            <div className="bg-gray-50 rounded p-3 mb-4">
              <p className="font-medium">{selectedWO.work_order_number}</p>
              <p className="text-sm text-gray-600">{selectedWO.part_number} - {selectedWO.part_name}</p>
            </div>
            <form onSubmit={handleCreate} className="space-y-4">
              <div>
                <label className="label">Ship To</label>
                <input
                  type="text"
                  value={shipForm.ship_to_name}
                  onChange={(e) => setShipForm({ ...shipForm, ship_to_name: e.target.value })}
                  className="input"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Carrier</label>
                  <select
                    value={shipForm.carrier}
                    onChange={(e) => setShipForm({ ...shipForm, carrier: e.target.value })}
                    className="input"
                  >
                    <option value="">Select...</option>
                    <option value="UPS">UPS</option>
                    <option value="FedEx">FedEx</option>
                    <option value="USPS">USPS</option>
                    <option value="Freight">Freight</option>
                    <option value="Customer Pickup">Customer Pickup</option>
                  </select>
                </div>
                <div>
                  <label className="label">Qty to Ship</label>
                  <input
                    type="number"
                    value={shipForm.quantity_shipped}
                    onChange={(e) => setShipForm({ ...shipForm, quantity_shipped: parseFloat(e.target.value) })}
                    className="input"
                    min={1}
                    max={selectedWO.quantity_complete}
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Weight (lbs)</label>
                  <input
                    type="number"
                    value={shipForm.weight_lbs}
                    onChange={(e) => setShipForm({ ...shipForm, weight_lbs: parseFloat(e.target.value) })}
                    className="input"
                    step={0.1}
                  />
                </div>
                <div>
                  <label className="label"># Packages</label>
                  <input
                    type="number"
                    value={shipForm.num_packages}
                    onChange={(e) => setShipForm({ ...shipForm, num_packages: parseInt(e.target.value) })}
                    className="input"
                    min={1}
                  />
                </div>
              </div>
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={shipForm.cert_of_conformance}
                    onChange={(e) => setShipForm({ ...shipForm, cert_of_conformance: e.target.checked })}
                    className="mr-2"
                  />
                  Include Certificate of Conformance
                </label>
              </div>
              <div>
                <label className="label">Packing Notes</label>
                <textarea
                  value={shipForm.packing_notes}
                  onChange={(e) => setShipForm({ ...shipForm, packing_notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Create Shipment</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
