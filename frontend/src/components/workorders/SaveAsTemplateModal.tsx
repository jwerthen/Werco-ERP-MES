/**
 * Save a work order's plan under a name — the catalog entry, not a copy.
 *
 * ---------------------------------------------------------------------------
 * THIS DIALOG WRITES ONE ROW AND TOUCHES NOTHING ELSE
 * ---------------------------------------------------------------------------
 * A template is a NAME plus a POINTER at the work order whose plan it stands
 * for. Nothing about the plan is copied here — operations, nests and material
 * ties are read at USE time, against whatever the source looks like then. So
 * this dialog does not navigate anywhere and the source work order comes
 * through completely unchanged: same status, same quantities, same dispatch
 * position. A planner saving a template must never wonder what it did to the
 * job they were looking at.
 *
 * That is also why there is no validity gate beyond the name. A job whose part
 * is currently retired, or whose process-sheet family has no released revision,
 * can still be catalogued; the refusal lands at USE time, where the planner can
 * see the cause and act on it.
 *
 * ---------------------------------------------------------------------------
 * THE DEFAULT QUANTITY IS A PREFILL, AND A LASER JOB HAS NONE TO GIVE
 * ---------------------------------------------------------------------------
 * `default_quantity` only seeds the field on the Use dialog. For a NEST-BEARING
 * source it seeds nothing at all: a laser work order's `quantity_ordered` is
 * DEFINED as the sum of its nests' planned runs, and the copy engine derives it
 * and overrules whatever was asked for. So the field is DISABLED, not hidden,
 * with the reason on it — a missing field is a mystery, a disabled one carrying
 * its reason is an explanation. (Nests only ever hang off a `laser_cutting`
 * work order, so callers answer this from the row they already hold; a laser
 * job that happens to carry no nests is locked out of an optional prefill,
 * which is the harmless direction to be wrong in.)
 *
 * ---------------------------------------------------------------------------
 * SERVER-GATED, THEREFORE NON-OPTIMISTIC
 * ---------------------------------------------------------------------------
 * The endpoint is role-gated (admin/manager/supervisor) and refuses a duplicate
 * name with a 409 this form cannot predict — names are compared
 * case-insensitively, so "Bracket set" collides with "bracket set". Nothing is
 * painted before the server answers: `pending` holds the spinner and the
 * double-click guard, disables Cancel, and refuses backdrop/Escape dismissal,
 * and a refusal leaves the dialog OPEN with the server's `detail` rendered
 * verbatim so the planner can edit the name they already typed.
 */

