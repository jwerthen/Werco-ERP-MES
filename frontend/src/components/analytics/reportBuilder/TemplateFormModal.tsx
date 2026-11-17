import React, { useEffect, useId } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Button, FormField, LoadingButton, Modal } from '../../ui';
import {
  REPORT_TEMPLATE_NAME_MAX,
  reportTemplateFormSchema,
  type ReportTemplateFormData,
  type ReportTemplateFormInput,
} from '../../../validation/reportTemplate';

export type TemplateFormMode = 'create' | 'edit';

interface TemplateFormModalProps {
  open: boolean;
  mode: TemplateFormMode;
  initial: { name: string; description: string | null; is_shared: boolean };
  /**
   * Edit only: the template is open in the builder with unsaved changes, so offer to
   * save the builder's definition onto it as well. Hidden otherwise.
   */
  offerDefinition?: boolean;
  pending: boolean;
  onSubmit: (data: ReportTemplateFormData) => void;
  onCancel: () => void;
}

/**
 * "Save as new" and "Edit" for a saved report. Server-gated and non-optimistic: the
 * caller keeps the dialog up while the request is in flight (`pending` blocks every
 * dismissal) and closes it only on success.
 */
export function TemplateFormModal({
  open,
  mode,
  initial,
  offerDefinition = false,
  pending,
  onSubmit,
  onCancel,
}: TemplateFormModalProps) {
  const titleId = useId();
  const defaults: ReportTemplateFormInput = {
    name: initial.name,
    description: initial.description ?? '',
    is_shared: initial.is_shared,
    include_definition: offerDefinition,
  };
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ReportTemplateFormInput, unknown, ReportTemplateFormData>({
    resolver: zodResolver(reportTemplateFormSchema),
    defaultValues: defaults,
  });

  // Re-seed each time the dialog opens, so a reopened dialog starts from the template.
  const seed = JSON.stringify(defaults);
  useEffect(() => {
    if (open) reset(JSON.parse(seed) as ReportTemplateFormInput);
  }, [open, seed, reset]);

  const cancel = () => {
    if (!pending) onCancel();
  };

  return (
    <Modal
      open={open}
      onClose={cancel}
      size="md"
      closeOnBackdrop={!pending}
      closeOnEscape={!pending}
      ariaLabelledBy={titleId}
    >
      <form
        noValidate
        className="space-y-4"
        onSubmit={handleSubmit(data => {
          if (!pending) onSubmit(data);
        })}
      >
        <h2 id={titleId} className="text-lg font-semibold text-fd-ink">
          {mode === 'create' ? 'Save report as new template' : 'Edit report template'}
        </h2>
        <FormField label="Name" required error={errors.name?.message}>
          {field => (
            <input
              {...field}
              {...register('name')}
              type="text"
              maxLength={REPORT_TEMPLATE_NAME_MAX}
              className={errors.name ? 'input-error' : 'input'}
              disabled={pending}
            />
          )}
        </FormField>
        <FormField label="Description" error={errors.description?.message}>
          {field => <textarea {...field} {...register('description')} rows={3} className="input" disabled={pending} />}
        </FormField>
        <label className="flex items-start gap-2 text-sm text-fd-body">
          <input type="checkbox" className="checkbox mt-0.5 h-4 w-4" {...register('is_shared')} disabled={pending} />
          <span>
            Shared
            <span className="block text-xs text-fd-mute">
              Everyone in this company can see and run it; only you or an Admin can change it.
            </span>
          </span>
        </label>
        {mode === 'edit' && offerDefinition && (
          <label className="flex items-start gap-2 text-sm text-fd-body">
            <input
              type="checkbox"
              className="checkbox mt-0.5 h-4 w-4"
              {...register('include_definition')}
              disabled={pending}
            />
            <span>
              Also save the builder&apos;s current columns, filters, grouping and sort
              <span className="block text-xs text-fd-mute">
                Replaces this template&apos;s stored definition with what is in the builder now.
              </span>
            </span>
          </label>
        )}
        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={cancel} disabled={pending}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={pending}>
            {mode === 'create' ? 'Save template' : 'Save changes'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default TemplateFormModal;
