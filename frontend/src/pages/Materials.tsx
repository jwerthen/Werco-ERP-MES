import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import api from '../services/api';
import { Part, PartType } from '../types';
import { MATERIAL_SUPPLY_PART_TYPE_OPTIONS } from '../utils/catalogGroups';
import { partTypeColors } from '../types/engineering';
import { StatusBadge } from '../components/ui/StatusBadge';
import { useToast } from '../components/ui/Toast';
import { Modal } from '../components/ui/Modal';
import {
  DataTable,
  DataTableColumn,
  MobileDataCard,
} from '../components/ui';
import {
  ArrowUpTrayIcon,
  CubeIcon,
  MagnifyingGlassIcon,
  PencilIcon,
  PlusIcon,
  TrashIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

type MaterialForm = {
  part_number: string;
  name: string;
  part_type: PartType;
  unit_of_measure: string;
  description: string;
  standard_cost: number;
  requires_inspection: boolean;
  version?: number;
};

const BLANK_FORM: MaterialForm = {
  part_number: '',
  name: '',
  part_type: 'raw_material',
  unit_of_measure: 'each',
  description: '',
  standard_cost: 0,
  requires_inspection: true,
};

const UOM_OPTIONS = [
  { value: 'each', label: 'Each' },
  { value: 'sheets', label: 'Sheets' },
  { value: 'feet', label: 'Feet' },
  { value: 'inches', label: 'Inches' },
  { value: 'pounds', label: 'Pounds' },
  { value: 'kilograms', label: 'Kilograms' },
  { value: 'gallons', label: 'Gallons' },
  { value: 'liters', label: 'Liters' },
];

const typeLabel = (partType: string) => (
  MATERIAL_SUPPLY_PART_TYPE_OPTIONS.find(option => option.value === partType)?.label || partType.replace('_', ' ')
);

export default function MaterialsPage() {
  const { showToast } = useToast();
  const importInputRef = useRef<HTMLInputElement | null>(null);

  const [materials, setMaterials] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [showModal, setShowModal] = useState(false);
  const [editingMaterial, setEditingMaterial] = useState<Part | null>(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<MaterialForm>(BLANK_FORM);

  const loadMaterials = useCallback(async () => {
    try {
      setLoading(true);
      setLoadError(false);
      const params: any = {};
      if (typeFilter) params.part_type = typeFilter;
      if (search.trim()) params.search = search.trim();
      const data = await api.getMaterials(params);
      setMaterials(data);
    } catch {
      setLoadError(true);
      showToast('error', 'Failed to load materials and supplies');
    } finally {
      setLoading(false);
    }
  }, [search, showToast, typeFilter]);

  useEffect(() => {
    const timer = window.setTimeout(loadMaterials, 200);
    return () => window.clearTimeout(timer);
  }, [loadMaterials]);

  const visibleMaterials = useMemo(() => {
    if (!statusFilter) return materials;
    return materials.filter(material => material.status === statusFilter);
  }, [materials, statusFilter]);

  const stats = useMemo(() => ({
    total: materials.length,
    active: materials.filter(material => material.status === 'active').length,
    raw: materials.filter(material => material.part_type === 'raw_material').length,
    hardware: materials.filter(material => material.part_type === 'hardware').length,
  }), [materials]);

  const openCreate = () => {
    setEditingMaterial(null);
    setForm(BLANK_FORM);
    setShowModal(true);
  };

  const openEdit = (material: Part) => {
    setEditingMaterial(material);
    setForm({
      part_number: material.part_number,
      name: material.name,
      part_type: material.part_type,
      unit_of_measure: material.unit_of_measure || 'each',
      description: material.description || '',
      standard_cost: Number(material.standard_cost || 0),
      requires_inspection: material.requires_inspection,
      version: material.version || 0,
    });
    setShowModal(true);
  };

  const closeModal = () => {
    if (saving) return;
    setShowModal(false);
    setEditingMaterial(null);
    setForm(BLANK_FORM);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    try {
      const payload = {
        ...form,
        revision: 'A',
        is_critical: false,
      };
      if (editingMaterial) {
        const updated = await api.updateMaterial(editingMaterial.id, payload);
        setMaterials(prev => prev.map(material => material.id === updated.id ? updated : material));
        showToast('success', `Updated ${updated.part_number}`);
      } else {
        const created = await api.createMaterial(payload);
        setMaterials(prev => [created, ...prev].sort((a, b) => a.part_number.localeCompare(b.part_number)));
        showToast('success', `Created ${created.part_number}`);
      }
      closeModal();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save material');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (material: Part) => {
    if (!window.confirm(`Delete ${material.part_number}? This will remove it from the active materials list.`)) return;
    try {
      await api.deleteMaterial(material.id);
      setMaterials(prev => prev.filter(item => item.id !== material.id));
      showToast('success', `Deleted ${material.part_number}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete material');
    }
  };

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const result = await api.importMaterialsCsv(file);
      await loadMaterials();
      const imported = result.imported_count || 0;
      const skipped = result.skipped_count || 0;
      showToast('success', `Imported ${imported} item${imported === 1 ? '' : 's'}${skipped ? `, skipped ${skipped}` : ''}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to import materials CSV');
    } finally {
      event.target.value = '';
    }
  };

  const columns = useMemo<Array<DataTableColumn<Part>>>(() => [
    {
      key: 'part_number',
      header: 'Item #',
      sortable: true,
      accessor: material => material.part_number,
      render: material => <span className="font-medium text-werco-navy-600">{material.part_number}</span>,
    },
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: material => material.name,
      csv: material => material.description ? `${material.name} — ${material.description}` : material.name,
      render: material => (
        <div>
          <div className="text-sm text-white">{material.name}</div>
          {material.description && <div className="text-xs text-slate-500 line-clamp-1">{material.description}</div>}
        </div>
      ),
    },
    {
      key: 'part_type',
      header: 'Type',
      sortable: true,
      accessor: material => typeLabel(material.part_type),
      render: material => (
        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[material.part_type]}`}>
          {typeLabel(material.part_type)}
        </span>
      ),
    },
    {
      key: 'unit_of_measure',
      header: 'UOM',
      sortable: true,
      className: 'text-slate-300',
      accessor: material => material.unit_of_measure ?? '',
    },
    {
      key: 'standard_cost',
      header: 'Cost',
      sortable: true,
      align: 'right',
      accessor: material => Number(material.standard_cost || 0),
      csv: material => Number(material.standard_cost || 0).toFixed(2),
      render: material => `$${Number(material.standard_cost || 0).toFixed(2)}`,
    },
    {
      key: 'requires_inspection',
      header: 'Inspection',
      sortable: true,
      align: 'center',
      className: 'text-slate-300',
      accessor: material => (material.requires_inspection ? 'Required' : 'Not required'),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      align: 'center',
      accessor: material => material.status,
      render: material => <StatusBadge status={material.status} />,
    },
    {
      key: 'actions',
      header: '',
      align: 'right',
      render: material => (
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={event => { event.stopPropagation(); openEdit(material); }}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-800 hover:text-slate-200"
            title={`Edit ${material.part_number}`}
            aria-label={`Edit ${material.part_number}`}
          >
            <PencilIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={event => { event.stopPropagation(); handleDelete(material); }}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-red-500/10 hover:text-red-400"
            title={`Delete ${material.part_number}`}
            aria-label={`Delete ${material.part_number}`}
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </div>
      ),
    },
  ], []);

  const renderMobileCard = useCallback((material: Part) => (
    <MobileDataCard
      title={material.part_number}
      subtitle={material.name}
      badge={<StatusBadge status={material.status} />}
      onClick={() => openEdit(material)}
      fields={[
        {
          label: 'Type',
          value: (
            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[material.part_type]}`}>
              {typeLabel(material.part_type)}
            </span>
          ),
        },
        { label: 'UOM', value: material.unit_of_measure || '—' },
        { label: 'Cost', value: `$${Number(material.standard_cost || 0).toFixed(2)}` },
        { label: 'Inspection', value: material.requires_inspection ? 'Required' : 'Not required' },
        ...(material.description ? [{ label: 'Description', value: material.description, fullWidth: true }] : []),
      ]}
      actions={(
        <>
          <button
            type="button"
            onClick={event => { event.stopPropagation(); openEdit(material); }}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-800 hover:text-slate-200"
            aria-label={`Edit ${material.part_number}`}
          >
            <PencilIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={event => { event.stopPropagation(); handleDelete(material); }}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-red-500/10 hover:text-red-400"
            aria-label={`Delete ${material.part_number}`}
          >
            <TrashIcon className="h-4 w-4" />
          </button>
        </>
      )}
    />
  ), []);

  const hasFilters = Boolean(search || typeFilter || statusFilter);

  return (
    <div className="space-y-5">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Materials & Supplies</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            {stats.total} items · {stats.active} active · {stats.raw} raw material · {stats.hardware} hardware
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <input ref={importInputRef} type="file" accept=".csv" className="hidden" onChange={handleImport} />
          <button type="button" onClick={() => importInputRef.current?.click()} className="btn-secondary flex items-center gap-2">
            <ArrowUpTrayIcon className="h-4 w-4" />
            Import CSV
          </button>
          <button type="button" onClick={openCreate} className="btn-primary flex items-center gap-2">
            <PlusIcon className="h-4 w-4" />
            New Item
          </button>
        </div>
      </div>

      <div className="flex flex-col lg:flex-row gap-3 lg:items-center">
        <div className="relative flex-1 max-w-md">
          <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input
            type="text"
            placeholder="Search material number, name, description..."
            value={search}
            onChange={event => setSearch(event.target.value)}
            className="input pl-9 py-2 text-sm"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <select value={typeFilter} onChange={event => setTypeFilter(event.target.value)} className="input py-2 text-sm w-44">
            <option value="">All Supply Types</option>
            {MATERIAL_SUPPLY_PART_TYPE_OPTIONS.map(option => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select value={statusFilter} onChange={event => setStatusFilter(event.target.value)} className="input py-2 text-sm w-32">
            <option value="">All Status</option>
            <option value="active">Active</option>
            <option value="obsolete">Obsolete</option>
            <option value="pending_approval">Pending</option>
          </select>
          {(search || typeFilter || statusFilter) && (
            <button
              type="button"
              onClick={() => {
                setSearch('');
                setTypeFilter('');
                setStatusFilter('');
              }}
              className="btn-secondary flex items-center gap-2 text-sm"
            >
              <XMarkIcon className="h-4 w-4" />
              Clear
            </button>
          )}
        </div>
      </div>

      <DataTable
        columns={columns}
        data={visibleMaterials}
        rowKey={material => material.id}
        onRowClick={openEdit}
        loading={loading}
        error={loadError}
        onRetry={loadMaterials}
        defaultSort={{ key: 'part_number', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: `werco-materials-${new Date().toISOString().slice(0, 10)}` }}
        mobileCards={renderMobileCard}
        empty={{
          icon: CubeIcon,
          title: hasFilters ? 'No matching materials or supplies' : 'No materials or supplies yet',
          description: hasFilters
            ? 'No materials or supplies match your current filters.'
            : 'Add your first item or import a CSV to get started.',
          action: hasFilters
            ? { label: 'Clear filters', onClick: () => { setSearch(''); setTypeFilter(''); setStatusFilter(''); } }
            : { label: 'New Item', onClick: openCreate },
        }}
      />

      <Modal open={showModal} onClose={closeModal} size="lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">{editingMaterial ? 'Edit Supply Item' : 'New Supply Item'}</h3>
              <button type="button" onClick={closeModal} className="text-slate-500 hover:text-slate-200">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="label">Item Number</label>
                  <input
                    type="text"
                    value={form.part_number}
                    onChange={event => setForm(prev => ({ ...prev, part_number: event.target.value }))}
                    className="input"
                    disabled={Boolean(editingMaterial)}
                    required
                    autoFocus
                  />
                </div>
                <div>
                  <label className="label">Type</label>
                  <select
                    value={form.part_type}
                    onChange={event => setForm(prev => ({ ...prev, part_type: event.target.value as PartType }))}
                    className="input"
                  >
                    {MATERIAL_SUPPLY_PART_TYPE_OPTIONS.map(option => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="label">Name</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={event => setForm(prev => ({ ...prev, name: event.target.value }))}
                  className="input"
                  required
                />
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="label">Unit of Measure</label>
                  <select
                    value={form.unit_of_measure}
                    onChange={event => setForm(prev => ({ ...prev, unit_of_measure: event.target.value }))}
                    className="input"
                  >
                    {UOM_OPTIONS.map(option => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">Standard Cost ($)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={form.standard_cost}
                    onChange={event => setForm(prev => ({ ...prev, standard_cost: parseFloat(event.target.value) || 0 }))}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <label className="label">Description</label>
                <textarea
                  value={form.description}
                  onChange={event => setForm(prev => ({ ...prev, description: event.target.value }))}
                  className="input"
                  rows={3}
                />
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.requires_inspection}
                  onChange={event => setForm(prev => ({ ...prev, requires_inspection: event.target.checked }))}
                  className="rounded border-slate-600 text-werco-navy-600"
                />
                Requires receiving inspection
              </label>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={closeModal} className="btn-secondary" disabled={saving}>
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={saving}>
                  {saving ? 'Saving...' : editingMaterial ? 'Save Changes' : 'Create Item'}
                </button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
