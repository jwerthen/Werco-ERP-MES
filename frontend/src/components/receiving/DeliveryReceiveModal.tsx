import React, { useState } from 'react';
import api from '../../services/api';
import { Button, FormField } from '../ui';
import { Modal } from '../ui/Modal';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { workspaceIdentity } from '../../hooks/useWorkspaceRecords';
import { DeliveryLine, DeliveryOutcome, DeliverySubmission, ReceivingCertificate } from '../../types/receivingDelivery';
import ReceiptCertificateField, { CertificateDownload } from './ReceiptCertificateField';

export interface DeliveryPO {
  po_id: number;
  po_number: string;
  vendor_name: string;
  lines: {
    line_id: number;
    part_number: string;
    part_name: string;
    quantity_remaining: number;
    requires_inspection?: boolean;
    is_closed?: boolean;
  }[];
}
const pendingKey = () => `werco:pending-delivery:${workspaceIdentity()}`;
export function pendingDelivery(): { po: DeliveryPO; body: DeliverySubmission } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingKey()) || 'null');
    if (
      !value ||
      !Number.isInteger(value.po?.po_id) ||
      !Array.isArray(value.po?.lines) ||
      value.body?.purchase_order_id !== value.po.po_id ||
      typeof value.body?.idempotency_key !== 'string' ||
      !Array.isArray(value.body?.lines) ||
      !value.body.lines.length ||
      value.body.lines.length > 50 ||
      value.body.lines.some(
        (line: DeliveryLine) =>
          !Number.isInteger(line.po_line_id) || !Number.isFinite(line.quantity_received) || line.quantity_received <= 0
      )
    )
      return null;
    return value;
  } catch {
    return null;
  }
}
export default function DeliveryReceiveModal({
  po,
  locations,
  onClose,
  onSaved,
}: {
  po: DeliveryPO;
  locations: { id: number; code: string; name: string }[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [identity] = useState(() => workspaceIdentity());
  const storageKey = `werco:pending-delivery:${identity}`;
  const [recovery] = useState(() => {
    const value = pendingDelivery();
    return value?.po.po_id === po.po_id ? value : null;
  });
  const [rows, setRows] = useState<Record<number, DeliveryLine>>(() =>
    Object.fromEntries((recovery?.body.lines || []).map(line => [line.po_line_id, line]))
  );
  const [certificates, setCertificates] = useState<Record<number, ReceivingCertificate>>({});
  const [uploads, setUploads] = useState<Record<number, boolean>>({});
  const [packingSlip, setPackingSlip] = useState(recovery?.body.lines[0]?.packing_slip_number || '');
  const [carrier, setCarrier] = useState(recovery?.body.lines[0]?.carrier || '');
  const [tracking, setTracking] = useState(recovery?.body.lines[0]?.tracking_number || '');
  const [location, setLocation] = useState(String(recovery?.body.lines[0]?.location_id || ''));
  const [review, setReview] = useState(!!recovery);
  const [submission, setSubmission] = useState<DeliverySubmission | null>(recovery?.body || null);
  const [outcome, setOutcome] = useState<DeliveryOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const uploading = Object.values(uploads).some(Boolean);
  const selected = Object.values(rows);
  const { confirmDiscard, markSaved } = useUnsavedChanges(!outcome && (selected.length > 0 || !!packingSlip));
  const close = () => {
    if (!busy && !uploading && (outcome || confirmDiscard())) onClose();
  };
  const change = (id: number, values: Partial<DeliveryLine>) =>
    setRows(previous => ({ ...previous, [id]: { ...previous[id], ...values } }));
  const toggleLine = (line: DeliveryPO['lines'][number], checked: boolean) => {
    setRows(previous => {
      const next = { ...previous };
      if (checked)
        next[line.line_id] = {
          po_line_id: line.line_id,
          quantity_received: line.quantity_remaining,
          requires_inspection: false,
          over_receive_approved: false,
        };
      else delete next[line.line_id];
      return next;
    });
    if (!checked)
      setCertificates(previous => {
        const next = { ...previous };
        delete next[line.line_id];
        return next;
      });
  };
  const reviewDelivery = () => {
    if (!selected.length || selected.length > 50) {
      setError('Select between 1 and 50 delivery lines.');
      return;
    }
    if (
      selected.some(
        line =>
          !Number.isFinite(line.quantity_received) ||
          line.quantity_received <= 0 ||
          (line.quantity_received >
            (po.lines.find(item => item.line_id === line.po_line_id)?.quantity_remaining ?? 0) &&
            !line.over_receive_approved)
      )
    ) {
      setError('Each selected line needs a positive quantity; explicitly approve any over-receipt.');
      return;
    }
    setError('');
    setReview(true);
  };
  const post = async () => {
    if (busy || workspaceIdentity() !== identity) return;
    const body = submission || {
      idempotency_key: crypto.randomUUID(),
      purchase_order_id: po.po_id,
      lines: selected.map(line => ({
        ...line,
        packing_slip_number: packingSlip,
        carrier,
        tracking_number: tracking,
        location_id: location ? Number(location) : undefined,
      })),
    };
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({ po, body }));
    } catch {
      setError('Unable to preserve this delivery for safe retry. Allow session storage before posting.');
      return;
    }
    setSubmission(body);
    setBusy(true);
    setError('');
    try {
      const result = await api.receiveDelivery(body);
      if (workspaceIdentity() !== identity) return;
      setOutcome(result);
      sessionStorage.removeItem(storageKey);
      markSaved();
      onSaved();
    } catch (err: unknown) {
      if (workspaceIdentity() !== identity) return;
      const failure = err as { response?: { status?: number; data?: { detail?: unknown } } };
      const detail = failure.response?.data?.detail;
      const knownRejected = [400, 401, 403, 404, 422].includes(failure.response?.status || 0);
      if (knownRejected) {
        sessionStorage.removeItem(storageKey);
        setSubmission(null);
        setReview(false);
      }
      setError(
        typeof detail === 'string'
          ? detail
          : knownRejected
            ? 'No delivery lines were posted. Review entries and retry.'
            : 'Submission could not be confirmed. Retry this same delivery to recover its outcome without posting twice.'
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={close} ariaLabel="Receive delivery" size="3xl" closeOnBackdrop={false}>
      <div className="space-y-4 min-w-0">
        <h2 className="text-xl font-semibold">Receive delivery · {po.po_number}</h2>
        <p>{po.vendor_name}</p>
        {error && (
          <p role="alert" className="border border-red-500/40 p-3 text-red-300">
            {error}
          </p>
        )}
        {outcome ? (
          <>
            <p role="status">Delivery received: {outcome.receipts.length} receipt records.</p>
            <ul className="space-y-3">
              {outcome.receipts.map(receipt => (
                <li key={receipt.id} className="border border-fd-line p-3">
                  <p>
                    {receipt.receipt_number} · {receipt.quantity_received} units · Lot {receipt.lot_number}
                  </p>
                  {receipt.certificate_document_id && (
                    <CertificateDownload documentId={receipt.certificate_document_id} />
                  )}
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            {submission && (
              <p className="text-amber-300">
                Pending submission preserved for this account. Retry the same delivery to check or complete it.
              </p>
            )}
            {!review ? (
              <>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <FormField label="Packing slip">
                    {field => (
                      <input
                        {...field}
                        className="input w-full"
                        value={packingSlip}
                        maxLength={50}
                        onChange={e => setPackingSlip(e.target.value)}
                      />
                    )}
                  </FormField>
                  <FormField label="Receiving location">
                    {field => (
                      <select
                        {...field}
                        className="input w-full"
                        value={location}
                        onChange={e => setLocation(e.target.value)}
                      >
                        <option value="">Default receiving area</option>
                        {locations.map(loc => (
                          <option key={loc.id} value={loc.id}>
                            {loc.code} — {loc.name}
                          </option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <FormField label="Carrier">
                    {field => (
                      <input
                        {...field}
                        className="input w-full"
                        value={carrier}
                        maxLength={100}
                        onChange={e => setCarrier(e.target.value)}
                      />
                    )}
                  </FormField>
                  <FormField label="Tracking number">
                    {field => (
                      <input
                        {...field}
                        className="input w-full"
                        value={tracking}
                        maxLength={100}
                        onChange={e => setTracking(e.target.value)}
                      />
                    )}
                  </FormField>
                </div>
                <p className="text-sm text-slate-400">
                  Select the lines that arrived. Lot, heat, certificate and inspection choices stay separate for each
                  part.
                </p>
                {po.lines
                  .filter(line => !line.is_closed && line.quantity_remaining > 0)
                  .map(line => (
                    <fieldset
                      key={line.line_id}
                      className="border border-fd-line p-3 space-y-3 min-w-0"
                      disabled={busy}
                    >
                      <legend className="max-w-full break-words px-1">
                        {line.part_number} · {line.part_name}
                      </legend>
                      <label className="flex gap-2">
                        <input
                          type="checkbox"
                          checked={!!rows[line.line_id]}
                          disabled={uploading}
                          onChange={e => toggleLine(line, e.target.checked)}
                        />
                        Receive this line · {line.quantity_remaining} remaining
                      </label>
                      {rows[line.line_id] && (
                        <>
                          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <FormField label="Quantity received">
                              {field => (
                                <input
                                  {...field}
                                  className="input w-full"
                                  type="number"
                                  min="0.0001"
                                  step="any"
                                  value={rows[line.line_id].quantity_received}
                                  onChange={e => change(line.line_id, { quantity_received: Number(e.target.value) })}
                                />
                              )}
                            </FormField>
                            {(['lot_number', 'heat_number', 'serial_numbers', 'cert_number'] as const).map(key => (
                              <FormField key={key} label={key.replace(/_/g, ' ')}>
                                {field => (
                                  <input
                                    {...field}
                                    className="input w-full"
                                    maxLength={key === 'serial_numbers' ? 500 : 50}
                                    value={rows[line.line_id][key] || ''}
                                    onChange={e => change(line.line_id, { [key]: e.target.value })}
                                  />
                                )}
                              </FormField>
                            ))}
                          </div>
                          <ReceiptCertificateField
                            lineId={line.line_id}
                            certificate={certificates[line.line_id]}
                            onBusy={value => setUploads(previous => ({ ...previous, [line.line_id]: value }))}
                            onChange={document => {
                              setCertificates(previous => ({ ...previous, [line.line_id]: document }));
                              change(line.line_id, { certificate_document_id: document.id });
                            }}
                          />
                          <label className="flex gap-2">
                            <input
                              type="checkbox"
                              checked={rows[line.line_id].requires_inspection}
                              onChange={e => change(line.line_id, { requires_inspection: e.target.checked })}
                            />
                            Requires inspection
                          </label>
                          {line.requires_inspection && (
                            <p className="text-amber-300 text-sm">
                              Part master flags incoming inspection; select the checkbox if required for this receipt.
                            </p>
                          )}
                          {rows[line.line_id].quantity_received > line.quantity_remaining && (
                            <label className="flex gap-2 text-amber-300">
                              <input
                                type="checkbox"
                                checked={rows[line.line_id].over_receive_approved}
                                onChange={e => change(line.line_id, { over_receive_approved: e.target.checked })}
                              />
                              Approve over-receipt for this line
                            </label>
                          )}
                        </>
                      )}
                    </fieldset>
                  ))}
              </>
            ) : (
              <>
                <h3 className="font-semibold">Review {selected.length} delivery lines</h3>
                <p>
                  {packingSlip ? `Packing slip ${packingSlip}` : 'No packing slip reference'} ·{' '}
                  {location ? locations.find(loc => String(loc.id) === location)?.code : 'Default receiving area'}
                </p>
                <ul className="space-y-2">
                  {selected.map(line => (
                    <li key={line.po_line_id} className="border border-fd-line p-3">
                      <p>
                        {po.lines.find(item => item.line_id === line.po_line_id)?.part_number} ·{' '}
                        {line.quantity_received} units
                      </p>
                      <p className="text-sm">
                        Lot {line.lot_number || 'auto-assigned'} · Heat {line.heat_number || 'not provided'} ·{' '}
                        {line.requires_inspection ? 'Hold for inspection' : 'Dock to stock'} ·{' '}
                        {line.certificate_document_id ? 'Stored certificate linked' : 'No certificate file'}
                      </p>
                    </li>
                  ))}
                </ul>
                <p className="text-sm text-slate-400">
                  All selected lines post together. If a line fails validation, none are received.
                </p>
              </>
            )}
          </>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" disabled={busy || uploading} onClick={close}>
            {outcome ? 'Done' : 'Close'}
          </Button>
          {!outcome &&
            (review ? (
              <>
                {!submission && (
                  <Button type="button" variant="secondary" onClick={() => setReview(false)}>
                    Edit entries
                  </Button>
                )}
                <Button type="button" disabled={busy || uploading} onClick={post}>
                  {busy
                    ? 'Receiving delivery…'
                    : submission
                      ? 'Retry same delivery'
                      : `Receive ${selected.length} lines`}
                </Button>
              </>
            ) : (
              <Button type="button" disabled={uploading || !selected.length} onClick={reviewDelivery}>
                Review delivery
              </Button>
            ))}
        </div>
      </div>
    </Modal>
  );
}
