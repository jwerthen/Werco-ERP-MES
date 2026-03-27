import React, { useEffect, useState, useMemo, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { Part, PartType } from '../types';
import { partTypeColors } from '../types/engineering';
import { StatusBadge } from '../components/ui/StatusBadge';
import { useToast } from '../components/ui/Toast';
import { BOMImportWizard } from '../components/parts/BOMImportWizard';
import { SkeletonTable } from '../components/ui/Skeleton';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  ArrowUpTrayIcon,
  ChevronRightIcon,
  Squares2X2Icon,
  ListBulletIcon as ListIcon,
} from '@heroicons/react/24/outline';

type ViewMode = 'table' | 'grid';

export default function PartsPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [parts, setParts] = useState<Part[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [viewMode, setViewMode] = useState<ViewMode>('table');
  const [showImport, setShowImport] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);

  // Create form
  const [createForm, setCreateForm] = useState({
    part_number: '',
    name: '',
    part_type: 'manufactured' as PartType,
    description: '',
    revision: 'A',
    standard_cost: 0,
    is_critical: false,
    requires_inspection: true,
    customer_name: '',
    customer_part_number: '',
    drawing_number: '',
  });

  const loadParts = useCallback(async () => {
    try {
      const params: any = {};
      if (typeFilter) params.part_type = typeFilter;
      const data = await api.getParts(params);
      setParts(data);
    } catch {
      showToast('error', 'Failed to load parts');
    } finally {
      setLoading(false);
    }
  }, [typeFilter, showToast]);

  useEffect(() => {
    loadParts();
  }, [loadParts]);

  const filteredParts = useMemo(() => {
    let result = parts;
    if (search) {
      const s = search.toLowerCase();
      result = result.filter(p =>
        p.part_number.toLowerCase().includes(s) ||
        p.name.toLowerCase().includes(s) ||
        (p.customer_name || '').toLowerCase().includes(s) ||
        (p.customer_part_number || '').toLowerCase().includes(s) ||
        (p.description || '').toLowerCase().includes(s)
      );
    }
    if (statusFilter) {
      result = result.filter(p => p.status === statusFilter);
    }
    return result;
  }, [parts, search, statusFilter]);

  const stats = useMemo(() => ({
    total: parts.length,
    active: parts.filter(p => p.status === 'active').length,
    manufactured: parts.filter(p => p.part_type === 'manufactured' || p.part_type === 'assembly').length,
    critical: parts.filter(p => p.is_critical).length,
  }), [parts]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const newPart = await api.createPart(createForm);
      showToast('success', `Part ${newPart.part_number} created`);
      setShowCreateModal(false);
      navigate(`/parts/${newPart.id}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create part');
    }
  };

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <div className="h-8 w-24 bg-gray-200 rounded animate-pulse" />
          <div className="h-10 w-32 bg-gray-200 rounded animate-pulse" />
        </div>
        <div className="card"><SkeletonTable rows={8} columns={8} /></div>
      </div>
    );
  }

  return (
    <div className="space-y-5" data-tour="eng-parts">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Parts</h1>
          <p className="text-sm text-gray-500 mt-0.5">{stats.total} parts · {stats.active} active · {stats.critical} critical</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowImport(true)} className="btn-secondary flex items-center gap-2">
            <ArrowUpTrayIcon className="h-4 w-4" />
            Import
          </button>
          <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center gap-2">
            <PlusIcon className="h-4 w-4" />
            New Part
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
        <div className="relative flex-1 max-w-md">
          <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search parts, customers, descriptions..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="input pl-9 py-2 text-sm"
          />
        </div>
        <div className="flex gap-2 items-center">
          <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)} className="input py-2 text-sm w-40">
            <option value="">All Types</option>
            <option value="manufactured">Manufactured</option>
            <option value="assembly">Assembly</option>
            <option value="purchased">Purchased</option>
            <option value="hardware">Hardware</option>
            <option value="consumable">Consumable</option>
            <option value="raw_material">Raw Material</option>
          </select>
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="input py-2 text-sm w-32">
            <option value="">All Status</option>
            <option value="active">Active</option>
            <option value="obsolete">Obsolete</option>
            <option value="pending_approval">Pending</option>
          </select>
          {/* View toggle */}
          <div className="flex rounded-lg border border-gray-300 overflow-hidden">
            <button
              onClick={() => setViewMode('table')}
              className={`p-2 ${viewMode === 'table' ? 'bg-gray-100' : 'bg-white hover:bg-gray-50'}`}
              title="Table view"
            >
              <ListIcon className="h-4 w-4 text-gray-600" />
            </button>
            <button
              onClick={() => setViewMode('grid')}
              className={`p-2 ${viewMode === 'grid' ? 'bg-gray-100' : 'bg-white hover:bg-gray-50'}`}
              title="Grid view"
            >
              <Squares2X2Icon className="h-4 w-4 text-gray-600" />
            </button>
          </div>
        </div>
      </div>

      {/* Table View */}
      {viewMode === 'table' && (
        <div className="card overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Rev</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Cost</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 w-10" />
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {filteredParts.map(part => (
                  <tr
                    key={part.id}
                    onClick={() => navigate(`/parts/${part.id}`)}
                    className="hover:bg-gray-50 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3">
                      <span className="font-medium text-werco-navy-600">{part.part_number}</span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="text-sm">{part.name}</div>
                      {part.customer_part_number && (
                        <div className="text-xs text-gray-400">Cust P/N: {part.customer_part_number}</div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500">{part.customer_name || '-'}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[part.part_type]}`}>
                        {part.part_type.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center text-sm font-medium">{part.revision}</td>
                    <td className="px-4 py-3 text-right text-sm">${Number(part.standard_cost || 0).toFixed(2)}</td>
                    <td className="px-4 py-3 text-center">
                      <div className="flex items-center justify-center gap-1.5">
                        <StatusBadge status={part.status} />
                        {part.is_critical && (
                          <span className="inline-flex px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-[10px] font-semibold">
                            CRIT
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <ChevronRightIcon className="h-4 w-4 text-gray-400" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {filteredParts.length === 0 && (
            <div className="text-center py-12 text-gray-500">
              <p className="text-sm">No parts found matching your filters</p>
            </div>
          )}
        </div>
      )}

      {/* Grid View */}
      {viewMode === 'grid' && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {filteredParts.map(part => (
            <div
              key={part.id}
              onClick={() => navigate(`/parts/${part.id}`)}
              className="card cursor-pointer hover:shadow-md hover:border-werco-navy-200 transition-all border border-gray-200 p-4"
            >
              <div className="flex items-start justify-between mb-2">
                <span className="font-semibold text-werco-navy-600 text-sm">{part.part_number}</span>
                <StatusBadge status={part.status} />
              </div>
              <h3 className="text-sm font-medium text-gray-900 mb-1 line-clamp-2">{part.name}</h3>
              {part.customer_name && (
                <p className="text-xs text-gray-500 mb-2">{part.customer_name}</p>
              )}
              <div className="flex items-center justify-between mt-auto pt-2 border-t border-gray-100">
                <span className={`inline-flex px-2 py-0.5 rounded text-[10px] font-medium ${partTypeColors[part.part_type]}`}>
                  {part.part_type.replace('_', ' ')}
                </span>
                <span className="text-xs text-gray-500">Rev {part.revision}</span>
              </div>
              {part.is_critical && (
                <div className="mt-2">
                  <span className="inline-flex px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-[10px] font-semibold">
                    Critical
                  </span>
                </div>
              )}
            </div>
          ))}
          {filteredParts.length === 0 && (
            <div className="col-span-full text-center py-12 text-gray-500">
              <p className="text-sm">No parts found matching your filters</p>
            </div>
          )}
        </div>
      )}

      {/* Create Part Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowCreateModal(false)}>
          <div className="bg-white rounded-xl p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">New Part</h3>
            <form onSubmit={handleCreate} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number</label>
                  <input
                    type="text"
                    value={createForm.part_number}
                    onChange={e => setCreateForm(p => ({ ...p, part_number: e.target.value }))}
                    className="input"
                    required
                    autoFocus
                  />
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={createForm.revision}
                    onChange={e => setCreateForm(p => ({ ...p, revision: e.target.value }))}
                    className="input"
                    required
                  />
                </div>
              </div>

              <div>
                <label className="label">Name</label>
                <input
                  type="text"
                  value={createForm.name}
                  onChange={e => setCreateForm(p => ({ ...p, name: e.target.value }))}
                  className="input"
                  required
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Type</label>
                  <select
                    value={createForm.part_type}
                    onChange={e => setCreateForm(p => ({ ...p, part_type: e.target.value as PartType }))}
                    className="input"
                  >
                    <option value="manufactured">Manufactured</option>
                    <option value="assembly">Assembly</option>
                    <option value="purchased">Purchased</option>
                    <option value="hardware">Hardware</option>
                    <option value="consumable">Consumable</option>
                    <option value="raw_material">Raw Material</option>
                  </select>
                </div>
                <div>
                  <label className="label">Standard Cost ($)</label>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={createForm.standard_cost}
                    onChange={e => setCreateForm(p => ({ ...p, standard_cost: parseFloat(e.target.value) || 0 }))}
                    className="input"
                  />
                </div>
              </div>

              <div>
                <label className="label">Description</label>
                <textarea
                  value={createForm.description}
                  onChange={e => setCreateForm(p => ({ ...p, description: e.target.value }))}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Customer</label>
                  <input
                    type="text"
                    value={createForm.customer_name}
                    onChange={e => setCreateForm(p => ({ ...p, customer_name: e.target.value }))}
                    className="input"
                    placeholder="Optional"
                  />
                </div>
                <div>
                  <label className="label">Drawing #</label>
                  <input
                    type="text"
                    value={createForm.drawing_number}
                    onChange={e => setCreateForm(p => ({ ...p, drawing_number: e.target.value }))}
                    className="input"
                    placeholder="Optional"
                  />
                </div>
              </div>

              <div className="flex gap-4">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={createForm.is_critical}
                    onChange={e => setCreateForm(p => ({ ...p, is_critical: e.target.checked }))}
                    className="rounded border-gray-300 text-werco-navy-600"
                  />
                  <span className="text-sm">Critical Characteristic</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={createForm.requires_inspection}
                    onChange={e => setCreateForm(p => ({ ...p, requires_inspection: e.target.checked }))}
                    className="rounded border-gray-300 text-werco-navy-600"
                  />
                  <span className="text-sm">Requires Inspection</span>
                </label>
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  Create Part
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Import Wizard */}
      {showImport && (
        <BOMImportWizard
          onComplete={async () => {
            await loadParts();
            setShowImport(false);
          }}
          onClose={() => setShowImport(false)}
        />
      )}
    </div>
  );
}
