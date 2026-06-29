import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { formatCentralDate } from '../utils/centralTime';
import { useToast } from '../components/ui/Toast';
import { EmptyState, ErrorState } from '../components/ui';
import { Modal } from '../components/ui/Modal';
import usePermissions from '../hooks/usePermissions';
import ScheduleShipmentModal, { ScheduleShipmentTarget } from '../components/shipping/ScheduleShipmentModal';
import ShipmentTrackingPanel from '../components/shipping/ShipmentTrackingPanel';
import {
  TruckIcon,
  PaperAirplaneIcon,
  PrinterIcon,
  ChevronDownIcon,
  ChevronRightIcon,
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
  tracking_status?: string;
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
  pending: 'bg-fd-amber/15 text-fd-amber',
  packed: 'bg-fd-blue/15 text-fd-blue',
  shipped: 'bg-fd-green/15 text-fd-green',
  delivered: 'bg-fd-green/15 text-fd-green',
  cancelled: 'bg-fd-red/15 text-fd-red',
};

const trackingBadge: Record<string, string> = {
  delivered: 'bg-fd-green/15 text-fd-green',
  out_for_delivery: 'bg-fd-blue/15 text-fd-blue',
  in_transit: 'bg-fd-blue/15 text-fd-blue',
  pre_transit: 'bg-fd-amber/15 text-fd-amber',
};

