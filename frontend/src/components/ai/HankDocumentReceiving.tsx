import React, { useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import type { HankIntakeFile, HankIntakeReceivingDraft } from '../../types/hankIntake';
import { FormField } from '../ui/FormField';
import { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankOperationalTask } from './HankOperationalTask';
import { HankSourceFile } from './HankSourceFile';
import { useHankSessionGuard } from './useHankSessionGuard';

/** ERP writes remain in the reviewed task workflow. */
export function HankDocumentReceiving({
  file,
  onNavigate,
  onBusyChange,
}: {
  file: HankIntakeFile;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [purchaseOrderId, setPurchaseOrderId] = useState<number>();
  const [draft, setDraft] = useState<HankIntakeReceivingDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [taskBusy, setTaskBusy] = useState(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const { current, controller, release, changed } = useHankSessionGuard();
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    callback.current?.(taskBusy);
  }, [taskBusy]);
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setError('');
    setDraft(null);
    api
      .getHankIntakeReceivingDraft(file.id, purchaseOrderId, request.signal)
      .then(value => {
        if (current() && !request.signal.aborted && value.company_id === file.company_id) setDraft(value);
      })
      .catch(cause => {
        if (!current() || request.signal.aborted) return;
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(typeof detail === 'string' ? detail : 'Receiving suggestions could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [file.id, file.company_id, purchaseOrderId, attempt, current, controller, release]);
  if (changed)
    return (
      <p role="alert" className="text-xs text-fd-amber">
        Your session changed. Reopen Hank to receive materials.
      </p>
    );
  return (
    <section aria-label="Receive materials from PDF" className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink">Receive materials from {file.filename}</h3>
      <p className="text-xs text-fd-mute">
        Check the source, purchase order, units, quantities, and lot or heat numbers. Only confirmed receipt lines will
        update receiving and inventory.
      </p>
      <HankSourceFile
        filename={file.filename}
        pages={draft?.lines.flatMap(line => line.evidence.map(item => item.page)) || []}
        load={signal => api.getHankIntakeSource(file.id, signal)}
      />
      <FormField label="Purchase order for this PDF" required>
        {field => (
          <HankPurchaseOrderPicker
            {...field}
            value={String(purchaseOrderId || draft?.purchase_order_id || '')}
            onChange={value => setPurchaseOrderId(value ? Number(value) : undefined)}
            disabled={taskBusy || loading}
          />
        )}
      </FormField>
      {draft && !draft.purchase_order_id && (
        <div className="space-y-2">
          <p className="text-xs text-fd-amber">Choose the purchase order before preparing receipt lines.</p>
          {draft.purchase_orders.map(po => (
            <button
              key={po.id}
              type="button"
              className="block text-xs text-fd-blue underline"
              onClick={() => setPurchaseOrderId(po.id)}
            >
              {po.po_number} · {po.vendor_name} — {po.reason}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        className="text-xs text-fd-blue underline"
        disabled={loading || taskBusy}
        onClick={() => setAttempt(value => value + 1)}
      >
        Refresh receiving suggestions
      </button>
      {loading && (
        <p role="status" className="text-xs text-fd-mute">
          Matching the PDF to receiving lines…
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}{' '}
          <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
            Retry receiving suggestions
          </button>
        </p>
      )}
      {draft && (
        <>
          {draft.warnings.map((warning, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {warning}
            </p>
          ))}
          {draft.has_duplicates && (
            <p className="text-xs text-fd-amber">
              This PDF matches a previous upload. Check prior receipts before proceeding.
            </p>
          )}
          <details open>
            <summary className="text-xs text-fd-blue cursor-pointer">Review {draft.lines.length} source lines</summary>
            <div className="mt-2 space-y-2">
              {draft.lines.map(line => (
                <div
                  key={line.source_line_index}
                  className="space-y-1 border border-slate-700 p-2 text-xs text-fd-body"
                >
                  <p>
                    {line.description || line.part_number || `Source line ${line.source_line_index + 1}`} ·{' '}
                    {line.confidence} confidence
                  </p>
                  <p>
                    Part {line.part_number || 'unknown'} · Quantity {line.quantity || 'unknown'}{' '}
                    {line.unit_of_measure || '(units unknown)'}
                  </p>
                  <p>
                    Lot {line.lot_number || 'unknown'} · Heat {line.heat_number || 'unknown'}
                  </p>
                  <p className="text-fd-mute">
                    {line.po_line_id && line.quantity_received !== null
                      ? 'Suggested in the receipt form below; verify before submitting.'
                      : 'Needs manual review. Enter the verified quantity on the correct PO line below.'}
                  </p>
                  {line.candidates.map(candidate => (
                    <p key={candidate.po_line_id}>
                      PO line {candidate.line_number} · {candidate.part_number} · Stocking unit{' '}
                      {candidate.unit_of_measure} · Remaining {candidate.quantity_remaining}
                    </p>
                  ))}
                  {line.warnings.map((warning, index) => (
                    <p key={index} className="text-fd-amber">
                      {warning}
                    </p>
                  ))}
                  {line.evidence.map((item, index) => (
                    <p key={index} className="text-fd-mute">
                      Page {item.page}: {item.excerpt}
                    </p>
                  ))}
                </div>
              ))}
            </div>
          </details>
          {draft.purchase_order_id && (
            <HankOperationalTask
              key={`${draft.file_id}:${draft.file_version}:${draft.purchase_order_id}`}
              kind="receive_delivery"
              purchaseOrderId={draft.purchase_order_id}
              receivingDraft={draft}
              onRefreshReceiving={() => setAttempt(value => value + 1)}
              onNavigate={onNavigate}
              onBusyChange={setTaskBusy}
            />
          )}
        </>
      )}
    </section>
  );
}
