/**
 * The fields of Werco's controlled CAR form ("AS9100 PROCESS - Corrective Action
 * Report") that the ERP's CAR record did not carry before migration 122: reason for
 * initiation, RMA #, Date Submitted, the part / customer / vendor it applies to, the
 * vendor PO, the four cost buckets, management action and the vendor / customer
 * approvals. Shared by the New CAR modal and the CAR detail so both send the same
 * payload shape.
 *
 * Three things the server owns, so this form never sends them:
 * - the customer NAME (snapshotted from the customer record when the customer changes);
 * - the cost TOTAL (computed at read time, never stored) — the total shown here is the
 *   sum of what is typed, for the operator's convenience only;
 * - the Quality Manager approval (its own audited verb, server-stamped).
 *
 * References: a NEW part or customer must be a live record of the company (the pickers
 * offer only those); a vendor may be inactive, because a supplier CAR can be the record
 * of the supplier's removal. A record's CURRENT reference is pinned into its picker so a
 * since-retired part or vendor still reads as what the CAR names — the server does not
 * re-check an unchanged reference.
 */
import React, { useMemo } from 'react';
import EntityPicker from '../operations/EntityPicker';
import { ComboBox, FormField } from '../ui';
import { CAR_INITIATION_REASONS, type CARRecord } from '../../types/quality';

export interface CarFormValues {
  initiation_reason: string;
  initiation_reason_other: string;
  rma_number: string;
  submitted_date: string;
  part_id: string;
  customer_id: string;
  vendor_id: string;
  vendor_po_number: string;
  cost_labor: string;
  cost_material: string;
  cost_outside_services: string;
  cost_shipping: string;
  management_action: string;
  vendor_approval_name: string;
  vendor_approval_title: string;
  vendor_approval_date: string;
  customer_approval_name: string;
  customer_approval_date: string;
}

export const BLANK_CAR_FORM_VALUES: CarFormValues = {
  initiation_reason: '',
  initiation_reason_other: '',
  rma_number: '',
  submitted_date: '',
  part_id: '',
  customer_id: '',
  vendor_id: '',
  vendor_po_number: '',
  cost_labor: '',
  cost_material: '',
  cost_outside_services: '',
  cost_shipping: '',
  management_action: '',
  vendor_approval_name: '',
  vendor_approval_title: '',
  vendor_approval_date: '',
  customer_approval_name: '',
  customer_approval_date: '',
};

const text = (value: unknown): string => (value === null || value === undefined ? '' : String(value));

export function carFormValuesFromRecord(record: Partial<CARRecord>): CarFormValues {
  return {
    initiation_reason: text(record.initiation_reason),
    initiation_reason_other: text(record.initiation_reason_other),
    rma_number: text(record.rma_number),
    submitted_date: text(record.submitted_date),
    part_id: text(record.part_id),
    customer_id: text(record.customer_id),
    vendor_id: text(record.vendor_id),
    vendor_po_number: text(record.vendor_po_number),
    cost_labor: text(record.cost_labor),
    cost_material: text(record.cost_material),
    cost_outside_services: text(record.cost_outside_services),
    cost_shipping: text(record.cost_shipping),
    management_action: text(record.management_action),
    vendor_approval_name: text(record.vendor_approval_name),
    vendor_approval_title: text(record.vendor_approval_title),
    vendor_approval_date: text(record.vendor_approval_date),
    customer_approval_name: text(record.customer_approval_name),
    customer_approval_date: text(record.customer_approval_date),
  };
}

const ID_FIELDS: ReadonlyArray<keyof CarFormValues> = ['part_id', 'customer_id', 'vendor_id'];
const TRIMMED_FIELDS: ReadonlyArray<keyof CarFormValues> = [
  'cost_labor',
  'cost_material',
  'cost_outside_services',
  'cost_shipping',
  'submitted_date',
  'vendor_approval_date',
  'customer_approval_date',
];

/**
 * The request body for these fields: an empty field becomes null (the columns are
 * nullable and the server treats `''` and null as the same value), ids become numbers,
 * and money stays the typed decimal string so the server's 2-place Decimal sees exactly
 * what was entered. Free text is sent as typed, never trimmed: the server compares each
 * field with what it holds, and a silently trimmed value would read as an edit (which a
 * closed CAR refuses).
 */
export function carFormPayload(values: CarFormValues): Record<string, string | number | null> {
  const payload: Record<string, string | number | null> = {};
  for (const [key, raw] of Object.entries(values) as Array<[keyof CarFormValues, string]>) {
    const value = TRIMMED_FIELDS.includes(key) || ID_FIELDS.includes(key) ? raw.trim() : raw;
    if (value === '') payload[key] = null;
    else if (ID_FIELDS.includes(key)) payload[key] = Number(value);
    else payload[key] = value;
  }
  return payload;
}

