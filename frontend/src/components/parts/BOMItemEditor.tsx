import React, { useRef, useState } from 'react';
import api from '../../services/api';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { FormField, Modal } from '../ui';
import { useToast } from '../ui/Toast';

export interface EditableBOMItem {
  id: number;
  quantity: number;
  line_type?: string;
  scrap_factor?: number;
  is_optional?: boolean;
  find_number?: string;
  notes?: string;
  torque_spec?: string;
  installation_notes?: string;
  component_part?: { part_number: string; name: string };
}

/** Updates only editable fields: component identity and historical line ID stay intact. */
export function BOMItemEditor({ item, onClose, onSaved }: {
  item: EditableBOMItem;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const { showToast } = useToast();
  const [form, setForm] = useState({
    quantity: String(item.quantity), line_type: item.line_type || 'component',
    scrap_factor: String(item.scrap_factor || 0), is_optional: !!item.is_optional,
    find_number: item.find_number || '', notes: item.notes || '',
    torque_spec: item.torque_spec || '', installation_notes: item.installation_notes || '',
  });
  const initialForm = useRef(JSON.stringify(form));
  const { confirmDiscard, markSaved } = useUnsavedChanges(JSON.stringify(form) !== initialForm.current);
  const requestClose = () => { if (!busy.current && confirmDiscard()) onClose(); };
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  const [error, setError] = useState('');
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy.current) return;
    const quantity = Number(form.quantity);
    const scrap = Number(form.scrap_factor);
    if (!form.quantity.trim() || !Number.isFinite(quantity) || quantity <= 0 || !Number.isFinite(scrap) || scrap < 0 || scrap > 1) {
      setError('Enter a positive quantity and a scrap factor from 0 to 1 (0.05 = 5%).');
      return;
    }
    busy.current = true;
    setPending(true);
    setError('');
    try {
      const saved = await api.updateBOMItem(item.id, { ...form, quantity, scrap_factor: scrap });
      markSaved();
      if (saved.backflush_armed_warning) showToast('warning', saved.backflush_armed_warning);
      showToast('success', `Updated ${item.component_part?.part_number || 'BOM component'}`);
      try { await onSaved(); } catch { showToast('warning', 'Component saved. Refresh the BOM to see the latest values.'); }
      onClose();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Could not save this component. Your changes are still here.');
    } finally {
      busy.current = false;
      setPending(false);
    }
  };
  return <Modal open onClose={requestClose} ariaLabelledBy="bom-item-edit-title">
    <h2 id="bom-item-edit-title" className="text-lg font-semibold mb-2">Edit BOM component</h2>
    <p className="text-sm text-slate-400 mb-4">{item.component_part?.part_number} {item.component_part?.name}</p>
    <form onSubmit={save} className="space-y-3">
      {error && <p role="alert" className="text-red-300">{error}</p>}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <FormField label="Quantity" required>{field => <input {...field} className="input" type="number" step="any" min="0" required value={form.quantity} onChange={e => setForm({ ...form, quantity: e.target.value })} />}</FormField>
        <FormField label="Category">{field => <select {...field} className="input" value={form.line_type} onChange={e => setForm({ ...form, line_type: e.target.value })}>{['component','hardware','consumable','reference'].map(value => <option key={value} value={value}>{value}</option>)}</select>}</FormField>
        <FormField label="Scrap factor" help="Fraction from 0 to 1; 0.05 means 5%.">{field => <input {...field} className="input" type="number" min="0" max="1" step="any" value={form.scrap_factor} onChange={e => setForm({ ...form, scrap_factor: e.target.value })} />}</FormField>
        <FormField label="Find number">{field => <input {...field} className="input" value={form.find_number} onChange={e => setForm({ ...form, find_number: e.target.value })} />}</FormField>
      </div>
      <label className="flex gap-2"><input type="checkbox" checked={form.is_optional} onChange={e => setForm({ ...form, is_optional: e.target.checked })} />Optional component</label>
      {(['notes','torque_spec','installation_notes'] as const).map(key => <FormField key={key} label={{ notes: 'Notes', torque_spec: 'Torque specification', installation_notes: 'Installation instructions' }[key]}>{field => <textarea {...field} className="input" rows={2} value={form[key]} onChange={e => setForm({ ...form, [key]: e.target.value })} />}</FormField>)}
      <div className="flex flex-wrap justify-end gap-2"><button type="button" className="btn-secondary" disabled={pending} onClick={requestClose}>Cancel</button><button className="btn-primary" type="submit" disabled={pending}>{pending ? 'Saving…' : 'Save component'}</button></div>
    </form>
  </Modal>;
}
