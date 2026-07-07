import React, { useEffect, useMemo, useState } from 'react';
import { CameraIcon, DocumentIcon, XCircleIcon } from '@heroicons/react/24/solid';

/** Client-side mirror of the server's attachment cap (process_sheet_service). */
export const MAX_STEP_ATTACHMENT_BYTES = 10 * 1024 * 1024; // 10 MB

interface KioskPhotoInputProps {
  /** PHOTO accepts images only (rear camera capture); FILE also accepts PDF. */
  stepType: 'photo' | 'file';
  value: File | null;
  onChange: (file: File | null) => void;
  disabled?: boolean;
  /** Unique per mounted instance — used for the input id + testids. */
  idPrefix: string;
  /** Authoring hint from the step config (e.g. "Photo of weld seam"). */
  hint?: string | null;
}

function formatMb(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Touch-first evidence picker for PHOTO/FILE steps. Opens the tablet's rear
 * camera (`capture="environment"`) for photos; FILE steps also take a PDF.
 * Size is checked client-side against the server's 10 MB cap so an oversize
 * pick fails instantly at the station instead of after a slow upload. The
 * upload itself is the PARENT's job (two-step: attachment endpoint first, then
 * the record create with the returned document_id).
 */
export default function KioskPhotoInput({ stepType, value, onChange, disabled = false, idPrefix, hint }: KioskPhotoInputProps) {
  const [sizeError, setSizeError] = useState<string | null>(null);
  const inputId = `${idPrefix}-file-input`;

  // Thumbnail preview for image picks (guarded: jsdom has no createObjectURL).
  const previewUrl = useMemo(() => {
    if (!value || !value.type.startsWith('image/')) return null;
    if (typeof URL.createObjectURL !== 'function') return null;
    return URL.createObjectURL(value);
  }, [value]);

  useEffect(() => {
    return () => {
      if (previewUrl && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  const handleChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    // Allow re-picking the same file after a clear.
    event.target.value = '';
    if (!file) return;
    if (file.size > MAX_STEP_ATTACHMENT_BYTES) {
      setSizeError(`${file.name} is too large (${formatMb(file.size)}) — the limit is 10 MB.`);
      onChange(null);
      return;
    }
    setSizeError(null);
    onChange(file);
  };

  return (
    <div data-testid={`${idPrefix}-photo-input`}>
      {hint && <p className="mb-2 text-lg text-fd-body">{hint}</p>}

      {value ? (
        <div className="flex items-center gap-4 rounded border border-fd-line-bright bg-fd-sunken p-3">
          {previewUrl ? (
            <img
              src={previewUrl}
              alt={`Selected evidence: ${value.name}`}
              className="h-24 w-24 shrink-0 rounded border border-fd-line object-cover"
            />
          ) : (
            <DocumentIcon className="h-16 w-16 shrink-0 text-fd-mute" aria-hidden="true" />
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-lg font-semibold text-fd-ink">{value.name}</p>
            <p className="text-base text-fd-mute">{formatMb(value.size)}</p>
          </div>
          <button
            type="button"
            onClick={() => onChange(null)}
            disabled={disabled}
            className="flex min-h-16 shrink-0 items-center gap-2 rounded border border-fd-line bg-fd-panel px-4 text-lg font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:cursor-not-allowed disabled:opacity-40"
          >
            <XCircleIcon className="h-6 w-6" aria-hidden="true" />
            {stepType === 'photo' ? 'Retake' : 'Replace'}
          </button>
        </div>
      ) : (
        <div className="relative focus-within:ring-2 focus-within:ring-fd-blue">
          <input
            id={inputId}
            type="file"
            accept={stepType === 'photo' ? 'image/*' : 'image/*,application/pdf'}
            // Rear camera on tablets — evidence photos, not selfies.
            {...(stepType === 'photo' ? { capture: 'environment' as const } : {})}
            onChange={handleChange}
            disabled={disabled}
            className="sr-only"
          />
          <label
            htmlFor={inputId}
            className={`flex min-h-20 w-full items-center justify-center gap-3 rounded border border-fd-blue bg-fd-blue/10 px-4 text-xl font-bold uppercase tracking-wide text-fd-blue transition-colors ${
              disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer hover:bg-fd-blue/20'
            }`}
          >
            <CameraIcon className="h-8 w-8 shrink-0" aria-hidden="true" />
            {stepType === 'photo' ? 'Take photo' : 'Take photo or choose PDF'}
          </label>
        </div>
      )}

      {sizeError && (
        <p role="alert" className="mt-2 rounded border border-fd-red bg-fd-red/10 px-4 py-3 text-lg font-semibold text-fd-red">
          {sizeError}
        </p>
      )}
    </div>
  );
}
