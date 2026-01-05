import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { format, differenceInDays } from 'date-fns';
import {
  PlusIcon,
  WrenchIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ClockIcon,
  XMarkIcon as XCircleIcon,
} from '@heroicons/react/24/outline';

interface Equipment {
  id: number;
  equipment_id: string;
  name: string;
  description?: string;
  equipment_type?: string;
  manufacturer?: string;
  model?: string;
  serial_number?: string;
  location?: string;
  assigned_to?: string;
  calibration_interval_days: number;
  last_calibration_date?: string;
  next_calibration_date?: string;
  calibration_provider?: string;
  status: string;
  is_active: boolean;
  days_until_due?: number;
}

const statusColors: Record<string, string> = {
  active: 'bg-green-100 text-green-800',
  due: 'bg-yellow-100 text-yellow-800',
  overdue: 'bg-red-100 text-red-800',
  out_of_service: 'bg-gray-100 text-gray-800',
  retired: 'bg-gray-100 text-gray-600',
};

const equipmentTypes = [
  'Caliper',
  'Micrometer',
  'Height Gauge',
  'Dial Indicator',
  'CMM',
  'Hardness Tester',
  'Surface Roughness',
  'Torque Wrench',
  'Pin Gauge',
  'Thread Gauge',
  'Other'
];

