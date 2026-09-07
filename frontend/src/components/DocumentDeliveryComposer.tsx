import React, { useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { DeliveryEntity, DocumentDelivery } from '../types/documentDelivery';
import { Modal } from './ui/Modal';
import { formatCentralDateTime } from '../utils/centralTime';
import { workspaceError } from '../hooks/useWorkspaceRecords';
import PdfPreview from './ui/PdfPreview';

const statusLabel: Record<DocumentDelivery['status'], string> = {
  prepared: 'Ready for review',
  sending: 'Sending — check status before retrying',
  accepted: 'Accepted by mail server',
  failed: 'Email failed',
  unknown: 'Delivery outcome unknown',
};
const requestKey = () => crypto.randomUUID?.() || `email-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const deliveryLabel = (row: DocumentDelivery) =>
  row.manually_verified && row.status === 'accepted' ? 'Manually verified as sent' : statusLabel[row.status];

export function DocumentDeliveryComposer({
  entityType,
  entityId,
  onClose,
  onAccepted,
  canReconcile = false,
  canPrepare = true,
}: {
  entityType: DeliveryEntity;
  entityId: number;
  onClose: () => void;
  onAccepted: () => void;
  canReconcile?: boolean;
  canPrepare?: boolean;
}) {
  const [delivery, setDelivery] = useState<DocumentDelivery | null>(null);
  const [history, setHistory] = useState<DocumentDelivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [attachmentError, setAttachmentError] = useState('');
  const [attachment, setAttachment] = useState('');
  const [attachmentRevision, setAttachmentRevision] = useState(0);
  const [recipient, setRecipient] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [verificationNote, setVerificationNote] = useState('');
  const [verifiedOutcome, setVerifiedOutcome] = useState<'accepted' | 'failed'>('failed');
  const pending = useRef(false);
  const key = useRef(requestKey());
  const alive = useRef(true);
  const sequence = useRef(0);
  const attempted = useRef(false);
  const accepted = useRef(onAccepted);
  accepted.current = onAccepted;
  const select = (row: DocumentDelivery, preserveEdits = false) => {
    setDelivery(row);
    if (!preserveEdits) {
      setRecipient(row.recipient);
      setSubject(row.subject);
      setBody(row.body);
    }
    setAttachmentRevision(value => value + 1);
    setReviewed(false);
    key.current = requestKey();
    attempted.current = false;
    setVerificationNote('');
  };
  const prepare = async () => {
    if (pending.current || !canPrepare) return;
    pending.current = true;
    setBusy(true);
    setError('');
    const seq = ++sequence.current;
    try {
      const row = await api.previewDocumentDelivery(entityType, entityId, delivery?.status === 'accepted');
      if (alive.current && sequence.current === seq) {
        select(row);
        setHistory(rows => [row, ...rows.filter(item => item.id !== row.id)]);
      }
    } catch (reason) {
      if (alive.current && sequence.current === seq)
        setError(workspaceError(reason, 'Could not prepare the email. Your document is unchanged.'));
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const refresh = async () => {
    const seq = ++sequence.current;
    setLoading(true);
    setError('');
    try {
      const rows = await api.listDocumentDeliveries(entityType, entityId);
      if (!alive.current || seq !== sequence.current) return;
      setHistory(rows);
      if (rows.length) {
        const row = rows.find(item => item.id === delivery?.id) || rows[0];
        select(row, row.id === delivery?.id && row.status === 'prepared');
      }
    } catch (reason) {
      if (alive.current && seq === sequence.current)
        setError(workspaceError(reason, 'Email history could not be loaded. Refresh before sending.'));
    } finally {
      if (alive.current && seq === sequence.current) setLoading(false);
    }
  };
  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      ++sequence.current;
    };
    // A composer instance is mounted for one document; callbacks stay in refs.
  }, [entityType, entityId]);

  useEffect(() => {
    if (!delivery) return;
    let active = true;
    let url = '';
    setAttachment('');
    setAttachmentError('');
    api
      .getDocumentDeliveryAttachment(delivery.id)
      .then(blob => {
        if (!active) return;
        url = URL.createObjectURL(blob);
        setAttachment(url);
      })
      .catch(() => {
        if (active) setAttachmentError('The reviewed PDF could not be loaded. Refresh the email before sending.');
      });
    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [delivery?.id, attachmentRevision]);

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    if (
      !delivery ||
      !canPrepare ||
      pending.current ||
      !reviewed ||
      !attachment ||
      !delivery.send_available ||
      attempted.current
    )
      return;
    pending.current = true;
    attempted.current = true;
    setBusy(true);
    setError('');
    try {
      const row = await api.sendDocumentDelivery(delivery.id, {
        expected_version: delivery.version,
        request_key: key.current,
        recipient: recipient.trim(),
        subject: subject.trim(),
        body,
      });
      if (!alive.current) return;
      setDelivery(row);
      setHistory(rows => [row, ...rows.filter(item => item.id !== row.id)]);
      if (row.status === 'accepted') accepted.current();
    } catch (reason) {
      if (alive.current)
        setError(
          workspaceError(reason, 'The send response was interrupted. Refresh email status before trying again.')
        );
      // Never retry an ambiguous send automatically or unlock the same form.
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const editable = canPrepare && delivery?.status === 'prepared' && !attempted.current && !busy && !error && !loading;
  const reconcile = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!delivery || pending.current || !canReconcile || verificationNote.trim().length < 10) return;
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      const row = await api.reconcileDocumentDelivery(delivery.id, {
        expected_version: delivery.version,
        outcome: verifiedOutcome,
        verification_note: verificationNote.trim(),
      });
      if (!alive.current) return;
      select(row);
      setHistory(rows => [row, ...rows.filter(item => item.id !== row.id)]);
      if (row.status === 'accepted') accepted.current();
    } catch (reason) {
      if (alive.current)
        setError(workspaceError(reason, 'Could not record verification. Refresh email status before retrying.'));
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const uncertain = history.some(row => ['sending', 'unknown'].includes(row.status));
  return (
    <Modal
      open
      onClose={() => {
        if (!pending.current) onClose();
      }}
      size="5xl"
      closeOnBackdrop={false}
      closeOnEscape={!busy}
      ariaLabel="Document email"
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">
            Email {delivery?.document_number || (entityType === 'quote' ? 'quote' : 'purchase order')}
          </h2>
          <p className="mt-1 text-sm text-surface-500">
            Review the recipient, message and attached document before sending.
          </p>
        </div>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>
          Close
        </button>
      </div>
      {loading && <p role="status">Loading email history…</p>}
      {!canPrepare && (
        <p className="mb-3 text-sm text-surface-500">
          You can review email history and download attachments. Preparing or sending an email requires approval
          permission.
        </p>
      )}
      {error && (
        <p role="alert" className="mb-3 border border-red-500/40 p-3 text-red-300">
          {error}
        </p>
      )}
      {uncertain && (
        <p className="mb-3 border border-amber-500/40 p-3 text-amber-200">
          An earlier email has an unknown outcome or is still sending. Check its status and your mail system before
          preparing another email to avoid sending it twice.
        </p>
      )}
      <div className="mb-4 flex flex-wrap gap-2">
        <button type="button" className="btn-secondary" disabled={busy || loading} onClick={() => void refresh()}>
          Refresh email status
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={!canPrepare || busy || loading || !!error}
          onClick={() => void prepare()}
        >
          {delivery ? 'Prepare new email' : 'Prepare email for review'}
        </button>
      </div>
      {delivery && (
        <>
          <p role="status" className="mb-3 font-medium">
            {deliveryLabel(delivery)}
            {delivery.accepted_at ? ` · ${formatCentralDateTime(delivery.accepted_at)}` : ''}
          </p>
          {delivery.status_detail && <p className="mb-3 text-sm">{delivery.status_detail}</p>}
          {delivery.status === 'accepted' && !delivery.manually_verified && (
            <p className="mb-3 text-sm text-surface-500">
              The mail server accepted this email. Inbox delivery has not been confirmed.
            </p>
          )}
          {delivery.manually_verified && (
            <p className="mb-3 text-sm">Verification note: {delivery.verification_note}</p>
          )}
          {!delivery.send_available && delivery.unavailable_reason && (
            <p className="mb-3 text-sm text-amber-200">{delivery.unavailable_reason}</p>
          )}
          <form onSubmit={send} className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="space-y-3">
              <label className="block text-sm">
                Recipient email
                <input
                  type="email"
                  value={recipient}
                  required
                  maxLength={320}
                  readOnly={!editable}
                  onChange={event => {
                    setRecipient(event.target.value);
                    setReviewed(false);
                  }}
                  className="input mt-1 w-full"
                />
              </label>
              <label className="block text-sm">
                Subject
                <input
                  value={subject}
                  required
                  maxLength={200}
                  readOnly={!editable}
                  onChange={event => {
                    setSubject(event.target.value);
                    setReviewed(false);
                  }}
                  className="input mt-1 w-full"
                />
              </label>
              <label className="block text-sm">
                Message
                <textarea
                  value={body}
                  required
                  maxLength={10000}
                  readOnly={!editable}
                  onChange={event => {
                    setBody(event.target.value);
                    setReviewed(false);
                  }}
                  rows={8}
                  className="input mt-1 w-full"
                />
              </label>
              {delivery.status === 'prepared' && (
                <>
                  <label className="flex items-start gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={reviewed}
                      disabled={!editable || !attachment}
                      onChange={event => setReviewed(event.target.checked)}
                      className="mt-1"
                    />
                    I reviewed the recipient, message and attached PDF.
                  </label>
                  <button
                    type="submit"
                    className="btn-primary"
                    disabled={!editable || !reviewed || !attachment || !delivery.send_available}
                  >
                    {busy ? 'Sending…' : 'Send email'}
                  </button>
                </>
              )}
            </div>
            <div className="min-w-0">
              <p className="mb-2 break-all text-sm">Attachment: {delivery.attachment_name}</p>
              {attachmentError && (
                <p role="alert" className="text-red-300">
                  {attachmentError}
                </p>
              )}
              {attachment ? (
                <>
                  <PdfPreview
                    title="Reviewed document PDF"
                    url={attachment}
                    fileName={delivery.attachment_name}
                    className="w-full rounded border border-fd-line"
                  />
                </>
              ) : (
                !attachmentError && <p role="status">Loading reviewed PDF…</p>
              )}
            </div>
          </form>
          {canReconcile && ['unknown', 'sending'].includes(delivery.status) && (
            <form onSubmit={reconcile} className="mt-5 space-y-3 border-t border-fd-line pt-4">
              <h3 className="font-semibold">Record a verified delivery outcome</h3>
              <p className="text-sm text-surface-500">
                Check the mail server or recipient first. This records your verification and sends no email. In-flight
                sends must be at least 10 minutes old.
              </p>
              <label className="block text-sm">
                Verified outcome
                <select
                  className="input mt-1 w-full"
                  value={verifiedOutcome}
                  disabled={busy}
                  onChange={event => setVerifiedOutcome(event.target.value as 'accepted' | 'failed')}
                >
                  <option value="failed">Confirmed not sent</option>
                  <option value="accepted">Confirmed sent</option>
                </select>
              </label>
              <label className="block text-sm">
                Verification note
                <textarea
                  className="input mt-1 w-full"
                  value={verificationNote}
                  disabled={busy}
                  required
                  minLength={10}
                  maxLength={1000}
                  rows={2}
                  onChange={event => setVerificationNote(event.target.value)}
                  placeholder="Record what you checked and the result."
                />
              </label>
              <button
                type="submit"
                className="btn-secondary"
                disabled={busy || loading || verificationNote.trim().length < 10}
              >
                Record verified outcome
              </button>
            </form>
          )}
        </>
      )}
      {history.length > 0 && (
        <div className="mt-6 border-t border-fd-line pt-4">
          <h3 className="font-semibold">Email history</h3>
          <ul className="mt-2 space-y-2 text-sm">
            {history.map(row => (
              <li key={row.id} className="flex flex-wrap justify-between gap-2 border-b border-fd-line py-2">
                <span className="min-w-0 break-all">
                  {deliveryLabel(row)} · {row.recipient || 'Recipient not set'} ·{' '}
                  {formatCentralDateTime(row.attempted_at || row.created_at)}
                </span>
                <button type="button" className="text-fd-link underline" disabled={busy} onClick={() => select(row)}>
                  Review email #{row.id}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Modal>
  );
}
