/**
 * Issue an API token (Admin → API Tokens).
 *
 * The token acts AS the chosen user — it carries exactly that user's role — and
 * its scope can only narrow that:
 *   - `read_only` (the default here): GET routes only, and never a route under a
 *     tag in WERCO_MCP_READ_ONLY_DENY_TAGS. The AI-external box defaults ON for
 *     it, because the expected holder is an outside AI connector.
 *   - `full`: everything the user's role allows. AI-external is forced OFF and
 *     disabled — the server refuses that pairing (422), so the form never offers it.
 * Scope and AI-external are fixed at mint (revoke and re-mint to change them).
 *
 * Server-gated, so non-optimistic: the dialog stays up while the request is in
 * flight and on any refusal (the server's `detail` is toasted verbatim), and
 * hands the issued token to `onIssued` only on success. The token is passed up
 * in memory for the one-time reveal — this component never logs or stores it.
 */

import React, { useEffect, useId, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, ComboBox, FormField, LoadingButton, Modal, useToast } from '../ui';
import type { ComboBoxOption } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { ApiTokenIssued } from '../../types/apiToken';
import { MAX_API_TOKEN_EXPIRES_DAYS } from '../../types/apiToken';
import {
  apiTokenIssueSchema,
  type ApiTokenIssueFormData,
  type ApiTokenIssueFormInput,
} from '../../validation/apiToken';

const EMPTY_FORM: ApiTokenIssueFormInput = {
  user_id: '',
  label: '',
  scope: 'read_only',
  ai_external: true,
  expires_days: '',
};

interface ApiTokenIssueDialogProps {
  open: boolean;
  /** Users a token may be bound to (active, not a platform principal). */
  userOptions: ComboBoxOption[];
  /** `false` when the company's AI-egress switch is off; `null` when unknown. */
  aiEgressAllowed: boolean | null;
  onCancel: () => void;
  onIssued: (issued: ApiTokenIssued) => void;
}

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}

