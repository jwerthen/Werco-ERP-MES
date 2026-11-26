import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Modal } from '../ui/Modal';
import { FormField } from '../ui/FormField';
import { Button } from '../ui/Button';
import { LoadingButton } from '../ui/LoadingButton';
import { useToast } from '../ui/Toast';
import api from '../../services/api';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { apiErrorDetail } from '../../utils/apiError';
import { formatOperationLabel } from '../../utils/operationLabel';
import {
  isOperationVersionConflict,
  nextFreeSequence,
  routingChangeNeedsReason,
  usedSequences,
} from '../../utils/routingChange';
import {
  contentFieldsChanged,
  RoutingOperationFormData,
  RoutingOperationFormInput,
  ROUTING_REASON_MAX_LENGTH,
  withRoutingRules,
} from '../../validation/routingChange';
import type {
  WorkCenter,
  WorkOrder,
  WorkOrderOperation,
  WorkOrderOperationAddPayload,
  WorkOrderOperationUpdatePayload,
} from '../../types';

export type RoutingOperationModalMode = { kind: 'add' } | { kind: 'edit'; operation: WorkOrderOperation };

interface RoutingOperationModalProps {
  open: boolean;
  /** Edit mode carries the step AS IT WAS when the dialog opened (its version is the lock). */
  mode: RoutingOperationModalMode;
  /**
   * Edit mode: the same step from the latest read of the work order. Its version is adopted
   * only after a stale-version refusal and the refresh that follows — never from a background
   * poll, which would silently disarm the optimistic lock.
   */
  liveOperation?: WorkOrderOperation;
  /** Re-read the work order (after a stale-version refusal). */
  onRefresh?: () => Promise<void> | void;
  workOrder: WorkOrder;
  onClose: () => void;
  /** Called after the server accepted the change; the caller refetches the work order. */
  onSaved: () => Promise<void> | void;
}

const FIELD = 'input w-full';
const FIELD_ERROR = 'input-error w-full';

function valuesFromOperation(op: WorkOrderOperation): RoutingOperationFormData {
  return {
    sequence: op.sequence,
    operation_number: op.operation_number ?? '',
    name: op.name ?? '',
    work_center_id: op.work_center_id,
    description: op.description ?? '',
    setup_instructions: op.setup_instructions ?? '',
    run_instructions: op.run_instructions ?? '',
    setup_time_hours: Number(op.setup_time_hours || 0),
    run_time_hours: Number(op.run_time_hours || 0),
    run_time_per_piece: Number(op.run_time_per_piece || 0),
    requires_inspection: Boolean(op.requires_inspection),
    change_reason: '',
  };
}

/** The form's raw input (numbers may still be strings) read as values, for the live reason marker. */
function coerceFormInput(input: Partial<RoutingOperationFormInput>, fallback: RoutingOperationFormData) {
  const num = (value: unknown, otherwise: number) => {
    const n = Number(value);
    return value === '' || value == null || !Number.isFinite(n) ? otherwise : n;
  };
  const text = (value: unknown) => String(value ?? '').trim();
  return {
    ...fallback,
    name: text(input.name),
    description: text(input.description),
    setup_instructions: text(input.setup_instructions),
    run_instructions: text(input.run_instructions),
    setup_time_hours: num(input.setup_time_hours, 0),
    run_time_hours: num(input.run_time_hours, 0),
    run_time_per_piece: num(input.run_time_per_piece, 0),
    requires_inspection: Boolean(input.requires_inspection),
  };
}

const blank = (value: string | undefined): string | null => (value && value.trim() ? value.trim() : null);

/**
 * Add a step to a work order's routing, or edit an unfinished one (owner decision 2026-11-25).
 *
 * SERVER-GATED and non-optimistic: the dialog stays open with the server's refusal shown
 * verbatim in an error toast, and closes only after the server accepted the change and the
 * caller's refetch returned. On a released work order a typed reason is required (an Edit
 * needs one only when a content field changes — a work-center move alone is reason-free, as
 * on the dispatch board); on a DRAFT the reason field is hidden.
 */