const COST_FIELDS: Array<{ key: keyof CarFormValues; label: string }> = [
  { key: 'cost_labor', label: 'Labor' },
  { key: 'cost_material', label: 'Material' },
  { key: 'cost_outside_services', label: 'Outside services' },
  { key: 'cost_shipping', label: 'Shipping / transportation' },
];

/** The typed costs' sum, in cents to avoid float drift; null when nothing is entered. */
export function enteredCostTotal(values: CarFormValues): number | null {
  let cents = 0;
  let any = false;
  for (const { key } of COST_FIELDS) {
    const amount = Number.parseFloat(values[key]);
    if (values[key].trim() && Number.isFinite(amount)) {
      cents += Math.round(amount * 100);
      any = true;
    }
  }
  return any ? cents / 100 : null;
}

export const formatMoney = (value: number | string | null | undefined): string => {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  return Number.isFinite(amount)
    ? `$${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : '—';
};

export type CarFormSection = 'initiation' | 'appliesTo' | 'cost' | 'approvals';
const ALL_SECTIONS: ReadonlyArray<CarFormSection> = ['initiation', 'appliesTo', 'cost', 'approvals'];

interface CarFormFieldsProps {
  values: CarFormValues;
  onChange: (patch: Partial<CarFormValues>) => void;
  /** Which blocks to render (all by default), so a caller can place them in the form's order. */
  sections?: ReadonlyArray<CarFormSection>;
  /**
   * The record being edited, so its current part / customer / vendor stay selectable
   * (and readable) even when the lookups no longer return them.
   */
  record?: Partial<CARRecord> | null;
  /** Parts already loaded by the caller; without it the part picker loads its own. */
  parts?: Array<{ id: number; part_number: string; name?: string }>;
}

const legendClass = 'px-1 text-xs font-semibold uppercase tracking-wider text-slate-400';
const fieldsetClass = 'min-w-0 border border-fd-line p-3 space-y-3';

export default function CarFormFields({
  values,
  onChange,
  sections = ALL_SECTIONS,
  record,
  parts,
}: CarFormFieldsProps) {
  const set =
    (key: keyof CarFormValues) =>
    (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      onChange({ [key]: event.target.value });

  const partOptions = useMemo(() => {
    const options = (parts ?? []).map(part => ({
      value: String(part.id),
      label: part.name ? `${part.part_number} — ${part.name}` : part.part_number,
    }));
    const current = record?.part_id ? String(record.part_id) : '';
    if (current && !options.some(option => option.value === current)) {
      options.unshift({ value: current, label: pinnedPartLabel(record) });
    }
    return options;
  }, [parts, record]);

  const total = enteredCostTotal(values);

  const show = (section: CarFormSection) => sections.includes(section);

  return (
    <div className="space-y-4">
      {show('initiation') && (
        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Initiation</legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FormField label="Reason for initiation">
              {field => (
                <select
                  {...field}
                  className="input"
                  value={values.initiation_reason}
                  onChange={event =>
                    onChange({
                      initiation_reason: event.target.value,
                      // The Other box's blank goes with the box.
                      ...(event.target.value === 'other' ? {} : { initiation_reason_other: '' }),
                    })
                  }
                >
                  <option value="">Not recorded</option>
                  {CAR_INITIATION_REASONS.map(reason => (
                    <option key={reason.value} value={reason.value}>
                      {reason.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {values.initiation_reason === 'other' && (
              <FormField label="Other reason" required>
                {field => (
                  <input
                    {...field}
                    className="input"
                    maxLength={255}
                    required
                    value={values.initiation_reason_other}
                    onChange={set('initiation_reason_other')}
                  />
                )}
              </FormField>
            )}
            <FormField label="RMA #">
              {field => (
                <input
                  {...field}
                  className="input"
                  maxLength={50}
                  value={values.rma_number}
                  onChange={set('rma_number')}
                />
              )}
            </FormField>
            <FormField
              label="Date submitted"
              help={
                record ? undefined : 'Blank records today (Central). Enter an earlier date to back-enter a paper CAR.'
              }
            >
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={values.submitted_date}
                  onChange={set('submitted_date')}
                />
              )}
            </FormField>
          </div>
        </fieldset>
      )}

      {show('appliesTo') && (
        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Applies to</legend>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <FormField label="Part number (if applicable)">
              {field =>
                parts ? (
                  <ComboBox
                    id={field.id}
                    ariaDescribedBy={field['aria-describedby']}
                    options={partOptions}
                    value={values.part_id}
                    onChange={value => onChange({ part_id: value })}
                    emptyOptionLabel="None"
                    placeholder="Search by part number or name…"
                    noResultsLabel="No matching parts"
                  />
                ) : (
                  <EntityPicker
                    kind="part"
                    optional
                    id={field.id}
                    aria-describedby={field['aria-describedby']}
                    value={values.part_id}
                    onChange={value => onChange({ part_id: value })}
                    pinned={record?.part_id ? { value: String(record.part_id), label: pinnedPartLabel(record) } : null}
                  />
                )
              }
            </FormField>
            <FormField label="Customer">
              {field => (
                <EntityPicker
                  kind="customer"
                  optional
                  id={field.id}
                  aria-describedby={field['aria-describedby']}
                  value={values.customer_id}
                  onChange={value => onChange({ customer_id: value })}
                  pinned={
                    record?.customer_id
                      ? {
                          value: String(record.customer_id),
                          label: record.customer_name || `Customer #${record.customer_id}`,
                        }
                      : null
                  }
                />
              )}
            </FormField>
            <FormField label="Vendor" help="Inactive vendors are listed: a supplier CAR can document the removal.">
              {field => (
                <EntityPicker
                  kind="vendor"
                  optional
                  includeInactive
                  id={field.id}
                  aria-describedby={field['aria-describedby']}
                  value={values.vendor_id}
                  onChange={value => onChange({ vendor_id: value })}
                  pinned={
                    record?.vendor_id
                      ? { value: String(record.vendor_id), label: record.vendor_name || `Vendor #${record.vendor_id}` }
                      : null
                  }
                />
              )}
            </FormField>
            <FormField label="Vendor P.O. number">
              {field => (
                <input
                  {...field}
                  className="input"
                  maxLength={100}
                  value={values.vendor_po_number}
                  onChange={set('vendor_po_number')}
                />
              )}
            </FormField>
          </div>
        </fieldset>
      )}

      {show('cost') && (
        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Cost of the nonconformance</legend>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            {COST_FIELDS.map(({ key, label }) => (
              <FormField key={key} label={`${label} cost`}>
                {field => (
                  <input
                    {...field}
                    type="number"
                    min="0"
                    step="0.01"
                    inputMode="decimal"
                    className="input tabular-nums"
                    value={values[key]}
                    onChange={set(key)}
                  />
                )}
              </FormField>
            ))}
            <div>
              <p className="label">Total</p>
              <p className="py-2 font-mono tabular-nums text-slate-100" data-testid="car-cost-total">
                {formatMoney(total)}
              </p>
            </div>
          </div>
        </fieldset>
      )}

      {show('approvals') && (
        <fieldset className={fieldsetClass}>
          <legend className={legendClass}>Management action and approvals</legend>
          <FormField label="Management action">
            {field => (
              <textarea
                {...field}
                className="input"
                rows={2}
                maxLength={2000}
                value={values.management_action}
                onChange={set('management_action')}
              />
            )}
          </FormField>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <FormField label="Vendor approval name">
              {field => (
                <input
                  {...field}
                  className="input"
                  maxLength={255}
                  value={values.vendor_approval_name}
                  onChange={set('vendor_approval_name')}
                />
              )}
            </FormField>
            <FormField label="Vendor approval title">
              {field => (
                <input
                  {...field}
                  className="input"
                  maxLength={255}
                  value={values.vendor_approval_title}
                  onChange={set('vendor_approval_title')}
                />
              )}
            </FormField>
            <FormField label="Vendor approval date">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={values.vendor_approval_date}
                  onChange={set('vendor_approval_date')}
                />
              )}
            </FormField>
            <FormField label="Customer approval name">
              {field => (
                <input
                  {...field}
                  className="input"
                  maxLength={255}
                  value={values.customer_approval_name}
                  onChange={set('customer_approval_name')}
                />
              )}
            </FormField>
            <FormField label="Customer approval date">
              {field => (
                <input
                  {...field}
                  type="date"
                  className="input"
                  value={values.customer_approval_date}
                  onChange={set('customer_approval_date')}
                />
              )}
            </FormField>
          </div>
        </fieldset>
      )}
    </div>
  );
}

function pinnedPartLabel(record?: Partial<CARRecord> | null): string {
  if (!record?.part_id) return '';
  if (!record.part_number) return `Part #${record.part_id}`;
  return record.part_name ? `${record.part_number} — ${record.part_name}` : record.part_number;
}
