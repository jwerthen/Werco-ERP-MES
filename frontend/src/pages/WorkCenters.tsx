import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { WorkCenter, WorkCenterType } from '../types';
import { PlusIcon, PencilIcon } from '@heroicons/react/24/outline';

const typeColors: Record<WorkCenterType, string> = {
  fabrication: 'bg-blue-100 text-blue-800',
  cnc_machining: 'bg-purple-100 text-purple-800',
  laser: 'bg-cyan-100 text-cyan-800',
  press_brake: 'bg-indigo-100 text-indigo-800',
  paint: 'bg-yellow-100 text-yellow-800',
  powder_coating: 'bg-orange-100 text-orange-800',
  assembly: 'bg-green-100 text-green-800',
  welding: 'bg-red-100 text-red-800',
  inspection: 'bg-cyan-100 text-cyan-800',
  shipping: 'bg-gray-100 text-gray-800',
};

const statusColors: Record<string, string> = {
  available: 'bg-green-500',
  in_use: 'bg-blue-500',
  maintenance: 'bg-yellow-500',
  offline: 'bg-red-500',
};

export default function WorkCenters() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editingWc, setEditingWc] = useState<WorkCenter | null>(null);
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
      const response = await api.getWorkCenters(false);
      setWorkCenters(response);
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
        <h1 className="text-2xl font-bold text-gray-900">Work Centers</h1>
        <button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="btn-primary flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Work Center
        </button>
      </div>

      {/* Work Centers Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {workCenters.map((wc) => (
          <div key={wc.id} className={`card ${!wc.is_active ? 'opacity-60' : ''}`}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center">
                <span className={`h-3 w-3 rounded-full mr-2 ${statusColors[wc.current_status]}`} />
                <span className="font-bold text-lg">{wc.code}</span>
              </div>
              <button
                onClick={() => handleEdit(wc)}
                className="text-gray-400 hover:text-gray-600"
              >
                <PencilIcon className="h-5 w-5" />
              </button>
            </div>
            
            <h3 className="font-medium text-gray-900 mb-2">{wc.name}</h3>
            
            <span className={`inline-block px-2 py-1 rounded text-xs font-medium mb-3 ${typeColors[wc.work_center_type]}`}>
              {wc.work_center_type.replace('_', ' ')}
            </span>
            
            {wc.description && (
              <p className="text-sm text-gray-500 mb-3">{wc.description}</p>
            )}
            
            <div className="grid grid-cols-2 gap-2 text-sm">
              <div>
                <span className="text-gray-500">Rate:</span>
                <span className="ml-1 font-medium">${wc.hourly_rate}/hr</span>
              </div>
              <div>
                <span className="text-gray-500">Capacity:</span>
                <span className="ml-1 font-medium">{wc.capacity_hours_per_day}h/day</span>
              </div>
            </div>
            
            {(wc.building || wc.area) && (
              <div className="mt-2 text-sm text-gray-500">
                {wc.building && <span>Building: {wc.building}</span>}
                {wc.building && wc.area && <span> | </span>}
                {wc.area && <span>Area: {wc.area}</span>}
              </div>
            )}
            
            {/* Status dropdown */}
            <div className="mt-4">
              <select
                value={wc.current_status}
                onChange={(e) => handleStatusChange(wc.id, e.target.value)}
                className="input text-sm"
              >
                <option value="available">Available</option>
                <option value="in_use">In Use</option>
                <option value="maintenance">Maintenance</option>
                <option value="offline">Offline</option>
              </select>
            </div>
          </div>
        ))}
      </div>

      {/* Add/Edit Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
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
                    <option value="fabrication">Fabrication</option>
                    <option value="cnc_machining">CNC Machining</option>
                    <option value="laser">Laser</option>
                    <option value="press_brake">Press Brake</option>
                    <option value="paint">Paint</option>
                    <option value="powder_coating">Powder Coating</option>
                    <option value="assembly">Assembly</option>
                    <option value="welding">Welding</option>
                    <option value="inspection">Inspection</option>
                    <option value="shipping">Shipping</option>
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
          </div>
        </div>
      )}
    </div>
  );
}