export default function RoutingOperationModal({
  open,
  mode,
  liveOperation,
  workOrder,
  onClose,
  onSaved,
  onRefresh,
}: RoutingOperationModalProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [workCentersFailed, setWorkCentersFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [version, setVersion] = useState<number | undefined>(mode.kind === 'edit' ? mode.operation.version : undefined);
  const adoptLiveVersion = useRef(false);

  const isEdit = mode.kind === 'edit';
  const editing = mode.kind === 'edit' ? mode.operation : null;
  const reasonRequired = routingChangeNeedsReason(workOrder);
  const original = useMemo(() => (editing ? valuesFromOperation(editing) : undefined), [editing]);
  const used = useMemo(() => usedSequences(workOrder.operations || []), [workOrder.operations]);

  const schema = useMemo(
    () => withRoutingRules({ reasonRequired, original, usedSequences: isEdit ? undefined : used }),
    [reasonRequired, original, isEdit, used]
  );

  const {
    register,
    handleSubmit,
    reset,
    watch,
    formState: { errors, isDirty },
  } = useForm<RoutingOperationFormInput, unknown, RoutingOperationFormData>({
    resolver: zodResolver(schema),
  });

  const { confirmDiscard, markSaved } = useUnsavedChanges(open && isDirty);

  useEffect(() => {
    if (!open) return;
    if (editing) {
      reset(valuesFromOperation(editing));
    } else {
      reset({
        sequence: nextFreeSequence(workOrder.operations || []),
        operation_number: '',
        name: '',
        work_center_id: '',
        description: '',
        setup_instructions: '',
        run_instructions: '',
        setup_time_hours: 0,
        run_time_hours: 0,
        run_time_per_piece: 0,
        requires_inspection: false,
        change_reason: '',
      });
    }
    // Seed only on open / target change, never on a background refetch of the work order.
  }, [open, editing?.id, reset]);

  useEffect(() => {
    if (!adoptLiveVersion.current || !liveOperation) return;
    adoptLiveVersion.current = false;
    setVersion(liveOperation.version);
  }, [liveOperation]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setWorkCentersFailed(false);
    api
      .getWorkCenters(true)
      .then(centers => {
        if (!cancelled) setWorkCenters((centers ?? []).filter(wc => wc.is_active));
      })
      .catch(() => {
        if (!cancelled) {
          setWorkCenters([]);
          setWorkCentersFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // The step's current work center stays selectable even when it is inactive or the load
  // failed, so an Edit never silently re-points the step.
  const workCenterOptions = useMemo(() => {
    const options = workCenters.map(wc => ({ id: wc.id, label: wc.code ? `${wc.code} — ${wc.name}` : wc.name }));
    if (editing && !options.some(option => option.id === editing.work_center_id)) {
      options.unshift({
        id: editing.work_center_id,
        label: editing.work_center_name || `Work center #${editing.work_center_id}`,
      });
    }
    return options;
  }, [workCenters, editing]);

  const watched = watch();
  const showReason = reasonRequired;
  // Live mirror of the schema's rule, so the required marker tracks what the save will need.
  const reasonNeededNow =
    reasonRequired && (!original || contentFieldsChanged(coerceFormInput(watched, original), original).length > 0);

  const close = () => {
    if (saving) return;
    if (confirmDiscard()) onClose();
  };

  const submitAdd = async (values: RoutingOperationFormData) => {
    const payload: WorkOrderOperationAddPayload = {
      work_center_id: values.work_center_id,
      sequence: values.sequence,
      // An identifier, never a display label: the bare sequence unless the planner typed one.
      operation_number: blank(values.operation_number) ?? String(values.sequence),
      name: values.name,
      description: blank(values.description),
      setup_instructions: blank(values.setup_instructions),
      run_instructions: blank(values.run_instructions),
      setup_time_hours: values.setup_time_hours,
      run_time_hours: values.run_time_hours,
      run_time_per_piece: values.run_time_per_piece,
      requires_inspection: values.requires_inspection,
    };
    if (reasonRequired) payload.change_reason = values.change_reason;
    await api.addOperation(workOrder.id, payload);
    return `Step ${formatOperationLabel(payload.operation_number, values.sequence)} added`;
  };

  const submitEdit = async (op: WorkOrderOperation, values: RoutingOperationFormData) => {
    const before = valuesFromOperation(op);
    const payload: WorkOrderOperationUpdatePayload = { version: version ?? op.version };
    // Only what changed: a work-center field sent unchanged would still be judged as a move.
    if (values.name !== before.name) payload.name = values.name;
    if (values.description !== before.description) payload.description = values.description;
    if (values.setup_instructions !== before.setup_instructions) payload.setup_instructions = values.setup_instructions;
    if (values.run_instructions !== before.run_instructions) payload.run_instructions = values.run_instructions;
    if (values.setup_time_hours !== before.setup_time_hours) payload.setup_time_hours = values.setup_time_hours;
    if (values.run_time_hours !== before.run_time_hours) payload.run_time_hours = values.run_time_hours;
    if (values.run_time_per_piece !== before.run_time_per_piece) payload.run_time_per_piece = values.run_time_per_piece;
    if (values.requires_inspection !== before.requires_inspection)
      payload.requires_inspection = values.requires_inspection;
    if (values.work_center_id !== before.work_center_id) payload.work_center_id = values.work_center_id;
    if (Object.keys(payload).length === 1) return null;
    if (reasonRequired && values.change_reason) payload.change_reason = values.change_reason;
    await api.updateOperation(op.id, payload);
    return `${formatOperationLabel(op.operation_number, op.sequence)} updated`;
  };

  const onSubmit = async (values: RoutingOperationFormData) => {
    if (saving) return;
    setSaving(true);
    try {
      const message = editing ? await submitEdit(editing, values) : await submitAdd(values);
      if (message === null) {
        showToast('info', 'Nothing changed');
        setSaving(false);
        return;
      }
      markSaved();
      await onSaved();
      showToast('success', message);
      setSaving(false);
      onClose();
    } catch (err) {
      // Refused (or failed): keep the dialog and everything typed; show the server's words.
      showToast('error', apiErrorDetail(err, editing ? 'Could not update the step' : 'Could not add the step'));
      if (editing && isOperationVersionConflict(err) && onRefresh) {
        adoptLiveVersion.current = true;
        try {
          await onRefresh();
        } catch {
          adoptLiveVersion.current = false;
        }
      }
      setSaving(false);
    }
  };

  const title = editing
    ? `Edit ${formatOperationLabel(editing.operation_number, editing.sequence)} — ${editing.name}`
    : 'Add step';

  return (
    <Modal open={open} onClose={close} size="2xl" ariaLabelledBy={titleId} closeOnBackdrop={!saving}>
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="space-y-4">
        <h2 id={titleId} className="text-lg font-semibold text-fd-ink">
          {title}
        </h2>
        {editing && editing.status === 'in_progress' && (
          <p className="rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-fd-amber">
            This step is in progress. Changes apply to the work still to be done; the server refuses a work-center move
            while someone is clocked in.
          </p>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {!isEdit && (
            <>
              <FormField
                label="Sequence"
                required
                error={errors.sequence?.message}
                help={used.length ? `In use: ${used.join(', ')}` : 'Multiples of 10, 10–990'}
              >
                {field => (
                  <input
                    {...field}
                    {...register('sequence')}
                    type="number"
                    min={10}
                    max={990}
                    step={10}
                    className={errors.sequence ? FIELD_ERROR : FIELD}
                  />
                )}
              </FormField>
              <FormField label="Operation #" error={errors.operation_number?.message} help="Defaults to the sequence">
                {field => (
                  <input
                    {...field}
                    {...register('operation_number')}
                    type="text"
                    maxLength={20}
                    className={errors.operation_number ? FIELD_ERROR : FIELD}
                  />
                )}
              </FormField>
            </>
          )}
          <FormField label="Name" required error={errors.name?.message} className="sm:col-span-2">
            {field => (
              <input
                {...field}
                {...register('name')}
                type="text"
                maxLength={255}
                className={errors.name ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <FormField
            label="Work center"
            required
            error={errors.work_center_id?.message ?? (workCentersFailed ? 'Work centers could not be loaded' : null)}
            className="sm:col-span-2"
          >
            {field => (
              <select
                {...field}
                {...register('work_center_id')}
                className={errors.work_center_id ? FIELD_ERROR : FIELD}
              >
                <option value="">Select a work center</option>
                {workCenterOptions.map(option => (
                  <option key={option.id} value={option.id}>
                    {option.label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField label="Setup (hours)" error={errors.setup_time_hours?.message}>
            {field => (
              <input
                {...field}
                {...register('setup_time_hours')}
                type="number"
                min={0}
                step="any"
                className={errors.setup_time_hours ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <FormField label="Run (hours)" error={errors.run_time_hours?.message}>
            {field => (
              <input
                {...field}
                {...register('run_time_hours')}
                type="number"
                min={0}
                step="any"
                className={errors.run_time_hours ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <FormField label="Run per piece (hours)" error={errors.run_time_per_piece?.message}>
            {field => (
              <input
                {...field}
                {...register('run_time_per_piece')}
                type="number"
                min={0}
                step="any"
                className={errors.run_time_per_piece ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <div className="flex items-end pb-2">
            <label className="flex items-center gap-2 text-sm text-fd-body">
              <input type="checkbox" {...register('requires_inspection')} className="h-4 w-4" />
              Requires inspection
            </label>
          </div>
          <FormField label="Description" error={errors.description?.message} className="sm:col-span-2">
            {field => (
              <input
                {...field}
                {...register('description')}
                type="text"
                className={errors.description ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <FormField label="Setup instructions" error={errors.setup_instructions?.message} className="sm:col-span-2">
            {field => (
              <textarea
                {...field}
                {...register('setup_instructions')}
                rows={2}
                className={errors.setup_instructions ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          <FormField label="Run instructions" error={errors.run_instructions?.message} className="sm:col-span-2">
            {field => (
              <textarea
                {...field}
                {...register('run_instructions')}
                rows={2}
                className={errors.run_instructions ? FIELD_ERROR : FIELD}
              />
            )}
          </FormField>
          {showReason && (
            <FormField
              label="Reason for the change"
              required={reasonNeededNow}
              error={errors.change_reason?.message}
              help={
                reasonNeededNow
                  ? 'This work order has been released, so the change is recorded with your reason.'
                  : 'A work-center move alone needs no reason.'
              }
              className="sm:col-span-2"
            >
              {field => (
                <textarea
                  {...field}
                  {...register('change_reason')}
                  rows={2}
                  maxLength={ROUTING_REASON_MAX_LENGTH}
                  className={errors.change_reason ? FIELD_ERROR : FIELD}
                />
              )}
            </FormField>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-fd-line pt-3">
          <Button variant="secondary" type="button" onClick={close} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving...">
            {isEdit ? 'Save step' : 'Add step'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}
