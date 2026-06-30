import React, { useState } from 'react';
import { Part } from '../../types';
import { useToast } from '../ui/Toast';
import api from '../../services/api';
import { PencilIcon, CheckIcon, XMarkIcon } from '@heroicons/react/24/outline';

interface Props {
  part: Part;
  onPartUpdated: (part: Part) => void;
}

export function PartOverviewTab({ part, onPartUpdated }: Props) {
  const { showToast } = useToast();
  const [editingField, setEditingField] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>('');

  const startEdit = (field: string, value: string) => {
    setEditingField(field);
    setEditValue(value);
  };

  const cancelEdit = () => {
    setEditingField(null);
    setEditValue('');
  };

  const saveEdit = async (field: string) => {
    try {
      let payload: any = { [field]: editValue, version: part.version };
      if (['standard_cost'].includes(field)) {
        payload[field] = parseFloat(editValue) || 0;
      }
      if (['is_critical', 'requires_inspection'].includes(field)) {
        payload[field] = editValue === 'true';
      }
      const updated = await api.updatePart(part.id, payload);
      onPartUpdated(updated);
      setEditingField(null);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || `Failed to update ${field}`);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent, field: string) => {
    if (e.key === 'Enter') saveEdit(field);
    if (e.key === 'Escape') cancelEdit();
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {/* Basic Information */}
      <div className="card !p-3">
        <h3 className="text-base font-semibold text-white mb-3">Basic Information</h3>
        <dl className="space-y-2">
          <EditableField
            label="Name"
            field="name"
            value={part.name}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
          />
          <EditableField
            label="Description"
            field="description"
            value={part.description || ''}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
            multiline
          />
          <Field label="Unit of Measure" value={part.unit_of_measure} />
        </dl>
      </div>

      {/* Costing */}
      <div className="card !p-3">
        <h3 className="text-base font-semibold text-white mb-3">Costing</h3>
        <dl className="space-y-2">
          <EditableField
            label="Standard Cost"
            field="standard_cost"
            value={`${Number(part.standard_cost || 0).toFixed(2)}`}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
            prefix="$"
            inputType="number"
          />
        </dl>

        <h3 className="text-base font-semibold text-white mb-3 mt-5">Customer Information</h3>
        <dl className="space-y-2">
          <EditableField
            label="Customer"
            field="customer_name"
            value={part.customer_name || ''}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
          />
          <EditableField
            label="Customer Part #"
            field="customer_part_number"
            value={part.customer_part_number || ''}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
          />
          <EditableField
            label="Drawing #"
            field="drawing_number"
            value={part.drawing_number || ''}
            editing={editingField}
            editValue={editValue}
            onStart={startEdit}
            onSave={saveEdit}
            onCancel={cancelEdit}
            onChange={setEditValue}
            onKeyDown={handleKeyDown}
          />
        </dl>
      </div>

      {/* Metadata */}
      <div className="card !p-3 lg:col-span-2">
        <h3 className="text-base font-semibold text-white mb-3">Metadata</h3>
        <dl className="space-y-2">
          <Field label="Active" value={part.is_active ? 'Yes' : 'No'} />
          <Field label="Created" value={new Date(part.created_at).toLocaleDateString()} />
          <Field label="Updated" value={new Date(part.updated_at).toLocaleDateString()} />
        </dl>
      </div>
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────

function Field({ label, value, children }: { label: string; value?: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-1.5 border-b border-fd-line last:border-0">
      <dt className="text-sm text-slate-400 min-w-[120px]">{label}</dt>
      <dd className="text-sm text-slate-100 text-right break-words tabular-nums">{children || value || <span className="text-slate-500">-</span>}</dd>
    </div>
  );
}

interface EditableFieldProps {
  label: string;
  field: string;
  value: string;
  editing: string | null;
  editValue: string;
  onStart: (field: string, value: string) => void;
  onSave: (field: string) => void;
  onCancel: () => void;
  onChange: (value: string) => void;
  onKeyDown: (e: React.KeyboardEvent, field: string) => void;
  prefix?: string;
  inputType?: string;
  multiline?: boolean;
}

function EditableField({
  label, field, value, editing, editValue,
  onStart, onSave, onCancel, onChange, onKeyDown,
  prefix, inputType = 'text', multiline,
}: EditableFieldProps) {
  const isEditing = editing === field;

  return (
    <div className="flex items-start justify-between gap-4 py-1.5 border-b border-fd-line last:border-0 group">
      <dt className="text-sm text-slate-400 min-w-[120px]">{label}</dt>
      <dd className="text-sm text-slate-100 text-right flex items-center justify-end gap-1 min-w-0 tabular-nums">
        {isEditing ? (
          <div className="flex items-center gap-1">
            {prefix && <span className="text-slate-400">{prefix}</span>}
            {multiline ? (
              <textarea
                value={editValue}
                onChange={e => onChange(e.target.value)}
                onKeyDown={e => { if (e.key === 'Escape') onCancel(); }}
                className="input text-sm py-1 px-2 w-48"
                rows={2}
                aria-label={`Edit ${label}`}
                autoFocus
              />
            ) : (
              <input
                type={inputType}
                value={editValue}
                onChange={e => onChange(e.target.value)}
                onKeyDown={e => onKeyDown(e, field)}
                className="input text-sm py-1 px-2 w-48"
                step={inputType === 'number' ? '0.01' : undefined}
                aria-label={`Edit ${label}`}
                autoFocus
              />
            )}
            <button onClick={() => onSave(field)} className="text-emerald-400 hover:text-emerald-300 p-0.5">
              <CheckIcon className="h-4 w-4" />
            </button>
            <button onClick={onCancel} className="text-slate-500 hover:text-slate-200 p-0.5">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <>
            <span className="break-words">{prefix}{value || <span className="text-slate-500">-</span>}</span>
            <button
              onClick={() => onStart(field, value)}
              className="text-slate-600 hover:text-cyan-300 p-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
              title={`Edit ${label}`}
            >
              <PencilIcon className="h-3.5 w-3.5" />
            </button>
          </>
        )}
      </dd>
    </div>
  );
}
