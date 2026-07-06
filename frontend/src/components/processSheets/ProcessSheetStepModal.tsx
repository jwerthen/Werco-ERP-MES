import React, { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { LoadingButton, useToast } from '../ui';
import {
  processSheetStepSchema,
  ProcessSheetStepFormData,
  ProcessSheetStepFormInput,
  PROCESS_SHEET_STEP_TYPES,
  parseListOptions,
} from '../../validation/schemas';
import {
  ProcessSheetStep,
  ProcessSheetStepConfig,
  ProcessSheetStepInput,
} from '../../types/processSheet';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import api from '../../services/api';

const STEP_TYPE_LABELS: Record<(typeof PROCESS_SHEET_STEP_TYPES)[number], string> = {
  measurement: 'Measurement',
  checkbox: 'Checkbox',
  list: 'List selection',
  value: 'Value entry',
  photo: 'Photo evidence',
  file: 'File attachment',
  instruction: 'Instruction (display only)',
};

interface SPCCharacteristicOption {
  id: number;
  name: string;
  unit_of_measure?: string | null;
}

interface ProcessSheetStepModalProps {
  open: boolean;
  onClose: () => void;
  /** Sheet the step belongs to (must be DRAFT — the server 409s otherwise). */
  sheetId: number;
  /** When set, the modal edits this step (PATCH) instead of creating. */
  step?: ProcessSheetStep | null;
  /** Pre-seeded sequence for a new step (max existing + 10). */
  defaultSequence: number;
  /** Called after a successful save so the parent can refresh the sheet. */
  onSaved: () => void;
}

/**
 * Build the API payload from the validated form data, mirroring the backend's
 * per-type invariants: INSTRUCTION is never required; requires_gauge and
 * spc_characteristic_id are MEASUREMENT-only; config shape swaps by type.
 * The full field set is sent on update too — null only ever lands on the
 * NULLABLE columns (instruction_text / config / spc_characteristic_id), which
 * the backend accepts as an explicit clear.
 */
export function buildStepPayload(data: ProcessSheetStepFormData): ProcessSheetStepInput {
  let config: ProcessSheetStepConfig | null = null;
  if (data.step_type === 'measurement') {
    config = {
      nominal: Number((data.nominal ?? '').trim()),
      lsl: Number((data.lsl ?? '').trim()),
      usl: Number((data.usl ?? '').trim()),
      unit: (data.unit ?? '').trim(),
    };
    const decimalsRaw = (data.decimals ?? '').trim();
    if (decimalsRaw) config.decimals = Number(decimalsRaw);
  } else if (data.step_type === 'list') {
    config = { options: parseListOptions(data.options_text ?? '') };
  } else if (data.step_type === 'photo' || data.step_type === 'file') {
    const hint = (data.hint ?? '').trim();
    config = hint ? { hint } : null;
  }

  const instructionText = (data.instruction_text ?? '').trim();
  const isMeasurement = data.step_type === 'measurement';

  return {
    sequence: data.sequence,
    label: data.label,
    instruction_text: instructionText ? instructionText : null,
    step_type: data.step_type,
    is_required: data.step_type === 'instruction' ? false : data.is_required,
    config,
    requires_gauge: isMeasurement ? data.requires_gauge : false,
    spc_characteristic_id: isMeasurement && data.spc_characteristic_id ? data.spc_characteristic_id : null,
  };
}

function defaultsFor(step: ProcessSheetStep | null | undefined, defaultSequence: number): ProcessSheetStepFormInput {
  return {
    sequence: step?.sequence ?? defaultSequence,
    label: step?.label ?? '',
    instruction_text: step?.instruction_text ?? '',
    step_type: (step?.step_type as ProcessSheetStepFormInput['step_type']) ?? 'checkbox',
    is_required: step?.is_required ?? true,
    requires_gauge: step?.requires_gauge ?? false,
    spc_characteristic_id: step?.spc_characteristic_id ?? 0,
    nominal: step?.config?.nominal !== undefined ? String(step.config.nominal) : '',
    lsl: step?.config?.lsl !== undefined ? String(step.config.lsl) : '',
    usl: step?.config?.usl !== undefined ? String(step.config.usl) : '',
    unit: step?.config?.unit ?? '',
    decimals: step?.config?.decimals !== undefined ? String(step.config.decimals) : '',
    options_text: (step?.config?.options ?? []).join('\n'),
    hint: step?.config?.hint ?? '',
  };
}

/**
 * Add / edit a typed step on a DRAFT process sheet. Per-type config fields
 * swap with the selected step type; validation mirrors the backend rules
 * client-side so bad tolerances never leave the form, while the server stays
 * the source of truth (its 400/409 detail is surfaced verbatim via toast).
 */
export default function ProcessSheetStepModal({
  open,
  onClose,
  sheetId,
  step,
  defaultSequence,
  onSaved,
}: ProcessSheetStepModalProps) {
  const isEdit = Boolean(step);
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const [spcCharacteristics, setSpcCharacteristics] = useState<SPCCharacteristicOption[]>([]);
  const [spcLoaded, setSpcLoaded] = useState(false);

  const {
    register,
    handleSubmit,
    reset,
    setValue,
    watch,
    formState: { errors, isDirty },
  } = useForm<ProcessSheetStepFormInput, unknown, ProcessSheetStepFormData>({
    resolver: zodResolver(processSheetStepSchema),
    defaultValues: defaultsFor(step, defaultSequence),
  });

  const stepType = watch('step_type');
  const isInstruction = stepType === 'instruction';
  const isMeasurement = stepType === 'measurement';

  // Seed the form whenever the modal opens (or the target step changes).
  useEffect(() => {
    if (!open) return;
    reset(defaultsFor(step, defaultSequence));
  }, [open, step, defaultSequence, reset]);

  // INSTRUCTION steps are display-only — force is_required off (the checkbox
  // is also disabled below), matching the backend which ignores the flag.
  useEffect(() => {
    if (open && isInstruction) setValue('is_required', false);
  }, [open, isInstruction, setValue]);

  // SPC characteristics feed the optional "feeds SPC" wiring on measurement
  // steps. Loaded lazily, non-fatally: a failure just leaves the select empty.
  useEffect(() => {
    if (!open || spcLoaded) return;
    let cancelled = false;
    (async () => {
      try {
        const characteristics = await api.getSPCCharacteristics({ is_active: true });
        if (!cancelled) {
          setSpcCharacteristics(characteristics ?? []);
          setSpcLoaded(true);
        }
      } catch (err) {
        console.error('Failed to load SPC characteristics:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, spcLoaded]);

  const { confirmDiscard } = useUnsavedChanges(open && isDirty);

  const handleCancel = () => {
    if (confirmDiscard()) onClose();
  };

  const onSubmit = async (data: ProcessSheetStepFormData) => {
    setSaving(true);
    try {
      const payload = buildStepPayload(data);
      if (isEdit && step) {
        await api.updateProcessSheetStep(sheetId, step.id, payload);
      } else {
        await api.addProcessSheetStep(sheetId, payload);
      }
      showToast('success', isEdit ? 'Step updated' : 'Step added');
      onSaved();
      onClose();
    } catch (err: any) {
      // 409 = sheet no longer a draft; 400 = server-side config validation.
      showToast('error', err?.response?.data?.detail || 'Failed to save step');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={handleCancel} size="2xl" ariaLabelledBy="process-sheet-step-modal-title">
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <div>
          <h3 id="process-sheet-step-modal-title" className="text-lg font-semibold text-white">
            {isEdit ? 'Edit Step' : 'Add Step'}
          </h3>
          <p className="mt-1 text-sm text-slate-400">
            Typed steps capture objective evidence on the shop floor. Required steps gate operation completion.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <FormField label="Sequence" required error={errors.sequence?.message}>
            {(field) => (
              <input
                {...field}
                type="number"
                step={10}
                min={10}
                {...register('sequence')}
                className={errors.sequence ? 'input input-error' : 'input'}
              />
            )}
          </FormField>
          <FormField label="Step Type" required error={errors.step_type?.message} className="sm:col-span-2">
            {(field) => (
              <select {...field} {...register('step_type')} className="input">
                {PROCESS_SHEET_STEP_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {STEP_TYPE_LABELS[type]}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        </div>

        <FormField label="Label" required error={errors.label?.message}>
          {(field) => (
            <input
              {...field}
              type="text"
              {...register('label')}
              className={errors.label ? 'input input-error' : 'input'}
              placeholder="e.g. Bore diameter, Deburr edges, Torque fasteners"
            />
          )}
        </FormField>

        <FormField label="Instructions" error={errors.instruction_text?.message}>
          {(field) => (
            <textarea
              {...field}
              rows={2}
              {...register('instruction_text')}
              className="input"
              placeholder="Optional operator-facing instructions for this step"
            />
          )}
        </FormField>

        {isMeasurement && (
          <fieldset className="border border-slate-700 p-3">
            <legend className="px-1 text-xs font-medium uppercase tracking-wide text-slate-400">
              Measurement tolerance
            </legend>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <FormField label="LSL" required error={errors.lsl?.message} help="Lower spec limit">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    inputMode="decimal"
                    {...register('lsl')}
                    className={errors.lsl ? 'input input-error' : 'input'}
                  />
                )}
              </FormField>
              <FormField label="Nominal" required error={errors.nominal?.message}>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    inputMode="decimal"
                    {...register('nominal')}
                    className={errors.nominal ? 'input input-error' : 'input'}
                  />
                )}
              </FormField>
              <FormField label="USL" required error={errors.usl?.message} help="Upper spec limit">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    inputMode="decimal"
                    {...register('usl')}
                    className={errors.usl ? 'input input-error' : 'input'}
                  />
                )}
              </FormField>
            </div>
            <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FormField label="Unit" required error={errors.unit?.message}>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    {...register('unit')}
                    className={errors.unit ? 'input input-error' : 'input'}
                    placeholder='e.g. in, mm, °F'
                  />
                )}
              </FormField>
              <FormField label="Decimals" error={errors.decimals?.message} help="Display precision (optional)">
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    min={0}
                    max={6}
                    step={1}
                    {...register('decimals')}
                    className={errors.decimals ? 'input input-error' : 'input'}
                  />
                )}
              </FormField>
            </div>
            <div className="mt-3 space-y-3">
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  {...register('requires_gauge')}
                  className="rounded border-slate-600 bg-slate-800"
                />
                Requires a calibrated gauge (gauge identity recorded at capture)
              </label>
              <FormField
                label="SPC Characteristic"
                error={errors.spc_characteristic_id?.message}
                help="Optional — recorded measurements also feed this SPC characteristic"
              >
                {(field) => (
                  <select {...field} {...register('spc_characteristic_id')} className="input">
                    <option value={0}>None</option>
                    {spcCharacteristics.map((characteristic) => (
                      <option key={characteristic.id} value={characteristic.id}>
                        {characteristic.name}
                        {characteristic.unit_of_measure ? ` (${characteristic.unit_of_measure})` : ''}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
            </div>
          </fieldset>
        )}

        {stepType === 'list' && (
          <FormField
            label="Options"
            required
            error={errors.options_text?.message}
            help="One option per line — the operator picks exactly one"
          >
            {(field) => (
              <textarea
                {...field}
                rows={4}
                {...register('options_text')}
                className={errors.options_text ? 'input input-error' : 'input'}
                placeholder={'Pass\nFail\nNot applicable'}
              />
            )}
          </FormField>
        )}

        {(stepType === 'photo' || stepType === 'file') && (
          <FormField
            label="Capture hint"
            error={errors.hint?.message}
            help="Optional — shown next to the capture control on the kiosk"
          >
            {(field) => (
              <input
                {...field}
                type="text"
                {...register('hint')}
                className="input"
                placeholder='e.g. "Photograph the weld seam from above"'
              />
            )}
          </FormField>
        )}

        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              {...register('is_required')}
              disabled={isInstruction}
              className="rounded border-slate-600 bg-slate-800 disabled:opacity-50"
            />
            <span className={isInstruction ? 'opacity-50' : ''}>Required to complete the operation</span>
          </label>
          {isInstruction && (
            <span className="text-xs text-slate-500">Instruction steps are display-only and never required.</span>
          )}
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <button type="button" onClick={handleCancel} disabled={saving} className="btn-secondary">
            Cancel
          </button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving...">
            {isEdit ? 'Save Step' : 'Add Step'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
