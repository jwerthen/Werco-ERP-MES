import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AdjustmentsHorizontalIcon,
  PlusIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { Button, ErrorState, FormField, LoadingButton, useToast } from '../components/ui';

type CutBendRow = {
  id: number;
  table_id: number;
  sort_order: number;
  thickness_in?: number | null;
  gauge?: number | null;
  mild_steel?: number | null;
  stainless?: number | null;
  aluminum?: number | null;
  value?: number | null;
  fillet_leg_in?: number | null;
  arc_in_per_min?: number | null;
  min_per_in?: number | null;
  notes?: string | null;
};

type CutBendTable = {
  id: number;
  kind: string;
  name: string;
  description?: string | null;
  columns: string[];
  rows: CutBendRow[];
  updated_at?: string | null;
};

type HistoryItem = {
  id: number;
  entity_name?: string | null;
  action: string;
  field_changed?: string | null;
  note?: string | null;
  old_value?: { value?: unknown; note?: string } | null;
  new_value?: { value?: unknown; note?: string } | null;
  changed_at?: string | null;
};

type JobActual = {
  id: number;
  quote_estimate_id?: number | null;
  job_label?: string | null;
  quoted_laser_hours: number;
  quoted_brake_hours: number;
  quoted_weld_hours: number;
  actual_laser_hours?: number | null;
  actual_brake_hours?: number | null;
  actual_weld_hours?: number | null;
  delta_laser_pct?: number | null;
  delta_brake_pct?: number | null;
  delta_weld_pct?: number | null;
  notes?: string | null;
  propose_tune: Array<{ bucket: string; kind: string; delta_pct: number; message: string }>;
};

const COL_LABELS: Record<string, string> = {
  thickness_in: 'Thk (in)',
  gauge: 'Gauge',
  mild_steel: 'Mild',
  stainless: 'SS',
  aluminum: 'Al',
  value: 'Value',
  fillet_leg_in: 'Fillet (in)',
  arc_in_per_min: 'Arc in/min',
  min_per_in: 'Min/in',
  notes: 'Notes',
};

const inputCls =
  'w-full min-w-0 bg-fd-sunken border border-fd-line rounded-sm px-1.5 py-1 text-sm text-white tabular-nums focus:outline-none focus:border-werco-navy-500';

function fmtPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—';
  return `${(n * 100).toFixed(0)}%`;
}

function cellDisplay(row: CutBendRow, col: string): string {
  const v = (row as Record<string, unknown>)[col];
  if (v == null || v === '') return '—';
  if (typeof v === 'number') return String(v);
  return String(v);
}

