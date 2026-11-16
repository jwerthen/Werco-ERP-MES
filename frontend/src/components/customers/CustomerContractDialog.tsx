/**
 * Add or edit one of a customer's contracts (Customers → Export control, ADMIN).
 *
 * A contract flagged ITAR / export controlled / CUI — or classified
 * `itar_export_controlled` / `cui` — makes its customer EXPORT-CONTROLLED: an
 * AI-external API token then sees that customer's records redacted, from the
 * next request on. The dialog says which way a save will tip before it is made.
 *
 * Every save carries a REQUIRED reason that the server writes to a required
 * audit row (a 503 means nothing was saved). There is no delete: un-marking a
 * customer is an edit that clears the flags, with a reason. Server-gated, so
 * non-optimistic — the dialog stays open on any refusal (the server's `detail`
 * is toasted verbatim) and hands the server's fresh view to `onSaved` only on
 * success. An edit sends only the fields that changed.
 */

import React, { useEffect, useId, useMemo, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { centralWallClockToUtcISO, getCentralDateStamp } from '../../utils/centralTime';
import {
  DATA_CLASSIFICATIONS,
  type CustomerContract,
  type CustomerContractUpdateInput,
  type CustomerExportControl,
  type DataClassification,
} from '../../types/exportControl';
import {
  contractValuesControl,
  customerContractSchema,
  type CustomerContractFormData,
  type CustomerContractFormInput,
} from '../../validation/exportControl';

interface CustomerContractDialogProps {
  open: boolean;
  customerId: number;
  customerName: string;
  /** The contract being edited; `null` adds a new one. */
  contract: CustomerContract | null;
  onCancel: () => void;
  onSaved: (view: CustomerExportControl) => void;
}

const EMPTY_FORM: CustomerContractFormInput = {
  contract_name: '',
  contract_number: '',
  itar_controlled: false,
  export_controlled: false,
  contains_cui: false,
  default_data_classification: 'internal',
  effective_date: '',
  expiration_date: '',
  active: true,
  handling_instructions: '',
  reason: '',
};

const KNOWN_CLASSIFICATIONS = new Set<string>(DATA_CLASSIFICATIONS.map(option => option.value));

function formFromContract(contract: CustomerContract): CustomerContractFormInput {
  const classification = KNOWN_CLASSIFICATIONS.has(contract.default_data_classification)
    ? (contract.default_data_classification as DataClassification)
    : 'unknown';
  return {
    contract_name: contract.contract_name,
    contract_number: contract.contract_number ?? '',
    itar_controlled: contract.itar_controlled,
    export_controlled: contract.export_controlled,
    contains_cui: contract.contains_cui,
    default_data_classification: classification,
    effective_date: contract.effective_date ? getCentralDateStamp(contract.effective_date) : '',
    expiration_date: contract.expiration_date ? getCentralDateStamp(contract.expiration_date) : '',
    active: contract.active,
    handling_instructions: contract.handling_instructions ?? '',
    reason: '',
  };
}

/** A Central calendar day → the UTC instant of its midnight (the column stores an instant). */
function centralDayToUtc(day: string): string | null {
  return day ? centralWallClockToUtcISO(`${day}T00:00`) : null;
}

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}

/** Only the fields an edit actually changed — the server writes nothing for the rest. */
function changedFields(
  data: CustomerContractFormData,
  initial: CustomerContractFormInput
): Omit<CustomerContractUpdateInput, 'reason'> {
  const changes: Omit<CustomerContractUpdateInput, 'reason'> = {};
  if (data.contract_name !== initial.contract_name.trim()) changes.contract_name = data.contract_name;
  if (data.contract_number !== initial.contract_number.trim()) changes.contract_number = data.contract_number || null;
  if (data.itar_controlled !== initial.itar_controlled) changes.itar_controlled = data.itar_controlled;
  if (data.export_controlled !== initial.export_controlled) changes.export_controlled = data.export_controlled;
  if (data.contains_cui !== initial.contains_cui) changes.contains_cui = data.contains_cui;
  if (data.default_data_classification !== initial.default_data_classification) {
    changes.default_data_classification = data.default_data_classification;
  }
  if (data.effective_date !== initial.effective_date) changes.effective_date = centralDayToUtc(data.effective_date);
  if (data.expiration_date !== initial.expiration_date) {
    changes.expiration_date = centralDayToUtc(data.expiration_date);
  }
  if (data.active !== initial.active) changes.active = data.active;
  if (data.handling_instructions.trim() !== initial.handling_instructions.trim()) {
    changes.handling_instructions = data.handling_instructions.trim() || null;
  }
  return changes;
}

