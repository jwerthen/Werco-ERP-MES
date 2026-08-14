/**
 * Material ties on a work order — the office-side view of
 * `work_order_material_allocations`.
 *
 * A tie is the OPTIONAL link between a work order (or one of its operations)
 * and the stock material it depletes. Everything on this panel is shaped by
 * four facts that are easy to get wrong:
 *
 * 1. **Consumption fires when an OPERATION completes, and never per run.** An
 *    operation-scoped tie deducts the moment its own operation closes — a laser
 *    child WO carries one operation per nest, so finishing nest 1 of 3 takes
 *    nest 1's sheets out of stock right then, not at the end of the job.
 *    Work-order completion re-runs the whole reconcile as a SELF-HEAL, and is
 *    still the moment a tie scoped to the WHOLE WORK ORDER (the "Whole work
 *    order" scope below) drains, through the completion backflush. What never
 *    happens is a per-run deduction: reporting runs on an open operation posts
 *    nothing, because an in-progress operation is still reducible and
 *    consumption never auto-reverses. This panel spans BOTH scopes, so its copy
 *    has to name both timings rather than picking one.
 * 2. **`qty_consumed` is a CACHE.** The authoritative consumed total is the sum
 *    of `inventory_transactions` carrying this allocation's `allocation_id`, so
 *    the column is labelled "Consumed (reported)" and must not be read as a
 *    compliance figure.
 * 3. **Status is the tombstone.** Rows are never physically deleted (the
 *    ledger's `allocation_id` back-reference has to keep resolving), so
 *    CANCELLED rows are shown dimmed rather than filtered out —
 *    `include_inactive` defaults to true deliberately. `closed` is reserved and
 *    never written: "fully consumed" is derived from
 *    `qty_consumed >= qty_planned`, never from status.
 * 4. **Every mutation here is server-GATED** — 409 on untie-after-consumption,
 *    409 on a terminal work order, 422 on a held-lot pin, 422 on an unbounded
 *    correction — so per the optimistic-UI convention these are strictly
 *    NON-optimistic: loading state on the control, only what the server returns
 *    is rendered, and the server's `detail` is surfaced verbatim.
 * 5. **RETURN is the only way consumption ever comes back.** The engine never
 *    auto-reverses (invariant 6b) — a negative delta is a no-op — so the "reverse
 *    the consumption first" refusals on this very panel would otherwise have no
 *    self-service path at all. That is also why the return affordance is offered
 *    on a **cancelled** tie: a consumed+cancelled tie is exactly what the work
 *    order's hard-delete 409 points at, and hiding the verb there would leave
 *    that refusal a dead end. The panel renders no other action on a
 *    non-open tie.
 *
 * An untied work order renders the panel's empty state and nothing else; it
 * never nags.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowUturnLeftIcon,
  CubeIcon,
  LinkSlashIcon,
  PencilSquareIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, FormField, LoadingButton, Modal, useToast } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatCentralDateTime } from '../../utils/centralTime';
import { formatTieQty, overConsumedQty } from '../../utils/materialTie';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import type {
  MaterialAllocation,
  MaterialAllocationUpdatePayload,
  MaterialConsumptionLine,
  MaterialReturnIntent,
  WorkOrderOperation,
} from '../../types';

export interface MaterialTiesPanelProps {
  workOrderId: number;
  /**
   * The work order's `updated_at`. Threaded in as a load dependency because the
   * panel deliberately does NOT join the page's 30s poll / websocket refresh
   * (two independent pollers on one page is worse than a stale figure) — but a
   * completion that posts consumption bumps the work order, which re-runs this
   * fetch and refreshes `qty_consumed`.
   */
  workOrderUpdatedAt?: string | null;
  /**
   * Bump to force a re-read, for a tie mutated OUTSIDE this panel.
   *
   * `workOrderUpdatedAt` above is the only other load dependency, and a tie
   * write does NOT touch `work_orders.updated_at` — so a tie created from the
   * Operations table (or anywhere else on the page) would otherwise leave a
   * stale list sitting right beside it, showing neither the new row nor the
   * changed plan until something unrelated bumped the work order. Any
   * monotonically-changing value works; the caller keeps a counter.
   */
  refreshToken?: number;
  /** `work_orders:edit` — ADMIN / MANAGER / SUPERVISOR. Gates PATCH + untie. */
  canEdit: boolean;
  /**
   * The work order's operations, used ONLY to compute each operation-scoped tie's
   * live consumption target so an OVER-CONSUMED tie can be flagged.
   *
   * Why this needs surfacing at all: the office reduce verb can now lower a COMPLETE
   * operation's quantities, which drops `target` below `qty_consumed` and leaves
   * material out on the floor that the production record no longer justifies. Nothing
   * forces the supervisor to then return it, and nothing else on this page — or in any
   * event — distinguishes that open loop from an ordinary tie. It is the one place the
   * reduce relaxation's soundness rests on human follow-through, so the human has to be
   * able to see it.
   *
   * Computed client-side from `utils/materialTie.ts`, which is the single home for this
   * arithmetic and carries a backend-parity test against the engine's own formula.
   * Optional: with no operations passed the flag simply does not render.
   */
  operations?: readonly WorkOrderOperation[];
}

