/**
 * The record dialog shared by an NCR's re-verification (POST /quality/ncr/{id}/reverification)
 * and a CAR's effectiveness verification (POST /quality/car/{id}/effectiveness): one required
 * result and one required text, both stamped with who and when by the server.
 *
 * Server-gated, so non-optimistic, like InputDialog: the caller passes `pending` while the
 * POST is in flight (submit spins, Cancel and backdrop / Escape are refused) and `error` with
 * the server's detail verbatim when it refuses. Closing is the caller's job, on success only,
 * so a refused record keeps what was typed.
 */
import React, { useEffect, useId, useState } from 'react';
import { Button, FormField, LoadingButton, Modal } from '../ui';

export interface ResultOption<T extends string> {
  value: T;
  label: string;
}

interface QualityRecordResultDialogProps<T extends string> {
  open: boolean;
  title: string;
  message?: React.ReactNode;
  resultLabel: string;
  options: ReadonlyArray<ResultOption<T>>;
  textLabel: string;
  textHelp?: React.ReactNode;
  /** Seeds the text each time the dialog opens. */
  defaultText?: string;
  submitLabel: string;
  pending: boolean;
  error?: string;
  /** Receives the chosen result and the TRIMMED, non-empty text. */
  onSubmit: (value: { result: T; text: string }) => void;
  onCancel: () => void;
}

/** The server caps both the re-verification notes and the effectiveness evidence here. */
const TEXT_MAX_LENGTH = 4000;

export default function QualityRecordResultDialog<T extends string>({
  open,
  title,
  message,
  resultLabel,
  options,
  textLabel,
  textHelp,
  defaultText = '',
  submitLabel,
  pending,
  error,
  onSubmit,
  onCancel,
}: QualityRecordResultDialogProps<T>) {
  const titleId = useId();
  const [result, setResult] = useState<T | ''>('');
  const [text, setText] = useState(defaultText);

  // Every opening starts from no result chosen (a result is a deliberate choice) and the
  // caller's text, never the leftovers of the last session.
  useEffect(() => {
    if (open) {
      setResult('');
      setText(defaultText);
    }
  }, [open, defaultText]);

  const trimmed = text.trim();
  const ready = result !== '' && trimmed !== '';

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    // The dialog is portaled to <body>, but React still bubbles its submit through the
    // component tree, where the record's own <form> would take it as "Save changes".
    event.stopPropagation();
    if (pending || result === '' || !trimmed) return;
    onSubmit({ result, text: trimmed });
  };

  return (
    <Modal
      open={open}
      onClose={onCancel}
      size="md"
      ariaLabelledBy={titleId}
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      <h3 id={titleId} className="text-lg font-semibold text-white">
        {title}
      </h3>
      {message && <p className="mt-1 text-sm text-slate-300">{message}</p>}
      <form onSubmit={submit} className="mt-4 space-y-4">
        <FormField label={resultLabel} required>
          {field => (
            <select
              {...field}
              required
              className="input"
              value={result}
              disabled={pending}
              onChange={event => setResult(event.target.value as T | '')}
            >
              <option value="">Select a result</option>
              {options.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <FormField label={textLabel} required help={textHelp}>
          {field => (
            <textarea
              {...field}
              required
              className="input"
              rows={4}
              maxLength={TEXT_MAX_LENGTH}
              value={text}
              disabled={pending}
              onChange={event => setText(event.target.value)}
            />
          )}
        </FormField>
        {error && (
          <p role="alert" className="text-sm text-red-300">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-3">
          <Button variant="secondary" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={pending} disabled={!ready}>
            {submitLabel}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
