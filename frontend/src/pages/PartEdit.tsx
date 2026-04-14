import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { Part, PartType } from '../types';
import { Breadcrumbs } from '../components/ui/Breadcrumbs';
import { useToast } from '../components/ui/Toast';

export default function PartEdit() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const partId = Number(id);

  const [part, setPart] = useState<Part | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({
    name: '',
    description: '',
    part_type: 'manufactured' as PartType,
    revision: '',
    standard_cost: 0,
    is_critical: false,
    requires_inspection: true,
    customer_name: '',
    customer_part_number: '',
    drawing_number: '',
    version: 0,
  });

  useEffect(() => {
    api.getPart(partId)
      .then(data => {
        setPart(data);
        setForm({
          name: data.name,
          description: data.description || '',
          part_type: data.part_type,
          revision: data.revision,
          standard_cost: data.standard_cost,
          is_critical: data.is_critical,
          requires_inspection: data.requires_inspection,
          customer_name: data.customer_name || '',
          customer_part_number: data.customer_part_number || '',
          drawing_number: data.drawing_number || '',
          version: data.version,
        });
        setLoading(false);
      })
      .catch(() => {
        showToast('error', 'Part not found');
        navigate('/parts');
      });
  }, [partId, navigate, showToast]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      await api.updatePart(partId, form);
      showToast('success', 'Part updated');
      navigate(`/parts/${partId}`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update part');
    } finally {
      setSaving(false);
    }
  };

  if (loading || !part) {
    return (
      <div className="space-y-6 animate-pulse">
        <div className="h-6 w-48 bg-slate-700 rounded" />
        <div className="h-10 w-72 bg-slate-700 rounded" />
        <div className="h-96 bg-slate-700 rounded-xl" />
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      <Breadcrumbs crumbs={[
        { label: 'Parts', href: '/parts' },
        { label: part.part_number, href: `/parts/${part.id}` },
        { label: 'Edit' },
      ]} />

      <h1 className="text-2xl font-bold text-white">Edit {part.part_number}</h1>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic Information */}
        <div className="card">
          <h2 className="text-base font-semibold text-white mb-4">Basic Information</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="label">Part Number</label>
              <input type="text" value={part.part_number} disabled className="input bg-slate-800 text-slate-400" />
            </div>
            <div>
              <label className="label">Revision</label>
              <input
                type="text"
                value={form.revision}
                onChange={e => setForm(p => ({ ...p, revision: e.target.value }))}
                className="input"
                required
              />
            </div>
            <div className="sm:col-span-2">
              <label className="label">Name</label>
              <input
                type="text"
                value={form.name}
                onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                className="input"
                required
              />
            </div>
            <div className="sm:col-span-2">
              <label className="label">Description</label>
              <textarea
                value={form.description}
                onChange={e => setForm(p => ({ ...p, description: e.target.value }))}
                className="input"
                rows={3}
              />
            </div>
            <div>
              <label className="label">Type</label>
              <select
                value={form.part_type}
                onChange={e => setForm(p => ({ ...p, part_type: e.target.value as PartType }))}
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
                value={form.standard_cost}
                onChange={e => setForm(p => ({ ...p, standard_cost: parseFloat(e.target.value) || 0 }))}
                className="input"
              />
            </div>
          </div>
        </div>

        {/* Customer */}
        <div className="card">
          <h2 className="text-base font-semibold text-white mb-4">Customer Information</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <label className="label">Customer</label>
              <input
                type="text"
                value={form.customer_name}
                onChange={e => setForm(p => ({ ...p, customer_name: e.target.value }))}
                className="input"
              />
            </div>
            <div>
              <label className="label">Customer Part #</label>
              <input
                type="text"
                value={form.customer_part_number}
                onChange={e => setForm(p => ({ ...p, customer_part_number: e.target.value }))}
                className="input"
              />
            </div>
            <div>
              <label className="label">Drawing #</label>
              <input
                type="text"
                value={form.drawing_number}
                onChange={e => setForm(p => ({ ...p, drawing_number: e.target.value }))}
                className="input"
              />
            </div>
          </div>
        </div>

        {/* Quality */}
        <div className="card">
          <h2 className="text-base font-semibold text-white mb-4">Quality (AS9100D)</h2>
          <div className="flex gap-6">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.is_critical}
                onChange={e => setForm(p => ({ ...p, is_critical: e.target.checked }))}
                className="rounded border-slate-600 text-werco-navy-600"
              />
              <span className="text-sm">Critical Characteristic</span>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.requires_inspection}
                onChange={e => setForm(p => ({ ...p, requires_inspection: e.target.checked }))}
                className="rounded border-slate-600 text-werco-navy-600"
              />
              <span className="text-sm">Requires Inspection</span>
            </label>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => navigate(`/parts/${part.id}`)}
            className="btn-secondary"
          >
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={saving}>
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </form>
    </div>
  );
}