import React, { useEffect, useId, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { serverErrorDetail } from './copyPlanSkips';
import type { WorkOrderTemplate, WorkOrderTemplateCreatePayload } from '../../types';

/** Mirrors `WorkOrderTemplate.name`'s `String(120)` and the schema's `max_length`. */
export const TEMPLATE_NAME_MAX_LENGTH = 120;

/**
 * The fields this dialog needs off the source work order. Deliberately narrow so
 * BOTH callers fit: the detail page holds a full `WorkOrder`, the list page a
 * `WorkOrderSummary`.
 */
export interface SaveAsTemplateSource {
  id: number;
  work_order_number: string;
  quantity_ordered?: number | null;
}

export interface SaveAsTemplateModalProps {
  open: boolean;
  /** `null` while closed; the work order being catalogued. */
  workOrder: SaveAsTemplateSource | null;
  /**
   * Whether the source carries laser nests, which makes its quantity derived
   * rather than typed. Callers answer it from the row they already have (nests
   * only land on a `laser_cutting` work order) — this dialog never probes for
   * it, because `getWorkOrder` is not a plain read: it runs the
   * operation-quantity reconcile and can COMMIT writes against the very work
   * order this dialog promises not to touch.
   */
  hasLaserNests?: boolean;
  onClose: () => void;
  /**
   * Optional hand-off, fired only after the server has stored the template.
   * Callers use it to refresh a catalog they are already showing — there is
   * nothing to navigate to.
   */
  onSaved?: (template: WorkOrderTemplate) => void;
}

export default function SaveAsTemplateModal({
  open,
  workOrder,
  hasLaserNests = false,
  onClose,
  onSaved,
}: SaveAsTemplateModalProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [name, setName] = useState('');
  const [notes, setNotes] = useState('');
  const [defaultQuantity, setDefaultQuantity] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const sourceId = workOrder?.id ?? null;
  const sourceQuantity = workOrder?.quantity_ordered ?? null;

  // Reset on every open. The name starts EMPTY rather than pre-seeded from the
  // work order number: a template called "WO-20260501-004" is a name nobody can
  // pick out of a list, and the whole feature is the picker.
  useEffect(() => {
    if (!open || sourceId == null) return;
    setName('');
    setNotes('');
    setDefaultQuantity(hasLaserNests || sourceQuantity == null ? '' : String(sourceQuantity));
    setError('');
  }, [open, sourceId, sourceQuantity, hasLaserNests]);

  const close = () => {
    // Never dismiss mid-request: the template may already exist server-side, and
    // this dialog must reflect only what the server actually did.
    if (submitting) return;
    onClose();
  };

  const handleSubmit = async () => {
    if (submitting || !workOrder) return;

    const trimmedName = name.trim();
    if (!trimmedName) {
      setError('Give the template a name — that name is the only thing anyone picks it by.');
      return;
    }

    let quantity: number | undefined;
    if (!hasLaserNests && defaultQuantity.trim() !== '') {
      const parsed = Number(defaultQuantity);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        setError('Default quantity must be greater than zero, or leave it blank.');
        return;
      }
      quantity = parsed;
    }

    const payload: WorkOrderTemplateCreatePayload = {
      source_work_order_id: workOrder.id,
      name: trimmedName,
    };
    const trimmedNotes = notes.trim();
    if (trimmedNotes) payload.notes = trimmedNotes;
    if (quantity !== undefined) payload.default_quantity = quantity;

    setSubmitting(true);
    setError('');
    try {
      const template = await api.createWorkOrderTemplate(payload);
      // The NAME comes off the response, not the form: the server collapses
      // whitespace, so quoting the typed value can name something that is not
      // what got stored.
      showToast(
        'success',
        `Saved "${template.name}" as a template. Find it under Work Orders → Templates; ` +
          `${workOrder.work_order_number} is unchanged.`
      );
      onSaved?.(template);
      onClose();
    } catch (err) {
      setError(serverErrorDetail(err, 'Failed to save this work order as a template'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open && workOrder !== null}
      onClose={close}
      size="lg"
      padded={false}
      scroll={false}
      closeOnBackdrop={!submitting}
      closeOnEscape={!submitting}
      ariaLabelledBy={titleId}
    >
      {workOrder && (
        <>
          <div className="modal-header">
            <h3 id={titleId} className="text-lg font-semibold">
              Save as template — {workOrder.work_order_number}
            </h3>
          </div>

          <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
            <div className="rounded-sm border border-fd-line bg-fd-sunken/40 px-3 py-2 text-xs text-slate-400">
              Saves a NAME pointing at{' '}
              <span className="font-mono text-slate-300">{workOrder.work_order_number}</span>. Nothing is copied
              now and nothing on that work order changes — its operations, nests and material ties are read fresh
              each time somebody uses the template, and each use creates a new{' '}
              <strong className="text-slate-300">draft</strong>.
            </div>

            <FormField
              label="Template name"
              required
              help="What the shop calls this job — “Miratech nest group”, “Bracket brake set”. Must be unique; two templates differing only in case are indistinguishable in a picker, so the server refuses them."
            >
              {(field) => (
                <input
                  {...field}
                  type="text"
                  className="input"
                  maxLength={TEMPLATE_NAME_MAX_LENGTH}
                  disabled={submitting}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              )}
            </FormField>

            <FormField
              label="Notes"
              help="Optional, for the next planner: what this job is, when to reach for it, anything the plan itself does not say."
            >
              {(field) => (
                <textarea
                  {...field}
                  rows={3}
                  className="input"
                  disabled={submitting}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              )}
            </FormField>

            <FormField
              label="Default quantity"
              help={
                hasLaserNests
                  ? 'Not used for a laser job: its quantity is the sum of its nests’ sheet runs, derived when the template is used. Add or remove nests on the new draft to change it.'
                  : `Optional. Prefills the quantity when someone uses this template; leave it blank to fall back to ${workOrder.work_order_number}’s own quantity.`
              }
            >
              {(field) => (
                <input
                  {...field}
                  type="number"
                  inputMode="numeric"
                  min={1}
                  step={1}
                  className="input"
                  // Disabled, never hidden: a missing field is a mystery, a
                  // disabled one carrying its reason is an explanation.
                  disabled={submitting || hasLaserNests}
                  value={hasLaserNests ? '' : defaultQuantity}
                  onChange={(e) => setDefaultQuantity(e.target.value)}
                />
              )}
            </FormField>

            {/* Verbatim server refusal — the primary display for a gated write. */}
            {error && (
              <div
                role="alert"
                data-testid="save-template-error"
                className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
              >
                {error}
              </div>
            )}
          </div>

          <div className="modal-footer">
            <Button variant="secondary" onClick={close} disabled={submitting}>
              Cancel
            </Button>
            {/* Deliberately NOT disabled on an empty name — a dead button says
                nothing about why; the handler's guard names the reason. */}
            <LoadingButton loading={submitting} loadingText="Saving…" onClick={handleSubmit}>
              Save template
            </LoadingButton>
          </div>
        </>
      )}
    </Modal>
  );
}
