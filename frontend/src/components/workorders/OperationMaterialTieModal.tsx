/**
 * Tie material to ONE OPERATION, from the work order's Operations table.
 *
 * The missing create path. Until now the only way a tie came into existence was
 * the laser-nest flow (which ties the operation it creates) — so a milling op
 * that eats bar stock, or a weld op that eats gas and wire, had no way to say
 * so. This is that door, and it is deliberately narrow.
 *
 * ---------------------------------------------------------------------------
 * OPERATION SCOPE IS HARD-CODED. IT IS NOT A DEFAULT.
 * ---------------------------------------------------------------------------
 * Every tie created here carries `work_order_operation_id`. There is no control
 * to remove it and no code path that omits it. That is a safety property, not
 * tidiness:
 *
 *  - An operation-scoped tie consumes on the per-run engine when ITS OWN
 *    operation completes, reconciling to `qty_per_run × (complete + scrapped)`.
 *    A work-order-scoped tie instead drains once, through the completion
 *    backflush, against `qty_planned`.
 *  - Those two legs post under DIFFERENT ledger reference shapes precisely so
 *    they can never double-issue the same material. Choosing the wrong one from
 *    a per-operation row is not a preference mistake; it moves the material at
 *    the wrong moment, in the wrong quantity, against the wrong record.
 *  - A work-order-scoped tie also fans out across every card of that work order
 *    on the dispatch board, reading as N separate ties.
 *
 * Whole-work-order ties remain reachable through the API for the flows that
 * genuinely need them. They are not offered here.
 *
 * ---------------------------------------------------------------------------
 * THE MATERIAL LIST DEFAULTS TO RAW STOCK, AND THE ESCAPE HATCH MATTERS MOST HERE
 * ---------------------------------------------------------------------------
 * `/materials` serves all four material-supply types, and three of them
 * (`purchased`, `hardware`, `consumable`) are bought COMPONENTS rather than
 * stock — the seeded catalog types bolts and nuts as `purchased`. So the
 * default view is `isRawStockPartType` only.
 *
 * But of the three tie pickers this is the ONE where a hardware or consumable
 * tie is routinely legitimate: a weld op really does eat wire and gas, an
 * assembly op really does eat rivets, and that is precisely the gap this dialog
 * was built to close. The "Show all materials" toggle is therefore not a
 * grudging safety valve here — it is an expected part of the flow, and it must
 * never be removed or made conditional on the raw-stock list being empty.
 *
 * A part the shop PRODUCES is excluded at BOTH tiers and has no escape hatch:
 * a work order's own output is not an input to it.
 *
 * The tiering itself lives in `partitionMaterialTiers`
 * (`utils/catalogGroups.ts`), shared with the two laser-nest pickers, so the
 * pinned-selection and hidden-count rules cannot drift between them.
 *
 * ---------------------------------------------------------------------------
 * COPY
 * ---------------------------------------------------------------------------
 * The deduction-timing sentence is `DEDUCTION_TIMING_NOTE` from
 * `utils/materialTie.ts`, used VERBATIM. That module's docstring names the two
 * ways this copy goes wrong — "when WO-#### finishes" understates (an
 * operation-scoped tie deducts at ITS operation, not the job's end), and "per
 * run" / "deducting now" overstates outside a completion screen — and it exists
 * so there is exactly one string to change if the trigger ever moves again. Do
 * not re-word it here. (`deductionHeadline` is deliberately NOT reused: that
 * builder is reserved for the two kiosk COMPLETE screens, the one place the
 * module may speak in the present tense because the operator is one tap from
 * firing the completion. This is an office planning form.)
 *
 * ---------------------------------------------------------------------------
 * SERVER-GATED, THEREFORE NON-OPTIMISTIC
 * ---------------------------------------------------------------------------
 * Create is refused 409 on a terminal work order and 409 on a duplicate tie for
 * the same part+operation; edit is refused 409 once the tie is cancelled and 422
 * when the new plan sits under what was already consumed. Nothing is painted
 * before the server answers, and the server's `detail` renders verbatim — a
 * refusal that names the allocation to edit instead is only useful if it is
 * read.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { Button, FormField, LoadingButton, Modal, useToast } from '../ui';
import { DEDUCTION_TIMING_NOTE, effectivePerRun, formatTieQty } from '../../utils/materialTie';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import { toDisplayString } from '../../utils/apiError';
import { partitionMaterialTiers } from '../../utils/catalogGroups';
import type {
  MaterialAllocation,
  MaterialAllocationCreatePayload,
  MaterialAllocationUpdatePayload,
  Part,
  WorkOrderOperation,
} from '../../types';

/** Matches the laser-nest picker's cap — a shop's material list, not every part. */
const MATERIAL_OPTION_LIMIT = 500;