export default function ShopData() {
  const { showToast } = useToast();
  const [tables, setTables] = useState<CutBendTable[]>([]);
  const [activeKind, setActiveKind] = useState<string>('');
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [actuals, setActuals] = useState<JobActual[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editRowId, setEditRowId] = useState<number | null>(null);
  const [editCol, setEditCol] = useState<string>('');
  const [editValue, setEditValue] = useState('');
  const [editNote, setEditNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newRow, setNewRow] = useState<Record<string, string>>({});
  const [newNote, setNewNote] = useState('');
  const [actualForm, setActualForm] = useState({
    quote_estimate_id: '',
    job_label: '',
    actual_laser_hours: '',
    actual_brake_hours: '',
    actual_weld_hours: '',
    notes: '',
  });
  const [savingActual, setSavingActual] = useState(false);

  const active = useMemo(
    () => tables.find((t) => t.kind === activeKind) || tables[0],
    [tables, activeKind]
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [shop, hist, acts] = await Promise.all([
        api.getEstimateShopData(),
        api.getEstimateShopDataHistory({ limit: 40 }),
        api.getEstimateJobActuals({ limit: 40 }),
      ]);
      setTables(shop.tables || []);
      if (!activeKind && shop.tables?.length) {
        setActiveKind(shop.tables[0].kind);
      }
      setHistory(hist || []);
      setActuals(acts || []);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to load shop data');
    } finally {
      setLoading(false);
    }
  }, [activeKind]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startEdit = (row: CutBendRow, col: string) => {
    if (col === 'notes') {
      setEditRowId(row.id);
      setEditCol(col);
      setEditValue(String(row.notes || ''));
      setEditNote('');
      return;
    }
    const raw = (row as Record<string, unknown>)[col];
    setEditRowId(row.id);
    setEditCol(col);
    setEditValue(raw == null ? '' : String(raw));
    setEditNote('');
  };

  const saveEdit = async () => {
    if (!active || editRowId == null || !editCol) return;
    if (!editNote.trim()) {
      showToast('error', 'Change note is required');
      return;
    }
    setSaving(true);
    try {
      let parsed: number | string | null = editValue.trim() === '' ? null : editValue;
      if (editCol !== 'notes' && parsed != null) {
        const n = Number(parsed);
        if (!Number.isFinite(n)) {
          showToast('error', 'Enter a number (or blank for past capacity)');
          setSaving(false);
          return;
        }
        parsed = editCol === 'gauge' ? Math.round(n) : n;
      }
      await api.patchEstimateShopDataRow(active.kind, editRowId, {
        note: editNote.trim(),
        [editCol]: parsed,
      });
      showToast('success', 'Shop data updated');
      setEditRowId(null);
      setEditCol('');
      setEditNote('');
      await load();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const saveNewRow = async () => {
    if (!active) return;
    if (!newNote.trim()) {
      showToast('error', 'Change note is required');
      return;
    }
    setSaving(true);
    try {
      const payload: Record<string, unknown> & { note: string } = { note: newNote.trim() };
      for (const col of active.columns) {
        const raw = newRow[col];
        if (raw == null || raw.trim() === '') continue;
        if (col === 'notes') {
          payload[col] = raw;
        } else if (col === 'gauge') {
          payload[col] = Math.round(Number(raw));
        } else {
          payload[col] = Number(raw);
        }
      }
      await api.createEstimateShopDataRow(active.kind, payload);
      showToast('success', 'Row added (sorted by thickness)');
      setAdding(false);
      setNewRow({});
      setNewNote('');
      await load();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Add failed');
    } finally {
      setSaving(false);
    }
  };

  const saveActual = async () => {
    setSavingActual(true);
    try {
      const estId = actualForm.quote_estimate_id.trim()
        ? Number(actualForm.quote_estimate_id)
        : undefined;
      await api.upsertEstimateJobActual({
        quote_estimate_id: estId,
        job_label: actualForm.job_label || undefined,
        actual_laser_hours: actualForm.actual_laser_hours
          ? Number(actualForm.actual_laser_hours)
          : null,
        actual_brake_hours: actualForm.actual_brake_hours
          ? Number(actualForm.actual_brake_hours)
          : null,
        actual_weld_hours: actualForm.actual_weld_hours
          ? Number(actualForm.actual_weld_hours)
          : null,
        notes: actualForm.notes || undefined,
      });
      showToast('success', 'Actuals saved');
      setActualForm({
        quote_estimate_id: '',
        job_label: '',
        actual_laser_hours: '',
        actual_brake_hours: '',
        actual_weld_hours: '',
        notes: '',
      });
      await load();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to save actuals');
    } finally {
      setSavingActual(false);
    }
  };

  if (loading && !tables.length) {
    return (
      <div className="space-y-4">
        <p className="text-fd-muted">Loading shop data…</p>
      </div>
    );
  }

  if (error && !tables.length) {
    return (
      <ErrorState title="Shop Data" message={error} onRetry={() => void load()} />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <AdjustmentsHorizontalIcon className="h-7 w-7 text-werco-navy-400" />
            Shop Data
          </h1>
          <p className="text-sm text-fd-muted mt-1">
            Thickness-banded laser / pierce / brake / gauge / weld tables. Edits require a note and
            feed the Estimate Workbench immediately.
          </p>
        </div>
        <Link to="/estimate-workbench" className="btn-secondary inline-flex items-center px-3 text-sm">
          Estimate Workbench
        </Link>
      </div>

      {/* Table tabs */}
      <div className="flex flex-wrap gap-1 border-b border-fd-line pb-2">
        {tables.map((t) => (
          <button
            key={t.kind}
            type="button"
            onClick={() => {
              setActiveKind(t.kind);
              setEditRowId(null);
              setAdding(false);
            }}
            className={`px-3 py-1.5 text-sm rounded-sm ${
              active?.kind === t.kind
                ? 'bg-werco-navy-700 text-white'
                : 'text-fd-muted hover:text-white hover:bg-fd-sunken'
            }`}
          >
            {t.name}
          </button>
        ))}
      </div>

      {active && (
        <div className="bg-fd-panel border border-fd-line rounded-sm p-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-white">{active.name}</h2>
              {active.description && (
                <p className="text-xs text-fd-muted mt-0.5">{active.description}</p>
              )}
            </div>
            <Button variant="secondary" size="sm" onClick={() => setAdding((v) => !v)}>
              <PlusIcon className="h-4 w-4 mr-1" />
              Add row
            </Button>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left">
              <thead className="text-fd-muted border-b border-fd-line">
                <tr>
                  {active.columns.map((c) => (
                    <th key={c} className="py-1.5 pr-2 font-medium">
                      {COL_LABELS[c] || c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {active.rows.map((row) => (
                  <tr key={row.id} className="border-b border-fd-line/40 hover:bg-fd-sunken/40">
                    {active.columns.map((col) => (
                      <td key={col} className="py-1 pr-2">
                        {editRowId === row.id && editCol === col ? (
                          <input
                            className={inputCls}
                            value={editValue}
                            onChange={(e) => setEditValue(e.target.value)}
                            autoFocus
                          />
                        ) : (
                          <button
                            type="button"
                            className="text-left w-full tabular-nums text-white/90 hover:text-werco-navy-300"
                            onClick={() => startEdit(row, col)}
                            title="Click to edit"
                          >
                            {cellDisplay(row, col)}
                          </button>
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {editRowId != null && (
            <div className="border border-amber-700/40 bg-amber-950/20 rounded-sm p-3 space-y-2">
              <p className="text-xs text-amber-200">
                Editing row #{editRowId} · {COL_LABELS[editCol] || editCol}. Blank numeric = past
                capacity (—).
              </p>
              <FormField label="Change note (required)">
                {(field) => (
                  <input
                    {...field}
                    className={inputCls}
                    value={editNote}
                    onChange={(e) => setEditNote(e.target.value)}
                    placeholder="e.g. measured on Ermaksan 3/8 A36 job — actual 80 in/min vs 95"
                  />
                )}
              </FormField>
              <div className="flex gap-2">
                <LoadingButton loading={saving} onClick={saveEdit} variant="primary" size="sm">
                  Save change
                </LoadingButton>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setEditRowId(null);
                    setEditCol('');
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {adding && (
            <div className="border border-fd-line rounded-sm p-3 space-y-2">
              <p className="text-xs text-fd-muted">New band — will auto-sort by thickness / gauge.</p>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                {active.columns.map((col) => (
                  <FormField key={col} label={COL_LABELS[col] || col}>
                    {(field) => (
                      <input
                        {...field}
                        className={inputCls}
                        value={newRow[col] || ''}
                        onChange={(e) => setNewRow((p) => ({ ...p, [col]: e.target.value }))}
                      />
                    )}
                  </FormField>
                ))}
              </div>
              <FormField label="Change note (required)">
                {(field) => (
                  <input
                    {...field}
                    className={inputCls}
                    value={newNote}
                    onChange={(e) => setNewNote(e.target.value)}
                    placeholder="Why this band was added"
                  />
                )}
              </FormField>
              <div className="flex gap-2">
                <LoadingButton loading={saving} onClick={saveNewRow} variant="primary" size="sm">
                  Add row
                </LoadingButton>
                <Button variant="secondary" size="sm" onClick={() => setAdding(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Quoted vs actual */}
      <div className="bg-fd-panel border border-fd-line rounded-sm p-4 space-y-3">
        <h2 className="text-sm font-semibold text-white">Quoted vs actual</h2>
        <p className="text-xs text-fd-muted">
          Enter floor hours after a job. ≥15% variance suggests which Cut/Bend table to tune.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          <FormField label="Estimate ID">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.quote_estimate_id}
                onChange={(e) =>
                  setActualForm((p) => ({ ...p, quote_estimate_id: e.target.value }))
                }
                placeholder="e.g. 12"
              />
            )}
          </FormField>
          <FormField label="Job label (if no estimate)">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.job_label}
                onChange={(e) => setActualForm((p) => ({ ...p, job_label: e.target.value }))}
              />
            )}
          </FormField>
          <FormField label="Actual laser hrs">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.actual_laser_hours}
                onChange={(e) =>
                  setActualForm((p) => ({ ...p, actual_laser_hours: e.target.value }))
                }
              />
            )}
          </FormField>
          <FormField label="Actual brake hrs">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.actual_brake_hours}
                onChange={(e) =>
                  setActualForm((p) => ({ ...p, actual_brake_hours: e.target.value }))
                }
              />
            )}
          </FormField>
          <FormField label="Actual weld hrs">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.actual_weld_hours}
                onChange={(e) =>
                  setActualForm((p) => ({ ...p, actual_weld_hours: e.target.value }))
                }
              />
            )}
          </FormField>
          <FormField label="Notes">
            {(field) => (
              <input
                {...field}
                className={inputCls}
                value={actualForm.notes}
                onChange={(e) => setActualForm((p) => ({ ...p, notes: e.target.value }))}
              />
            )}
          </FormField>
        </div>
        <LoadingButton loading={savingActual} onClick={saveActual} variant="primary" size="sm">
          Save actuals
        </LoadingButton>

        {actuals.length > 0 && (
          <div className="overflow-x-auto mt-2">
            <table className="w-full text-xs text-left">
              <thead className="text-fd-muted border-b border-fd-line">
                <tr>
                  <th className="py-1 pr-2">Job</th>
                  <th className="py-1 pr-2">Laser Δ</th>
                  <th className="py-1 pr-2">Brake Δ</th>
                  <th className="py-1 pr-2">Weld Δ</th>
                  <th className="py-1">Tune hint</th>
                </tr>
              </thead>
              <tbody>
                {actuals.map((a) => (
                  <tr key={a.id} className="border-b border-fd-line/40">
                    <td className="py-1 pr-2">
                      {a.job_label || (a.quote_estimate_id ? `EW-${a.quote_estimate_id}` : `#${a.id}`)}
                    </td>
                    <td className="py-1 pr-2 tabular-nums">{fmtPct(a.delta_laser_pct)}</td>
                    <td className="py-1 pr-2 tabular-nums">{fmtPct(a.delta_brake_pct)}</td>
                    <td className="py-1 pr-2 tabular-nums">{fmtPct(a.delta_weld_pct)}</td>
                    <td className="py-1 text-amber-200/90">
                      {a.propose_tune?.[0] ? (
                        <button
                          type="button"
                          className="underline hover:text-white"
                          onClick={() => setActiveKind(a.propose_tune[0].kind)}
                        >
                          {a.propose_tune[0].message}
                        </button>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* History */}
      <div className="bg-fd-panel border border-fd-line rounded-sm p-4 space-y-2">
        <h2 className="text-sm font-semibold text-white flex items-center gap-2">
          <ClockIcon className="h-4 w-4" />
          Change history
        </h2>
        {history.length === 0 ? (
          <p className="text-xs text-fd-muted">No Cut/Bend edits yet.</p>
        ) : (
          <ul className="text-xs space-y-1.5 max-h-56 overflow-y-auto">
            {history.map((h) => (
              <li key={h.id} className="border-b border-fd-line/30 pb-1 text-fd-muted">
                <span className="text-white/80">{h.entity_name}</span>
                {' · '}
                {h.action} {h.field_changed}
                {h.old_value && typeof h.old_value === 'object' && 'value' in h.old_value
                  ? ` ${String(h.old_value.value)} → `
                  : ' '}
                {h.new_value && typeof h.new_value === 'object' && 'value' in h.new_value
                  ? String(h.new_value.value)
                  : ''}
                {h.note ? ` — ${h.note}` : ''}
                {h.changed_at ? (
                  <span className="text-fd-muted/70"> · {h.changed_at.slice(0, 19)}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