export default function Shipping({ embedded }: { embedded?: boolean }) {
  const { showToast } = useToast();
  const { can } = usePermissions();
  // Carrier writes (rate-shop / buy-label / void) are gated server-side to the
  // ADMIN / MANAGER / SUPERVISOR / SHIPPING set -- the frontend equivalent is the
  // shipping:complete permission, held by exactly that role set.
  const canSchedule = can('shipping:complete');

  const [shipments, setShipments] = useState<Shipment[]>([]);
  const [readyToShip, setReadyToShip] = useState<ReadyToShip[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedWO, setSelectedWO] = useState<ReadyToShip | null>(null);

  // Carrier Schedule-Shipment wizard target (a shipment that exists).
  const [scheduleTarget, setScheduleTarget] = useState<ScheduleShipmentTarget | null>(null);
  const [schedulingWoId, setSchedulingWoId] = useState<number | null>(null);
  // Expanded tracking rows.
  const [expandedTracking, setExpandedTracking] = useState<Record<number, boolean>>({});

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
    setLoadError(false);
    try {
      const [shipmentsRes, readyRes] = await Promise.all([
        api.getShipments(),
        api.getReadyToShip()
      ]);
      setShipments(shipmentsRes);
      setReadyToShip(readyRes);
    } catch (err) {
      console.error('Failed to load shipping data:', err);
      setLoadError(true);
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
      showToast('error', err.response?.data?.detail || 'Failed to create shipment');
    }
  };

  const handleShip = async (shipmentId: number) => {
    try {
      await api.markShipped(shipmentId);
      showToast('success', 'Shipment marked as shipped');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to mark as shipped');
    }
  };

  // Schedule a carrier shipment for an existing shipment row.
  const scheduleForShipment = (s: Shipment) => {
    setScheduleTarget({
      shipment_id: s.id,
      shipment_number: s.shipment_number,
      work_order_number: s.work_order_number,
      ship_to_name: s.ship_to_name,
      customer_name: s.customer_name,
      part_number: s.part_number,
    });
  };

  // From a ready-to-ship WO: create the shipment first (the carrier flow needs a
  // shipment id), then open the wizard against it.
  const scheduleForWorkOrder = async (wo: ReadyToShip) => {
    setSchedulingWoId(wo.work_order_id);
    try {
      const created = await api.createShipment({
        work_order_id: wo.work_order_id,
        ship_to_name: wo.customer_name || '',
        quantity_shipped: wo.quantity_complete,
        num_packages: 1,
        cert_of_conformance: true,
      });
      await loadData();
      setScheduleTarget({
        shipment_id: created.id,
        shipment_number: created.shipment_number,
        work_order_number: wo.work_order_number,
        ship_to_name: wo.customer_name,
        customer_name: wo.customer_name,
        part_number: wo.part_number,
      });
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to start shipment');
    } finally {
      setSchedulingWoId(null);
    }
  };

  const toggleTracking = (id: number) =>
    setExpandedTracking((prev) => ({ ...prev, [id]: !prev[id] }));

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {!embedded && (
        <div className="flex justify-between items-center">
          <h1 className="text-2xl font-bold text-white">Shipping</h1>
        </div>
      )}

      {loadError && (
        <ErrorState
          message="Could not load shipping data."
          onRetry={loadData}
        />
      )}

      {/* Ready to Ship */}
      {!loadError && (
      <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
        <h2 className="text-lg font-semibold mb-4">Ready to Ship ({readyToShip.length})</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-fd-line">
            <thead className="bg-fd-sunken">
              <tr>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">WO #</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Customer</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Due</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-fd-line">
              {readyToShip.map((wo) => (
                <tr key={wo.work_order_id} className="hover:bg-fd-sunken">
                  <td className="px-4 py-2 font-medium text-werco-primary">{wo.work_order_number}</td>
                  <td className="px-4 py-2 min-w-0">
                    <div className="font-medium truncate">{wo.part_number}</div>
                    <div className="text-sm text-slate-400 truncate">{wo.part_name}</div>
                  </td>
                  <td className="px-4 py-2 min-w-0 truncate">{wo.customer_name || '-'}</td>
                  <td className="px-4 py-2 text-right font-medium tabular-nums">{wo.quantity_complete}</td>
                  <td className="px-4 py-2 tabular-nums">
                    {wo.due_date ? formatCentralDate(wo.due_date, { year: undefined }) : '-'}
                  </td>
                  <td className="px-4 py-2 text-center space-x-2 whitespace-nowrap">
                    {canSchedule && (
                      <button
                        onClick={() => scheduleForWorkOrder(wo)}
                        disabled={schedulingWoId === wo.work_order_id}
                        className="btn-primary text-sm px-3 py-1 disabled:opacity-60"
                      >
                        <PaperAirplaneIcon className="h-4 w-4 inline mr-1" />
                        {schedulingWoId === wo.work_order_id ? 'Starting…' : 'Schedule Shipment'}
                      </button>
                    )}
                    <button
                      onClick={() => openCreateModal(wo)}
                      className="btn-secondary text-sm px-3 py-1"
                    >
                      <TruckIcon className="h-4 w-4 inline mr-1" />
                      Manual
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {readyToShip.length === 0 && (
            <EmptyState
              icon={TruckIcon}
              title="No work orders ready to ship"
              description="Completed work orders awaiting shipment will appear here."
            />
          )}
        </div>
      </div>
      )}

      {/* Recent Shipments */}
      {!loadError && (
      <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
        <h2 className="text-lg font-semibold mb-4">Recent Shipments</h2>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-fd-line">
            <thead className="bg-fd-sunken">
              <tr>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase w-8"></th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Shipment #</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">WO #</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Customer</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Carrier</th>
                <th className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase">Tracking</th>
                <th className="px-4 py-2 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                <th className="px-4 py-2 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-fd-line">
              {shipments.map((s) => {
                const expanded = !!expandedTracking[s.id];
                return (
                  <React.Fragment key={s.id}>
                    <tr className="hover:bg-fd-sunken">
                      <td className="px-4 py-2">
                        <button
                          onClick={() => toggleTracking(s.id)}
                          className="text-slate-500 hover:text-werco-primary"
                          title="Show tracking"
                        >
                          {expanded ? (
                            <ChevronDownIcon className="h-4 w-4" />
                          ) : (
                            <ChevronRightIcon className="h-4 w-4" />
                          )}
                        </button>
                      </td>
                      <td className="px-4 py-2 font-mono">{s.shipment_number}</td>
                      <td className="px-4 py-2 font-medium">{s.work_order_number}</td>
                      <td className="px-4 py-2 min-w-0 truncate">{s.customer_name || s.ship_to_name || '-'}</td>
                      <td className="px-4 py-2">
                        <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${statusColors[s.status]}`}>
                          {s.status}
                        </span>
                      </td>
                      <td className="px-4 py-2">{s.carrier || '-'}</td>
                      <td className="px-4 py-2">
                        {s.tracking_number ? (
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-sm">{s.tracking_number}</span>
                            {s.tracking_status && (
                              <span
                                className={`px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${
                                  trackingBadge[s.tracking_status.toLowerCase()] || 'bg-slate-500/20 text-slate-300'
                                }`}
                              >
                                {s.tracking_status.replace(/_/g, ' ')}
                              </span>
                            )}
                          </div>
                        ) : (
                          '-'
                        )}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums">{s.quantity_shipped}</td>
                      <td className="px-4 py-2 text-center space-x-2 whitespace-nowrap">
                        {canSchedule && s.status !== 'cancelled' && (
                          <button
                            onClick={() => scheduleForShipment(s)}
                            className="text-werco-primary hover:text-blue-300 text-sm"
                            title="Schedule carrier shipment"
                          >
                            <PaperAirplaneIcon className="h-5 w-5 inline" /> Schedule
                          </button>
                        )}
                        {s.status === 'pending' && canSchedule && (
                          <button
                            onClick={() => handleShip(s.id)}
                            className="text-green-600 hover:text-emerald-300 text-sm"
                            title="Mark shipped (manual)"
                          >
                            <TruckIcon className="h-5 w-5 inline" /> Ship
                          </button>
                        )}
                        <button
                          onClick={() => window.open(`/print/packing-slip/${s.id}`, '_blank')}
                          className="text-blue-600 hover:text-blue-300 text-sm"
                          title="Print Packing Slip"
                        >
                          <PrinterIcon className="h-5 w-5 inline" />
                        </button>
                      </td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={9} className="p-0">
                          <ShipmentTrackingPanel shipmentId={s.id} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
          {shipments.length === 0 && (
            <EmptyState
              icon={PaperAirplaneIcon}
              title="No shipments yet"
              description="Shipments you create will appear here."
            />
          )}
        </div>
      </div>
      )}

      {/* Carrier Schedule-Shipment wizard */}
      {scheduleTarget && (
        <ScheduleShipmentModal
          target={scheduleTarget}
          onClose={() => setScheduleTarget(null)}
          onCompleted={loadData}
        />
      )}

      {/* Create Shipment Modal (legacy / manual path -- still supported) */}
      <Modal
        open={showCreateModal && !!selectedWO}
        onClose={() => setShowCreateModal(false)}
        size="md"
        closeOnBackdrop={false}
      >
        {selectedWO && (
          <>
            <h3 className="text-lg font-semibold mb-4">Create Shipment (Manual)</h3>
            <div className="bg-fd-sunken border border-fd-line rounded-sm p-3 mb-4">
              <p className="font-medium">{selectedWO.work_order_number}</p>
              <p className="text-sm text-slate-400">{selectedWO.part_number} - {selectedWO.part_name}</p>
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
          </>
        )}
      </Modal>
    </div>
  );
}