/**
 * Bespoke status chip.
 *
 * `statusColors.ts` maps `open` to RED — correct for its usual defect/NCR
 * sense, and exactly wrong here: an allocation's `open` is the HEALTHY live
 * state, and painting it red makes every working tie indistinguishable from a
 * cancelled one.
 */
const TIE_STATUS_CLASS: Record<string, string> = {
  open: 'bg-green-500/20 text-emerald-300',
  cancelled: 'bg-slate-800/50 text-slate-400',
  // Reserved by the model, never written by any code today.
  closed: 'bg-slate-800/50 text-slate-400',
};

const SOURCE_LABEL: Record<string, string> = {
  nest: 'Nest',
  bom: 'BOM',
  manual: 'Manual',
};

/**
 * The two named return intents, in plain language.
 *
 * The raw enum values are NEVER shown — `correct_over_consumption` describes the
 * server's arithmetic, not the thing a supervisor is doing. There is deliberately
 * nothing between these two: a return that would leave `qty_consumed` below the
 * tie's live target with the tie still open is refused 422, because the sum-delta
 * engine would simply re-consume it on the next completion (or on a
 * reconcile-on-read GET, which re-runs FIFO and can credit a DIFFERENT lot than
 * the material came from — fabricated heat/cert linkage in an as-built record).
 */
const RETURN_INTENTS: {
  value: MaterialReturnIntent;
  label: string;
  help: string;
}[] = [
  {
    value: 'correct_over_consumption',
    label: 'Correct an over-count — the job keeps this material tie',
    help:
      'Puts back material the job never used. The tie stays live and keeps drawing as more runs complete. ' +
      'Bounded by the server: you can only return what was consumed ABOVE what this operation’s live ' +
      'quantities justify. Ask for more and it is refused, naming the other option.',
  },
  {
    value: 'return_and_untie',
    label: 'Return everything and untie — this job is done with this material',
    help:
      'Returns the full reported consumption and cancels the tie in the same transaction, so nothing can ' +
      'be drawn against it again. It does NOT unlock nest re-import — the ledger rows still point at this ' +
      'job’s operations, so that refusal stands and the remedy is a new work order.',
  },
];

/** `qty_consumed >= qty_planned` — the ONLY correct "fully consumed" test. */
const isFullyConsumed = (tie: MaterialAllocation): boolean =>
  Number(tie.qty_consumed || 0) >= Number(tie.qty_planned || 0);

/**
 * Float-residue guard, matching `utils/materialTie.TIE_EPSILON`. A tie sitting at
 * 1e-12 consumed has nothing worth returning and must not sprout a return button.
 */
const RETURN_EPSILON = 1e-6;

/** Anything consumed against this tie? The only gate on offering a return. */
const hasConsumption = (tie: MaterialAllocation): boolean => Number(tie.qty_consumed || 0) > RETURN_EPSILON;

const qtyWithUom = (value: number | null | undefined, uom: string): string =>
  `${formatTieQty(Number(value || 0))}${uom ? ` ${uom}` : ''}`;

/**
 * Server refusals here can be structured objects (the 409 bodies carry a code),
 * so the string guard is mandatory — rendering one raw yields `[object Object]`.
 */
function tieErrorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const nested = (detail as { detail?: unknown }).detail;
    if (typeof nested === 'string' && nested.trim()) return nested;
  }
  const message = (err as { message?: unknown })?.message;
  if (typeof message === 'string' && message.trim()) return message;
  return fallback;
}

/**
 * How a tie is scoped, in words.
 *
 * `detached_from_operation_id` is rendered explicitly: without it a tie whose
 * operation was wiped by a nest re-import is byte-identical to one that was
 * always work-order-scoped (both read `work_order_operation_id: null`), which
 * is precisely what that field exists to prevent.
 */
function scopeLabel(tie: MaterialAllocation): string {
  if (tie.work_order_operation_id != null) {
    // The `Operation #{id}` fallback is kept, NOT unified on the kiosk em-dash:
    // an untitled tie still needs to name which operation it is scoped to, and
    // the row's id is the only handle left when the number is blank.
    return hasOperationNumber(tie.operation_number)
      ? formatOperationLabel(tie.operation_number)
      : `Operation #${tie.work_order_operation_id}`;
  }
  if (tie.detached_from_operation_id != null) {
    return `was Op ${tie.detached_from_operation_id} — superseded by re-import`;
  }
  return 'Whole work order';
}

/** Sortable primitive for the scope column. */
const scopeSortKey = (tie: MaterialAllocation): string | number =>
  tie.work_order_operation_id ?? tie.detached_from_operation_id ?? Number.MAX_SAFE_INTEGER;

