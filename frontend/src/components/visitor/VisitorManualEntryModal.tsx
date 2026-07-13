import React, { useEffect, useMemo, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { FormField, LoadingButton, Modal, SelectField, useToast } from '../ui';
import {
  visitorManualEntrySchema,
  VisitorManualEntryFormData,
  VisitorManualEntryFormInput,
} from '../../validation/schemas';
import { PURPOSE_TILES } from './visitorConstants';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { centralWallClockToUtcISO, getCentralNowDateTimeLocal } from '../../utils/centralTime';
import api from '../../services/api';

interface VisitorManualEntryModalProps {
  open: boolean;
  onClose: () => void;
  /** Called after a successful back-entry so the parent can refresh the log. */
  onSaved: () => void;
}

const PURPOSE_OPTIONS = PURPOSE_TILES.map((tile) => ({ value: tile.value, label: tile.label }));

const EMPTY_FORM: VisitorManualEntryFormInput = {
  visitor_name: '',
  visitor_company: '',
  host_name: '',
  purpose: 'meeting',
  purpose_note: '',
  safety_acknowledged: false,
  signed_in_at: '',
  signed_out_at: '',
};

/**
 * Staff back-entry ("Add visit") for a visit that happened offline — e.g. a
 * paper-logged arrival during a lobby-tablet outage. ADMIN/MANAGER only (the
 * page gates the trigger; the server enforces the role and is the final
 * authority on the times).
 *
 * Datetime fields collect shop-local Central wall-clock via
 * `<input type="datetime-local">` and are converted to UTC ISO 'Z' on submit
 * (centralWallClockToUtcISO). Client-side rules mirror the server
 * (VisitorManualEntryRequest): sign-in required + in the past, sign-out
 * on/after sign-in + in the past, note required for "Other", acknowledgment
 * required — but a 422 from the server is surfaced verbatim via toast.
 */
export default function VisitorManualEntryModal({ open, onClose, onSaved }: VisitorManualEntryModalProps) {
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);

  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    formState: { errors, isDirty },
  } = useForm<VisitorManualEntryFormInput, unknown, VisitorManualEntryFormData>({
    resolver: zodResolver(visitorManualEntrySchema),
    defaultValues: EMPTY_FORM,
  });

  // Seed a clean form each time the modal opens.
  useEffect(() => {
    if (open) reset(EMPTY_FORM);
  }, [open, reset]);

  // Upper bound for both datetime pickers: neither time may be in the future.
  // Captured once per open so it stays stable while the modal is up; the schema
  // re-checks against the real clock on submit and the server is authoritative.
  const maxDateTime = useMemo(() => getCentralNowDateTimeLocal(), [open]);

  const purpose = watch('purpose');
  const isOther = purpose === 'other';

  const { confirmDiscard } = useUnsavedChanges(open && isDirty);

  const handleCancel = () => {
    if (confirmDiscard()) onClose();
  };

  const onSubmit = async (data: VisitorManualEntryFormData) => {
    const signedInIso = centralWallClockToUtcISO(data.signed_in_at);
    if (!signedInIso) {
      // The schema already guards this; belt-and-suspenders so we never POST a
      // malformed time.
      showToast('error', 'Enter a valid sign-in date and time.');
      return;
    }
    const signedOutRaw = (data.signed_out_at ?? '').trim();
    const signedOutIso = signedOutRaw ? centralWallClockToUtcISO(signedOutRaw) : null;

    setSaving(true);
    try {
      const created = await api.addManualVisit({
        visitor_name: data.visitor_name,
        visitor_company: data.visitor_company || undefined,
        host_name: data.host_name || undefined,
        purpose: data.purpose,
        purpose_note: isOther ? (data.purpose_note ?? '').trim() || undefined : undefined,
        safety_acknowledged: data.safety_acknowledged,
        signed_in_at: signedInIso,
        signed_out_at: signedOutIso || undefined,
      });
      showToast('success', `Logged visit for ${created.visitor_name}.`);
      onSaved();
      onClose();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      showToast('error', typeof detail === 'string' ? detail : 'Could not add the visit.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={handleCancel} size="2xl" ariaLabelledBy="visitor-add-title">
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <div>
          <h2 id="visitor-add-title" className="text-lg font-bold text-fd-ink">
            Add visit
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            Back-enter a visit that was logged offline (e.g. on paper during a tablet outage). Record the actual
            Central-time arrival and departure — both must be in the past.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <FormField label="Visitor name" required error={errors.visitor_name?.message}>
            {(field) => (
              <input
                {...field}
                type="text"
                {...register('visitor_name')}
                className={errors.visitor_name ? 'input input-error w-full' : 'input w-full'}
                placeholder="Full name"
              />
            )}
          </FormField>

          <FormField label="Visitor company" error={errors.visitor_company?.message}>
            {(field) => (
              <input
                {...field}
                type="text"
                {...register('visitor_company')}
                className="input w-full"
                placeholder="Company (optional)"
              />
            )}
          </FormField>

          <FormField label="Host" error={errors.host_name?.message}>
            {(field) => (
              <input
                {...field}
                type="text"
                {...register('host_name')}
                className="input w-full"
                placeholder="Who they were here to see (optional)"
              />
            )}
          </FormField>

          <FormField label="Purpose" required error={errors.purpose?.message}>
            <Controller
              name="purpose"
              control={control}
              render={({ field }) => (
                <SelectField
                  value={field.value}
                  onChange={field.onChange}
                  options={PURPOSE_OPTIONS}
                  ariaLabel="Purpose"
                />
              )}
            />
          </FormField>

          {isOther && (
            <FormField
              label="Purpose note"
              required
              error={errors.purpose_note?.message}
              className="sm:col-span-2"
              help="Required when the purpose is “Other”."
            >
              {(field) => (
                <input
                  {...field}
                  type="text"
                  {...register('purpose_note')}
                  className={errors.purpose_note ? 'input input-error w-full' : 'input w-full'}
                  placeholder="Briefly describe the reason for the visit"
                />
              )}
            </FormField>
          )}

          <FormField label="Signed in" required error={errors.signed_in_at?.message} help="Shop-local Central time.">
            {(field) => (
              <input
                {...field}
                type="datetime-local"
                max={maxDateTime}
                {...register('signed_in_at')}
                className={errors.signed_in_at ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>

          <FormField
            label="Signed out"
            error={errors.signed_out_at?.message}
            help="Leave blank if still on-site."
          >
            {(field) => (
              <input
                {...field}
                type="datetime-local"
                max={maxDateTime}
                {...register('signed_out_at')}
                className={errors.signed_out_at ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
        </div>

        <div>
          <label className="flex items-start gap-2 text-sm text-fd-body">
            <input
              type="checkbox"
              {...register('safety_acknowledged')}
              aria-invalid={errors.safety_acknowledged ? true : undefined}
              className="mt-0.5 rounded-sm border-slate-600 bg-slate-800"
            />
            <span>The visitor completed the required safety / NDA briefing.</span>
          </label>
          {errors.safety_acknowledged && (
            <p role="alert" aria-live="polite" className="mt-1 text-xs text-fd-red">
              {errors.safety_acknowledged.message}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={handleCancel} disabled={saving} className="btn-secondary">
            Cancel
          </button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving…">
            Add visit
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
