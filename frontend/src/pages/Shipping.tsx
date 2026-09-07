import { useTableWorkspace } from '../hooks/useTableWorkspace';
import { TableWorkspaceControls } from '../components/ui/TableWorkspaceControls';
import { PageHeader, RecordHeader } from '../components/ui/PageHeader';
import React, { useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import { Link } from 'react-router-dom';
import { useQueuedSearchParams } from '../hooks/useQueuedSearchParams';
import { formatCentralDate } from '../utils/centralTime';
import { useToast } from '../components/ui/Toast';
import {
  Button,
  EmptyState,
  ErrorState,
  FormField,
  DataTable,
  DataTableColumn,
  MobileDataCard,
  MobileDataList,
  statusColor,
} from '../components/ui';
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
  quantity_remaining: number;
  quantity_reserved: number;
  quantity_shipped: number;
  due_date?: string;
}

export default function Shipping({ embedded }: { embedded?: boolean }) {
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useQueuedSearchParams();
  const search = searchParams.get('shippingSearch') || '';
  const shippingStatus = searchParams.get('shippingStatus') || '';
  const shippingFrom = searchParams.get('shippingFrom') || '';
  const shippingTo = searchParams.get('shippingTo') || '';
  const [shipmentPage, setShipmentPage] = useState(0);
  const updateShippingFilter = (key: string, value: string) => setSearchParams(previous => {
    const next = new URLSearchParams(previous);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  }, { replace: true });
  useEffect(() => setShipmentPage(0), [search, shippingStatus, shippingFrom, shippingTo]);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');
  const [actingId, setActingId] = useState<number | null>(null);
  const [editTarget, setEditTarget] = useState<Shipment | null>(null);
  const [editQuantity, setEditQuantity] = useState('');
  const [editError, setEditError] = useState('');
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
  const [newScheduledShipment, setNewScheduledShipment] = useState<number | null>(null);
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
    packing_notes: '',
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoadError(false);
    try {
      const [shipmentsRes, readyRes] = await Promise.all([api.getShipments(), api.getReadyToShip()]);
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
    setFormError('');
    setSelectedWO(wo);
    setShipForm({
      ship_to_name: wo.customer_name || '',
      carrier: '',
      quantity_shipped: wo.quantity_remaining ?? wo.quantity_complete,
      weight_lbs: 0,
      num_packages: 1,
      cert_of_conformance: true,
      packing_notes: '',
    });
    setShowCreateModal(true);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedWO || saving) return;
    if (
      !Number.isFinite(shipForm.quantity_shipped) ||
      shipForm.quantity_shipped <= 0 ||
      shipForm.quantity_shipped > (selectedWO.quantity_remaining ?? selectedWO.quantity_complete)
    ) {
      setFormError('Enter a positive quantity within the available remainder.');
      return;
    }
    setSaving(true);
    setFormError('');
    try {
      await api.createShipment({
        work_order_id: selectedWO.work_order_id,
        ...shipForm,
      });
      setShowCreateModal(false);
      loadData();
    } catch (err: any) {
      setFormError(err.response?.data?.detail || 'Failed to create shipment');
    } finally {
      setSaving(false);
    }
  };

  const handleShip = async (shipmentId: number) => {
    if (actingId !== null) return;
    setActingId(shipmentId);
    try {
      await api.markShipped(shipmentId);
      showToast('success', 'Shipment marked as shipped');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to mark as shipped');
    } finally {
      setActingId(null);
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
        quantity_shipped: wo.quantity_remaining ?? wo.quantity_complete,
        num_packages: 1,
        cert_of_conformance: true,
      });
      setNewScheduledShipment(created.id);
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

  const toggleTracking = (id: number) => setExpandedTracking(prev => ({ ...prev, [id]: !prev[id] }));

  // ---- Ready-to-Ship row actions (shared by table + mobile cards) ----
  const renderReadyActions = (wo: ReadyToShip) => (
    <>
      {canSchedule && (
        <Button
          size="sm"
          onClick={e => {
            e.stopPropagation();
            scheduleForWorkOrder(wo);
          }}
          disabled={schedulingWoId === wo.work_order_id}
        >
          <PaperAirplaneIcon className="h-4 w-4 inline mr-1" />
          {schedulingWoId === wo.work_order_id ? 'Starting…' : 'Schedule Shipment'}
        </Button>
      )}
      <Button
        variant="secondary"
        size="sm"
        onClick={e => {
          e.stopPropagation();
          openCreateModal(wo);
        }}
      >
        <TruckIcon className="h-4 w-4 inline mr-1" />
        Manual
      </Button>
    </>
  );

  // ---- Ready-to-Ship columns ----
  const readyColumns = useMemo<Array<DataTableColumn<ReadyToShip>>>(
    () => [
      {
        key: 'work_order_number',
        header: 'WO #',
        sortable: true,
        accessor: wo => wo.work_order_number,
        className: 'font-medium text-werco-primary',
      },
      {
        key: 'part',
        header: 'Part',
        sortable: true,
        accessor: wo => wo.part_number ?? '',
        csv: wo => `${wo.part_number ?? ''} ${wo.part_name ?? ''}`.trim(),
        render: wo => (
          <div className="min-w-0">
            <div className="font-medium truncate">{wo.part_number}</div>
            <div className="text-sm text-slate-400 truncate">{wo.part_name}</div>
          </div>
        ),
      },
      {
        key: 'customer',
        header: 'Customer',
        sortable: true,
        accessor: wo => wo.customer_name ?? '',
        render: wo => <span className="truncate">{wo.customer_name || '-'}</span>,
      },
      {
        key: 'qty',
        header: 'Available to ship',
        sortable: true,
        align: 'right',
        className: 'font-medium tabular-nums',
        accessor: wo => wo.quantity_remaining ?? wo.quantity_complete,
        render: wo => (
          <div>
            {wo.quantity_remaining ?? wo.quantity_complete}
            <div className="text-xs text-slate-400">
              {wo.quantity_shipped || 0} shipped · {wo.quantity_reserved || 0} reserved · {wo.quantity_complete}{' '}
              completed
            </div>
          </div>
        ),
      },
      {
        key: 'due',
        header: 'Due',
        sortable: true,
        className: 'tabular-nums',
        accessor: wo => wo.due_date ?? '',
        render: wo => (wo.due_date ? formatCentralDate(wo.due_date, { year: undefined }) : '-'),
      },
      {
        key: 'actions',
        header: 'Actions',
        align: 'center',
        className: 'whitespace-nowrap space-x-2',
        render: wo => renderReadyActions(wo),
      },
    ],
    [canSchedule, schedulingWoId]
  );

  const renderReadyCard = (wo: ReadyToShip) => (
    <MobileDataCard
      title={wo.work_order_number}
      subtitle={`${wo.part_number ?? ''}${wo.part_name ? ` — ${wo.part_name}` : ''}`}
      fields={[
        { label: 'Customer', value: wo.customer_name || '-' },
        {
          label: 'Available to ship',
          value: <span className="tabular-nums">{wo.quantity_remaining ?? wo.quantity_complete}</span>,
        },
        {
          label: 'Due',
          value: wo.due_date ? formatCentralDate(wo.due_date, { year: undefined }) : '-',
        },
      ]}
      actions={<div className="flex flex-wrap gap-2 justify-end">{renderReadyActions(wo)}</div>}
    />
  );

  const savePendingShipment = async (cancel = false) => {
    if (!editTarget || saving) return;
    if (!cancel && (!Number.isFinite(Number(editQuantity)) || Number(editQuantity) <= 0)) {
      setEditError('Enter a positive quantity.');
      return;
    }
    setSaving(true);
    setEditError('');
    try {
      await api.updateShipment(
        editTarget.id,
        cancel ? { status: 'cancelled' } : { quantity_shipped: Number(editQuantity) }
      );
      setEditTarget(null);
      await loadData();
      showToast('success', cancel ? 'Shipment cancelled; quantity is available again' : 'Shipment quantity updated');
    } catch (err: any) {
      setEditError(err.response?.data?.detail || 'Unable to update shipment');
    } finally {
      setSaving(false);
    }
  };
  const matchesSearch = (row: object) =>
    Object.values(row).some(value =>
      String(value ?? '')
        .toLowerCase()
        .includes(search.toLowerCase())
    );
  const visibleShipments = shipments.filter(
    s =>
      matchesSearch(s) &&
      (!shippingStatus || s.status === shippingStatus) &&
      (!shippingFrom || (s.ship_date || s.created_at.slice(0, 10)) >= shippingFrom) &&
      (!shippingTo || (s.ship_date || s.created_at.slice(0, 10)) <= shippingTo)
  );
  const shipmentPages = Math.max(1, Math.ceil(visibleShipments.length / 25));
  const currentShipmentPage = Math.min(shipmentPage, shipmentPages - 1);
  const pagedShipments = visibleShipments.slice(currentShipmentPage * 25, (currentShipmentPage + 1) * 25);
  const closeSchedule = async () => {
    setScheduleTarget(null);
    if (newScheduledShipment !== null) {
      const id = newScheduledShipment;
      setNewScheduledShipment(null);
      try {
        await api.updateShipment(id, { status: 'cancelled' });
        await loadData();
      } catch (err: any) {
        showToast(
          'error',
          err.response?.data?.detail || 'The pending shipment was retained. Use Edit / Cancel to release its quantity.'
        );
      }
    }
  };

  // ---- Recent-shipment row actions (shared by table + mobile cards) ----
  const renderShipmentActions = (s: Shipment) => (
    <>
      {canSchedule && ['pending', 'packed'].includes(s.status) && (
        <button
          type="button"
          onClick={e => {
            e.stopPropagation();
            setEditTarget(s);
            setEditQuantity(String(s.quantity_shipped));
            setEditError('');
          }}
          className="text-werco-primary text-sm"
        >
          Edit / Cancel
        </button>
      )}
      {canSchedule && ['pending', 'packed'].includes(s.status) && (
        <button
          onClick={e => {
            e.stopPropagation();
            scheduleForShipment(s);
          }}
          className="text-werco-primary hover:text-blue-300 text-sm"
          title="Schedule carrier shipment"
        >
          <PaperAirplaneIcon className="h-5 w-5 inline" /> Schedule
        </button>
      )}
      {['pending', 'packed'].includes(s.status) && canSchedule && (
        <button
          onClick={e => {
            e.stopPropagation();
            handleShip(s.id);
          }}
          className="text-green-600 hover:text-emerald-300 text-sm"
          disabled={actingId !== null}
          title="Mark shipped (manual)"
        >
          <TruckIcon className="h-5 w-5 inline" /> Ship
        </button>
      )}
      <button
        onClick={e => {
          e.stopPropagation();
          window.open(`/print/packing-slip/${s.id}`, '_blank');
        }}
        className="text-blue-600 hover:text-blue-300 text-sm"
        title="Print Packing Slip"
      >
        <PrinterIcon className="h-5 w-5 inline" />
      </button>
    </>
  );

  const renderTrackingBadge = (s: Shipment) =>
    s.tracking_number ? (
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">{s.tracking_number}</span>
        {s.tracking_status && (
          <span className={`px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${statusColor(s.tracking_status)}`}>
            {s.tracking_status.replace(/_/g, ' ')}
          </span>
        )}
      </div>
    ) : (
      <span className="text-slate-400">-</span>
    );

  const renderShipmentCard = (s: Shipment) => {
    const expanded = !!expandedTracking[s.id];
    return (
      <div key={s.id}>
        <MobileDataCard
          title={s.shipment_number}
          subtitle={s.customer_name || s.ship_to_name || undefined}
          badge={
            <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${statusColor(s.status)}`}>{s.status}</span>
          }
          onClick={() => toggleTracking(s.id)}
          fields={[
            { label: 'WO #', value: s.work_order_number || '-' },
            { label: 'Qty', value: <span className="tabular-nums">{s.quantity_shipped}</span> },
            { label: 'Carrier', value: s.carrier || '-' },
            { label: 'Tracking', value: renderTrackingBadge(s), fullWidth: true },
          ]}
          actions={<div className="flex flex-wrap gap-3 justify-end">{renderShipmentActions(s)}</div>}
        />
        {expanded && (
          <div className="mt-2">
            <ShipmentTrackingPanel shipmentId={s.id} />
          </div>
        )}
      </div>
    );
  };

  const shippingFilters = { search, shippingStatus, shippingFrom, shippingTo };
  const applyShippingFilters = (filters: Record<string, string>) => {
    const next = new URLSearchParams(searchParams);
    if (filters.search) next.set('shippingSearch', filters.search); else next.delete('shippingSearch');
    for (const key of ['shippingStatus', 'shippingFrom', 'shippingTo']) {
      if (filters[key]) next.set(key, filters[key]); else next.delete(key);
    }
    setShipmentPage(0);
    setSearchParams(next);
  };
  const readyWorkspace = useTableWorkspace('shipping', 'ready', readyColumns, { search }, filters => updateShippingFilter('shippingSearch', filters.search || ''), { key: 'due', dir: 'asc' });
  const shipmentWorkspace = useTableWorkspace('shipping', 'shipments', [{ key: 'shipment_number', header: 'Shipment' }], shippingFilters, applyShippingFilters);

  const pageHeader = (
    <PageHeader
      title="Shipping"
      level={embedded ? 2 : 1}
      description="Allocate completed quantities, arrange shipments, and follow delivery progress"
    />
  );
  if (loading) {
    return (
      <div className="space-y-6">
        {pageHeader}
        <div role="status" className="flex items-center justify-center gap-3 h-64 text-slate-400">
          <div aria-hidden="true" className="animate-spin rounded-full h-8 w-8 border-b-2 border-werco-primary" />
          Loading shipping…
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {pageHeader}

      {loadError && <ErrorState message="Could not load shipping data." onRetry={loadData} />}

      <FormField label="Find a shipment or work order">
        {field => (
          <input
            {...field}
            className="input"
            value={search}
            onChange={e => updateShippingFilter('shippingSearch', e.target.value)}
            placeholder="Shipment, work order, part, customer, or tracking number"
          />
        )}
      </FormField>
      {/* Ready to Ship */}
      {!loadError && (
        <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
          <h2 className="text-lg font-semibold mb-4">Ready to Ship ({readyToShip.length})</h2>
          <TableWorkspaceControls workspace={readyWorkspace} />
          <DataTable
            columns={readyWorkspace.displayColumns(readyColumns)}
            {...readyWorkspace.tableProps}
            data={readyToShip.filter(matchesSearch)}
            rowKey={wo => wo.work_order_id}
            defaultSort={{ key: 'due', dir: 'asc' }}
            pageSize={25}
            csvExport={{ filename: 'ready-to-ship' }}
            mobileCards={renderReadyCard}
            empty={{
              icon: TruckIcon,
              title: 'No work orders ready to ship',
              description: 'Completed work orders awaiting shipment will appear here.',
            }}
          />
        </div>
      )}

      {/* Recent Shipments — bespoke master/detail (expandable per-row tracking
          panel), so it stays a hand-rolled table on desktop with a responsive
          mobile-card fallback rather than a <DataTable>. */}
      {!loadError && (
        <div className="bg-fd-panel border border-fd-line rounded-sm p-3">
          <h2 className="text-lg font-semibold mb-4">
            Shipments ({visibleShipments.length} of {shipments.length})
          </h2>
          <p className="text-sm text-slate-400 mb-3">
            All shipment records loaded. Date filters use dispatch date, or creation date for a pending shipment.
            Quantity is allocated when a shipment is created; cancel an unused pending shipment to release it.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
            <FormField label="Shipment status">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={shippingStatus}
                  onChange={e => updateShippingFilter('shippingStatus', e.target.value)}
                >
                  <option value="">All statuses</option>
                  {['pending', 'packed', 'shipped', 'delivered', 'cancelled'].map(status => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="From date">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={shippingFrom}
                  onChange={e => updateShippingFilter('shippingFrom', e.target.value)}
                />
              )}
            </FormField>
            <FormField label="Through date">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={shippingTo}
                  onChange={e => updateShippingFilter('shippingTo', e.target.value)}
                />
              )}
            </FormField>
          </div>
          <TableWorkspaceControls workspace={shipmentWorkspace} tableOptions={false} />
          <div className="flex justify-between items-center mb-3">
            <span className="text-sm text-slate-400">
              Page {currentShipmentPage + 1} of {shipmentPages} · {visibleShipments.length} matching shipments
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={currentShipmentPage === 0}
                onClick={() => setShipmentPage(page => page - 1)}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={currentShipmentPage + 1 >= shipmentPages}
                onClick={() => setShipmentPage(page => page + 1)}
              >
                Next
              </Button>
            </div>
          </div>
          {/* Desktop table */}
          <div className="hidden md:block overflow-x-auto">
            <table className="min-w-full divide-y divide-fd-line">
              <thead className="bg-fd-sunken">
                <tr>
                  <th
                    className="px-4 py-2 text-left text-xs font-medium text-slate-400 uppercase w-8"
                    aria-label="Expand tracking"
                  ></th>
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
                {pagedShipments.map(s => {
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
                        <td className="px-4 py-2 font-medium">
                          <Link className="text-werco-primary underline" to={`/work-orders/${s.work_order_id}`}>
                            {s.work_order_number}
                          </Link>
                          <div className="text-xs text-slate-400">{s.part_number}</div>
                          <div className="text-xs text-slate-400">
                            {s.ship_date
                              ? `Shipped ${formatCentralDate(s.ship_date)}`
                              : `Created ${formatCentralDate(s.created_at)}`}
                          </div>
                        </td>
                        <td className="px-4 py-2 min-w-0 truncate">{s.customer_name || s.ship_to_name || '-'}</td>
                        <td className="px-4 py-2">
                          <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${statusColor(s.status)}`}>
                            {s.status}
                          </span>
                        </td>
                        <td className="px-4 py-2">{s.carrier || '-'}</td>
                        <td className="px-4 py-2">{renderTrackingBadge(s)}</td>
                        <td className="px-4 py-2 text-right tabular-nums">{s.quantity_shipped}</td>
                        <td className="px-4 py-2 text-center space-x-2 whitespace-nowrap">
                          {renderShipmentActions(s)}
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
            {visibleShipments.length === 0 && (
              <EmptyState
                icon={PaperAirplaneIcon}
                title="No shipments yet"
                description="Shipments you create will appear here."
              />
            )}
          </div>

          {/* Mobile cards (below md) — tap a card to expand its tracking panel. */}
          <div className="md:hidden">
            {visibleShipments.length === 0 ? (
              <EmptyState
                icon={PaperAirplaneIcon}
                title="No shipments yet"
                description="Shipments you create will appear here."
              />
            ) : (
              <MobileDataList>{pagedShipments.map(s => renderShipmentCard(s))}</MobileDataList>
            )}
          </div>
        </div>
      )}

      <Modal
        open={!!editTarget}
        onClose={() => {
          if (!saving) setEditTarget(null);
        }}
        size="md"
      >
        <div className="space-y-4">
          <RecordHeader
            title={`Pending shipment ${editTarget?.shipment_number || ''}`}
            closeLabel="Close pending shipment"
            closeDisabled={saving}
            onClose={() => setEditTarget(null)}
            fields={[
              { label: 'Work order', value: editTarget?.work_order_number },
              { label: 'Part', value: editTarget?.part_number },
              { label: 'Ship to', value: editTarget?.ship_to_name || editTarget?.customer_name },
            ]}
          />
          <FormField label="Quantity allocated">
            {field => (
              <input
                {...field}
                type="number"
                min="0.000001"
                step="any"
                className="input"
                value={editQuantity}
                onChange={e => setEditQuantity(e.target.value)}
              />
            )}
          </FormField>
          <p className="text-sm text-slate-400">
            Cancellation releases this quantity for another shipment. Purchased carrier labels must be voided first.
          </p>
          {editError && (
            <p role="alert" className="text-red-300">
              {editError}
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-3">
            <Button variant="danger" disabled={saving} onClick={() => savePendingShipment(true)}>
              Cancel shipment
            </Button>
            <Button disabled={saving} onClick={() => savePendingShipment()}>
              {saving ? 'Saving…' : 'Save quantity'}
            </Button>
          </div>
        </div>
      </Modal>
      {/* Carrier Schedule-Shipment wizard */}
      {scheduleTarget && (
        <ScheduleShipmentModal
          target={scheduleTarget}
          onClose={closeSchedule}
          onCompleted={() => {
            setNewScheduledShipment(null);
            loadData();
          }}
        />
      )}

      {/* Create Shipment Modal (legacy / manual path -- still supported) */}
      <Modal
        open={showCreateModal && !!selectedWO}
        onClose={() => {
          if (!saving) setShowCreateModal(false);
        }}
        size="md"
        closeOnBackdrop={false}
      >
        {selectedWO && (
          <>
            <RecordHeader
              title="Create Shipment (Manual)"
              closeLabel="Close manual shipment"
              closeDisabled={saving}
              onClose={() => setShowCreateModal(false)}
              fields={[
                { label: 'Work order', value: selectedWO.work_order_number },
                { label: 'Part', value: selectedWO.part_number },
                { label: 'Description', value: selectedWO.part_name },
              ]}
            />
            <form onSubmit={handleCreate} className="space-y-4">
              <p className="text-sm text-slate-400">
                {selectedWO.quantity_remaining ?? selectedWO.quantity_complete} available;{' '}
                {selectedWO.quantity_reserved || 0} already reserved.
              </p>
              {formError && (
                <p role="alert" className="text-red-300">
                  {formError}
                </p>
              )}
              <FormField label="Ship To">
                {field => (
                  <input
                    {...field}
                    type="text"
                    value={shipForm.ship_to_name}
                    onChange={e => setShipForm({ ...shipForm, ship_to_name: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Carrier">
                  {field => (
                    <select
                      {...field}
                      value={shipForm.carrier}
                      onChange={e => setShipForm({ ...shipForm, carrier: e.target.value })}
                      className="input"
                    >
                      <option value="">Select...</option>
                      <option value="UPS">UPS</option>
                      <option value="FedEx">FedEx</option>
                      <option value="USPS">USPS</option>
                      <option value="Freight">Freight</option>
                      <option value="Customer Pickup">Customer Pickup</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Qty to Ship">
                  {field => (
                    <input
                      {...field}
                      type="number"
                      value={shipForm.quantity_shipped}
                      onChange={e => setShipForm({ ...shipForm, quantity_shipped: parseFloat(e.target.value) })}
                      className="input"
                      min={0.000001}
                      max={selectedWO.quantity_remaining ?? selectedWO.quantity_complete}
                      step="any"
                      required
                    />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Weight (lbs)">
                  {field => (
                    <input
                      {...field}
                      type="number"
                      value={shipForm.weight_lbs}
                      onChange={e => setShipForm({ ...shipForm, weight_lbs: parseFloat(e.target.value) })}
                      className="input"
                      step={0.1}
                    />
                  )}
                </FormField>
                <FormField label="# Packages">
                  {field => (
                    <input
                      {...field}
                      type="number"
                      value={shipForm.num_packages}
                      onChange={e => setShipForm({ ...shipForm, num_packages: parseInt(e.target.value) })}
                      className="input"
                      min={1}
                    />
                  )}
                </FormField>
              </div>
              <div>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={shipForm.cert_of_conformance}
                    onChange={e => setShipForm({ ...shipForm, cert_of_conformance: e.target.checked })}
                    className="mr-2"
                    aria-label="Include Certificate of Conformance"
                  />
                  Include Certificate of Conformance
                </label>
              </div>
              <FormField label="Packing Notes">
                {field => (
                  <textarea
                    {...field}
                    value={shipForm.packing_notes}
                    onChange={e => setShipForm({ ...shipForm, packing_notes: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>
              <div className="flex flex-wrap justify-end gap-3 pt-4 border-t">
                <Button variant="secondary" disabled={saving} onClick={() => setShowCreateModal(false)}>
                  Cancel
                </Button>
                <Button type="submit" disabled={saving}>
                  {saving ? 'Creating…' : 'Create Shipment'}
                </Button>
              </div>
            </form>
          </>
        )}
      </Modal>
    </div>
  );
}
