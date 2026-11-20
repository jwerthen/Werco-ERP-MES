import React, { useEffect, useRef, useState } from 'react';
import { Button, LoadingButton, Modal } from '../ui';
import { FormField } from '../ui/FormField';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { apiErrorDetail } from '../../utils/apiError';

/** The server's cap on `compliance_notes` (QMSClauseUpdate, max_length=5000). */
export const CLAUSE_NOTES_MAX_LENGTH = 5000;

interface ClauseNotesDialogProps {
  open: boolean;
  clauseNumber: string;
  clauseTitle: string;
  /** The notes as stored; null or empty when the clause has none. */
  initialNotes: string | null | undefined;
  /**
   * Saves the notes. `null` clears them. Rejects with the API error on refusal, so the dialog
   * stays open with what was typed and shows the server's reason.
   */
  onSave: (notes: string | null) => Promise<void>;
  onClose: () => void;
}

/**
 * Edit a QMS clause's compliance notes (`PUT /qms-standards/clauses/{id}` with
 * `compliance_notes` only). Saving notes never changes the clause's status or its
 * last-assessed stamp; those move only when the status itself changes.
 *
 * Server-gated (Admin / Manager / Quality), so it is non-optimistic: the dialog closes only
 * after the server accepts the write.
 */
export default function ClauseNotesDialog({
  open,
  clauseNumber,
  clauseTitle,
  initialNotes,
  onSave,
  onClose,
}: ClauseNotesDialogProps) {
  const [text, setText] = useState(initialNotes ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // Re-seed from the stored notes only at the moment the dialog opens. A newer `initialNotes`
  // arriving while it is open (the page's reload after a save) must never overwrite what the
  // user is typing.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setText(initialNotes ?? '');
      setError('');
    }
    wasOpen.current = open;
  }, [open, initialNotes]);

  const dirty = open && text !== (initialNotes ?? '');
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);
  const tooLong = text.length > CLAUSE_NOTES_MAX_LENGTH;

  const close = () => {
    if (saving) return;
    if (!confirmDiscard()) return;
    onClose();
  };

  const save = async () => {
    if (saving || tooLong || !dirty) return;
    setSaving(true);
    setError('');
    try {
      const trimmed = text.trim();
      await onSave(trimmed ? text : null);
      markSaved();
      onClose();
    } catch (err: unknown) {
      setError(apiErrorDetail(err, 'The notes could not be saved. Try again.'));
    } finally {
      setSaving(false);
    }
  };

  if (!open) return null;

  return (
    <Modal open onClose={close} size="lg" closeOnBackdrop={false} ariaLabel={`Notes for clause ${clauseNumber}`}>
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-bold text-lg">
          Notes for {clauseNumber} {clauseTitle}
        </h3>
        <button
          type="button"
          className="btn btn-ghost btn-sm btn-circle"
          onClick={close}
          disabled={saving}
          aria-label="Close"
        >
          X
        </button>
      </div>
      {error && (
        <p role="alert" className="mb-4 text-sm text-red-300">
          {error}
        </p>
      )}
      <FormField
        label={<span className="label-text font-medium">Compliance notes</span>}
        error={tooLong ? `Notes can be at most ${CLAUSE_NOTES_MAX_LENGTH.toLocaleString()} characters.` : null}
        help={`${text.length.toLocaleString()} / ${CLAUSE_NOTES_MAX_LENGTH.toLocaleString()} characters. Saving notes does not change the clause's compliance status.`}
      >
        {field => (
          <textarea
            {...field}
            className="textarea textarea-bordered w-full font-mono text-xs"
            rows={16}
            value={text}
            onChange={e => setText(e.target.value)}
            disabled={saving}
          />
        )}
      </FormField>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="ghost" size="sm" onClick={close} disabled={saving}>
          Cancel
        </Button>
        <LoadingButton size="sm" onClick={save} loading={saving} loadingText="Saving..." disabled={!dirty || tooLong}>
          Save notes
        </LoadingButton>
      </div>
    </Modal>
  );
}
