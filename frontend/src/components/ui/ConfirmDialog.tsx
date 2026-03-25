import React from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'warning' | 'info';
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;

  const confirmStyles = {
    danger: 'bg-red-600 hover:bg-red-700 text-white',
    warning: 'bg-amber-500 hover:bg-amber-600 text-white',
    info: 'bg-werco-navy-600 hover:bg-werco-navy-700 text-white',
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onCancel}>
      <div
        className="bg-white rounded-xl p-6 max-w-sm w-full mx-4 shadow-xl animate-scale-in"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          <div className={`p-2 rounded-full ${variant === 'danger' ? 'bg-red-100' : variant === 'warning' ? 'bg-amber-100' : 'bg-blue-100'}`}>
            <ExclamationTriangleIcon className={`h-5 w-5 ${variant === 'danger' ? 'text-red-600' : variant === 'warning' ? 'text-amber-600' : 'text-blue-600'}`} />
          </div>
          <div className="flex-1">
            <h3 className="text-lg font-semibold text-gray-900">{title}</h3>
            <p className="text-sm text-gray-600 mt-1">{message}</p>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-6">
          <button onClick={onCancel} className="btn-secondary">
            {cancelLabel}
          </button>
          <button onClick={onConfirm} className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${confirmStyles[variant]}`}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