export default function MaterialTiesPanel({
  workOrderId,
  workOrderUpdatedAt,
  refreshToken,
  canEdit,
  operations,
}: MaterialTiesPanelProps) {
  const { showToast } = useToast();
  const [ties, setTies] = useState<MaterialAllocation[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const [editTarget, setEditTarget] = useState<MaterialAllocation | null>(null);
  const [editDraft, setEditDraft] = useState({ qtyPlanned: '', qtyPerRun: '', notes: '', clearPin: false });
  const [editError, setEditError] = useState('');
  const [saving, setSaving] = useState(false);

  const [untieTarget, setUntieTarget] = useState<MaterialAllocation | null>(null);
  const [untieError, setUntieError] = useState('');
  const [untying, setUntying] = useState(false);

  // --- RETURN (see fact 5 in the module docstring) ---------------------------
  const [returnTarget, setReturnTarget] = useState<MaterialAllocation | null>(null);
  const [returnDraft, setReturnDraft] = useState<{
    quantity: string;
    intent: MaterialReturnIntent;
    reason: string;
  }>({ quantity: '', intent: 'correct_over_consumption', reason: '' });
  const [returnError, setReturnError] = useState('');
  const [returning, setReturning] = useState(false);
  // Where the material actually came from. A pure read — it moves nothing, and a
  // failure here is advisory: it must not block the return itself.
  const [lots, setLots] = useState<MaterialConsumptionLine[]>([]);
  const [lotsLoading, setLotsLoading] = useState(false);
  const [lotsError, setLotsError] = useState('');

  const load = useCallback(async () => {
    setLoadError('');
    try {
      // include_inactive stays TRUE: cancelled rows are the tombstones the
      // ledger's allocation_id resolves to, so hiding them would hide history.
      const rows = await api.getMaterialAllocations(workOrderId);
      setTies(Array.isArray(rows) ? rows : []);
    } catch (err) {
      setLoadError(tieErrorDetail(err, 'Failed to load material ties'));
    } finally {
      setLoading(false);
    }
  }, [workOrderId]);

  useEffect(() => {
    setLoading(true);
    void load();
    // Two freshness seams, both deliberate — see the prop docstrings.
    // `workOrderUpdatedAt` catches a completion that posted consumption;
    // `refreshToken` catches a tie written elsewhere on the page, which does not
    // bump the work order at all.
  }, [load, workOrderUpdatedAt, refreshToken]);

  const openEdit = (tie: MaterialAllocation) => {
    setEditTarget(tie);
    setEditError('');
    setEditDraft({
      qtyPlanned: String(tie.qty_planned ?? ''),
      qtyPerRun: tie.qty_per_run == null ? '' : String(tie.qty_per_run),
      notes: tie.notes ?? '',
      clearPin: false,
    });
  };

  const closeEdit = () => {
    // Never dismiss mid-request: the panel must reflect only what the server
    // actually returned.
    if (saving) return;
    setEditTarget(null);
    setEditError('');
  };

  const handleSave = async () => {
    if (!editTarget || saving) return;
    const payload: MaterialAllocationUpdatePayload = {};
    const nextPlanned = Number(editDraft.qtyPlanned);
    if (editDraft.qtyPlanned.trim() !== '' && Number.isFinite(nextPlanned) && nextPlanned !== editTarget.qty_planned) {
      payload.qty_planned = nextPlanned;
    }
    if (editTarget.work_order_operation_id != null) {
      const nextPerRun = editDraft.qtyPerRun.trim() === '' ? null : Number(editDraft.qtyPerRun);
      if (nextPerRun === null ? editTarget.qty_per_run != null : nextPerRun !== editTarget.qty_per_run) {
        payload.qty_per_run = nextPerRun;
      }
    }
    const nextNotes = editDraft.notes.trim() === '' ? null : editDraft.notes;
    if (nextNotes !== (editTarget.notes ?? null)) payload.notes = nextNotes;
    if (editDraft.clearPin && editTarget.pinned_inventory_item_id != null) {
      payload.clear_pinned_inventory_item = true;
    }

    if (Object.keys(payload).length === 0) {
      setEditError('Nothing changed.');
      return;
    }

    setSaving(true);
    setEditError('');
    try {
      await api.updateMaterialAllocation(workOrderId, editTarget.id, payload);
      showToast('success', `Updated the ${editTarget.part_number || 'material'} tie`);
      setEditTarget(null);
      // Re-read rather than patch state in place: the server owns qty_consumed
      // and the derived fields, and this write is gated.
      await load();
    } catch (err) {
      setEditError(tieErrorDetail(err, 'Failed to update the material tie'));
    } finally {
      setSaving(false);
    }
  };

  const handleUntie = async () => {
    if (!untieTarget || untying) return;
    setUntying(true);
    setUntieError('');
    try {
      // Untie = status -> cancelled, never a physical delete. Refused with 409
      // once anything was consumed; the refusal renders verbatim below.
      await api.deleteMaterialAllocation(workOrderId, untieTarget.id);
      showToast('success', `Untied ${untieTarget.part_number || 'material'} from this work order`);
      setUntieTarget(null);
      await load();
    } catch (err) {
      setUntieError(tieErrorDetail(err, 'Failed to untie this material'));
    } finally {
      setUntying(false);
    }
  };

  /**
   * Open the return dialog and fetch the per-lot consumption breakdown.
   *
   * The intent DEFAULTS to `return_and_untie` on a tie that is already cancelled:
   * there is no live tie left to keep, so "correct an over-count and carry on" is
   * not a thing that can happen there. On a live tie the correction is the
   * default — it is the reversible, bounded one.
   */
  const openReturn = useCallback(
    async (tie: MaterialAllocation) => {
      setReturnTarget(tie);
      setReturnError('');
      setReturnDraft({
        quantity: '',
        intent: tie.status === 'open' ? 'correct_over_consumption' : 'return_and_untie',
        reason: '',
      });
      setLots([]);
      setLotsError('');
      setLotsLoading(true);
      try {
        const rows = await api.getMaterialAllocationConsumption(workOrderId, tie.id);
        setLots(Array.isArray(rows) ? rows : []);
      } catch (err) {
        setLotsError(tieErrorDetail(err, 'Could not read the per-lot consumption for this tie'));
      } finally {
        setLotsLoading(false);
      }
    },
    [workOrderId]
  );

  const closeReturn = () => {
    // Never dismiss mid-request: this verb MOVES STOCK, and the panel must
    // reflect only what the server actually did.
    if (returning) return;
    setReturnTarget(null);
    setReturnError('');
  };

  const consumedTotal = Number(returnTarget?.qty_consumed || 0);
  const isUntieIntent = returnDraft.intent === 'return_and_untie';
  /**
   * What actually goes on the wire.
   *
   * For `return_and_untie` the server requires `quantity == qty_consumed`
   * EXACTLY — it is a confirmation value that catches a stale client. So that
   * intent sends the tie's own float verbatim rather than anything re-parsed from
   * the (2-decimal, display-rounded) input, which would 422 on a value like
   * 2.9999999999.
   */
  const returnQuantity = isUntieIntent ? consumedTotal : Number(returnDraft.quantity);
  const returnReasonBlank = returnDraft.reason.trim().length === 0;
  const returnQuantityInvalid =
    !Number.isFinite(returnQuantity) || returnQuantity <= 0 || returnQuantity > consumedTotal + RETURN_EPSILON;

  const handleReturn = async () => {
    if (!returnTarget || returning) return;
    // Client-side gate on the two things that are decidable here. Everything
    // else — above all the correction BOUND, which is recomputed server-side
    // from live operation state — is the server's call and renders verbatim.
    if (returnQuantityInvalid || returnReasonBlank) {
      setReturnError(
        returnReasonBlank
          ? 'A reason is required — it is written to the inventory ledger and the audit trail.'
          : `Enter a quantity between 0 and the ${qtyWithUom(consumedTotal, returnTarget.unit_of_measure)} reported consumed.`
      );
      return;
    }

    setReturning(true);
    setReturnError('');
    try {
      const result = await api.returnMaterialAllocation(workOrderId, returnTarget.id, {
        quantity: returnQuantity,
        intent: returnDraft.intent,
        reason: returnDraft.reason.trim(),
      });
      // Report what the SERVER credited, not what was asked for: `returned_lots`
      // and `quantity_returned` are the response's own fields (see
      // `MaterialReturnResult`). This read `result.lines` — a field the server has
      // never sent — so the lot count was always 0 and the "…to 2 lots"
      // disclosure silently vanished with no error to notice.
      const creditedLots = Array.isArray(result?.returned_lots) ? result.returned_lots : [];
      const creditedQty = Number.isFinite(result?.quantity_returned) ? result.quantity_returned : returnQuantity;
      const lotNames = creditedLots
        .map((lot) => lot.lot_number)
        .filter((lot): lot is string => Boolean(lot && lot.trim()));
      // Name the lots when there are few enough to read; fall back to a count.
      // Which lots took the material back is the fact an operator has to check
      // against the rack, so it belongs in the confirmation, not just the ledger.
      const lotClause =
        lotNames.length > 0 && lotNames.length <= 3
          ? ` to lot ${lotNames.join(', ')}`
          : creditedLots.length > 0
            ? ` to ${creditedLots.length} lot${creditedLots.length === 1 ? '' : 's'}`
            : '';
      showToast(
        'success',
        `Returned ${qtyWithUom(creditedQty, result?.unit_of_measure || returnTarget.unit_of_measure)} of ${
          result?.part_number || returnTarget.part_number || 'material'
        }${lotClause}`
      );
      setReturnTarget(null);
      // Re-read rather than patch: the server owns qty_consumed and status, and
      // this write moved stock.
      await load();
    } catch (err) {
      setReturnError(tieErrorDetail(err, 'Failed to return this material'));
    } finally {
      setReturning(false);
    }
  };

  // Operation id -> operation, for the over-consumption flag. A tie whose operation
  // is absent (work-order-scoped, or detached by a nest re-import) simply resolves
  // undefined and the helper returns null rather than guessing "squared up".
  const operationsById = useMemo(
    () => new Map<number, WorkOrderOperation>((operations ?? []).map((op) => [op.id, op])),
    [operations]
  );

  const columns = useMemo<DataTableColumn<MaterialAllocation>[]>(() => {
    const base: DataTableColumn<MaterialAllocation>[] = [
      {
        key: 'part',
        header: 'Material',
        sortable: true,
        accessor: (tie) => tie.part_number || '',
        render: (tie) => (
          <div className="min-w-0">
            <p className="truncate font-mono text-sm font-semibold text-slate-100">
              {tie.part_number || `Part #${tie.part_id}`}
            </p>
            {tie.part_name && <p className="truncate text-xs text-slate-400">{tie.part_name}</p>}
          </div>
        ),
      },
      {
        key: 'scope',
        header: 'Scope',
        sortable: true,
        accessor: scopeSortKey,
        csv: (tie) => scopeLabel(tie),
        render: (tie) => (
          <span
            className={`text-xs ${tie.detached_from_operation_id != null && tie.work_order_operation_id == null ? 'text-amber-300' : 'text-slate-300'}`}
          >
            {scopeLabel(tie)}
          </span>
        ),
      },
      {
        key: 'source',
        header: 'Source',
        sortable: true,
        accessor: (tie) => tie.source,
        render: (tie) => (
          <span className="font-mono text-[11px] uppercase tracking-wider text-slate-400">
            {SOURCE_LABEL[tie.source] || tie.source}
          </span>
        ),
      },
      {
        key: 'per_run',
        header: 'Per run',
        align: 'right',
        sortable: true,
        accessor: (tie) => tie.qty_per_run ?? -1,
        csv: (tie) => (tie.qty_per_run == null ? '' : formatTieQty(tie.qty_per_run)),
        render: (tie) => (
          <span className="font-mono text-sm tabular-nums text-slate-300">
            {tie.qty_per_run == null ? '—' : formatTieQty(tie.qty_per_run)}
          </span>
        ),
      },
      {
        key: 'planned',
        header: 'Planned',
        align: 'right',
        sortable: true,
        accessor: (tie) => Number(tie.qty_planned || 0),
        csv: (tie) => qtyWithUom(tie.qty_planned, tie.unit_of_measure),
        render: (tie) => (
          <span className="font-mono text-sm font-semibold tabular-nums text-slate-100">
            {qtyWithUom(tie.qty_planned, tie.unit_of_measure)}
          </span>
        ),
      },
      {
        key: 'consumed',
        // A CACHE, not a compliance figure — the ledger is authoritative.
        header: 'Consumed (reported)',
        align: 'right',
        sortable: true,
        accessor: (tie) => Number(tie.qty_consumed || 0),
        csv: (tie) => qtyWithUom(tie.qty_consumed, tie.unit_of_measure),
        render: (tie) => {
          // The open loop an office reduce can leave: material consumed that the
          // production record no longer justifies. Flagged here rather than left
          // silent — nothing else distinguishes it from an ordinary tie, and it is
          // the one state whose resolution depends on a human noticing.
          const over = overConsumedQty(tie, operationsById.get(tie.work_order_operation_id ?? -1));
          return (
            <span className="inline-flex items-center justify-end gap-1.5">
              <span
                title="Reported total (a cache). The inventory ledger is the authoritative consumed record."
                className={`font-mono text-sm tabular-nums ${
                  isFullyConsumed(tie) ? 'text-emerald-300' : 'text-slate-300'
                }`}
              >
                {qtyWithUom(tie.qty_consumed, tie.unit_of_measure)}
              </span>
              {over != null && over > 0 && (
                <span
                  data-testid={`tie-over-consumed-${tie.id}`}
                  title={
                    `${formatTieQty(over)} ${tie.unit_of_measure} more than this operation's ` +
                    'recorded production accounts for — usually a corrected count whose material ' +
                    'was never returned. Use Return material to square it up.'
                  }
                  className="rounded-none border border-fd-amber/45 bg-fd-amber/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-amber"
                >
                  +{formatTieQty(over)} out
                </span>
              )}
            </span>
          );
        },
      },
      {
        key: 'lot',
        header: 'Lot',
        sortable: true,
        accessor: (tie) => tie.pinned_lot_number || '',
        csv: (tie) => tie.pinned_lot_number || 'FIFO',
        render: (tie) =>
          tie.pinned_lot_number ? (
            <span className="font-mono text-xs text-slate-200" title="Pinned: consumption draws from this lot only.">
              {tie.pinned_lot_number}
            </span>
          ) : (
            <span className="text-xs text-slate-500" title="Unpinned — FIFO picks the lot at consume time.">
              FIFO
            </span>
          ),
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: (tie) => tie.status,
        render: (tie) => (
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium capitalize ${
                TIE_STATUS_CLASS[tie.status] || 'bg-slate-800/50 text-slate-400'
              }`}
            >
              {tie.status}
            </span>
            {tie.status === 'open' && isFullyConsumed(tie) && (
              <span
                className="font-mono text-[10px] uppercase tracking-wider text-emerald-300"
                title="Fully consumed against plan. Ties are never auto-closed — status stays open."
              >
                Fully consumed
              </span>
            )}
          </div>
        ),
      },
      {
        key: 'created',
        header: 'Tied',
        sortable: true,
        accessor: (tie) => tie.created_at,
        csv: (tie) => formatCentralDateTime(tie.created_at),
        render: (tie) => (
          <span className="whitespace-nowrap text-xs text-slate-400">{formatCentralDateTime(tie.created_at)}</span>
        ),
      },
    ];

    if (!canEdit) return base;

    return [
      ...base,
      {
        key: 'actions',
        header: 'Actions',
        align: 'right',
        render: (tie) => {
          const live = tie.status === 'open';
          // RETURN is offered on a CANCELLED tie too — see fact 5. Edit/untie are
          // not: there is no live tie left for either of them to act on.
          const canReturn = hasConsumption(tie);
          if (!live && !canReturn) return <span className="text-xs text-slate-600">—</span>;
          return (
            <div className="flex items-center justify-end gap-1">
              {live && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit the ${tie.part_number || 'material'} tie`}
                  title="Edit planned quantity, per-run rate, lot pin or notes"
                  onClick={() => openEdit(tie)}
                >
                  <PencilSquareIcon className="h-4 w-4" aria-hidden="true" />
                </Button>
              )}
              {canReturn && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Return consumed ${tie.part_number || 'material'} to stock`}
                  title="Return consumed material to the lots it came from (reason required)"
                  className="text-amber-300"
                  onClick={() => void openReturn(tie)}
                >
                  <ArrowUturnLeftIcon className="h-4 w-4" aria-hidden="true" />
                </Button>
              )}
              {live && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Untie ${tie.part_number || 'material'} from this work order`}
                  title="Untie this material (refused once anything has been consumed)"
                  className="text-fd-red"
                  onClick={() => {
                    setUntieTarget(tie);
                    setUntieError('');
                  }}
                >
                  <LinkSlashIcon className="h-4 w-4" aria-hidden="true" />
                </Button>
              )}
            </div>
          );
        },
      },
    ];
  }, [canEdit, openReturn, operationsById]);

  const openTies = ties.filter((tie) => tie.status === 'open');

  /**
   * LIVE ties first, then by scope, then by id — cancelled rows are history and
   * must not sit above the ties that are actually going to consume. A new array
   * every time: the `data` prop is never mutated (DataTable's own sort is pure
   * for the same reason, and takes over the moment a header is clicked).
   */
  const orderedTies = useMemo(
    () =>
      [...ties].sort((a, b) => {
        const liveDelta = Number(b.status === 'open') - Number(a.status === 'open');
        if (liveDelta !== 0) return liveDelta;
        const scopeDelta = Number(scopeSortKey(a)) - Number(scopeSortKey(b));
        if (scopeDelta !== 0) return scopeDelta;
        return a.id - b.id;
      }),
    [ties]
  );

  return (
    <div className="card card-compact" data-testid="wo-material-ties">
      <div className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="card-title">Material Ties</h2>
          <p className="card-subtitle">
            Stock material tied to this work order. A tie scoped to an operation is deducted when that{' '}
            <strong className="text-slate-300">operation</strong> completes — not per run. A whole-work-order tie
            drains when the work order finishes.
          </p>
        </div>
        {openTies.length > 0 && (
          <span className="shrink-0 whitespace-nowrap rounded border border-fd-line px-2 py-0.5 font-mono text-xs text-slate-300">
            {openTies.length} live tie{openTies.length === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <DataTable<MaterialAllocation>
        columns={columns}
        data={orderedTies}
        rowKey={(tie) => tie.id}
        loading={loading}
        error={loadError || undefined}
        onRetry={() => {
          setLoading(true);
          void load();
        }}
        dense
        // Cancelled rows are TOMBSTONES, kept on purpose so the ledger's
        // allocation_id always resolves — dimmed, never filtered away.
        rowClassName={(tie) => (tie.status === 'open' ? '' : 'opacity-50')}
        empty={{
          icon: CubeIcon,
          title: 'No material tied to this work order',
          description:
            'Tying material is optional. An untied work order behaves exactly as it did before this feature existed — nothing is deducted from stock.',
        }}
      />

      {ties.some((tie) => tie.status !== 'open') && (
        <p className="mt-2 text-xs text-slate-500">
          Cancelled ties stay listed: they are the record the inventory ledger points back to.
        </p>
      )}

      {/* --- Edit (server-GATED, NON-optimistic) ------------------------------ */}
      <Modal open={editTarget !== null} onClose={closeEdit} size="md" padded={false} scroll={false}>
        {editTarget && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">
                Edit tie — {editTarget.part_number || `Part #${editTarget.part_id}`}
              </h3>
            </div>
            <div className="modal-body space-y-4">
              <p className="text-xs text-slate-400">
                {scopeLabel(editTarget)} · reported consumed{' '}
                {qtyWithUom(editTarget.qty_consumed, editTarget.unit_of_measure)}. The material part and scope are
                fixed at creation — untie and re-tie to change what this points at.
              </p>

              <div>
                <label htmlFor="tie-qty-planned" className="label">
                  Planned quantity ({editTarget.unit_of_measure})
                </label>
                <input
                  id="tie-qty-planned"
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step="any"
                  className="input"
                  value={editDraft.qtyPlanned}
                  onChange={(e) => setEditDraft({ ...editDraft, qtyPlanned: e.target.value })}
                />
                <p className="mt-1 text-xs text-slate-500">
                  Cannot be lowered below what has already been consumed.
                </p>
              </div>

              {editTarget.work_order_operation_id != null && (
                <div>
                  <label htmlFor="tie-qty-per-run" className="label">
                    Per completed run ({editTarget.unit_of_measure})
                  </label>
                  <input
                    id="tie-qty-per-run"
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="any"
                    className="input"
                    placeholder="1"
                    value={editDraft.qtyPerRun}
                    onChange={(e) => setEditDraft({ ...editDraft, qtyPerRun: e.target.value })}
                  />
                  <p className="mt-1 text-xs text-slate-500">
                    Blank means 1 per run. Scrapped runs count too — a scrapped run still used its material.
                  </p>
                </div>
              )}

              {editTarget.pinned_inventory_item_id != null && (
                <div className="flex items-start gap-2">
                  <input
                    id="tie-clear-pin"
                    type="checkbox"
                    className="mt-1"
                    checked={editDraft.clearPin}
                    onChange={(e) => setEditDraft({ ...editDraft, clearPin: e.target.checked })}
                  />
                  <label htmlFor="tie-clear-pin" className="text-sm text-slate-300">
                    Clear the lot pin ({editTarget.pinned_lot_number || 'pinned lot'}) and fall back to FIFO
                  </label>
                </div>
              )}

              <div>
                <label htmlFor="tie-notes" className="label">
                  Notes
                </label>
                <input
                  id="tie-notes"
                  type="text"
                  maxLength={255}
                  className="input"
                  value={editDraft.notes}
                  onChange={(e) => setEditDraft({ ...editDraft, notes: e.target.value })}
                />
              </div>

              {/* Verbatim server refusal — the primary display for a gated write. */}
              {editError && (
                <div
                  role="alert"
                  data-testid="wo-tie-edit-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
                >
                  {editError}
                </div>
              )}
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={closeEdit} disabled={saving}>
                Cancel
              </Button>
              <LoadingButton loading={saving} loadingText="Saving…" onClick={handleSave}>
                Save tie
              </LoadingButton>
            </div>
          </>
        )}
      </Modal>

      {/* --- Untie (server-GATED, NON-optimistic) ---------------------------- */}
      <Modal
        open={untieTarget !== null}
        onClose={() => {
          if (untying) return;
          setUntieTarget(null);
          setUntieError('');
        }}
        size="md"
        padded={false}
        scroll={false}
      >
        {untieTarget && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">
                Untie {untieTarget.part_number || `Part #${untieTarget.part_id}`}?
              </h3>
            </div>
            <div className="modal-body space-y-3">
              {/* No timing word on purpose: this dialog is reached from both
                  scopes, and an operation-scoped tie stops drawing at its
                  operation's completion while a whole-work-order tie stops at
                  the job's. "Nothing further" is true of both. */}
              <p className="text-sm text-slate-300">
                Nothing further will be drawn from stock for{' '}
                {untieTarget.part_number || 'this material'} on this work order. The tie is cancelled, never
                deleted, so any consumption already posted keeps its traceability link.
              </p>
              <p className="text-xs text-slate-500">
                Refused if anything has already been consumed against it. Use <strong>Return material</strong> on
                the row instead — reversing a posted consumption is a separate, reasoned correction, and
                &ldquo;return everything and untie&rdquo; does both in one transaction.
              </p>
              {untieError && (
                <div
                  role="alert"
                  data-testid="wo-tie-untie-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
                >
                  {untieError}
                </div>
              )}
            </div>
            <div className="modal-footer">
              <Button
                variant="secondary"
                onClick={() => {
                  if (untying) return;
                  setUntieTarget(null);
                  setUntieError('');
                }}
                disabled={untying}
              >
                Cancel
              </Button>
              <LoadingButton variant="danger" loading={untying} loadingText="Untying…" onClick={handleUntie}>
                Untie material
              </LoadingButton>
            </div>
          </>
        )}
      </Modal>

      {/* --- Return material (server-GATED, NON-optimistic) ------------------ */}
      <Modal open={returnTarget !== null} onClose={closeReturn} size="2xl" padded={false} scroll={false}>
        {returnTarget && (
          <>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">
                Return material — {returnTarget.part_number || `Part #${returnTarget.part_id}`}
              </h3>
            </div>
            <div className="modal-body max-h-[70vh] space-y-4 overflow-y-auto">
              <div className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200/90">
                Puts consumed material back on the shelf as a signed compensating transaction. The original
                consumption rows are never edited — the ledger only ever grows. Recorded on the audit trail with
                your name and reason.
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <div className="rounded-sm border border-fd-line px-3 py-2">
                  <p className="text-[11px] uppercase tracking-wider text-slate-500">Reported consumed</p>
                  <p
                    data-testid="wo-tie-return-consumed"
                    className="font-mono text-lg font-semibold tabular-nums text-slate-100"
                  >
                    {qtyWithUom(returnTarget.qty_consumed, returnTarget.unit_of_measure)}
                  </p>
                </div>
                <div className="rounded-sm border border-fd-line px-3 py-2">
                  <p className="text-[11px] uppercase tracking-wider text-slate-500">Planned</p>
                  <p className="font-mono text-lg font-semibold tabular-nums text-slate-300">
                    {qtyWithUom(returnTarget.qty_planned, returnTarget.unit_of_measure)}
                  </p>
                </div>
                <div className="rounded-sm border border-fd-line px-3 py-2">
                  <p className="text-[11px] uppercase tracking-wider text-slate-500">Scope</p>
                  <p className="text-sm text-slate-300">{scopeLabel(returnTarget)}</p>
                </div>
              </div>

              {/* The live target is NOT shown, because this panel genuinely does
                  not have it: `MaterialAllocation` carries the plan, not the
                  operation's live complete/scrap quantities the bound is built
                  from. Saying so is better than showing `qty_planned` and letting
                  it be read as the bound. */}
              <p className="text-xs text-slate-500">
                The correction bound is <strong className="text-slate-400">recomputed on the server</strong> from
                this operation&rsquo;s live completed and scrapped quantities at the moment you submit — never from
                the planned figure above, and not available to this panel. Ask for more than it allows and the
                refusal says so, and names the other option.
              </p>

              {/* --- Which lots the material goes back to ---------------------- */}
              <div>
                <p className="label mb-1">Lots this material came from</p>
                {lotsLoading ? (
                  <p className="text-xs text-slate-500">Reading the consumption ledger…</p>
                ) : lotsError ? (
                  <p role="status" data-testid="wo-tie-return-lots-error" className="text-xs text-amber-300">
                    {lotsError} — the return itself is unaffected; the server picks the source lots.
                  </p>
                ) : lots.length === 0 ? (
                  <p className="text-xs text-slate-500">No per-lot detail returned for this tie.</p>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[26rem] text-sm" data-testid="wo-tie-return-lots">
                      <thead>
                        <tr className="border-b border-fd-line text-[11px] uppercase tracking-wider text-slate-500">
                          <th scope="col" className="py-1 text-left font-medium">
                            Lot
                          </th>
                          <th scope="col" className="py-1 text-right font-medium">
                            Issued
                          </th>
                          <th scope="col" className="py-1 text-right font-medium">
                            Already returned
                          </th>
                          <th scope="col" className="py-1 text-right font-medium">
                            Still out
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {lots.map((lot) => (
                          <tr key={`${lot.inventory_item_id}`} className="border-b border-fd-line/50">
                            <td className="py-1 font-mono text-xs text-slate-200">
                              {lot.lot_number || <span className="text-slate-500">no lot</span>}
                            </td>
                            <td className="py-1 text-right font-mono tabular-nums text-slate-400">
                              {formatTieQty(Number(lot.issued || 0))}
                            </td>
                            <td className="py-1 text-right font-mono tabular-nums text-slate-400">
                              {formatTieQty(Number(lot.returned || 0))}
                            </td>
                            <td className="py-1 text-right font-mono font-semibold tabular-nums text-slate-100">
                              {formatTieQty(Number(lot.net || 0))}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <p className="mt-1 text-xs text-slate-500">
                  Material always goes back to the lots it came from, newest first — a single consumption can
                  spill across several FIFO lots, so one return can be several credits. Crediting any other lot
                  would invent heat/cert linkage, so there is no lot to choose.
                </p>
              </div>

              {/* --- The two named intents ------------------------------------ */}
              <fieldset className="space-y-2">
                <legend className="label mb-1">What are you doing?</legend>
                {RETURN_INTENTS.map((option) => (
                  <div
                    key={option.value}
                    className={`flex items-start gap-2 rounded-sm border px-3 py-2 ${
                      returnDraft.intent === option.value
                        ? 'border-werco-navy/70 bg-werco-navy/10'
                        : 'border-fd-line'
                    }`}
                  >
                    <input
                      id={`tie-return-intent-${option.value}`}
                      type="radio"
                      name="tie-return-intent"
                      className="mt-1"
                      value={option.value}
                      checked={returnDraft.intent === option.value}
                      disabled={returning}
                      onChange={() =>
                        setReturnDraft((draft) => ({ ...draft, intent: option.value }))
                      }
                    />
                    <div className="min-w-0">
                      <label
                        htmlFor={`tie-return-intent-${option.value}`}
                        className="block text-sm font-medium text-slate-200"
                      >
                        {option.label}
                      </label>
                      <p className="mt-0.5 text-xs text-slate-500">{option.help}</p>
                    </div>
                  </div>
                ))}
              </fieldset>

              {returnTarget.status !== 'open' && (
                <p className="text-xs text-slate-500">
                  This tie is already <strong className="text-slate-400">{returnTarget.status}</strong>, so nothing
                  will be drawn against it again either way — returning everything is the usual choice here.
                </p>
              )}

              <FormField
                label={`Quantity to return (${returnTarget.unit_of_measure})`}
                required
                help={
                  isUntieIntent
                    ? 'Fixed to the full reported consumption — the server takes this as your confirmation that you mean all of it.'
                    : `At most the ${qtyWithUom(returnTarget.qty_consumed, returnTarget.unit_of_measure)} reported consumed, and within the server's bound.`
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
                    disabled={isUntieIntent || returning}
                    value={isUntieIntent ? formatTieQty(consumedTotal) : returnDraft.quantity}
                    onChange={(e) => setReturnDraft((draft) => ({ ...draft, quantity: e.target.value }))}
                  />
                )}
              </FormField>

              <FormField
                label="Reason for the return"
                required
                help="Written to the inventory ledger note AND the audit trail. Required — a blank one is refused."
              >
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    maxLength={500}
                    className="input"
                    placeholder="e.g. nest 2 was re-cut on remnant; two full sheets never left the rack"
                    disabled={returning}
                    value={returnDraft.reason}
                    onChange={(e) => setReturnDraft((draft) => ({ ...draft, reason: e.target.value }))}
                  />
                )}
              </FormField>

              {/* Verbatim server refusal — the primary display for a gated write.
                  The 422 on an unbounded correction NAMES the intent to use
                  instead, which is the whole reason it renders in full. */}
              {returnError && (
                <div
                  role="alert"
                  data-testid="wo-tie-return-error"
                  className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
                >
                  {returnError}
                </div>
              )}
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={closeReturn} disabled={returning}>
                Cancel
              </Button>
              {/* Deliberately NOT `disabled` on an invalid draft — the same call
                  the Edit dialog above makes. A dead button says nothing about
                  WHY; the handler's guard renders a specific reason into the
                  role="alert" block instead. */}
              <LoadingButton variant="danger" loading={returning} loadingText="Returning…" onClick={handleReturn}>
                Return material
              </LoadingButton>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
