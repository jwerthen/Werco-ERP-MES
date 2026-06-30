import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import api from '../../services/api';
import { ImportPreview, ImportItem, ImportAssembly } from '../../types/engineering';
import { useToast } from '../ui/Toast';
import { ArrowUturnLeftIcon, TrashIcon, XMarkIcon } from '@heroicons/react/24/outline';

interface Props {
  onComplete: () => Promise<void>;
  onClose: () => void;
}

type Step = 'upload' | 'preview';
type RemovedImportItem = { item: ImportItem; index: number };

const COLUMN_FIELDS = [
  { key: 'line_number', label: 'Line #' },
  { key: 'part_number', label: 'Part #' },
  { key: 'description', label: 'Description' },
  { key: 'quantity', label: 'Qty' },
  { key: 'unit_of_measure', label: 'UOM' },
  { key: 'item_type', label: 'Item Type' },
  { key: 'line_type', label: 'Line Type' },
];

export function BOMImportWizard({ onComplete, onClose }: Props) {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [step, setStep] = useState<Step>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [createMissingParts, setCreateMissingParts] = useState(true);
  const [loading, setLoading] = useState(false);

  // Preview state
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [columnMap, setColumnMap] = useState<Record<string, number | null>>({});
  const [derivedItems, setDerivedItems] = useState<ImportItem[]>([]);
  const [removedItems, setRemovedItems] = useState<RemovedImportItem[]>([]);

  // ── Helpers ──────────────────────────────────────────────────────────

  const buildItemsFromRaw = (rawColumns: string[], rawRows: string[][], mapping: Record<string, number | null>) => {
    const items: ImportItem[] = [];
    let nextLine = 10;
    rawRows.forEach(row => {
      const hasData = row.some(cell => (cell || '').toString().trim() !== '');
      if (!hasData) return;
      const getVal = (field: string) => {
        const idx = mapping[field];
        if (idx === null || idx === undefined || idx >= row.length) return '';
        return (row[idx] || '').toString().trim();
      };
      const lineVal = getVal('line_number');
      const lineNumber = lineVal ? parseInt(lineVal) : nextLine;
      nextLine = (isNaN(lineNumber) ? nextLine : lineNumber) + 10;
      const quantityVal = getVal('quantity');
      const quantity = quantityVal ? parseFloat(quantityVal) : 1;
      items.push({
        line_number: isNaN(lineNumber) ? nextLine : lineNumber,
        part_number: getVal('part_number') || undefined,
        description: getVal('description') || undefined,
        quantity: isNaN(quantity) ? 1 : quantity,
        unit_of_measure: getVal('unit_of_measure') || undefined,
        item_type: getVal('item_type') || undefined,
        line_type: (getVal('line_type') as any) || undefined,
      });
    });
    return items;
  };

  // ── Actions ──────────────────────────────────────────────────────────

  const handlePreview = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setLoading(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const result = await api.previewBOMImport(formData);
      setPreview(result);
      setWarnings(result.warnings || []);
      if (result.raw_columns?.length && result.raw_rows) {
        const mapping = result.suggested_mapping || {};
        setColumnMap(mapping);
        setDerivedItems(buildItemsFromRaw(result.raw_columns, result.raw_rows, mapping));
      } else {
        setDerivedItems(result.items || []);
      }
      setRemovedItems([]);
      setStep('preview');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to generate preview');
    } finally {
      setLoading(false);
    }
  };

  const updateAssembly = (field: keyof ImportAssembly, value: string) => {
    if (!preview) return;
    setPreview({ ...preview, assembly: { ...preview.assembly, [field]: value } });
  };

  const handleColumnMapChange = (field: string, value: string) => {
    if (!preview?.raw_columns || !preview.raw_rows) return;
    const idx = value === '' ? null : parseInt(value);
    const nextMap = { ...columnMap, [field]: idx };
    setColumnMap(nextMap);
    setDerivedItems(buildItemsFromRaw(preview.raw_columns, preview.raw_rows, nextMap));
    setRemovedItems([]);
  };

  const updateDerivedItem = (index: number, patch: Partial<ImportItem>) => {
    setDerivedItems(items => items.map((item, idx) => (
      idx === index ? { ...item, ...patch } : item
    )));
  };

  const removeDerivedItem = (index: number) => {
    const item = derivedItems[index];
    if (!item) return;
    setDerivedItems(items => items.filter((_, idx) => idx !== index));
    setRemovedItems(items => [{ item, index }, ...items]);
  };

  const restoreLastRemovedItem = () => {
    const lastRemoved = removedItems[0];
    if (!lastRemoved) return;
    setDerivedItems(items => {
      const next = [...items];
      next.splice(Math.min(lastRemoved.index, next.length), 0, lastRemoved.item);
      return next;
    });
    setRemovedItems(items => items.slice(1));
  };

  const handleCommit = async () => {
    if (!preview) return;
    if (preview.document_type === 'bom' && derivedItems.length === 0) {
      showToast('error', 'Add at least one BOM line before creating');
      return;
    }
    setLoading(true);
    try {
      const result = await api.commitBOMImport({
        document_type: preview.document_type,
        assembly: preview.assembly,
        items: derivedItems,
        create_missing_parts: createMissingParts,
      });
      if (result.warnings?.length) {
        showToast('info', `Import completed with ${result.warnings.length} warning(s)`);
      } else {
        showToast('success', 'Import completed');
      }
      await onComplete();
      if (result.bom_id) {
        // Navigate to the part's BOM tab
        const bom = await api.getBOM(result.bom_id);
        if (bom?.part_id) {
          navigate(`/parts/${bom.part_id}?tab=bom`);
        }
      }
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to import');
    } finally {
      setLoading(false);
    }
  };

  const displayItems = derivedItems;

  // ── Render ───────────────────────────────────────────────────────────

  // Portal to <body> so the overlay is a sibling of the app layout root rather
  // than nested under <main>. Inline-rendered modals tie the sidebar's z-50
  // (both fixed), letting the opaque sidebar paint over the backdrop and clip
  // the modal's left edge. z-[60] matches the other BOM/parts modals that sit
  // above the sidebar.
  const overlay = (
    // The backdrop is purely presentational — keyboard users dismiss via the
    // in-panel close button (the X / Cancel controls). Restricting the close to
    // clicks that land directly on the backdrop (target === currentTarget)
    // replaces the panel-level stopPropagation handler.
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60]"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="bg-fd-panel rounded-xl shadow-xl mx-4 animate-scale-in flex flex-col"
        style={{ maxWidth: step === 'preview' ? '72rem' : '32rem', maxHeight: '90vh', width: '100%' }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <div>
            <h3 className="text-lg font-semibold">
              {step === 'upload' ? 'Import BOM / Drawing' : 'Review Import'}
            </h3>
            {step === 'preview' && preview && (
              <p className="text-sm text-slate-400">
                {preview.document_type === 'bom' ? 'Assembly BOM' : 'Single Part'}
                {preview.extraction_confidence && ` · Confidence: ${preview.extraction_confidence}`}
              </p>
            )}
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-200">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {step === 'upload' && (
            <form onSubmit={handlePreview} id="upload-form" className="space-y-4">
              <div>
                <label className="label">PDF, Word, or Excel Document</label>
                <input
                  type="file"
                  accept=".pdf,.doc,.docx,.xlsx,.xls"
                  onChange={e => setFile(e.target.files?.[0] || null)}
                  className="input"
                  required
                />
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={createMissingParts}
                  onChange={e => setCreateMissingParts(e.target.checked)}
                  className="rounded border-slate-600 text-werco-navy-400"
                />
                <span className="text-sm">Create missing parts automatically</span>
              </label>
            </form>
          )}

          {step === 'preview' && preview && (
            <div className="space-y-4">
              {warnings.length > 0 && (
                <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 text-sm text-amber-300">
                  {warnings.map((w, i) => <div key={i}>{w}</div>)}
                </div>
              )}

              {/* Assembly Info */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div>
                  <label className="label">Part Number</label>
                  <input className="input" value={preview.assembly.part_number || ''} onChange={e => updateAssembly('part_number', e.target.value)} />
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input className="input" value={preview.assembly.revision || ''} onChange={e => updateAssembly('revision', e.target.value)} />
                </div>
                <div>
                  <label className="label">Part Type</label>
                  <select className="input" value={preview.assembly.part_type || 'assembly'} onChange={e => updateAssembly('part_type', e.target.value)}>
                    <option value="manufactured">Manufactured</option>
                    <option value="assembly">Assembly</option>
                    <option value="purchased">Purchased</option>
                    <option value="raw_material">Raw Material</option>
                    <option value="hardware">Hardware</option>
                    <option value="consumable">Consumable</option>
                  </select>
                </div>
                <div className="md:col-span-2">
                  <label className="label">Name</label>
                  <input className="input" value={preview.assembly.name || ''} onChange={e => updateAssembly('name', e.target.value)} />
                </div>
                <div>
                  <label className="label">Drawing #</label>
                  <input className="input" value={preview.assembly.drawing_number || ''} onChange={e => updateAssembly('drawing_number', e.target.value)} />
                </div>
              </div>

              {/* Column Mapping */}
              {preview.raw_columns?.length ? (
                <div>
                  <p className="text-sm text-slate-300 mb-2 font-medium">Column Mapping</p>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {COLUMN_FIELDS.map(f => (
                      <div key={f.key}>
                        <label className="text-xs text-slate-400">{f.label}</label>
                        <select className="input text-sm py-1" value={columnMap[f.key] ?? ''} onChange={e => handleColumnMapChange(f.key, e.target.value)}>
                          <option value="">Not mapped</option>
                          {preview.raw_columns!.map((col, idx) => (
                            <option key={`${col}-${idx}`} value={idx}>{col || `Col ${idx + 1}`}</option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              {/* Items Table */}
              {preview.document_type === 'bom' && (
                <div className="space-y-2">
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <p className="text-sm text-slate-400">
                      {displayItems.length} line{displayItems.length !== 1 ? 's' : ''} ready
                      {removedItems.length > 0 && ` · ${removedItems.length} removed`}
                    </p>
                    {removedItems.length > 0 && (
                      <button
                        type="button"
                        onClick={restoreLastRemovedItem}
                        className="btn-secondary btn-sm inline-flex items-center gap-1 self-start sm:self-auto"
                      >
                        <ArrowUturnLeftIcon className="h-4 w-4" />
                        Undo Remove
                      </button>
                    )}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-slate-700 text-sm">
                      <thead className="bg-slate-800">
                        <tr>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Line</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Part #</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Description</th>
                          <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">UOM</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Line Type</th>
                          <th className="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Remove</th>
                        </tr>
                      </thead>
                      <tbody className="bg-fd-panel divide-y divide-slate-700">
                        {displayItems.map((item, idx) => {
                          const lineNumber = item.line_number || (idx + 1) * 10;
                          return (
                            <tr key={`${lineNumber}-${item.part_number || 'line'}-${idx}`}>
                              <td className="px-3 py-2">
                                <input
                                  className="input w-16 py-1 text-sm"
                                  type="number"
                                  value={lineNumber}
                                  onChange={e => updateDerivedItem(idx, { line_number: parseInt(e.target.value) })}
                                />
                              </td>
                              <td className="px-3 py-2">
                                <input
                                  className="input py-1 text-sm w-32"
                                  value={item.part_number || ''}
                                  onChange={e => updateDerivedItem(idx, { part_number: e.target.value })}
                                />
                              </td>
                              <td className="px-3 py-2">
                                <input
                                  className="input py-1 text-sm"
                                  value={item.description || ''}
                                  onChange={e => updateDerivedItem(idx, { description: e.target.value })}
                                />
                              </td>
                              <td className="px-3 py-2">
                                <input
                                  className="input w-20 py-1 text-sm text-right"
                                  type="number"
                                  step="1"
                                  value={item.quantity ?? 1}
                                  onChange={e => updateDerivedItem(idx, { quantity: parseFloat(e.target.value) })}
                                />
                              </td>
                              <td className="px-3 py-2">
                                <input
                                  className="input w-16 py-1 text-sm"
                                  value={item.unit_of_measure || ''}
                                  onChange={e => updateDerivedItem(idx, { unit_of_measure: e.target.value })}
                                />
                              </td>
                              <td className="px-3 py-2">
                                <select
                                  className="input py-1 text-sm"
                                  value={item.item_type || 'buy'}
                                  onChange={e => updateDerivedItem(idx, { item_type: e.target.value })}
                                >
                                  <option value="make">Make</option>
                                  <option value="buy">Buy</option>
                                  <option value="phantom">Phantom</option>
                                </select>
                              </td>
                              <td className="px-3 py-2">
                                <select
                                  className="input py-1 text-sm"
                                  value={item.line_type || 'component'}
                                  onChange={e => updateDerivedItem(idx, { line_type: e.target.value as any })}
                                >
                                  <option value="component">Component</option>
                                  <option value="hardware">Hardware</option>
                                  <option value="consumable">Consumable</option>
                                  <option value="reference">Reference</option>
                                </select>
                              </td>
                              <td className="px-3 py-2 text-center">
                                <button
                                  type="button"
                                  onClick={() => removeDerivedItem(idx)}
                                  className="rounded-lg p-2 text-slate-500 hover:bg-red-500/10 hover:text-red-300 focus:outline-none focus:ring-2 focus:ring-red-500/40"
                                  title={`Remove line ${lineNumber}`}
                                  aria-label={`Remove line ${lineNumber}`}
                                >
                                  <TrashIcon className="h-4 w-4" />
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    {displayItems.length === 0 && (
                      <p className="text-sm text-slate-400 py-4 text-center">No BOM items selected.</p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t bg-slate-800 rounded-b-xl">
          {step === 'upload' && (
            <>
              <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
              <button type="submit" form="upload-form" className="btn-primary" disabled={loading || !file}>
                {loading ? 'Analyzing...' : 'Preview'}
              </button>
            </>
          )}
          {step === 'preview' && (
            <>
              <button type="button" onClick={() => setStep('upload')} className="btn-secondary">Back</button>
              <button type="button" onClick={handleCommit} className="btn-primary" disabled={loading || (preview?.document_type === 'bom' && displayItems.length === 0)}>
                {loading ? 'Creating...' : 'Create'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );

  return typeof document !== 'undefined' && document.body ? createPortal(overlay, document.body) : overlay;
}
