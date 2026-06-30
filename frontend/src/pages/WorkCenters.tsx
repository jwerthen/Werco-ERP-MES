import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import {
  Button,
  DataTable,
  DataTableColumn,
  MobileDataCard,
  StatusBadge,
  statusVariant,
  useToast,
} from '../components/ui';
import { WorkCenter, WorkCenterType } from '../types';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import {
  PlusIcon,
  PencilIcon,
  Cog6ToothIcon,
  CheckCircleIcon,
  BoltIcon,
  WrenchScrewdriverIcon,
  NoSymbolIcon,
} from '@heroicons/react/24/outline';

// Solid status-dot color per canonical semantic variant (resolved from the
// central statusColors source so this page can't drift from the rest of the app).
const statusDotColor: Record<ReturnType<typeof statusVariant>, string> = {
  green: 'bg-green-500',
  blue: 'bg-blue-500',
  amber: 'bg-amber-500',
  red: 'bg-red-500',
  slate: 'bg-slate-500',
};

export default function WorkCenters() {
  const { showToast } = useToast();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingWc, setEditingWc] = useState<WorkCenter | null>(null);
  const [workCenterTypes, setWorkCenterTypes] = useState<string[]>([]);
  const [formData, setFormData] = useState({
    code: '',
    name: '',
    work_center_type: 'fabrication' as WorkCenterType,
    description: '',
    hourly_rate: 0,
    capacity_hours_per_day: 8,
    efficiency_factor: 1,
    building: '',
    area: '',
    version: 0
  });

  useEffect(() => {
    loadWorkCenters();
  }, []);

  const loadWorkCenters = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [wcResult, typesResult] = await Promise.allSettled([
        api.getWorkCenters(false),
        api.getWorkCenterTypes()
      ]);

      if (wcResult.status === 'fulfilled') {
        setWorkCenters(wcResult.value);
      } else {
        console.error('Failed to load work centers:', wcResult.reason);
        setLoadError(true);
      }

      if (typesResult.status === 'fulfilled') {
        setWorkCenterTypes(typesResult.value?.types || []);
      } else {
        console.error('Failed to load work center types:', typesResult.reason);
      }
    } catch (err) {
      console.error('Failed to load work centers:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const isEditing = !!editingWc;
    try {
      if (editingWc) {
        const updatePayload = {
          name: formData.name,
          work_center_type: formData.work_center_type,
          description: formData.description,
          hourly_rate: formData.hourly_rate,
          capacity_hours_per_day: formData.capacity_hours_per_day,
          efficiency_factor: formData.efficiency_factor,
          building: formData.building,
          area: formData.area,
          version: editingWc.version ?? formData.version ?? 0
        };
        await api.updateWorkCenter(editingWc.id, updatePayload);
      } else {
        const createPayload = {
          code: formData.code,
          name: formData.name,
          work_center_type: formData.work_center_type,
          description: formData.description,
          hourly_rate: formData.hourly_rate,
          capacity_hours_per_day: formData.capacity_hours_per_day,
          efficiency_factor: formData.efficiency_factor,
          building: formData.building,
          area: formData.area
        };
        await api.createWorkCenter(createPayload);
      }
      setShowModal(false);
      resetForm();
      loadWorkCenters();
      showToast('success', isEditing ? 'Work center updated' : 'Work center created');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save work center');
    }
  };

  const handleEdit = (wc: WorkCenter) => {
    setEditingWc(wc);
    setFormData({
      code: wc.code,
      name: wc.name,
      work_center_type: wc.work_center_type,
      description: wc.description || '',
      hourly_rate: wc.hourly_rate,
      capacity_hours_per_day: wc.capacity_hours_per_day,
      efficiency_factor: wc.efficiency_factor ?? 1,
      building: wc.building || '',
      area: wc.area || '',
      version: wc.version ?? 0
    });
    setShowModal(true);
  };

  const resetForm = () => {
    setEditingWc(null);
    setFormData({
      code: '',
      name: '',
      work_center_type: 'fabrication',
      description: '',
      hourly_rate: 0,
      capacity_hours_per_day: 8,
      efficiency_factor: 1,
      building: '',
      area: '',
      version: 0
    });
  };

  const handleStatusChange = async (id: number, status: string) => {
    try {
      await api.updateWorkCenterStatus(id, status);
      loadWorkCenters();
    } catch (err) {
      console.error('Failed to update status:', err);
      showToast('error', 'Failed to update work center status');
    }
  };

  const workCenterTypeOrder = workCenterTypes.length
    ? workCenterTypes
    : [
        'fabrication',
        'laser',
        'press_brake',
        'cnc_machining',
        'welding',
        'assembly',
        'paint',
        'powder_coating',
        'inspection',
        'shipping'
      ];

  const formatTypeLabel = (value: string) =>
    value
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());

  const statusCounts = workCenters.reduce(
    (acc, wc) => {
      acc[wc.current_status] = (acc[wc.current_status] || 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  // Inline status-change control — preserved from the cockpit layout. The wrapper
  // is purely presentational: its only job is to stop row click-through so changing
  // status never navigates / triggers an edit. Keyboard users operate the <select>
  // inside it directly, so role="presentation" (no focus/keyboard handler of its own)
  // is the accurate a11y shape here.
  const renderStatusCell = (wc: WorkCenter) => (
    <div className="flex items-center gap-2" role="presentation" onClick={(e) => e.stopPropagation()}>
      <span
        className={`h-2 w-2 flex-shrink-0 rounded-full ${statusDotColor[statusVariant(wc.current_status)]}`}
        aria-hidden="true"
      />
      <StatusBadge status={wc.current_status} className="hidden xl:inline-flex" />
      <select
        value={wc.current_status}
        onChange={(e) => handleStatusChange(wc.id, e.target.value)}
        aria-label={`Status for ${wc.code}`}
        className="input !py-0.5 !px-1.5 !text-xs !min-h-0 h-7"
      >
        <option value="available">Available</option>
        <option value="in_use">In Use</option>
        <option value="maintenance">Maintenance</option>
        <option value="offline">Offline</option>
      </select>
    </div>
  );

  const columns: Array<DataTableColumn<WorkCenter>> = [
    {
      key: 'code',
      header: 'Code',
      sortable: true,
      accessor: (wc) => wc.code,
      render: (wc) => <span className="font-semibold text-white">{wc.code}</span>,
    },
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (wc) => wc.name,
      render: (wc) => {
        const detail = [
          wc.description,
          wc.building && `Building: ${wc.building}`,
          wc.area && `Area: ${wc.area}`,
        ]
          .filter(Boolean)
          .join(' • ');
        return (
          <span className="text-slate-300" title={detail || undefined}>
            {wc.name}
          </span>
        );
      },
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (wc) => wc.current_status,
      render: renderStatusCell,
    },
    {
      key: 'hourly_rate',
      header: 'Rate/hr',
      sortable: true,
      align: 'right',
      accessor: (wc) => wc.hourly_rate,
      render: (wc) => <span className="tabular-nums">${wc.hourly_rate}</span>,
      csv: (wc) => wc.hourly_rate,
    },
    {
      key: 'capacity_hours_per_day',
      header: 'Capacity',
      sortable: true,
      align: 'right',
      accessor: (wc) => wc.capacity_hours_per_day,
      render: (wc) => <span className="tabular-nums">{wc.capacity_hours_per_day}h</span>,
      csv: (wc) => wc.capacity_hours_per_day,
    },
    {
      key: 'efficiency_factor',
      header: 'Efficiency',
      sortable: true,
      align: 'right',
      accessor: (wc) => wc.efficiency_factor ?? null,
      render: (wc) => (
        <span className="tabular-nums">
          {wc.efficiency_factor != null ? wc.efficiency_factor.toFixed(2) : '—'}
        </span>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      // Group key is exported in CSV so the flat export keeps the grouping.
      accessor: (wc) => formatTypeLabel(wc.work_center_type),
      // Header is hidden visually (the group rows carry the type label) but the
      // column still feeds CSV export — render nothing in the body.
      headerClassName: 'sr-only',
      render: () => null,
    },
    {
      key: 'actions',
      header: '',
      align: 'right',
      render: (wc) => (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            handleEdit(wc);
          }}
          aria-label={`Edit ${wc.code}`}
          className="text-slate-400 hover:text-white"
        >
          <PencilIcon className="h-4 w-4" />
        </button>
      ),
    },
  ];

  const renderMobileCard = (wc: WorkCenter) => (
    <MobileDataCard
      key={wc.id}
      className={wc.is_active === false ? 'opacity-60' : ''}
      title={wc.code}
      subtitle={wc.name}
      badge={<StatusBadge status={wc.current_status} />}
      fields={[
        { label: 'Rate/hr', value: `$${wc.hourly_rate}` },
        { label: 'Capacity', value: `${wc.capacity_hours_per_day}h` },
        { label: 'Status', fullWidth: true, value: renderStatusCell(wc) },
      ]}
      actions={
        <Button variant="secondary" size="sm" onClick={() => handleEdit(wc)}>
          <PencilIcon className="h-4 w-4 mr-1.5" />
          Edit
        </Button>
      }
    />
  );

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Work Centers</h1>
        <Button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Work Center
        </Button>
      </div>

      {/* Status KPI strip */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-5 gap-2">
        <MiniStat
          icon={Cog6ToothIcon}
          iconBg="bg-werco-navy/15"
          iconColor="text-werco-navy"
          label="Total"
          value={workCenters.length}
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Available"
          value={statusCounts.available || 0}
          valueColor="text-fd-green"
        />
        <MiniStat
          icon={BoltIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="In Use"
          value={statusCounts.in_use || 0}
          valueColor="text-fd-blue"
        />
        <MiniStat
          icon={WrenchScrewdriverIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Maintenance"
          value={statusCounts.maintenance || 0}
          valueColor="text-fd-amber"
        />
        <MiniStat
          icon={NoSymbolIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Offline"
          value={statusCounts.offline || 0}
          valueColor="text-fd-red"
        />
      </MiniStatStrip>

      {/* Grouped-by-type, sortable table. Group order is the curated
          workCenterTypeOrder; sorting reorders within each type group. */}
      <DataTable
        columns={columns}
        data={workCenters}
        rowKey={(wc) => wc.id}
        loading={loading}
        error={loadError}
        onRetry={loadWorkCenters}
        defaultSort={{ key: 'code', dir: 'asc' }}
        groupBy={{
          key: (wc) => wc.work_center_type,
          order: workCenterTypeOrder,
          header: (type, rows) => (
            <span className="inline-flex items-center gap-2">
              <span className="text-slate-200">{formatTypeLabel(type)}</span>
              <span className="text-fd-mute tabular-nums">
                {rows.length} center{rows.length !== 1 ? 's' : ''}
              </span>
            </span>
          ),
        }}
        csvExport={{ filename: 'work-centers' }}
        mobileCards={renderMobileCard}
        rowClassName={(wc) => (wc.is_active === false ? 'opacity-60' : '')}
        empty={{
          icon: Cog6ToothIcon,
          title: 'No work centers',
          description:
            'Add a work center to start tracking shop-floor capacity and status.',
          action: {
            label: 'Add Work Center',
            onClick: () => {
              resetForm();
              setShowModal(true);
            },
          },
        }}
      />

      {/* Add/Edit Modal */}
      <Modal
        open={showModal}
        onClose={() => { setShowModal(false); resetForm(); }}
        size="lg"
        closeOnBackdrop={false}
      >
            <h3 className="text-lg font-semibold mb-4">
              {editingWc ? 'Edit Work Center' : 'Add Work Center'}
            </h3>
            
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Code</label>
                  <input
                    type="text"
                    value={formData.code}
                    onChange={(e) => setFormData({ ...formData, code: e.target.value })}
                    className="input"
                    required
                    disabled={!!editingWc}
                  />
                </div>
                <div>
                  <label className="label">Type</label>
                  <select
                    value={formData.work_center_type}
                    onChange={(e) => setFormData({ ...formData, work_center_type: e.target.value as WorkCenterType })}
                    className="input"
                    required
                  >
                    {workCenterTypeOrder.map((type) => (
                      <option key={type} value={type}>
                        {formatTypeLabel(type)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              
              <div>
                <label className="label">Name</label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="input"
                  required
                />
              </div>
              
              <div>
                <label className="label">Description</label>
                <textarea
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Hourly Rate ($)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={formData.hourly_rate}
                    onChange={(e) => setFormData({ ...formData, hourly_rate: parseFloat(e.target.value) || 0 })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Capacity (hrs/day)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.5"
                    value={formData.capacity_hours_per_day}
                    onChange={(e) => setFormData({ ...formData, capacity_hours_per_day: parseFloat(e.target.value) || 8 })}
                    className="input"
                  />
                </div>
              </div>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Building</label>
                  <input
                    type="text"
                    value={formData.building}
                    onChange={(e) => setFormData({ ...formData, building: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Area</label>
                  <input
                    type="text"
                    value={formData.area}
                    onChange={(e) => setFormData({ ...formData, area: e.target.value })}
                    className="input"
                  />
                </div>
              </div>
              
              <div className="flex justify-end gap-3 mt-6">
                <Button
                  variant="secondary"
                  onClick={() => { setShowModal(false); resetForm(); }}
                >
                  Cancel
                </Button>
                <Button type="submit">
                  {editingWc ? 'Update' : 'Create'}
                </Button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
