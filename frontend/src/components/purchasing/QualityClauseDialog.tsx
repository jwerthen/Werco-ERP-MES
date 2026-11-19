/**
 * Add or edit one quality clause in the purchase-order flow-down catalog
 * (Purchasing → Quality flow-down; ADMIN / MANAGER / QUALITY).
 *
 * The code is the clause's identity on every purchase order that selects it, so it is set
 * once and read-only after create (the server refuses a change). There is no delete — a
 * clause leaves use by being retired (`is_active` off). Server-GATED (409 on a duplicate
 * code, 503 when the required audit row cannot be written), so non-optimistic: the dialog
 * stays open on any refusal with the server's `detail` toasted verbatim, and hands the saved
 * clause to `onSaved` only on success.
 */
import React, { useEffect, useId, useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { POQualityClause, POQualityClauseUpdate } from '../../types/poFlowdown';
import {
  qualityClauseSchema,
  type QualityClauseFormData,
  type QualityClauseFormInput,
} from '../../validation/poFlowdown';
import { flowdownErrorDetail } from '../../utils/qualityFlowdown';

interface QualityClauseDialogProps {
  open: boolean;
  /** The clause being edited; `null` adds a new one. */
  clause: POQualityClause | null;
  /** Suggested sort position for a new clause (after the current last one). */
  nextSortOrder?: number;
  onCancel: () => void;
  onSaved: (clause: POQualityClause) => void;
}

function formFromClause(clause: POQualityClause | null, nextSortOrder: number): QualityClauseFormInput {
  if (!clause) {
    return {
      code: '',
      title: '',
      body: '',
      default_selected: false,
      is_active: true,
      sort_order: String(nextSortOrder),
    };
  }
  return {
    code: clause.code,
    title: clause.title,
    body: clause.body ?? '',
    default_selected: clause.default_selected,
    is_active: clause.is_active,
    sort_order: String(clause.sort_order),
  };
}

export function QualityClauseDialog({ open, clause, nextSortOrder = 0, onCancel, onSaved }: QualityClauseDialogProps) {
  const titleId = useId();
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const initial = useMemo(() => formFromClause(clause, nextSortOrder), [clause, nextSortOrder]);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<QualityClauseFormInput, unknown, QualityClauseFormData>({
    resolver: zodResolver(qualityClauseSchema),
    defaultValues: initial,
  });

  useEffect(() => {
    if (open) reset(initial);
  }, [open, initial, reset]);

  const { confirmDiscard, markSaved } = useUnsavedChanges(open && isDirty);

  const handleCancel = () => {
    if (saving) return;
    if (confirmDiscard()) onCancel();
  };

  const onSubmit = async (data: QualityClauseFormData) => {
    if (saving) return;
    const fields = {
      title: data.title,
      body: data.body.trim() ? data.body.trim() : null,
      default_selected: data.default_selected,
      is_active: data.is_active,
      sort_order: data.sort_order,
    };
    // An edit sends only what changed (the PUT is partial), so it cannot overwrite a field
    // someone else changed in the meantime.
    const changes: POQualityClauseUpdate = {};
    if (clause) {
      if (fields.title !== clause.title) changes.title = fields.title;
      if (fields.body !== (clause.body ?? null)) changes.body = fields.body;
      if (fields.default_selected !== clause.default_selected) changes.default_selected = fields.default_selected;
      if (fields.is_active !== clause.is_active) changes.is_active = fields.is_active;
      if (fields.sort_order !== clause.sort_order) changes.sort_order = fields.sort_order;
      if (Object.keys(changes).length === 0) {
        showToast('info', `Nothing changed on clause ${clause.code}.`);
        return;
      }
    }
    setSaving(true);
    try {
      const saved = clause
        ? await api.updatePOQualityClause(clause.id, changes)
        : await api.createPOQualityClause({ code: data.code, ...fields });
      markSaved();
      onSaved(saved);
    } catch (err) {
      showToast('error', flowdownErrorDetail(err, 'Could not save the clause. Nothing was changed.'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleCancel}
      size="xl"
      closeOnBackdrop={false}
      closeOnEscape={!saving}
      ariaLabelledBy={titleId}
    >
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
        <div>
          <h2 id={titleId} className="text-lg font-bold text-fd-ink">
            {clause ? `Edit clause ${clause.code}` : 'Add quality clause'}
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            Printed on purchase orders that select it, as the code and title with the clause text under them.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <FormField
            label="Code"
            required={!clause}
            error={errors.code?.message}
            help={clause ? 'The code cannot change after the clause is created.' : undefined}
          >
            {field => (
              <input
                {...field}
                {...register('code')}
                type="text"
                maxLength={20}
                readOnly={!!clause}
                className={
                  errors.code ? 'input input-error w-full font-mono' : 'input w-full font-mono read-only:opacity-70'
                }
              />
            )}
          </FormField>
          <FormField label="Title" required error={errors.title?.message} className="sm:col-span-2">
            {field => (
              <input
                {...field}
                {...register('title')}
                type="text"
                maxLength={255}
                className={errors.title ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
        </div>

        <FormField label="Clause text" error={errors.body?.message}>
          {field => <textarea {...field} {...register('body')} rows={5} className="input w-full" />}
        </FormField>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <FormField label="Sort order" error={errors.sort_order?.message} help="Lower prints first; ties go by code.">
            {field => (
              <input
                {...field}
                {...register('sort_order')}
                type="number"
                step={1}
                min={0}
                max={1000000}
                className={errors.sort_order ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <div className="flex items-center gap-2 sm:pt-6">
            <input
              id={`${titleId}-default`}
              type="checkbox"
              className="rounded border-slate-600"
              {...register('default_selected')}
            />
            <label htmlFor={`${titleId}-default`} className="text-sm text-fd-body">
              Pre-check on new purchase orders
            </label>
          </div>
          <div className="flex items-center gap-2 sm:pt-6">
            <input
              id={`${titleId}-active`}
              type="checkbox"
              className="rounded border-slate-600"
              {...register('is_active')}
            />
            <label htmlFor={`${titleId}-active`} className="text-sm text-fd-body">
              Active (offered on purchase orders)
            </label>
          </div>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={handleCancel} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving…">
            {clause ? 'Save clause' : 'Add clause'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default QualityClauseDialog;
