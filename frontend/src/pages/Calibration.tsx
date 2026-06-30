import React, { useEffect, useState, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui/FormField';
import {
  useToast,
  Button,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { formatCentralDate, getCentralTodayISODate } from '../utils/centralTime';
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
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [equipment, setEquipment] = useState<Equipment[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
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
    calibration_date: getCentralTodayISODate(),
    performed_by: '',
    calibration_provider: '',
    certificate_number: '',
    result: 'pass',
    as_found: '',
    as_left: '',
    cost: 0,
    notes: ''
  });

  const loadEquipment = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const response = await api.getEquipment(statusFilter || undefined);
      setEquipment(response);
    } catch (err) {
      console.error('Failed to load equipment:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    loadEquipment();
  }, [loadEquipment]);

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
      showToast('error', err.response?.data?.detail || 'Failed to save equipment');
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
      showToast('error', err.response?.data?.detail || 'Failed to record calibration');
    }
  };

  const openCalibrationModal = (eq: Equipment) => {
    setSelectedEquipmentId(eq.id);
    setCalibrationData({
      calibration_date: getCentralTodayISODate(),
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

  const applyFilter = (value: string) => {
    const next = statusFilter === value ? '' : value;
    setStatusFilter(next);
    setSearchParams(next ? { filter: next } : {});
  };

  const dueText = (eq: Equipment) => {
    if (eq.days_until_due === undefined) return null;
    return eq.days_until_due < 0
      ? `${Math.abs(eq.days_until_due)} days overdue`
      : eq.days_until_due === 0
      ? 'Due today'
      : `${eq.days_until_due} days`;
  };

  const dueColor = (eq: Equipment) =>
    eq.days_until_due === undefined
      ? ''
      : eq.days_until_due < 0
      ? 'text-red-600'
      : eq.days_until_due <= 30
      ? 'text-yellow-600'
      : 'text-slate-400';

  const columns: DataTableColumn<Equipment>[] = [
    {
      key: 'equipment_id',
      header: 'ID',
      sortable: true,
      accessor: (eq) => eq.equipment_id,
      render: (eq) => <span className="font-mono text-sm">{eq.equipment_id}</span>,
    },
    {
      key: 'name',
      header: 'Equipment',
      sortable: true,
      accessor: (eq) => eq.name,
      csv: (eq) =>
        eq.manufacturer ? `${eq.name} (${eq.manufacturer} ${eq.model || ''})`.trim() : eq.name,
      render: (eq) => (
        <div>
          <div className="font-medium">{eq.name}</div>
          {eq.manufacturer && (
            <div className="text-sm text-slate-400">
              {eq.manufacturer} {eq.model}
            </div>
          )}
        </div>
      ),
    },
    {
      key: 'equipment_type',
      header: 'Type',
      sortable: true,
      accessor: (eq) => eq.equipment_type || '',
      render: (eq) => eq.equipment_type || '-',
    },
    {
      key: 'location',
      header: 'Location',
      sortable: true,
      accessor: (eq) => eq.location || '',
      render: (eq) => eq.location || '-',
    },
    {
      key: 'last_calibration_date',
      header: 'Last Cal',
      sortable: true,
      accessor: (eq) => eq.last_calibration_date || '',
      csv: (eq) => (eq.last_calibration_date ? formatCentralDate(eq.last_calibration_date) : ''),
      render: (eq) =>
        eq.last_calibration_date ? formatCentralDate(eq.last_calibration_date) : '-',
    },
    {
      key: 'next_calibration_date',
      header: 'Next Due',
      sortable: true,
      accessor: (eq) =>
        eq.days_until_due !== undefined ? eq.days_until_due : eq.next_calibration_date || '',
      csv: (eq) => (eq.next_calibration_date ? formatCentralDate(eq.next_calibration_date) : ''),
      render: (eq) =>
        eq.next_calibration_date ? (
          <div>
            <div className="text-sm">{formatCentralDate(eq.next_calibration_date)}</div>
            {eq.days_until_due !== undefined && (
              <div className={`text-xs ${dueColor(eq)}`}>{dueText(eq)}</div>
            )}
          </div>
        ) : (
          '-'
        ),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (eq) => eq.status,
      csv: (eq) => eq.status.replace('_', ' '),
      render: (eq) => <StatusBadge status={eq.status} />,
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (eq) => (
        <div className="flex justify-center gap-2">
          <button
            onClick={(e) => {
              e.stopPropagation();
              openCalibrationModal(eq);
            }}
            className="text-green-600 hover:text-green-300 text-sm font-medium"
            title="Record Calibration"
            aria-label="Record Calibration"
          >
            <WrenchIcon className="h-5 w-5" aria-hidden="true" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleEdit(eq);
            }}
            className="text-slate-500 hover:text-slate-400 text-sm"
          >
            Edit
          </button>
        </div>
      ),
    },
  ];

  const renderMobileCard = (eq: Equipment) => (
    <MobileDataCard
      title={eq.name}
      subtitle={
        [eq.equipment_id, eq.manufacturer && `${eq.manufacturer} ${eq.model || ''}`.trim()]
          .filter(Boolean)
          .join(' • ') || undefined
      }
      badge={<StatusBadge status={eq.status} />}
      fields={[
        { label: 'Type', value: eq.equipment_type || '-' },
        { label: 'Location', value: eq.location || '-' },
        {
          label: 'Last Cal',
          value: eq.last_calibration_date ? formatCentralDate(eq.last_calibration_date) : '-',
        },
        {
          label: 'Next Due',
          value: eq.next_calibration_date ? (
            <div>
              <div>{formatCentralDate(eq.next_calibration_date)}</div>
              {eq.days_until_due !== undefined && (
                <div className={`text-xs ${dueColor(eq)}`}>{dueText(eq)}</div>
              )}
            </div>
          ) : (
            '-'
          ),
        },
      ]}
      actions={
        <>
          <button
            onClick={() => openCalibrationModal(eq)}
            className="inline-flex items-center gap-1 text-sm font-medium text-green-400 hover:text-green-300"
          >
            <WrenchIcon className="h-4 w-4" />
            Record
          </button>
          <button
            onClick={() => handleEdit(eq)}
            className="text-sm text-slate-300 hover:text-slate-100"
          >
            Edit
          </button>
        </>
      }
    />
  );

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Calibration Tracking</h1>
        <Button
          onClick={() => { resetForm(); setShowModal(true); }}
          className="flex items-center"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Add Equipment
        </Button>
      </div>

      {/* Summary tiles — click to filter by status */}
      <div data-tour="qa-calibration">
        <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg="bg-fd-red/15"
            iconColor="text-fd-red"
            label="Overdue"
            value={overdueCount}
            valueColor="text-fd-red"
            onClick={() => applyFilter('overdue')}
            active={statusFilter === 'overdue'}
          />
          <MiniStat
            icon={ClockIcon}
            iconBg="bg-fd-amber/15"
            iconColor="text-fd-amber"
            label="Due Soon (30 days)"
            value={dueCount}
            valueColor="text-fd-amber"
            onClick={() => applyFilter('due')}
            active={statusFilter === 'due'}
          />
          <MiniStat
            icon={CheckCircleIcon}
            iconBg="bg-fd-green/15"
            iconColor="text-fd-green"
            label="Current"
            value={activeCount}
            valueColor="text-fd-green"
            onClick={() => applyFilter('active')}
            active={statusFilter === 'active'}
          />
        </MiniStatStrip>
      </div>

      {/* Filter */}
      <div className="flex gap-2 items-center">
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
            className="flex items-center gap-1 rounded-sm border border-fd-line px-2.5 py-1.5 text-sm text-slate-300 hover:border-fd-line-bright"
          >
            <XCircleIcon className="h-4 w-4" />
            Clear filter
          </button>
        )}
      </div>

      {/* Equipment Table */}
      <DataTable
        columns={columns}
        data={equipment}
        rowKey={(eq) => eq.id}
        loading={loading}
        error={loadError}
        onRetry={loadEquipment}
        defaultSort={{ key: 'next_calibration_date', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: 'calibration-equipment' }}
        mobileCards={renderMobileCard}
        empty={{
          icon: WrenchIcon,
          title: 'No equipment found',
          description: statusFilter
            ? 'No equipment matches the current filter.'
            : 'Add measurement equipment to start tracking calibration.',
          action: {
            label: 'Add Equipment',
            onClick: () => {
              resetForm();
              setShowModal(true);
            },
          },
        }}
      />

      {/* Add/Edit Equipment Modal */}
      <Modal open={showModal} onClose={() => { setShowModal(false); resetForm(); }} size="2xl" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">
              {editingEquipment ? 'Edit Equipment' : 'Add Equipment'}
            </h3>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Equipment ID" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.equipment_id}
                      onChange={(e) => setFormData({ ...formData, equipment_id: e.target.value })}
                      className="input"
                      required
                      disabled={!!editingEquipment}
                      placeholder="e.g., CAL-001"
                    />
                  )}
                </FormField>
                <FormField label="Name" required>
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.name}
                      onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                      className="input"
                      required
                      placeholder="e.g., 6in Digital Caliper"
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Equipment Type">
                  {(field) => (
                    <select
                      {...field}
                      value={formData.equipment_type}
                      onChange={(e) => setFormData({ ...formData, equipment_type: e.target.value })}
                      className="input"
                    >
                      <option value="">Select type...</option>
                      {equipmentTypes.map(t => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label="Calibration Interval (days)">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      value={formData.calibration_interval_days}
                      onChange={(e) => setFormData({ ...formData, calibration_interval_days: parseInt(e.target.value) })}
                      className="input"
                      min={1}
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <FormField label="Manufacturer">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.manufacturer}
                      onChange={(e) => setFormData({ ...formData, manufacturer: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
                <FormField label="Model">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.model}
                      onChange={(e) => setFormData({ ...formData, model: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
                <FormField label="Serial Number">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.serial_number}
                      onChange={(e) => setFormData({ ...formData, serial_number: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Location">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.location}
                      onChange={(e) => setFormData({ ...formData, location: e.target.value })}
                      className="input"
                      placeholder="e.g., QC Lab, Machine Shop"
                    />
                  )}
                </FormField>
                <FormField label="Calibration Provider">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={formData.calibration_provider}
                      onChange={(e) => setFormData({ ...formData, calibration_provider: e.target.value })}
                      className="input"
                      placeholder="e.g., Precision Calibration Inc."
                    />
                  )}
                </FormField>
              </div>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <Button type="button" variant="secondary" onClick={() => { setShowModal(false); resetForm(); }}>
                  Cancel
                </Button>
                <Button type="submit">
                  {editingEquipment ? 'Update' : 'Create'}
                </Button>
              </div>
            </form>
      </Modal>

      {/* Record Calibration Modal */}
      <Modal open={showCalibrationModal} onClose={() => setShowCalibrationModal(false)} size="lg" closeOnBackdrop={false}>
            <h3 className="text-lg font-semibold mb-4">Record Calibration</h3>
            <form onSubmit={handleCalibration} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Calibration Date" required>
                  {(field) => (
                    <input
                      {...field}
                      type="date"
                      value={calibrationData.calibration_date}
                      onChange={(e) => setCalibrationData({ ...calibrationData, calibration_date: e.target.value })}
                      className="input"
                      required
                    />
                  )}
                </FormField>
                <FormField label="Result" required>
                  {(field) => (
                    <select
                      {...field}
                      value={calibrationData.result}
                      onChange={(e) => setCalibrationData({ ...calibrationData, result: e.target.value })}
                      className="input"
                      required
                    >
                      <option value="pass">Pass</option>
                      <option value="fail">Fail</option>
                      <option value="adjusted">Adjusted</option>
                    </select>
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Performed By">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={calibrationData.performed_by}
                      onChange={(e) => setCalibrationData({ ...calibrationData, performed_by: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
                <FormField label="Certificate #">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={calibrationData.certificate_number}
                      onChange={(e) => setCalibrationData({ ...calibrationData, certificate_number: e.target.value })}
                      className="input"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Calibration Provider">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={calibrationData.calibration_provider}
                    onChange={(e) => setCalibrationData({ ...calibrationData, calibration_provider: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="As Found">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={calibrationData.as_found}
                      onChange={(e) => setCalibrationData({ ...calibrationData, as_found: e.target.value })}
                      className="input"
                      placeholder="Condition before cal"
                    />
                  )}
                </FormField>
                <FormField label="As Left">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={calibrationData.as_left}
                      onChange={(e) => setCalibrationData({ ...calibrationData, as_left: e.target.value })}
                      className="input"
                      placeholder="Condition after cal"
                    />
                  )}
                </FormField>
              </div>

              <FormField label="Cost ($)">
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    value={calibrationData.cost}
                    onChange={(e) => setCalibrationData({ ...calibrationData, cost: parseFloat(e.target.value) || 0 })}
                    className="input"
                    step="0.01"
                    min="0"
                  />
                )}
              </FormField>

              <FormField label="Notes">
                {(field) => (
                  <textarea
                    {...field}
                    value={calibrationData.notes}
                    onChange={(e) => setCalibrationData({ ...calibrationData, notes: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>

              <div className="flex justify-end gap-3 mt-6 pt-4 border-t">
                <Button type="button" variant="secondary" onClick={() => setShowCalibrationModal(false)}>
                  Cancel
                </Button>
                <Button type="submit">Record Calibration</Button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