export default function CustomerContractDialog({
  open,
  customerId,
  customerName,
  contract,
  onCancel,
  onSaved,
}: CustomerContractDialogProps) {
  const titleId = useId();
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const initial = useMemo(() => (contract ? formFromContract(contract) : EMPTY_FORM), [contract]);

  const {
    register,
    handleSubmit,
    reset,
    watch,
    formState: { errors, isDirty },
  } = useForm<CustomerContractFormInput, unknown, CustomerContractFormData>({
    resolver: zodResolver(customerContractSchema),
    defaultValues: initial,
  });

  useEffect(() => {
    if (open) reset(initial);
  }, [open, initial, reset]);

  const { confirmDiscard } = useUnsavedChanges(open && isDirty);

  const handleCancel = () => {
    if (saving) return;
    if (confirmDiscard()) onCancel();
  };

  const willControl = contractValuesControl({
    itar_controlled: watch('itar_controlled'),
    export_controlled: watch('export_controlled'),
    contains_cui: watch('contains_cui'),
    default_data_classification: watch('default_data_classification'),
  });
  const wasControlling = contract?.controlling ?? false;

  const onSubmit = async (data: CustomerContractFormData) => {
    if (saving) return;
    let request: Promise<CustomerExportControl>;
    if (contract) {
      const changes = changedFields(data, initial);
      if (Object.keys(changes).length === 0) {
        showToast('info', 'No contract field changed, so there is nothing to save.');
        return;
      }
      request = api.updateCustomerContract(customerId, contract.id, { ...changes, reason: data.reason });
    } else {
      request = api.createCustomerContract(customerId, {
        contract_name: data.contract_name,
        contract_number: data.contract_number || null,
        itar_controlled: data.itar_controlled,
        export_controlled: data.export_controlled,
        contains_cui: data.contains_cui,
        default_data_classification: data.default_data_classification,
        effective_date: centralDayToUtc(data.effective_date),
        expiration_date: centralDayToUtc(data.expiration_date),
        active: data.active,
        handling_instructions: data.handling_instructions.trim() || null,
        reason: data.reason,
      });
    }
    setSaving(true);
    try {
      const view = await request;
      onSaved(view);
    } catch (err) {
      showToast('error', errorDetail(err, 'Could not save the contract. Nothing was changed.'));
    } finally {
      setSaving(false);
    }
  };

  const flag = (name: 'itar_controlled' | 'export_controlled' | 'contains_cui', label: string) => (
    <div className="flex items-center gap-2">
      <input id={`${titleId}-${name}`} type="checkbox" className="checkbox checkbox-sm" {...register(name)} />
      <label htmlFor={`${titleId}-${name}`} className="text-sm text-fd-body">
        {label}
      </label>
    </div>
  );

  return (
    <Modal
      open={open}
      onClose={handleCancel}
      size="2xl"
      closeOnBackdrop={false}
      closeOnEscape={!saving}
      ariaLabelledBy={titleId}
    >
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
        <div>
          <h2 id={titleId} className="text-lg font-bold text-fd-ink">
            {contract ? 'Edit contract' : 'Add contract'}
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            {customerName}. Any contract that is ITAR, export controlled or CUI — active or not, whatever its dates —
            makes the customer export-controlled.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <FormField label="Contract name" required error={errors.contract_name?.message}>
            {field => (
              <input
                {...field}
                {...register('contract_name')}
                type="text"
                maxLength={255}
                className={errors.contract_name ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <FormField label="Contract number" error={errors.contract_number?.message}>
            {field => (
              <input
                {...field}
                {...register('contract_number')}
                type="text"
                maxLength={120}
                className={errors.contract_number ? 'input input-error w-full font-mono' : 'input w-full font-mono'}
              />
            )}
          </FormField>
        </div>

        <fieldset className="rounded-sm border border-fd-line bg-fd-panel p-3">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-fd-mute">Export control</legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            {flag('itar_controlled', 'ITAR controlled')}
            {flag('export_controlled', 'Export controlled')}
            {flag('contains_cui', 'Contains CUI')}
          </div>
          <FormField label="Data classification" className="mt-3">
            {field => (
              <select {...field} {...register('default_data_classification')} className="input w-full sm:w-72">
                {DATA_CLASSIFICATIONS.map(option => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <p
            className={`mt-3 text-xs ${willControl ? 'text-fd-red' : 'text-fd-mute'}`}
            aria-live="polite"
            data-testid="contract-control-preview"
          >
            {willControl
              ? `This contract makes ${customerName} export-controlled: AI-external API tokens see its records redacted from the next request on.`
              : wasControlling
                ? `This contract will no longer control ${customerName}. The customer stays controlled if another contract or a classified handling instruction controls it.`
                : 'This contract does not make the customer export-controlled.'}
          </p>
        </fieldset>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <FormField label="Effective date" error={errors.effective_date?.message}>
            {field => <input {...field} {...register('effective_date')} type="date" className="input w-full" />}
          </FormField>
          <FormField label="Expiration date" error={errors.expiration_date?.message}>
            {field => (
              <input
                {...field}
                {...register('expiration_date')}
                type="date"
                className={errors.expiration_date ? 'input input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <div className="flex items-center gap-2 sm:pt-6">
            <input id={`${titleId}-active`} type="checkbox" className="checkbox checkbox-sm" {...register('active')} />
            <label htmlFor={`${titleId}-active`} className="text-sm text-fd-body">
              Active
            </label>
          </div>
        </div>

        <FormField label="Handling instructions" error={errors.handling_instructions?.message}>
          {field => <textarea {...field} {...register('handling_instructions')} rows={3} className="input w-full" />}
        </FormField>

        <FormField
          label="Reason for this change"
          required
          error={errors.reason?.message}
          help="Recorded on the audit trail with the change."
        >
          {field => (
            <textarea
              {...field}
              {...register('reason')}
              rows={2}
              maxLength={1000}
              className={errors.reason ? 'input input-error w-full' : 'input w-full'}
            />
          )}
        </FormField>

        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={handleCancel} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={saving}>
            {contract ? 'Save contract' : 'Add contract'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
