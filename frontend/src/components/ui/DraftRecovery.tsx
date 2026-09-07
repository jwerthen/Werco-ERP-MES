import React, { useState } from 'react';
import { ResumableDraft } from '../../hooks/useResumableDraft';

export function DraftRecovery<T>({ draft }: { draft: ResumableDraft<T> }) {
  const [confirmRemove, setConfirmRemove] = useState(false);
  if (!draft.enabled) return null;
  return (
    <div className="mb-4 rounded border border-fd-line bg-surface-50 p-3 text-sm" aria-label="Draft recovery">
      {draft.candidate ? (
        <>
          <p className="font-medium">You have an unfinished draft.</p>
          <p className="mt-1 text-surface-600">
            Resume replaces the entries currently in this form. Your draft is private to your account.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={draft.busy || !draft.canResume}
              onClick={draft.resume}
            >
              Resume draft
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={draft.busy}
              onClick={() => setConfirmRemove(true)}
            >
              Remove saved draft
            </button>
            {confirmRemove && (
              <span className="flex flex-wrap items-center gap-2">
                Permanently remove the saved draft?
                <button
                  type="button"
                  className="btn btn-danger btn-sm"
                  disabled={draft.busy}
                  onClick={() => {
                    void draft.discard();
                    setConfirmRemove(false);
                  }}
                >
                  Confirm removal
                </button>
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirmRemove(false)}>
                  Keep draft
                </button>
              </span>
            )}
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p role="status">
            {draft.loading
              ? 'Checking for an unfinished draft…'
              : draft.status || 'Changes save to your account as you work.'}
          </p>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!draft.ready || draft.busy || !!draft.error}
            onClick={() => void draft.saveNow().catch(() => undefined)}
          >
            Save draft now
          </button>
        </div>
      )}
      {draft.error && (
        <p className="mt-2 text-danger-700" role="alert">
          {draft.error}{' '}
          <button type="button" className="underline" disabled={draft.busy} onClick={() => void draft.retry()}>
            Reload saved draft
          </button>
        </p>
      )}
    </div>
  );
}
