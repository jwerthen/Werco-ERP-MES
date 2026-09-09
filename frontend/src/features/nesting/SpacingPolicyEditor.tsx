import React from 'react';
import { useFieldArray, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { AUTO_QUOTING_SPACING_POLICY_INCHES } from './lib/spacing';
import { normalizePolicyDecimal, validateSpacingContent, type SpacingPolicyContent } from './lib/spacing-policy';

const decimal = z.string().transform((value, ctx) => {
  try {
    return normalizePolicyDecimal(value);
  } catch (error) {
    ctx.addIssue({ code: z.ZodIssueCode.custom, message: (error as Error).message });
    return z.NEVER;
  }
});
const bandSchema = z.object({
  id: z.string(),
  material: z.enum(['Carbon steel', 'Stainless steel', 'Aluminum']),
  thickness_min_in: decimal,
  thickness_max_in: decimal,
  minimum_gap_in: decimal,
  gap_thickness_multiplier: decimal,
  minimum_margin_in: decimal,
  margin_thickness_multiplier: decimal,
});
const schema = z
  .object({
    name: z.string().trim().min(1).max(199),
    reason: z.string().trim().min(1, 'Enter a change reason.').max(1000),
    bands: z.array(bandSchema).min(1, 'Add at least one thickness band.').max(128),
  })
  .superRefine((values, ctx) => {
    try {
      validateSpacingContent({ schema_version: 1, units: 'in', name: values.name, bands: values.bands });
    } catch (error) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['bands'], message: (error as Error).message });
    }
  });
const fields = [
  ['thickness_min_in', 'Thickness from (in, inclusive)'],
  ['thickness_max_in', 'Thickness to (in, exclusive)'],
  ['minimum_gap_in', 'Minimum gap (in)'],
  ['gap_thickness_multiplier', 'Gap × thickness'],
  ['minimum_margin_in', 'Minimum edge margin (in)'],
  ['margin_thickness_multiplier', 'Margin × thickness'],
] as const;

export default function SpacingPolicyEditor({
  initial,
  disabled,
  onSave,
  onCancel,
}: {
  initial: SpacingPolicyContent | null;
  disabled: boolean;
  onSave: (content: SpacingPolicyContent, reason: string) => void;
  onCancel: () => void;
}) {
  const form = useForm<z.input<typeof schema>, unknown, z.output<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: initial?.name ?? 'Shop quoting spacing',
      reason: '',
      bands: initial?.bands.map(band => ({ ...band })) ?? [],
    },
  });
  const {
    fields: bands,
    append,
    remove,
    replace,
  } = useFieldArray({ control: form.control, name: 'bands', keyName: 'formKey' });
  return (
    <form
      className="spacing-policy-editor"
      onSubmit={form.handleSubmit(values =>
        onSave({ schema_version: 1, units: 'in', name: values.name, bands: values.bands }, values.reason)
      )}
    >
      <h3>{initial ? 'Create a revised policy draft' : 'New policy draft'}</h3>
      <p className="helper inset-free">
        Saving creates an immutable draft revision. Approval is a separate explicit action. All measurements are inches.
      </p>
      <fieldset disabled={disabled}>
        <label className="field-label">
          Policy name
          <input {...form.register('name')} maxLength={199} />
        </label>
        <div className="team-draft-actions">
          <button
            className="secondary compact"
            type="button"
            disabled={bands.length >= 128}
            onClick={() =>
              append({
                id: crypto.randomUUID(),
                material: 'Carbon steel',
                thickness_min_in: '',
                thickness_max_in: '',
                minimum_gap_in: '',
                gap_thickness_multiplier: '',
                minimum_margin_in: '',
                margin_thickness_multiplier: '',
              })
            }
          >
            Add thickness band
          </button>
          <button
            className="secondary compact"
            type="button"
            onClick={() => {
              const p = AUTO_QUOTING_SPACING_POLICY_INCHES;
              replace(
                (['Carbon steel', 'Stainless steel', 'Aluminum'] as const).map(material => ({
                  id: crypto.randomUUID(),
                  material,
                  thickness_min_in: '0',
                  thickness_max_in: '4',
                  minimum_gap_in: String(p.minimumGap),
                  gap_thickness_multiplier: String(p.gapThicknessMultiplier),
                  minimum_margin_in: String(p.minimumMargin),
                  margin_thickness_multiplier: String(p.marginThicknessMultiplier),
                }))
              );
            }}
          >
            Copy starting allowances into this draft
          </button>
        </div>
        <p className="helper inset-free">
          Copied starting values are unapproved examples. Confirm your shop's quoting allowances before approving a
          revision.
        </p>
        {bands.map((band, index) => (
          <fieldset className="policy-band" key={band.formKey}>
            <legend>Band {index + 1}</legend>
            <label className="field-label">
              Material family
              <select {...form.register(`bands.${index}.material`)}>
                {['Carbon steel', 'Stainless steel', 'Aluminum'].map(material => (
                  <option key={material}>{material}</option>
                ))}
              </select>
            </label>
            <div className="policy-band-fields">
              {fields.map(([key, label]) => (
                <label className="field-label" key={key}>
                  {label}
                  <input inputMode="decimal" {...form.register(`bands.${index}.${key}`)} />
                  {form.formState.errors.bands?.[index]?.[key] && (
                    <span role="alert">{form.formState.errors.bands[index]?.[key]?.message}</span>
                  )}
                </label>
              ))}
            </div>
            <button type="button" className="secondary compact" onClick={() => remove(index)}>
              Remove band {index + 1}
            </button>
          </fieldset>
        ))}
        {form.formState.errors.bands?.message && <p role="alert">{form.formState.errors.bands.message}</p>}
        <label className="field-label">
          Change reason
          <textarea rows={3} maxLength={1000} {...form.register('reason')} />
        </label>
        {form.formState.errors.name && <p role="alert">{form.formState.errors.name.message}</p>}
        {form.formState.errors.reason && <p role="alert">{form.formState.errors.reason.message}</p>}
        <div className="team-draft-actions">
          <button className="primary" type="submit">
            Save policy draft
          </button>
          <button className="secondary" type="button" onClick={onCancel}>
            Back to history
          </button>
        </div>
      </fieldset>
    </form>
  );
}