export default function Calibration() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [equipment, setEquipment] = useState<Equipment[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [showCalibrationModal, setShowCalibrationModal] = useState(false);
  const [editingEquipment, setEditingEquipment] = useState<Equipment | null>(null);
  const [selectedEquipmentId, setSelectedEquipmentId] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>(() => {
    const filter = searchParams.get('filter');
    if (filter === 'overdue') return 'overdue';
    if (filter === 'due') return 'due';
    return '';
  });

  const [formData, setFormData] = useState({
    equipment_id: '',
    name: '',
    description: '',
    equipment_type: '',
    manufacturer: '',
    model: '',
    serial_number: '',
    location: '',
    assigned_to: '',
    calibration_interval_days: 365,
    calibration_provider: '',
    range_min: '',
    range_max: '',
    accuracy: '',
    resolution: '',
    notes: ''
  });

  const [calibrationData, setCalibrationData] = useState({
    calibration_date: format(new Date(), 'yyyy-MM-dd'),
    performed_by: '',
    calibration_provider: '',
    certificate_number: '',
    result: 'pass',
    as_found: '',
    as_left: '',
    cost: 0,
    notes: ''
  });

  useEffect(() => {
    loadEquipment();
  }, [statusFilter]);

  const loadEquipment = async () => {
    try {
      const response = await api.getEquipment(statusFilter || undefined);
      setEquipment(response);
    } catch (err) {
      console.error('Failed to load equipment:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingEquipment) {
        await api.updateEquipment(editingEquipment.id, formData);
      } else {
        await api.createEquipment(formData);
      }
      setShowModal(false);
      resetForm();
      loadEquipment();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save equipment');
    }
  };

  const handleCalibration = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedEquipmentId) return;
    try {
      await api.recordCalibration(selectedEquipmentId, {
        ...calibrationData,
        calibration_date: calibrationData.calibration_date
      });
      setShowCalibrationModal(false);
      setSelectedEquipmentId(null);
      loadEquipment();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to record calibration');
    }
  };

  const openCalibrationModal = (eq: Equipment) => {
    setSelectedEquipmentId(eq.id);
    setCalibrationData({
      calibration_date: format(new Date(), 'yyyy-MM-dd'),
      performed_by: '',
      calibration_provider: eq.calibration_provider || '',
      certificate_number: '',
      result: 'pass',
      as_found: '',
      as_left: '',
      cost: 0,
      notes: ''
    });
    setShowCalibrationModal(true);
  };

  const handleEdit = (eq: Equipment) => {
    setEditingEquipment(eq);
    setFormData({
      equipment_id: eq.equipment_id,
      name: eq.name,
      description: eq.description || '',
      equipment_type: eq.equipment_type || '',
      manufacturer: eq.manufacturer || '',
      model: eq.model || '',
      serial_number: eq.serial_number || '',
      location: eq.location || '',
      assigned_to: eq.assigned_to || '',
      calibration_interval_days: eq.calibration_interval_days,
      calibration_provider: eq.calibration_provider || '',
      range_min: '',
      range_max: '',
      accuracy: '',
      resolution: '',
      notes: ''
    });
    setShowModal(true);
  };

  const resetForm = () => {
    setEditingEquipment(null);
    setFormData({
      equipment_id: '',
      name: '',
      description: '',
      equipment_type: '',
      manufacturer: '',
      model: '',
      serial_number: '',
      location: '',
      assigned_to: '',
      calibration_interval_days: 365,
      calibration_provider: '',
      range_min: '',
      range_max: '',
      accuracy: '',
      resolution: '',
      notes: ''
    });
  };

  // Summary stats
  const overdueCount = equipment.filter(e => e.status === 'overdue').length;
  const dueCount = equipment.filter(e => e.status === 'due').length;
  const activeCount = equipment.filter(e => e.status === 'active').length;

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
        <h1 className="text-2xl font-bold text-gray-900">Calibration Tracking</h1>
        <button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="btn-primary flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Equipment
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="card flex items-center">
          <div className="p-3 rounded-full bg-red-100 mr-4">
            <ExclamationTriangleIcon className="h-6 w-6 text-red-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Overdue</p>
            <p className="text-2xl font-bold text-red-600">{overdueCount}</p>
          </div>
        </div>
        <div className="card flex items-center">
          <div className="p-3 rounded-full bg-yellow-100 mr-4">
            <ClockIcon className="h-6 w-6 text-yellow-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Due Soon (30 days)</p>
            <p className="text-2xl font-bold text-yellow-600">{dueCount}</p>
          </div>
        </div>
        <div className="card flex items-center">
          <div className="p-3 rounded-full bg-green-100 mr-4">
            <CheckCircleIcon className="h-6 w-6 text-green-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Current</p>
            <p className="text-2xl font-bold text-green-600">{activeCount}</p>
          </div>
        </div>
      </div>

      {/* Filter */}
      <div className="flex gap-4 items-center">
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value);
            if (e.target.value) {
              setSearchParams({ filter: e.target.value });
            } else {
              setSearchParams({});
            }
          }}
          className="input w-48"
        >
          <option value="">All Status</option>
          <option value="active">Active/Current</option>
          <option value="due">Due Soon</option>
          <option value="overdue">Overdue</option>
          <option value="out_of_service">Out of Service</option>
        </select>
        {statusFilter && (
          <button
            onClick={() => {
              setStatusFilter('');
              setSearchParams({});
            }}
            className="flex items-center gap-1 px-3 py-1.5 text-sm bg-werco-100 text-werco-700 rounded-full hover:bg-werco-200"
          >
            <XCircleIcon className="h-4 w-4" />
            Clear filter
          </button>
        )}
      </div>

      {/* Equipment Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Equipment</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Location</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Last Cal</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Next Due</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {equipment.map((eq) => (
                <tr key={eq.id} className="hover:bg-gray-50">
                  <td className="px-4 py-4 font-mono text-sm">{eq.equipment_id}</td>
                  <td className="px-4 py-4">
                    <div>
                      <div className="font-medium">{eq.name}</div>
                      {eq.manufacturer && (
                        <div className="text-sm text-gray-500">{eq.manufacturer} {eq.model}</div>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-4 text-sm">{eq.equipment_type || '-'}</td>
                  <td className="px-4 py-4 text-sm">{eq.location || '-'}</td>
                  <td className="px-4 py-4 text-sm">
                    {eq.last_calibration_date ? format(new Date(eq.last_calibration_date), 'MMM d, yyyy') : '-'}
                  </td>
                  <td className="px-4 py-4">
                    {eq.next_calibration_date ? (
                      <div>
                        <div className="text-sm">{format(new Date(eq.next_calibration_date), 'MMM d, yyyy')}</div>
                        {eq.days_until_due !== undefined && (
                          <div className={`text-xs ${eq.days_until_due < 0 ? 'text-red-600' : eq.days_until_due <= 30 ? 'text-yellow-600' : 'text-gray-500'}`}>
                            {eq.days_until_due < 0 ? `${Math.abs(eq.days_until_due)} days overdue` : 
                             eq.days_until_due === 0 ? 'Due today' : 
                             `${eq.days_until_due} days`}
                          </div>
                        )}
                      </div>
                    ) : '-'}
                  </td>
                  <td className="px-4 py-4">
                    <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${statusColors[eq.status]}`}>
                      {eq.status.replace('_', ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-4 text-center">
                    <div className="flex justify-center gap-2">
                      <button
                        onClick={() => openCalibrationModal(eq)}
                        className="text-green-600 hover:text-green-800 text-sm font-medium"
                        title="Record Calibration"
                      >
                        <WrenchIcon className="h-5 w-5" />
                      </button>
                      <button
                        onClick={() => handleEdit(eq)}
                        className="text-gray-400 hover:text-gray-600 text-sm"
                      >
                        Edit
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {equipment.length === 0 && (
          <div className="text-center py-8 text-gray-500">No equipment found</div>
        )}
      </div>

      {/* Add/Edit Equipment Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold mb-4">
              {editingEquipment ? 'Edit Equipment' : 'Add Equipment'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Equipment ID *</label>
                  <input
                    type="text"
                    value={formData.equipment_id}
                    onChange={(e) => setFormData({ ...formData, equipment_id: e.target.value })}
                    className="input"
                    required
                    disabled={!!editingEquipment}
                    placeholder="e.g., CAL-001"
                  />
                </div>
                <div>
                  <label className="label">Name *</label>
                  <input
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="input"
                    required
                    placeholder="e.g., 6in Digital Caliper"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Equipment Type</label>
                  <select
                    value={formData.equipment_type}
                    onChange={(e) => setFormData({ ...formData, equipment_type: e.target.value })}
                    className="input"
                  >
                    <option value="">Select type...</option>
                    {equipmentTypes.map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">Calibration Interval (days)</label>
                  <input
                    type="number"
                    value={formData.calibration_interval_days}
                    onChange={(e) => setFormData({ ...formData, calibration_interval_days: parseInt(e.target.value) })}
                    className="input"
                    min={1}
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="label">Manufacturer</label>
                  <input
                    type="text"
                    value={formData.manufacturer}
                    onChange={(e) => setFormData({ ...formData, manufacturer: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Model</label>
                  <input
                    type="text"
                    value={formData.model}
                    onChange={(e) => setFormData({ ...formData, model: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Serial Number</label>
                  <input
                    type="text"
                    value={formData.serial_number}
                    onChange={(e) => setFormData({ ...formData, serial_number: e.target.value })}
                    className="input"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Location</label>
                  <input
                    type="text"
                    value={formData.location}
                    onChange={(e) => setFormData({ ...formData, location: e.target.value })}
                    className="input"
                    placeholder="e.g., QC Lab, Machine Shop"
                  />
                </div>
                <div>
                  <label className="label">Calibration Provider</label>
                  <input
                    type="text"
                    value={formData.calibration_provider}
                    onChange={(e) => setFormData({ ...formData, calibration_provider: e.target.value })}
                    className="input"
                    placeholder="e.g., Precision Calibration Inc."
                  />
                </div>
              </div>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <button type="button" onClick={() => { setShowModal(false); resetForm(); }} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingEquipment ? 'Update' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Record Calibration Modal */}
      {showCalibrationModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Record Calibration</h3>
            <form onSubmit={handleCalibration} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Calibration Date *</label>
                  <input
                    type="date"
                    value={calibrationData.calibration_date}
                    onChange={(e) => setCalibrationData({ ...calibrationData, calibration_date: e.target.value })}
                    className="input"
                    required
                  />
                </div>
                <div>
                  <label className="label">Result *</label>
                  <select
                    value={calibrationData.result}
                    onChange={(e) => setCalibrationData({ ...calibrationData, result: e.target.value })}
                    className="input"
                    required
                  >
                    <option value="pass">Pass</option>
                    <option value="fail">Fail</option>
                    <option value="adjusted">Adjusted</option>
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Performed By</label>
                  <input
                    type="text"
                    value={calibrationData.performed_by}
                    onChange={(e) => setCalibrationData({ ...calibrationData, performed_by: e.target.value })}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Certificate #</label>
                  <input
                    type="text"
                    value={calibrationData.certificate_number}
                    onChange={(e) => setCalibrationData({ ...calibrationData, certificate_number: e.target.value })}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <label className="label">Calibration Provider</label>
                <input
                  type="text"
                  value={calibrationData.calibration_provider}
                  onChange={(e) => setCalibrationData({ ...calibrationData, calibration_provider: e.target.value })}
                  className="input"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">As Found</label>
                  <input
                    type="text"
                    value={calibrationData.as_found}
                    onChange={(e) => setCalibrationData({ ...calibrationData, as_found: e.target.value })}
                    className="input"
                    placeholder="Condition before cal"
                  />
                </div>
                <div>
                  <label className="label">As Left</label>
                  <input
                    type="text"
                    value={calibrationData.as_left}
                    onChange={(e) => setCalibrationData({ ...calibrationData, as_left: e.target.value })}
                    className="input"
                    placeholder="Condition after cal"
                  />
                </div>
              </div>

              <div>
                <label className="label">Cost ($)</label>
                <input
                  type="number"
                  value={calibrationData.cost}
                  onChange={(e) => setCalibrationData({ ...calibrationData, cost: parseFloat(e.target.value) || 0 })}
                  className="input"
                  step="0.01"
                  min="0"
                />
              </div>

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={calibrationData.notes}
                  onChange={(e) => setCalibrationData({ ...calibrationData, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <button type="button" onClick={() => setShowCalibrationModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">Record Calibration</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
