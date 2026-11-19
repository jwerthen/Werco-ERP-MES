import React, { useEffect, useId, useState } from 'react';
import { ExclamationTriangleIcon, ShieldExclamationIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { FormField, LoadingButton, Modal, useToast } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import {
  DOCUMENT_ACCESS_DESCRIPTIONS,
  DOCUMENT_ACCESS_LABELS,
  DOCUMENT_ACCESS_LEVELS,
  documentAccessLevel,
  type DocumentAccessChangeResult,
  type DocumentAccessFields,
  type DocumentAccessLevel,
} from '../../types/documentAccess';
import { DocumentAccessBadge } from './DocumentAccessBadge';

/** The warning the dialog raises when nobody in the company can open the document it just flagged. */
export const NOBODY_AUTHORIZED_WARNING =
  'Nobody in the company can open an export-controlled document yet. An Admin grants export authorization on the Users page.';

/** True when a change left an export-controlled document that nobody can open. */
export function nobodyCanOpen(result: DocumentAccessChangeResult): boolean {
  return documentAccessLevel(result) === 'export_controlled' && result.authorized_user_count === 0;
}

interface DocumentAccessDialogProps {
  /** The document whose level is being changed; null = closed. */
  document: DocumentAccessFields | null;
  onClose: () => void;
  /** Called with the server's answer after it accepted the change. */
  onChanged: (result: DocumentAccessChangeResult) => void | Promise<void>;
}

/**
 * Documents → Change access (AS9100D 7.5.3.1). Admin / Quality; the caller gates it.
 *
 * Non-optimistic on purpose: the server owns every refusal (an unchanged level is 409, an API
 * token is 403, a document the caller cannot view is 404), shown here verbatim, and the record
 * reflects only what the server returns. The reason is required and lands on the audit row
 * with the old and new level.
 */
export default function DocumentAccessDialog({ document, onClose, onChanged }: DocumentAccessDialogProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const current = documentAccessLevel(document);
  const [level, setLevel] = useState<DocumentAccessLevel>(current);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // A fresh form every time the dialog opens on a document (or the document's level changes).
  const documentId = document?.id;
  useEffect(() => {
    setLevel(current);
    setReason('');
    setReasonError(null);
    setServerError(null);
  }, [documentId, current]);

  if (!document) return null;

  const label = `${document.document_number} Rev ${document.revision}`;
  const unchanged = level === current;

  const close = () => {
    if (!saving) onClose();
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || unchanged) return;
    const trimmed = reason.trim();
    if (!trimmed) {
      setReasonError('Give a reason. It is recorded on the audit trail with the old and new level.');
      return;
    }
    setReasonError(null);
    setSaving(true);
    setServerError(null);
    try {
      const result = await api.changeDocumentAccessLevel(document.id, { access_level: level, reason: trimmed });
      const savedLabel = DOCUMENT_ACCESS_LABELS[documentAccessLevel(result)];
      if (nobodyCanOpen(result)) {
        showToast('warning', `${label} is now ${savedLabel}. ${NOBODY_AUTHORIZED_WARNING}`);
      } else {
        showToast('success', `${label} is now ${savedLabel}.`);
      }
      await onChanged(result);
    } catch (err) {
      setServerError(apiErrorDetail(err, 'Could not change the access level.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={close} size="lg" ariaLabelledBy={titleId} closeOnBackdrop={false} closeOnEscape={!saving}>
      <h3 id={titleId} className="mb-1 flex items-center gap-2 text-lg font-semibold">
        <ShieldExclamationIcon className="h-5 w-5 text-slate-300" aria-hidden="true" />
        Change access — {label}
      </h3>
      <p className="mb-4 text-sm text-slate-400 [overflow-wrap:anywhere]">{document.title}</p>

      <dl className="mb-4 grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-1.5 rounded-[4px] border border-slate-700 px-3 py-2.5 text-sm">
        <dt className="text-slate-400">Current level</dt>
        <dd>
          <DocumentAccessBadge level={current} showStandard />
        </dd>
        <dt className="text-slate-400">Who can open it</dt>
        <dd className="text-slate-200">{DOCUMENT_ACCESS_DESCRIPTIONS[current]}</dd>
      </dl>

      {serverError && (
        <div role="alert" className="alert alert-danger mb-4 text-sm">
          <span>{serverError}</span>
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        <fieldset className="space-y-2" disabled={saving}>
          <legend className="mb-1 text-sm font-medium text-slate-200">New access level</legend>
          {DOCUMENT_ACCESS_LEVELS.map(option => (
            <label
              key={option}
              className={`flex cursor-pointer items-start gap-3 rounded-[4px] border px-3 py-2 text-sm ${
                level === option ? 'border-fd-blue/60 bg-fd-blue/5' : 'border-slate-700 hover:border-slate-500'
              }`}
            >
              <input
                type="radio"
                name="document-access-level"
                value={option}
                checked={level === option}
                onChange={() => setLevel(option)}
                className="mt-0.5"
              />
              {/* The level's name sits directly in the span so the label's text is within the
                  a11y rule's reach; the description and "(current)" ride beside it. */}
              <span className="min-w-0 font-medium text-slate-100">
                {DOCUMENT_ACCESS_LABELS[option]}
                {option === current && <span className="ml-2 text-xs font-normal text-slate-400">(current)</span>}
                <span className="block text-xs font-normal text-slate-400">{DOCUMENT_ACCESS_DESCRIPTIONS[option]}</span>
              </span>
            </label>
          ))}
        </fieldset>

        {level === 'export_controlled' && !unchanged && (
          <p className="flex items-start gap-2 rounded-[4px] border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-fd-amber">
            <ExclamationTriangleIcon className="mt-px h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              Only export-authorized people can open it once saved, on every screen including the kiosk. If you are not
              export-authorized yourself, you lose access too.
            </span>
          </p>
        )}

        <FormField
          label="Reason"
          required
          error={reasonError}
          help="Recorded on the audit trail with the old and new level."
        >
          {field => (
            <textarea
              {...field}
              rows={3}
              maxLength={500}
              value={reason}
              onChange={e => setReason(e.target.value)}
              className={reasonError ? 'input-error' : 'input'}
              disabled={saving}
            />
          )}
        </FormField>

        <div className="flex flex-wrap items-center justify-end gap-2 pt-2">
          {unchanged && <span className="mr-auto text-xs text-slate-400">Choose a different level to save.</span>}
          <button type="button" onClick={close} disabled={saving} className="btn-secondary">
            Cancel
          </button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving…" disabled={unchanged}>
            Save access level
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