export interface OperationMaterialTieModalProps {
  open: boolean;
  workOrderId: number;
  /** `null` while closed; the operation every tie in this dialog is scoped to. */
  operation: WorkOrderOperation | null;
  /**
   * The operation's run target — nest planned runs, else its component quantity,
   * else the work order's ordered quantity. Used ONLY to derive the default
   * planned total (`qty_per_run × runs`), the same derivation the nest modal
   * uses, so a planner does not have to do the multiplication by hand.
   */
  operationTarget: number;
  onClose: () => void;
  /** Fired after any successful write, so the caller can refresh its tie list. */
  onSaved: () => void;
}

const blankDraft = { partId: '', qtyPerRun: '1', qtyPlanned: '', notes: '', clearPin: false };

/** Pull a displayable `detail` off any error shape, incl. a structured 409 body. */
function tieErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const rendered = toDisplayString(detail);
  if (rendered.trim()) return rendered;
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

const qtyWithUom = (value: number | null | undefined, uom: string): string =>
  `${formatTieQty(Number(value || 0))}${uom ? ` ${uom}` : ''}`;

export default function OperationMaterialTieModal({
  open,
  workOrderId,
  operation,
  operationTarget,
  onClose,
  onSaved,
}: OperationMaterialTieModalProps) {
  const { showToast } = useToast();
  const [materials, setMaterials] = useState<Part[]>([]);
  const [existingTies, setExistingTies] = useState<MaterialAllocation[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadNote, setLoadNote] = useState('');
  // Material filter escape hatch: false = raw stock only (the default), true =
  // every material part the load returned. See the docstring — on THIS picker
  // the widened view is a routine choice, not an exception.
  const [showAllMaterials, setShowAllMaterials] = useState(false);

  /** `null` = creating a new tie; otherwise the OPEN tie being edited. */
  const [editing, setEditing] = useState<MaterialAllocation | null>(null);
  const [draft, setDraft] = useState(blankDraft);
  /** Once the planner types a planned total we stop overwriting it from per-run. */
  const [plannedTouched, setPlannedTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const operationId = operation?.id ?? null;

  const load = useCallback(async () => {
    if (operationId == null) return;
    setLoading(true);
    setLoadNote('');
    try {
      const [parts, ties] = await Promise.all([
        api.getMaterials({ active_only: true, limit: MATERIAL_OPTION_LIMIT }),
        // OPEN ties only: `closed` is never written (a fully consumed tie stays
        // `open`), and a cancelled row is a tombstone, not something to edit.
        api.getMaterialAllocations(workOrderId, false),
      ]);
      setMaterials(parts ?? []);
      setExistingTies(
        (ties ?? []).filter((tie) => tie.status === 'open' && tie.work_order_operation_id === operationId)
      );
    } catch (err) {
      // Advisory: a failed read must not hide the form. The picker is simply
      // empty, and the server remains the authority on any write attempted.
      setMaterials([]);
      setExistingTies([]);
      setLoadNote(tieErrorDetail(err, 'Could not read this operation’s material list'));
    } finally {
      setLoading(false);
    }
  }, [workOrderId, operationId]);

  // Reset and reload whenever the dialog opens against an operation.
  useEffect(() => {
    if (!open || operationId == null) return;
    setEditing(null);
    setDraft(blankDraft);
    setPlannedTouched(false);
    setError('');
    setShowAllMaterials(false);
    void load();
  }, [open, operationId, load]);

  const { materialOptions, hiddenMaterialCount } = useMemo(() => {
    const toOption = (part: Part) => ({
      id: part.id,
      label: part.part_number ? `${part.part_number} — ${part.name}` : part.name,
      uom: part.unit_of_measure || '',
    });

    // The tiering — production parts excluded outright, raw stock by default,
    // the rest behind the toggle, the current pick pinned so narrowing cannot
    // blank it, and a count that advertises only what the toggle would really
    // reveal — is `partitionMaterialTiers`, shared with the two laser pickers.
    const { defaultTier: rawStock, hiddenTier: otherMaterials, pinned, hiddenCount } = partitionMaterialTiers(
      materials,
      {
        showAll: showAllMaterials,
        // Pin the current pick so flipping the toggle back cannot blank a part
        // the planner chose through the escape hatch.
        pinnedIds: [draft.partId, editing?.part_id],
      }
    );

    const options = rawStock.map(toOption);
    options.push(...(showAllMaterials ? otherMaterials : pinned).map(toOption));

    // Keep an edited tie's part selectable even when the (capped, filtered, or
    // failed) material load did not return it — otherwise the form reads as
    // "no part" while the tie is very much live. Safe for a legacy tie to a
    // part the shop PRODUCES, unlike the same-shaped branch in
    // `LaserNestManualModal`: the edit branch renders the part as a READONLY
    // input, never this <select>, so nothing here is selectable while
    // `editing` is set.
    if (editing && !options.some((option) => option.id === editing.part_id)) {
      options.push({
        id: editing.part_id,
        label: [editing.part_number, editing.part_name].filter(Boolean).join(' — ') || `Part ${editing.part_id}`,
        uom: editing.unit_of_measure || '',
      });
    }

    return { materialOptions: options, hiddenMaterialCount: hiddenCount };
  }, [materials, editing, showAllMaterials, draft.partId]);

  const selectedUom = useMemo(() => {
    if (editing) return editing.unit_of_measure || '';
    const chosen = materialOptions.find((option) => String(option.id) === draft.partId);
    return chosen?.uom || '';
  }, [editing, materialOptions, draft.partId]);

  const runs = Math.max(0, Number(operationTarget) || 0);
  const perRunValue = draft.qtyPerRun.trim() === '' ? null : Number(draft.qtyPerRun);
  const derivedPlanned = effectivePerRun(
    perRunValue !== null && Number.isFinite(perRunValue) ? perRunValue : null
  ) * runs;
  const plannedValue = plannedTouched || draft.qtyPlanned.trim() !== '' ? Number(draft.qtyPlanned) : derivedPlanned;

  const startEdit = (tie: MaterialAllocation) => {
    setEditing(tie);
    setError('');
    setPlannedTouched(true);
    setDraft({
      partId: String(tie.part_id),
      qtyPerRun: tie.qty_per_run == null ? '' : String(tie.qty_per_run),
      qtyPlanned: String(tie.qty_planned ?? ''),
      notes: tie.notes ?? '',
      clearPin: false,
    });
  };

  const startCreate = () => {
    setEditing(null);
    setError('');
    setPlannedTouched(false);
    setDraft(blankDraft);
  };

  const close = () => {
    // Never dismiss mid-request: this write decides what a completion takes out
    // of stock, and the dialog must reflect only what the server actually did.
    if (saving) return;
    onClose();
  };

  const handleSave = async () => {
    if (saving || operationId == null) return;

    if (editing) {
      const payload: MaterialAllocationUpdatePayload = {};
      const nextPerRun = draft.qtyPerRun.trim() === '' ? null : Number(draft.qtyPerRun);
      if (nextPerRun !== null && !Number.isFinite(nextPerRun)) {
        setError('Per completed run must be a number, or blank for 1.');
        return;
      }
      if (nextPerRun === null ? editing.qty_per_run != null : nextPerRun !== editing.qty_per_run) {
        payload.qty_per_run = nextPerRun;
      }
      const nextPlanned = Number(draft.qtyPlanned);
      if (draft.qtyPlanned.trim() !== '' && Number.isFinite(nextPlanned) && nextPlanned !== editing.qty_planned) {
        payload.qty_planned = nextPlanned;
      }
      const nextNotes = draft.notes.trim() === '' ? null : draft.notes;
      if (nextNotes !== (editing.notes ?? null)) payload.notes = nextNotes;
      if (draft.clearPin && editing.pinned_inventory_item_id != null) payload.clear_pinned_inventory_item = true;

      if (Object.keys(payload).length === 0) {
        setError('Nothing changed.');
        return;
      }

      setSaving(true);
      setError('');
      try {
        await api.updateMaterialAllocation(workOrderId, editing.id, payload);
        showToast('success', `Updated the ${editing.part_number || 'material'} tie on this operation`);
        onSaved();
        onClose();
      } catch (err) {
        setError(tieErrorDetail(err, 'Failed to update this material tie'));
      } finally {
        setSaving(false);
      }
      return;
    }

    const partId = Number(draft.partId);
    if (!draft.partId || !Number.isFinite(partId)) {
      setError('Pick the material this operation consumes.');
      return;
    }
    const perRun = draft.qtyPerRun.trim() === '' ? 1 : Number(draft.qtyPerRun);
    if (!Number.isFinite(perRun) || perRun < 0) {
      setError('Per completed run must be zero or more.');
      return;
    }
    const planned = Number(plannedValue);
    if (!Number.isFinite(planned) || planned <= 0) {
      setError('Planned total must be greater than zero.');
      return;
    }

    setSaving(true);
    setError('');
    try {
      // OPERATION SCOPE IS NOT NEGOTIABLE — see the module docstring. There is
      // no branch here that omits `work_order_operation_id`.
      const payload: MaterialAllocationCreatePayload = {
        part_id: partId,
        work_order_operation_id: operationId,
        source: 'manual',
        qty_per_run: perRun,
        qty_planned: planned,
      };
      if (draft.notes.trim()) payload.notes = draft.notes.trim();
      const created = await api.createMaterialAllocation(workOrderId, payload);
      showToast(
        'success',
        `Tied ${created.part_number || 'material'} to ${
          hasOperationNumber(operation?.operation_number)
            ? formatOperationLabel(operation?.operation_number)
            : operation?.name || 'this operation'
        }`
      );
      onSaved();
      onClose();
    } catch (err) {
      setError(tieErrorDetail(err, 'Failed to tie material to this operation'));
    } finally {
      setSaving(false);
    }
  };

  const operationLabel = operation
    // `Seq {n}` stays the fallback (a real, numeric sequence names the operation
    // better than an em-dash would in this dialog's title).
    ? `${
        hasOperationNumber(operation.operation_number)
          ? formatOperationLabel(operation.operation_number)
          : `Seq ${operation.sequence}`
      } · ${operation.name}`
    : '';

  return (
    <Modal open={open && operation !== null} onClose={close} size="2xl" padded={false} scroll={false}>
      {operation && (
        <>
          <div className="modal-header">
            <h3 className="text-lg font-semibold">
              {editing ? 'Edit material tie' : 'Tie material to this operation'} — {operationLabel}
            </h3>
          </div>
          <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
            <div className="rounded-sm border border-fd-line bg-fd-sunken/40 px-3 py-2 text-xs text-slate-400">
              Ties created here are scoped to <strong className="text-slate-300">this operation</strong> and always
              will be — that is what makes them deduct at this operation&rsquo;s completion, on the per-run engine,
              rather than draining once at the end of the job. {DEDUCTION_TIMING_NOTE}
            </div>

            {/* --- The operation's existing open ties ------------------------ */}
            {existingTies.length > 0 && (
              <div>
                <p className="label mb-1">Already tied to this operation</p>
                <ul className="space-y-1">
                  {existingTies.map((tie) => {
                    const isEditing = editing?.id === tie.id;
                    return (
                      <li
                        key={tie.id}
                        className={`flex flex-wrap items-center justify-between gap-2 rounded-sm border px-2.5 py-1.5 text-xs ${
                          isEditing ? 'border-werco-navy/70 bg-werco-navy/10' : 'border-fd-line'
                        }`}
                      >
                        <span className="min-w-0">
                          <span className="font-mono text-sm text-slate-100">
                            {tie.part_number || `Part #${tie.part_id}`}
                          </span>
                          <span className="ml-2 text-slate-400">
                            {formatTieQty(effectivePerRun(tie.qty_per_run))} per run ·{' '}
                            {qtyWithUom(tie.qty_planned, tie.unit_of_measure)} planned ·{' '}
                            {qtyWithUom(tie.qty_consumed, tie.unit_of_measure)} consumed (reported)
                          </span>
                        </span>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={saving}
                          onClick={() => (isEditing ? startCreate() : startEdit(tie))}
                        >
                          {isEditing ? 'Cancel edit' : 'Edit'}
                        </Button>
                      </li>
                    );
                  })}
                </ul>
                {!editing && (
                  <p className="mt-1 text-xs text-slate-500">
                    The form below adds another material. Tying the same part to this operation twice is refused —
                    edit the existing tie instead.
                  </p>
                )}
              </div>
            )}

            {loadNote && (
              <p role="status" className="text-xs text-amber-300">
                {loadNote} — you can still submit; the server decides.
              </p>
            )}

            {/* --- The form -------------------------------------------------- */}
            {editing ? (
              <FormField label="Material" help="Fixed at creation — changing what a tie points at would rewrite genealogy. Untie and re-tie to swap the part.">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    readOnly
                    className="input"
                    value={
                      [editing.part_number, editing.part_name].filter(Boolean).join(' — ') || `Part #${editing.part_id}`
                    }
                  />
                )}
              </FormField>
            ) : (
              <div>
                <FormField
                  label="Material this operation consumes"
                  required
                  help={
                    loading
                      ? 'Loading the material list…'
                      : showAllMaterials
                        ? 'Every material part — hardware and consumables included. The MATERIAL part depleted, never the part being produced.'
                        : 'Raw stock only by default. The MATERIAL part depleted — never the part being produced.'
                  }
                >
                  {(field) => (
                    <select
                      {...field}
                      className="input"
                      disabled={saving}
                      value={draft.partId}
                      onChange={(e) => setDraft({ ...draft, partId: e.target.value })}
                    >
                      <option value="">Select material…</option>
                      {materialOptions.map((option) => (
                        <option key={option.id} value={String(option.id)}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  )}
                </FormField>
                {/* Rivets, weld wire and gas are legitimate ties on this picker
                    and are typed hardware/consumable, so this toggle is part of
                    the flow rather than an exception. It sits OUTSIDE the
                    FormField so its text stays out of the select's accessible
                    name. */}
                {(hiddenMaterialCount > 0 || showAllMaterials) && (
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setShowAllMaterials((prev) => !prev)}
                    className="mt-1 text-xs font-medium text-fd-blue hover:underline disabled:opacity-60"
                  >
                    {showAllMaterials
                      ? 'Show raw stock only'
                      : `Show all materials (${hiddenMaterialCount} more)`}
                  </button>
                )}
              </div>
            )}

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <FormField
                label={`Per completed run${selectedUom ? ` (${selectedUom})` : ''}`}
                help="Blank means 1 per run. Scrapped runs count too — a scrapped run still used its material."
              >
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="any"
                    className="input"
                    placeholder="1"
                    disabled={saving}
                    value={draft.qtyPerRun}
                    onChange={(e) => setDraft({ ...draft, qtyPerRun: e.target.value })}
                  />
                )}
              </FormField>

              <FormField
                label={`Planned total${selectedUom ? ` (${selectedUom})` : ''}`}
                required={!editing}
                help={
                  editing
                    ? 'Cannot be lowered below what has already been consumed.'
                    : `Defaults to per-run × ${formatTieQty(runs)} run${runs === 1 ? '' : 's'}. It is the plan figure only — what actually deducts is recomputed from this operation's completed and scrapped quantities.`
                }
              >
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="any"
                    className="input"
                    disabled={saving}
                    value={plannedTouched || draft.qtyPlanned !== '' ? draft.qtyPlanned : formatTieQty(derivedPlanned)}
                    onChange={(e) => {
                      setPlannedTouched(true);
                      setDraft({ ...draft, qtyPlanned: e.target.value });
                    }}
                  />
                )}
              </FormField>
            </div>

            {editing?.pinned_inventory_item_id != null && (
              // The node form of FormField (not the render prop): `htmlFor` points
              // the primitive's own <label> straight at the checkbox, so the
              // association is real rather than a dangling generated id.
              <FormField
                label={`Clear the lot pin (${editing.pinned_lot_number || 'pinned lot'}) and fall back to FIFO`}
                htmlFor={`op-tie-clear-pin-${editing.id}`}
                help="Pinned consumption draws from that lot exclusively. Clearing it lets FIFO pick at the moment the material is drawn."
              >
                <input
                  id={`op-tie-clear-pin-${editing.id}`}
                  type="checkbox"
                  disabled={saving}
                  checked={draft.clearPin}
                  onChange={(e) => setDraft({ ...draft, clearPin: e.target.checked })}
                />
              </FormField>
            )}

            <FormField label="Notes" help="Optional. Shown on the Material Ties panel.">
              {(field) => (
                <input
                  {...field}
                  type="text"
                  maxLength={255}
                  className="input"
                  disabled={saving}
                  value={draft.notes}
                  onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                />
              )}
            </FormField>

            <p className="text-xs text-slate-500">
              New ties are created unpinned, so FIFO picks the lot at the moment the material is drawn. Pin a
              specific heat from the Material Ties panel below if the job requires one.
            </p>

            {/* Verbatim server refusal — the primary display for a gated write.
                The 409 on a duplicate NAMES the allocation to edit instead,
                which is the whole reason it renders in full. */}
            {error && (
              <div
                role="alert"
                data-testid="op-tie-error"
                className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
              >
                {error}
              </div>
            )}
          </div>
          <div className="modal-footer">
            <Button variant="secondary" onClick={close} disabled={saving}>
              Cancel
            </Button>
            {/* Deliberately NOT disabled on an incomplete draft — the same call
                MaterialTiesPanel makes. A dead button says nothing about why;
                the handler's guard renders a specific reason instead. */}
            <LoadingButton loading={saving} loadingText="Saving…" onClick={handleSave}>
              {editing ? 'Save tie' : 'Tie material'}
            </LoadingButton>
          </div>
        </>
      )}
    </Modal>
  );
}
