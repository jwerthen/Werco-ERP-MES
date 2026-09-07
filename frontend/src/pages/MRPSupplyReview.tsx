import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { Modal } from '../components/ui';
import { formatCentralDate } from '../utils/centralTime';

export interface SupplyDraft {
  action_id: number;
  mrp_run_id: number;
  kind: 'purchase_order' | 'work_order';
  id: number;
  number: string;
  url: string;
  status: string;
  quantity?: number;
  replayed?: boolean;
}
interface SupplyReview {
  action_id: number;
  mrp_run_id: number;
  mrp_run_number: string;
  part_id: number;
  part_number: string;
  part_name: string;
  kind: SupplyDraft['kind'];
  quantity: number;
  source_quantity: number;
  required_date: string;
  due_date: string;
  review_token: string;
  blocked_reason: string | null;
  vendor_id: number | null;
  unit_price: number;
  vendors: Array<{ id: number; code: string; name: string }>;
  work_center_id: number | null;
  work_centers: Array<{ id: number; code: string; name: string }>;
  routing: Array<{ id: number; sequence: number; name: string; work_center_name: string }>;
  existing_draft: SupplyDraft | null;
}
interface DraftPayload {
  request_key: string;
  review_token: string;
  quantity: number;
  due_date: string;
  vendor_id: number | null;
  unit_price: number;
  work_center_id: number | null;
  notes: string;
}
const retryKey = () => globalThis.crypto?.randomUUID?.() ?? `mrp-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export default function MRPSupplyReview({
  actionId,
  onClose,
  onCreated,
}: {
  actionId: number;
  onClose: () => void;
  onCreated: (draft: SupplyDraft) => void;
}) {
  const [review, setReview] = useState<SupplyReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [result, setResult] = useState<SupplyDraft | null>(null);
  const [quantity, setQuantity] = useState('');
  const [dueDate, setDueDate] = useState('');
  const [vendor, setVendor] = useState('');
  const [cost, setCost] = useState('0');
  const [center, setCenter] = useState('');
  const [notes, setNotes] = useState('');
  const requestRef = useRef(0);
  const pendingRef = useRef(false);
  const attemptRef = useRef<DraftPayload | null>(null);
  const load = async () => {
    const request = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const data: SupplyReview = await api.getMRPSupplyReview(actionId);
      if (request !== requestRef.current) return;
      setReview(data);
      setResult(data.existing_draft);
      setQuantity(String(data.quantity));
      setDueDate(data.due_date);
      setVendor(data.vendor_id ? String(data.vendor_id) : '');
      setCost(String(data.unit_price));
      setCenter(data.work_center_id ? String(data.work_center_id) : '');
      setUncertain(false);
      attemptRef.current = null;
    } catch {
      if (request === requestRef.current)
        setError('Could not load the current shortage. Retry to review its supply details.');
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  };
  useEffect(() => {
    void load();
    return () => {
      requestRef.current += 1;
    };
    // Each mounted dialog owns one recommendation; parent keys it by actionId.
  }, [actionId]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!review || pendingRef.current || result || review.blocked_reason) return;
    pendingRef.current = true;
    setSaving(true);
    setError('');
    const payload = attemptRef.current ?? {
      request_key: retryKey(),
      review_token: review.review_token,
      quantity: Number(quantity),
      due_date: dueDate,
      vendor_id: vendor ? Number(vendor) : null,
      unit_price: Number(cost),
      work_center_id: center ? Number(center) : null,
      notes,
    };
    attemptRef.current = payload;
    try {
      const draft: SupplyDraft = await api.createMRPSupplyDraft(actionId, payload);
      setResult(draft);
      setUncertain(false);
      onCreated(draft);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      if (detail?.existing_draft) {
        setResult(detail.existing_draft);
        onCreated(detail.existing_draft);
        setUncertain(false);
      } else {
        const unknown = !err.response || err.response.status >= 500;
        setUncertain(unknown);
        if (!unknown) attemptRef.current = null;
        setError(
          unknown
            ? 'The result could not be confirmed. Retry this same request to recover the draft safely; its reviewed fields are retained.'
            : typeof detail === 'string'
              ? detail
              : detail?.message || 'The draft could not be created. Check the fields or reload the review.'
        );
      }
    } finally {
      pendingRef.current = false;
      setSaving(false);
    }
  };
  const locked = saving || uncertain;
  const purchase = review?.kind === 'purchase_order';
  return (
    <Modal
      open
      onClose={() => {
        if (!saving) onClose();
      }}
      closeOnBackdrop={false}
      closeOnEscape={false}
      size="2xl"
      ariaLabel="Review MRP supply draft"
    >
      <div className="space-y-4">
        <h2 className="text-xl font-semibold">Review supply draft</h2>
        {loading ? (
          <p role="status">Checking current shortage and planned supply…</p>
        ) : result ? (
          <div className="space-y-3">
            <p role="status">
              {result.replayed ? 'Recovered existing' : 'Supply document'} {result.number} · {result.status}
            </p>
            <p className="text-sm text-slate-300">
              This document remains linked to the MRP recommendation. Mark reviewed is a separate action.
            </p>
            <Link className="btn-primary inline-flex" to={result.url}>
              Open {result.number}
            </Link>
          </div>
        ) : (
          review && (
            <form onSubmit={submit} className="space-y-4">
              <div className="rounded border border-slate-700 p-3">
                <p className="font-semibold">
                  {review.part_number} · {review.part_name}
                </p>
                <p className="text-sm text-slate-300">
                  {review.mrp_run_number}, recommendation #{actionId} · Needed {formatCentralDate(review.required_date)}
                </p>
                <p className="text-sm">
                  Current shortage: {review.quantity} · Run snapshot: {review.source_quantity}
                </p>
              </div>
              <p className="text-sm text-slate-300">
                Create a {purchase ? 'purchase order' : 'work order'} in Draft status.{' '}
                {purchase ? 'Review and send it from Purchasing.' : 'Review and release it from Work Orders.'} Creating
                this draft reserves its quantity in future MRP calculations. If you create less than the shortage, rerun
                MRP for the remainder.
              </p>
              {review.blocked_reason && (
                <p role="alert" className="text-amber-300">
                  {review.blocked_reason}
                </p>
              )}
              <fieldset
                disabled={locked || Boolean(review.blocked_reason)}
                className="grid grid-cols-1 sm:grid-cols-2 gap-3"
              >
                <label className="text-sm">
                  Quantity
                  <input
                    className="input w-full mt-1"
                    type="number"
                    required
                    min="0.0001"
                    max={review.quantity}
                    step="0.0001"
                    value={quantity}
                    onChange={e => setQuantity(e.target.value)}
                  />
                </label>
                <label className="text-sm">
                  {purchase ? 'Required date' : 'Due date'}
                  <input
                    className="input w-full mt-1"
                    type="date"
                    required
                    value={dueDate}
                    onChange={e => setDueDate(e.target.value)}
                  />
                </label>
                {purchase ? (
                  <>
                    <label className="text-sm">
                      Supplier
                      <select
                        className="input w-full mt-1"
                        required
                        value={vendor}
                        onChange={e => setVendor(e.target.value)}
                      >
                        <option value="">Choose a supplier</option>
                        {review.vendors.map(v => (
                          <option value={v.id} key={v.id}>
                            {v.code} · {v.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="text-sm">
                      Unit price
                      <input
                        className="input w-full mt-1"
                        type="number"
                        required
                        min="0"
                        step="0.01"
                        value={cost}
                        onChange={e => setCost(e.target.value)}
                      />
                    </label>
                    <p className="sm:col-span-2 text-sm text-slate-400">
                      Estimated line total: ${(Number(quantity) * Number(cost)).toFixed(2)}. Suggested cost is the part
                      standard cost; confirm supplier pricing.
                    </p>
                  </>
                ) : review.routing.length > 0 ? (
                  <div className="sm:col-span-2 text-sm">
                    <p className="font-medium">Released routing to copy</p>
                    <ul className="mt-1 space-y-1">
                      {review.routing.map(op => (
                        <li key={op.id}>
                          {op.name} · {op.work_center_name}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <>
                    <label className="text-sm sm:col-span-2">
                      Work center
                      <select
                        className="input w-full mt-1"
                        required
                        value={center}
                        onChange={e => setCenter(e.target.value)}
                      >
                        <option value="">Choose a work center</option>
                        {review.work_centers.map(c => (
                          <option value={c.id} key={c.id}>
                            {c.code} · {c.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="text-sm text-amber-300 sm:col-span-2">
                      No released routing is available. This draft starts with one Supply production operation; review
                      its instructions and timing before release.
                    </p>
                  </>
                )}
                <label className="text-sm sm:col-span-2">
                  Planner notes
                  <textarea
                    className="input w-full mt-1"
                    rows={3}
                    maxLength={1500}
                    value={notes}
                    onChange={e => setNotes(e.target.value)}
                  />
                </label>
              </fieldset>
              <button type="submit" className="btn-primary" disabled={saving || Boolean(review.blocked_reason)}>
                {saving
                  ? 'Creating draft…'
                  : uncertain
                    ? 'Retry same draft request'
                    : `Create ${purchase ? 'PO' : 'WO'} draft`}
              </button>
            </form>
          )
        )}
        {error && (
          <p role="alert" className="text-red-300">
            {error}
          </p>
        )}
        <div className="flex gap-3 justify-end">
          {!loading && !result && !uncertain && (
            <button type="button" className="btn-secondary" disabled={saving} onClick={load}>
              Reload review
            </button>
          )}
          <button type="button" className="btn-secondary" disabled={saving} onClick={onClose}>
            {result ? 'Done' : 'Cancel'}
          </button>
        </div>
      </div>
    </Modal>
  );
}
