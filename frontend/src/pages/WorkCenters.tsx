import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import { WorkCenter, WorkCenterType } from '../types';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import {
  PlusIcon,
  PencilIcon,
  Cog6ToothIcon,
  CheckCircleIcon,
  BoltIcon,
  WrenchScrewdriverIcon,
  NoSymbolIcon,
} from '@heroicons/react/24/outline';

const statusColors: Record<string, string> = {
  available: 'bg-green-500/100',
  in_use: 'bg-blue-500/100',
  maintenance: 'bg-yellow-500/100',
  offline: 'bg-red-500/100',
};

export default function WorkCenters() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
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
    try {
      const [wcResult, typesResult] = await Promise.allSettled([
        api.getWorkCenters(false),
        api.getWorkCenterTypes()
      ]);

      if (wcResult.status === 'fulfilled') {
        setWorkCenters(wcResult.value);
      } else {
        console.error('Failed to load work centers:', wcResult.reason);
      }

      if (typesResult.status === 'fulfilled') {
        setWorkCenterTypes(typesResult.value?.types || []);
      } else {
        console.error('Failed to load work center types:', typesResult.reason);
      }
    } catch (err) {
      console.error('Failed to load work centers:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
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
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save work center');
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

  const groupedWorkCenters = workCenterTypeOrder
    .map((type) => ({
      type,
      items: workCenters.filter((wc) => wc.work_center_type === type)
    }))
    .filter((group) => group.items.length > 0);

  const ungrouped = workCenters.filter((wc) => !workCenterTypeOrder.includes(wc.work_center_type));
  if (ungrouped.length) {
    groupedWorkCenters.push({ type: 'other', items: ungrouped });
  }

  const statusCounts = workCenters.reduce(
    (acc, wc) => {
      acc[wc.current_status] = (acc[wc.current_status] || 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Work Centers</h1>
        <button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="btn-primary flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Work Center
        </button>
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

      {/* Per-type panels, side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        {groupedWorkCenters.map((group) => (
          <CockpitPanel
            key={group.type}
            title={formatTypeLabel(group.type)}
            className="min-w-0"
            footer={`${group.items.length} center${group.items.length !== 1 ? 's' : ''}`}
          >
            <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-slate-500 border-b border-fd-line">
                  <th className="text-left font-medium py-1.5 pr-2">Code / Name</th>
                  <th className="text-right font-medium py-1.5 px-1.5">Rate</th>
                  <th className="text-right font-medium py-1.5 px-1.5">Cap</th>
                  <th className="text-right font-medium py-1.5 px-1.5">Eff</th>
                  <th className="text-left font-medium py-1.5 px-1.5">Status</th>
                  <th className="py-1.5 pl-1.5" />
                </tr>
              </thead>
              <tbody className="divide-y divide-fd-line">
                {group.items.map((wc) => {
                  const detail = [
                    wc.description,
                    wc.building && `Building: ${wc.building}`,
                    wc.area && `Area: ${wc.area}`,
                  ]
                    .filter(Boolean)
                    .join(' • ');
                  return (
                    <tr key={wc.id} className={`align-middle ${!wc.is_active ? 'opacity-50' : ''}`}>
                      <td className="py-1.5 pr-2 min-w-0" title={detail || undefined}>
                        <div className="flex items-center gap-2 min-w-0">
                          <span
                            className={`h-2 w-2 flex-shrink-0 rounded-full ${statusColors[wc.current_status]}`}
                          />
                          <span className="font-semibold text-white flex-shrink-0">{wc.code}</span>
                          <span className="text-slate-400 truncate">{wc.name}</span>
                        </div>
                      </td>
                      <td className="py-1.5 px-1.5 text-right whitespace-nowrap">${wc.hourly_rate}</td>
                      <td className="py-1.5 px-1.5 text-right whitespace-nowrap">{wc.capacity_hours_per_day}h</td>
                      <td className="py-1.5 px-1.5 text-right whitespace-nowrap">{wc.efficiency_factor}</td>
                      <td className="py-1.5 px-1.5">
                        <select
                          value={wc.current_status}
                          onChange={(e) => handleStatusChange(wc.id, e.target.value)}
                          className="input !py-0.5 !px-1.5 !text-xs !min-h-0 h-7"
                        >
                          <option value="available">Available</option>
                          <option value="in_use">In Use</option>
                          <option value="maintenance">Maintenance</option>
                          <option value="offline">Offline</option>
                        </select>
                      </td>
                      <td className="py-1.5 pl-1.5 text-right">
                        <button
                          onClick={() => handleEdit(wc)}
                          className="text-slate-400 hover:text-white"
                          title="Edit work center"
                        >
                          <PencilIcon className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          </CockpitPanel>
        ))}
      </div>

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
                <button
                  type="button"
                  onClick={() => { setShowModal(false); resetForm(); }}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingWc ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
