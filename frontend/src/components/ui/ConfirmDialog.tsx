/**
 * ConfirmDialog — the shared confirm/cancel dialog primitive.
 *
 * Built on Modal + Button/LoadingButton so every confirm in the app shares the
 * instrument-panel chrome and the async-action conventions instead of
 * hand-rolled footer buttons.
 *
 * `pending` is the in-flight state for a server-GATED confirm (delete, void,
 * obsolete — actions the server may refuse, which per the app convention stay
 * NON-optimistic): while true the confirm button shows the LoadingButton
 * spinner and double-click guard, the cancel button is disabled, and the
 * underlying Modal refuses backdrop/Escape dismissal — so an action already on
 * the wire can't be visually "cancelled" while the server may still apply it.
 * Callers that used to fake this with `confirmLabel={pending ? 'Deleting…' :
 * 'Delete'}` ternaries pass `pending` instead.
 *
 * Fully backward compatible: `pending` is optional and every pre-existing
 * caller compiles and behaves unchanged without it.
 */

import React from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import { Modal } from './Modal';
import { Button } from './Button';
import { LoadingButton } from './LoadingButton';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'warning' | 'info';
  /** In-flight state for the confirm action. See the docblock above. */
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

// LoadingButton variant per dialog variant. There is no amber LoadingButton
// variant, so 'warning' rides on 'primary' and overrides the color below.
const confirmButtonVariant: Record<'danger' | 'warning' | 'info', 'danger' | 'primary'> = {
  danger: 'danger',
  warning: 'primary',
  info: 'primary',
};

// The amber override for 'warning'. Plain utilities deliberately: the .btn-*
// chrome lives in @layer components, so these utility classes win the cascade
// deterministically (utilities layer comes after components). hover:shadow-none
// suppresses .btn-primary:hover's blue ring/glow box-shadow, which the bg/text
// overrides alone would leave rendering around the amber button.
const warningConfirmClasses = 'bg-amber-500 hover:bg-amber-600 hover:shadow-none text-white';

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  pending = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      size="sm"
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
    >
      <div className="flex items-start gap-3">
        <div className={`p-2 rounded-full ${variant === 'danger' ? 'bg-red-500/20' : variant === 'warning' ? 'bg-amber-500/20' : 'bg-blue-500/20'}`}>
          <ExclamationTriangleIcon className={`h-5 w-5 ${variant === 'danger' ? 'text-red-600' : variant === 'warning' ? 'text-amber-600' : 'text-blue-600'}`} />
        </div>
        <div className="flex-1">
          <h3 className="text-lg font-semibold text-white">{title}</h3>
          <p className="text-sm text-slate-300 mt-1">{message}</p>
        </div>
      </div>
      <div className="flex justify-end gap-3 mt-6">
        <Button variant="secondary" onClick={onCancel} disabled={pending}>
          {cancelLabel}
        </Button>
        <LoadingButton
          loading={pending}
          variant={confirmButtonVariant[variant]}
          className={variant === 'warning' ? warningConfirmClasses : ''}
          onClick={onConfirm}
        >
          {confirmLabel}
        </LoadingButton>
      </div>
    </Modal>
  );
}
