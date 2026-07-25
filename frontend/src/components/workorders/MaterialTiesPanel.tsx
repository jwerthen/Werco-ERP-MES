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
 *    409 on a terminal work order, 422 on a held-lot pin — so per the
 *    optimistic-UI convention these are strictly NON-optimistic: loading state
 *    on the control, only what the server returns is rendered, and the server's
 *    `detail` is surfaced verbatim.
 *
 * An untied work order renders the panel's empty state and nothing else; it
 * never nags.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { CubeIcon, LinkSlashIcon, PencilSquareIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, LoadingButton, Modal, useToast } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatCentralDateTime } from '../../utils/centralTime';
import { formatTieQty } from '../../utils/materialTie';
import type { MaterialAllocation, MaterialAllocationUpdatePayload } from '../../types';

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
  /** `work_orders:edit` — ADMIN / MANAGER / SUPERVISOR. Gates PATCH + untie. */
  canEdit: boolean;
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

/** `qty_consumed >= qty_planned` — the ONLY correct "fully consumed" test. */
const isFullyConsumed = (tie: MaterialAllocation): boolean =>
  Number(tie.qty_consumed || 0) >= Number(tie.qty_planned || 0);

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
    return tie.operation_number ? `Op ${tie.operation_number}` : `Operation #${tie.work_order_operation_id}`;
  }
  if (tie.detached_from_operation_id != null) {
    return `was Op ${tie.detached_from_operation_id} — superseded by re-import`;
  }
  return 'Whole work order';
}

/** Sortable primitive for the scope column. */
const scopeSortKey = (tie: MaterialAllocation): string | number =>
  tie.work_order_operation_id ?? tie.detached_from_operation_id ?? Number.MAX_SAFE_INTEGER;

export default function MaterialTiesPanel({ workOrderId, workOrderUpdatedAt, canEdit }: MaterialTiesPanelProps) {
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
    // `workOrderUpdatedAt` is the freshness seam — see the prop docstring.
  }, [load, workOrderUpdatedAt]);

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
        render: (tie) => (
          <span
            title="Reported total (a cache). The inventory ledger is the authoritative consumed record."
            className={`font-mono text-sm tabular-nums ${isFullyConsumed(tie) ? 'text-emerald-300' : 'text-slate-300'}`}
          >
            {qtyWithUom(tie.qty_consumed, tie.unit_of_measure)}
          </span>
        ),
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
        render: (tie) =>
          tie.status === 'open' ? (
            <div className="flex items-center justify-end gap-1">
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Edit the ${tie.part_number || 'material'} tie`}
                title="Edit planned quantity, per-run rate, lot pin or notes"
                onClick={() => openEdit(tie)}
              >
                <PencilSquareIcon className="h-4 w-4" aria-hidden="true" />
              </Button>
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
            </div>
          ) : (
            <span className="text-xs text-slate-600">—</span>
          ),
      },
    ];
  }, [canEdit]);

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
                Refused if anything has already been consumed against it — reversing a posted consumption is a
                separate, reasoned correction.
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
    </div>
  );
}
