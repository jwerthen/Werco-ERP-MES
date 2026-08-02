/**
 * InputDialog — the shared single-field text-capture dialog.
 *
 * Replaces the native prompt() calls that used to collect a one-line value
 * (blocker notes, saved-filter names): prompt() blocks the main thread, cannot
 * be styled to match the instrument panel, ignores the app's focus-management
 * conventions, and is stripped by some kiosk/embedded browsers. This dialog is
 * built from the shared primitives instead — Modal (portal, focus trap,
 * Escape/backdrop close), FormField (label association + aria wiring), and
 * Button/LoadingButton (footer chrome + in-flight guard).
 *
 * Semantics:
 *   - The field submits on Enter (native form submission — the footer submit
 *     button makes the form implicitly submittable).
 *   - The value is TRIMMED before it reaches `onSubmit`, and an empty trimmed
 *     value disables submit — `onSubmit` never receives an empty string.
 *   - `pending` mirrors ConfirmDialog: the submit button shows the
 *     LoadingButton spinner, cancel is disabled, and backdrop/Escape dismissal
 *     is refused, so a server-gated submit can't be dismissed mid-flight.
 *   - Closing is the CALLER's job (via `open`), so a caller can keep the
 *     dialog up when the server refuses and close only on success — the same
 *     non-optimistic pattern the ConfirmDialog callers use.
 */

import React, { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { FormField } from './FormField';
import { Button } from './Button';
import { LoadingButton } from './LoadingButton';

export interface InputDialogProps {
  open: boolean;
  title: string;
  /** Optional supporting copy shown under the title. */
  message?: string;
  /** Visible label on the text field (programmatically associated via FormField). */
  label: string;
  defaultValue?: string;
  placeholder?: string;
  submitLabel?: string;
  /** In-flight state for the submit action. See the docblock above. */
  pending?: boolean;
  /** Receives the trimmed, non-empty value. */
  onSubmit: (value: string) => void;
  onCancel: () => void;
}

export function InputDialog({
  open,
  title,
  message,
  label,
  defaultValue = '',
  placeholder,
  submitLabel = 'Save',
  pending = false,
  onSubmit,
  onCancel,
}: InputDialogProps) {
  const [value, setValue] = useState(defaultValue);

  // Re-seed the field each time the dialog opens so a reopened dialog starts
  // from the caller's default, not the previous session's leftovers.
  useEffect(() => {
    if (open) setValue(defaultValue);
  }, [open, defaultValue]);

  const trimmed = value.trim();

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (pending || !trimmed) return;
    onSubmit(trimmed);
  };

  return (
    <Modal
      open={open}
      onClose={onCancel}
      size="sm"
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      <h3 className="text-lg font-semibold text-white">{title}</h3>
      {message && <p className="text-sm text-slate-300 mt-1">{message}</p>}
      <form onSubmit={handleSubmit} className="mt-4">
        <FormField label={label} required>
          {(field) => (
            <input
              {...field}
              type="text"
              required
              className="input"
              value={value}
              placeholder={placeholder}
              disabled={pending}
              onChange={(event) => setValue(event.target.value)}
            />
          )}
        </FormField>
        <div className="flex justify-end gap-3 mt-6">
          <Button variant="secondary" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={pending} disabled={!trimmed}>
            {submitLabel}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default InputDialog;
