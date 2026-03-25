import React, { useState } from 'react';
import { Part, PartType } from '../../types';
import { partTypeColors, partTypeLabels } from '../../types/engineering';
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
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Basic Information */}
      <div className="card">
        <h3 className="text-base font-semibold text-gray-900 mb-4">Basic Information</h3>
        <dl className="space-y-3">
          <Field label="Part Number" value={part.part_number} />
          <Field label="Revision" value={part.revision} />
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
          <Field label="Type">
            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${partTypeColors[part.part_type]}`}>
              {partTypeLabels[part.part_type] || part.part_type}
            </span>
          </Field>
          <Field label="Unit of Measure" value={part.unit_of_measure} />
        </dl>
      </div>

      {/* Costing */}
      <div className="card">
        <h3 className="text-base font-semibold text-gray-900 mb-4">Costing</h3>
        <dl className="space-y-3">
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

        <h3 className="text-base font-semibold text-gray-900 mb-4 mt-6">Customer Information</h3>
        <dl className="space-y-3">
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

      {/* Quality / AS9100D */}
      <div className="card">
        <h3 className="text-base font-semibold text-gray-900 mb-4">Quality (AS9100D)</h3>
        <dl className="space-y-3">
          <Field label="Critical Characteristic">
            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
              part.is_critical ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-600'
            }`}>
              {part.is_critical ? 'Yes - Critical' : 'No'}
            </span>
          </Field>
          <Field label="Requires Inspection">
            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
              part.requires_inspection ? 'bg-blue-100 text-blue-800' : 'bg-gray-100 text-gray-600'
            }`}>
              {part.requires_inspection ? 'Yes' : 'No'}
            </span>
          </Field>
        </dl>
      </div>

      {/* Metadata */}
      <div className="card">
        <h3 className="text-base font-semibold text-gray-900 mb-4">Metadata</h3>
        <dl className="space-y-3">
          <Field label="Status" value={part.status} />
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
    <div className="flex items-start justify-between py-2 border-b border-gray-100 last:border-0">
      <dt className="text-sm text-gray-500 min-w-[120px]">{label}</dt>
      <dd className="text-sm text-gray-900 text-right">{children || value || <span className="text-gray-400">-</span>}</dd>
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
    <div className="flex items-start justify-between py-2 border-b border-gray-100 last:border-0 group">
      <dt className="text-sm text-gray-500 min-w-[120px]">{label}</dt>
      <dd className="text-sm text-gray-900 text-right flex items-center gap-1">
        {isEditing ? (
          <div className="flex items-center gap-1">
            {prefix && <span className="text-gray-500">{prefix}</span>}
            {multiline ? (
              <textarea
                value={editValue}
                onChange={e => onChange(e.target.value)}
                onKeyDown={e => { if (e.key === 'Escape') onCancel(); }}
                className="input text-sm py-1 px-2 w-48"
                rows={2}
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
                autoFocus
              />
            )}
            <button onClick={() => onSave(field)} className="text-green-600 hover:text-green-700 p-0.5">
              <CheckIcon className="h-4 w-4" />
            </button>
            <button onClick={onCancel} className="text-gray-400 hover:text-gray-600 p-0.5">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <>
            <span>{prefix}{value || <span className="text-gray-400">-</span>}</span>
            <button
              onClick={() => onStart(field, value)}
              className="text-gray-300 hover:text-werco-navy-600 p-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
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
