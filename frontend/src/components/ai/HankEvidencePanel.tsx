import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import type { HankEvidence, HankEvidenceKind } from '../../types/hankWork';
import { formatCentralDateTime } from '../../utils/centralTime';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankJobScan } from './HankJobScan';
import { useHankSessionGuard } from './useHankSessionGuard';

const TITLES: Record<HankEvidenceKind, string> = {
  readiness: 'Job readiness',
  knowledge: 'Job knowledge',
  purchasing: 'Purchasing exceptions',
  shipping: 'Shipping packet',
  trace: 'Trace a lot or serial',
};
export function HankEvidencePanel({
  kind,
  workOrderId,
  purchaseOrderId,
  onNavigate,
  onBusyChange,
}: {
  kind: HankEvidenceKind;
  workOrderId?: number;
  purchaseOrderId?: number;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [target, setTarget] = useState(String((kind === 'purchasing' ? purchaseOrderId : workOrderId) || ''));
  const [traceKind, setTraceKind] = useState<'lot' | 'serial'>('lot');
  const [traceValue, setTraceValue] = useState('');
  const [result, setResult] = useState<HankEvidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const { current, controller, release, changed } = useHankSessionGuard();
  const inFlight = useRef(false);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  useEffect(() => () => busyCallback.current?.(false), []);
  const load = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!current() || inFlight.current || (kind === 'trace' ? !traceValue.trim() : !/^[1-9]\d*$/.test(target))) return;
    const request = controller();
    inFlight.current = true;
    setBusy(true);
    busyCallback.current?.(true);
    setError('');
    setResult(null);
    setCopied(false);
    try {
      const response =
        kind === 'trace'
          ? await api.getHankTrace(traceKind, traceValue.trim(), request.signal)
          : kind === 'purchasing'
            ? await api.getHankPurchasingImpact(Number(target), request.signal)
            : kind === 'knowledge'
              ? await api.getHankKnowledge(Number(target), request.signal)
              : kind === 'shipping'
                ? await api.getHankShippingPacket(Number(target), request.signal)
                : await api.getHankReadiness(Number(target), request.signal);
      if (current() && !request.signal.aborted) setResult(response);
    } catch (cause) {
      if (current() && !request.signal.aborted) {
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : null;
        setError(typeof detail === 'string' ? detail : 'The evidence could not be loaded. Try again.');
      }
    } finally {
      release(request);
      inFlight.current = false;
      if (current()) {
        setBusy(false);
        busyCallback.current?.(false);
      }
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to review evidence.
      </p>
    );
  return (
    <section aria-label={TITLES[kind]} className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink">{TITLES[kind]}</h3>
      <form onSubmit={load} className="space-y-3">
        {kind === 'trace' ? (
          <>
            <FormField label="Trace type">
              {field => (
                <select
                  {...field}
                  value={traceKind}
                  disabled={busy}
                  onChange={event => {
                    setTraceKind(event.target.value as 'lot' | 'serial');
                    setResult(null);
                  }}
                  className="input w-full"
                >
                  <option value="lot">Lot number</option>
                  <option value="serial">Serial number</option>
                </select>
              )}
            </FormField>
            <FormField label="Exact lot or serial">
              {field => (
                <input
                  {...field}
                  className="input w-full"
                  value={traceValue}
                  maxLength={100}
                  disabled={busy}
                  onChange={event => {
                    setTraceValue(event.target.value);
                    setResult(null);
                  }}
                />
              )}
            </FormField>
          </>
        ) : (
          <FormField label={kind === 'purchasing' ? 'Purchase order' : 'Work order'} required>
            {field =>
              kind === 'purchasing' ? (
                <HankPurchaseOrderPicker
                  {...field}
                  value={target}
                  onChange={value => {
                    setTarget(value);
                    setResult(null);
                  }}
                  disabled={busy}
                />
              ) : (
                <EntityPicker
                  {...field}
                  kind="workOrder"
                  value={target}
                  onChange={value => {
                    setTarget(value);
                    setResult(null);
                  }}
                  disabled={busy}
                />
              )
            }
          </FormField>
        )}
        <LoadingButton
          type="submit"
          size="sm"
          loading={busy}
          loadingText="Checking source records…"
          disabled={kind === 'trace' ? !traceValue.trim() : !target}
        >
          Check records
        </LoadingButton>
      </form>
      {kind !== 'purchasing' && kind !== 'trace' && (
        <details>
          <summary className="text-xs text-fd-blue cursor-pointer">Select by scan</summary>
          <div className="mt-2">
            <HankJobScan
              disabled={busy}
              onSelect={job => {
                setTarget(String(job.workOrderId));
                setResult(null);
              }}
            />
          </div>
        </details>
      )}
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {result && (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold text-fd-ink">{result.title}</h4>
          <p className="text-sm text-fd-body">{result.summary}</p>
          <p className="text-[11px] text-fd-mute">Checked {formatCentralDateTime(result.checked_at)}</p>
          {result.checks.map(check => (
            <article key={check.key} className="space-y-2 border border-slate-700 rounded-[3px] p-3">
              <p className="text-xs font-semibold text-fd-ink">
                {check.title}{' '}
                <span
                  className={
                    check.status === 'attention' || check.status === 'unknown' ? 'text-fd-amber' : 'text-fd-mute'
                  }
                >
                  · {check.status}
                </span>
              </p>
              <p className="text-xs text-fd-body">{check.detail}</p>
              <div className="flex flex-wrap gap-2">
                {check.references.map(reference => (
                  <Link
                    key={`${reference.type}:${reference.id}`}
                    to={reference.url}
                    onClick={onNavigate}
                    className="text-xs text-fd-blue underline"
                  >
                    {reference.label}
                  </Link>
                ))}
              </div>
            </article>
          ))}
          {result.coverage_notes.map((note, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {note}
            </p>
          ))}
          {result.draft_text && (
            <div className="space-y-2">
              <FormField
                label="Draft for your review"
                help="Review the wording and send it through your usual process."
              >
                {field => (
                  <textarea {...field} readOnly rows={8} value={result.draft_text!} className="input w-full h-auto" />
                )}
              </FormField>
              <button
                type="button"
                className="btn text-xs"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(result.draft_text!)
                    .then(() => setCopied(true))
                    .catch(() => setError('Copy was unavailable. Select and copy the draft text.'));
                }}
              >
                Copy draft
              </button>
              {copied && (
                <p role="status" className="text-xs text-fd-mute">
                  Draft copied.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
