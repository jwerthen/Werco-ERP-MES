import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { Modal } from '../components/ui';
import { MRPPurchaseBatchPayload, MRPPurchaseBatchResult, MRPPurchaseReviewLine } from '../types/mrpBatch';

interface EditedLine {
  quantity: string;
  due_date: string;
  vendor_id: string;
  unit_price: string;
  notes: string;
}
const retryKey = () =>
  globalThis.crypto?.randomUUID?.() ?? `mrp-batch-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export default function MRPPurchaseBatchReview({
  actionIds,
  onClose,
  onCreated,
}: {
  actionIds: number[];
  onClose: () => void;
  onCreated: (result: MRPPurchaseBatchResult) => void;
}) {
  const [reviews, setReviews] = useState<MRPPurchaseReviewLine[]>([]);
  const [edits, setEdits] = useState<Record<number, EditedLine>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<MRPPurchaseBatchResult | null>(null);
  const sequence = useRef(0);
  const pending = useRef(false);
  const attempt = useRef<MRPPurchaseBatchPayload | null>(null);
  const load = async () => {
    const seq = ++sequence.current;
    setLoading(true);
    setError('');
    setReviewed(false);
    try {
      const response = await api.reviewMRPPurchaseBatch(actionIds);
      if (seq !== sequence.current) return;
      setReviews(response.lines);
      setEdits(
        Object.fromEntries(
          response.lines.map(line => [
            line.action_id,
            {
              quantity: String(line.quantity),
              due_date: line.due_date,
              vendor_id: line.vendor_id ? String(line.vendor_id) : '',
              unit_price: String(line.unit_price),
              notes: '',
            },
          ])
        )
      );
      attempt.current = null;
    } catch (reason: any) {
      if (seq === sequence.current)
        setError(
          typeof reason.response?.data?.detail === 'string'
            ? reason.response.data.detail
            : 'Could not check the selected shortages. Reload this review before creating drafts.'
        );
    } finally {
      if (seq === sequence.current) setLoading(false);
    }
  };
  const selection = [...actionIds].sort((a, b) => a - b).join(',');
  useEffect(() => {
    void load();
    return () => {
      sequence.current += 1;
    };
  }, [selection]);
  const update = (id: number, field: keyof EditedLine, value: string) => {
    setEdits(current => ({ ...current, [id]: { ...current[id], [field]: value } }));
    setReviewed(false);
  };
  const groups = new Map<string, { name: string; count: number; total: number }>();
  for (const line of reviews) {
    const edit = edits[line.action_id];
    if (!edit) continue;
    const key = edit.vendor_id;
    const current = groups.get(key) || {
      name: line.vendors.find(v => String(v.id) === key)?.name || 'Choose supplier',
      count: 0,
      total: 0,
    };
    current.count += 1;
    current.total += Number(edit.quantity) * Number(edit.unit_price);
    groups.set(key, current);
  }
  const blocked = reviews.some(line => !!line.blocked_reason || !!line.existing_draft);
  const locked = saving || uncertain;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending.current || !reviewed || blocked || result || !reviews.length) return;
    pending.current = true;
    setSaving(true);
    setError('');
    const payload = attempt.current || {
      request_key: retryKey(),
      lines: reviews.map(line => ({
        action_id: line.action_id,
        review_token: line.review_token,
        quantity: Number(edits[line.action_id].quantity),
        due_date: edits[line.action_id].due_date,
        vendor_id: Number(edits[line.action_id].vendor_id),
        unit_price: Number(edits[line.action_id].unit_price),
        notes: edits[line.action_id].notes,
      })),
    };
    attempt.current = payload;
    try {
      const created = await api.createMRPPurchaseBatch(payload);
      setResult(created);
      setUncertain(false);
      onCreated(created);
    } catch (reason: any) {
      const detail = reason.response?.data?.detail;
      const unknown = !reason.response || reason.response.status >= 500;
      setUncertain(unknown);
      if (!unknown) {
        attempt.current = null;
        setReviewed(false);
      }
      setError(
        unknown
          ? 'The result could not be confirmed. Retry this same batch to recover its drafts safely. The reviewed selection and fields are retained.'
          : typeof detail === 'string'
            ? detail
            : detail?.message || 'The batch was not created. Check the fields or reload the review.'
      );
    } finally {
      pending.current = false;
      setSaving(false);
    }
  };
  return (
    <Modal
      open
      onClose={() => {
        if (!pending.current) onClose();
      }}
      closeOnBackdrop={false}
      closeOnEscape={false}
      size="5xl"
      ariaLabel="Review MRP purchase batch"
    >
      <div className="space-y-4">
        <h2 className="text-xl font-semibold">Review purchases by supplier</h2>
        <p className="text-sm text-slate-300">
          Create one draft PO per supplier. Each line keeps its required date and MRP source. Review and send the drafts
          from Purchasing. Mark reviewed is a separate action.
        </p>
        {loading ? (
          <p role="status">Checking selected shortages and planned supply…</p>
        ) : result ? (
          <div className="space-y-3">
            <p role="status">
              {result.replayed ? 'Recovered existing' : 'Created'} {result.purchase_orders.length} draft purchase
              {result.purchase_orders.length === 1 ? 'order' : 'orders'}.
            </p>
            {result.purchase_orders.map(po => (
              <div key={po.id} className="rounded border border-slate-700 p-3">
                <Link to={po.url} className="text-fd-link underline">
                  Open {po.number}
                </Link>
                <span className="ml-3 text-sm">
                  {po.action_ids.length} recommendations · ${po.total.toFixed(2)} · {po.status}
                </span>
              </div>
            ))}
          </div>
        ) : (
          !!reviews.length && (
            <form onSubmit={submit} className="space-y-4">
              <div className="rounded border border-slate-700 p-3 text-sm" aria-label="Supplier purchase order summary">
                <p className="font-medium">
                  {groups.size} supplier {groups.size === 1 ? 'group' : 'groups'}
                </p>
                {Array.from(groups).map(([id, group]) => (
                  <p key={id}>
                    {group.name}: {group.count} lines · ${group.total.toFixed(2)}
                  </p>
                ))}
              </div>
              {reviews.map(line => (
                <fieldset
                  key={line.action_id}
                  disabled={locked || !!line.blocked_reason || !!line.existing_draft}
                  className="rounded border border-slate-700 p-3"
                >
                  <legend className="px-1 font-semibold">
                    {line.part_number} · {line.part_name}
                  </legend>
                  <p className="mb-3 text-sm text-slate-400">
                    {line.mrp_run_number}, recommendation #{line.action_id} · current shortage {line.quantity}
                  </p>
                  {line.blocked_reason && (
                    <p role="alert" className="mb-3 text-amber-300">
                      {line.blocked_reason}
                    </p>
                  )}
                  {line.existing_draft && (
                    <p className="mb-3">
                      Supply already exists:{' '}
                      <Link to={line.existing_draft.url} className="text-fd-link underline">
                        {line.existing_draft.number}
                      </Link>
                      . Close and remove this recommendation from the selection.
                    </p>
                  )}
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <label className="text-sm">
                      Supplier
                      <select
                        aria-label={`Supplier for ${line.part_number}`}
                        required
                        className="input mt-1 w-full"
                        value={edits[line.action_id]?.vendor_id || ''}
                        onChange={e => update(line.action_id, 'vendor_id', e.target.value)}
                      >
                        <option value="">Choose supplier</option>
                        {line.vendors.map(v => (
                          <option key={v.id} value={v.id}>
                            {v.code} · {v.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="text-sm">
                      Quantity
                      <input
                        aria-label={`Quantity for ${line.part_number}`}
                        className="input mt-1 w-full"
                        required
                        type="number"
                        min="0.0001"
                        max={line.quantity}
                        step="0.0001"
                        value={edits[line.action_id]?.quantity || ''}
                        onChange={e => update(line.action_id, 'quantity', e.target.value)}
                      />
                    </label>
                    <label className="text-sm">
                      Required date
                      <input
                        aria-label={`Required date for ${line.part_number}`}
                        className="input mt-1 w-full"
                        required
                        type="date"
                        value={edits[line.action_id]?.due_date || ''}
                        onChange={e => update(line.action_id, 'due_date', e.target.value)}
                      />
                    </label>
                    <label className="text-sm">
                      Unit price
                      <input
                        aria-label={`Unit price for ${line.part_number}`}
                        className="input mt-1 w-full"
                        required
                        type="number"
                        min="0"
                        step="0.01"
                        value={edits[line.action_id]?.unit_price || ''}
                        onChange={e => update(line.action_id, 'unit_price', e.target.value)}
                      />
                    </label>
                    <label className="text-sm sm:col-span-2 lg:col-span-4">
                      Notes
                      <input
                        aria-label={`Notes for ${line.part_number}`}
                        className="input mt-1 w-full"
                        maxLength={350}
                        value={edits[line.action_id]?.notes || ''}
                        onChange={e => update(line.action_id, 'notes', e.target.value)}
                      />
                    </label>
                  </div>
                </fieldset>
              ))}
              <p className="text-sm text-slate-400">
                Suggested prices use part standard costs. Confirm supplier pricing. Partial quantities leave a remainder
                to plan on the next MRP run.
              </p>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={reviewed}
                  disabled={locked || blocked}
                  onChange={e => setReviewed(e.target.checked)}
                />
                I reviewed the suppliers, quantities, dates and prices for this batch.
              </label>
              <button type="submit" className="btn-primary" disabled={saving || !reviewed || blocked}>
                {saving
                  ? 'Creating purchase drafts…'
                  : uncertain
                    ? 'Retry same purchase batch'
                    : 'Create purchase drafts'}
              </button>
            </form>
          )
        )}
        {error && (
          <p role="alert" className="text-red-300">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-3">
          {!result && !uncertain && (
            <button className="btn-secondary" disabled={loading || saving} onClick={load}>
              Reload review
            </button>
          )}
          <button className="btn-secondary" disabled={saving} onClick={onClose}>
            {result ? 'Done' : 'Cancel'}
          </button>
        </div>
      </div>
    </Modal>
  );
}