export default function ApiTokenIssueDialog({
  open,
  userOptions,
  aiEgressAllowed,
  onCancel,
  onIssued,
}: ApiTokenIssueDialogProps) {
  const titleId = useId();
  const { showToast } = useToast();
  const [submitting, setSubmitting] = useState(false);

  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    setValue,
    formState: { errors, isDirty },
  } = useForm<ApiTokenIssueFormInput, unknown, ApiTokenIssueFormData>({
    resolver: zodResolver(apiTokenIssueSchema),
    defaultValues: EMPTY_FORM,
  });

  useEffect(() => {
    if (open) reset(EMPTY_FORM);
  }, [open, reset]);

  const scope = watch('scope');
  const aiExternal = watch('ai_external');
  const readOnly = scope === 'read_only';

  const { confirmDiscard } = useUnsavedChanges(open && isDirty);

  const handleCancel = () => {
    if (submitting) return;
    if (confirmDiscard()) onCancel();
  };

  const onSubmit = async (data: ApiTokenIssueFormData) => {
    if (submitting) return;
    setSubmitting(true);
    try {
      const issued = await api.createApiToken({
        user_id: data.user_id,
        label: data.label,
        scope: data.scope,
        ai_external: data.ai_external,
        ...(data.expires_days !== null ? { expires_days: data.expires_days } : {}),
      });
      reset(EMPTY_FORM);
      onIssued(issued);
    } catch (err) {
      showToast('error', errorDetail(err, 'Could not issue the API token.'));
    } finally {
      setSubmitting(false);
    }
  };

  const scopeField = register('scope');

  return (
    <Modal
      open={open}
      onClose={handleCancel}
      size="xl"
      closeOnBackdrop={false}
      closeOnEscape={!submitting}
      ariaLabelledBy={titleId}
    >
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
        <div>
          <h2 id={titleId} className="text-lg font-bold text-fd-ink">
            Issue API token
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            The token acts as the chosen user, with exactly that user&apos;s role. A dedicated bot user is recommended.
            Issuance is audited; the token itself is shown once and never stored.
          </p>
        </div>

        <FormField label="Acts as user" required error={errors.user_id?.message}>
          {field => (
            <Controller
              name="user_id"
              control={control}
              render={({ field: controlled }) => (
                <ComboBox
                  id={field.id}
                  ariaDescribedBy={field['aria-describedby']}
                  options={userOptions}
                  value={controlled.value}
                  onChange={controlled.onChange}
                  placeholder="Search users…"
                  noResultsLabel="No matching active user"
                  className="py-2"
                  disabled={submitting}
                />
              )}
            />
          )}
        </FormField>

        <FormField
          label="Label"
          required
          error={errors.label?.message}
          help="Who holds it, e.g. “Werco Assistant – Claude connector”."
        >
          {field => (
            <input
              {...field}
              {...register('label')}
              type="text"
              maxLength={100}
              className={errors.label ? 'input input-error w-full' : 'input w-full'}
            />
          )}
        </FormField>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <FormField
            label="Scope"
            required
            help={
              readOnly
                ? 'GET routes only; denied resources (users, documents, audit, settings…) are refused.'
                : 'Everything the user’s role allows, including writes.'
            }
          >
            {field => (
              <select
                {...field}
                {...scopeField}
                className="input w-full"
                onChange={event => {
                  void scopeField.onChange(event);
                  // Full can never be AI-external; read-only defaults back to it.
                  setValue('ai_external', event.target.value === 'read_only', { shouldDirty: true });
                }}
              >
                <option value="read_only">Read-only</option>
                <option value="full">Full</option>
              </select>
            )}
          </FormField>

          <FormField
            label="Expires after (days)"
            error={errors.expires_days?.message}
            help={`Blank = never expires. 1 to ${MAX_API_TOKEN_EXPIRES_DAYS}.`}
          >
            {field => (
              <input
                {...field}
                {...register('expires_days')}
                type="text"
                inputMode="numeric"
                placeholder="Never"
                className={errors.expires_days ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
        </div>

        <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
          <div className="flex items-start gap-2.5">
            {/* Controller, not register: a natively disabled registered input
                submits as undefined, and Full disables this box. */}
            <Controller
              name="ai_external"
              control={control}
              render={({ field }) => (
                <input
                  id={`${titleId}-ai-external`}
                  type="checkbox"
                  className="checkbox checkbox-sm mt-0.5"
                  disabled={!readOnly || submitting}
                  aria-describedby={`${titleId}-ai-external-help`}
                  name={field.name}
                  ref={field.ref}
                  checked={readOnly && field.value}
                  onBlur={field.onBlur}
                  onChange={event => field.onChange(event.target.checked)}
                />
              )}
            />
            <div className="text-sm">
              <label htmlFor={`${titleId}-ai-external`} className="font-semibold text-fd-ink">
                AI-external
              </label>
              <p id={`${titleId}-ai-external-help`} className="mt-0.5 text-xs text-fd-mute">
                {readOnly
                  ? 'Its answers go to an outside AI service. Only allowlisted reads are served, records of export-controlled or unlinkable customers are redacted, and every redaction is logged.'
                  : 'Only a read-only token can be AI-external.'}
              </p>
              {errors.ai_external?.message && (
                <p role="alert" className="mt-1 text-xs text-fd-red">
                  {errors.ai_external.message}
                </p>
              )}
            </div>
          </div>
          {readOnly && aiExternal && aiEgressAllowed === false && (
            <div
              className="mt-3 flex items-start gap-2 rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-2 text-xs text-fd-body"
              data-testid="issue-egress-warning"
            >
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 flex-shrink-0 text-fd-amber" aria-hidden="true" />
              <span>
                AI egress is off for this company, so this token will be refused (403) until an Admin enables it under
                Admin Settings → AI Privacy.
              </span>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={handleCancel} disabled={submitting}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={submitting}>
            Issue token
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
