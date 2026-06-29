import React, { useEffect, useMemo, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { Part, PartType } from '../types';
import { Breadcrumbs } from '../components/ui/Breadcrumbs';
import { FormField } from '../components/ui';
import { useToast } from '../components/ui/Toast';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import { ENGINEERING_PART_TYPE_OPTIONS } from '../utils/catalogGroups';

const EMPTY_FORM = {
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
};

export default function PartEdit() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const partId = Number(id);

  const [part, setPart] = useState<Part | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  // Snapshot of the values the form opened with, so an untouched edit form is not "dirty".
  const [initialForm, setInitialForm] = useState(EMPTY_FORM);

  useEffect(() => {
    api.getPart(partId)
      .then(data => {
        setPart(data);
        const loaded = {
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
        };
        setForm(loaded);
        setInitialForm(loaded);
        setLoading(false);
      })
      .catch(() => {
        showToast('error', 'Part not found');
        navigate('/parts');
      });
  }, [partId, navigate, showToast]);

  const isFormDirty = useMemo(
    () => !loading && JSON.stringify(form) !== JSON.stringify(initialForm),
    [loading, form, initialForm]
  );
  const { confirmDiscard } = useUnsavedChanges(isFormDirty);

  const handleCancel = () => {
    if (!confirmDiscard()) return;
    navigate(`/parts/${part?.id}`);
  };

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
            <FormField label="Part Number">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={part.part_number}
                  disabled
                  className="input bg-slate-800 text-slate-400"
                />
              )}
            </FormField>
            <FormField label="Revision" required>
              {(field) => (
                <input
                  {...field}
                  type="text"
                  autoFocus
                  value={form.revision}
                  onChange={e => setForm(p => ({ ...p, revision: e.target.value }))}
                  className="input"
                  required
                />
              )}
            </FormField>
            <FormField label="Name" required className="sm:col-span-2">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={form.name}
                  onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                  className="input"
                  required
                />
              )}
            </FormField>
            <FormField label="Description" className="sm:col-span-2">
              {(field) => (
                <textarea
                  {...field}
                  value={form.description}
                  onChange={e => setForm(p => ({ ...p, description: e.target.value }))}
                  className="input"
                  rows={3}
                />
              )}
            </FormField>
            <FormField label="Type">
              {(field) => (
                <select
                  {...field}
                  value={form.part_type}
                  onChange={e => setForm(p => ({ ...p, part_type: e.target.value as PartType }))}
                  className="input"
                >
                  {ENGINEERING_PART_TYPE_OPTIONS.map(option => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Standard Cost ($)">
              {(field) => (
                <input
                  {...field}
                  type="number"
                  min="0"
                  step="0.01"
                  value={form.standard_cost}
                  onChange={e => setForm(p => ({ ...p, standard_cost: parseFloat(e.target.value) || 0 }))}
                  className="input"
                />
              )}
            </FormField>
          </div>
        </div>

        {/* Customer */}
        <div className="card">
          <h2 className="text-base font-semibold text-white mb-4">Customer Information</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <FormField label="Customer">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={form.customer_name}
                  onChange={e => setForm(p => ({ ...p, customer_name: e.target.value }))}
                  className="input"
                />
              )}
            </FormField>
            <FormField label="Customer Part #">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={form.customer_part_number}
                  onChange={e => setForm(p => ({ ...p, customer_part_number: e.target.value }))}
                  className="input"
                />
              )}
            </FormField>
            <FormField label="Drawing #">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  value={form.drawing_number}
                  onChange={e => setForm(p => ({ ...p, drawing_number: e.target.value }))}
                  className="input"
                />
              )}
            </FormField>
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
            onClick={handleCancel}
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
