/**
 * Combine two SKUs into one.
 *
 * A materials-numbering recut can leave two part numbers describing the SAME
 * physical sheet — `.0625-60X144-304SS` with 92 on hand and
 * `SH-A240-304-0.0625-60X144-2B` with 141. The shop has 233 sheets, and every
 * obvious fix is wrong: RECEIVING the 92 onto the target overstates the shop by
 * 92, and ISSUING them against a made-up work order puts phantom material into
 * job costing. So this screen drives one verb that does neither — each moved lot
 * line posts a linked pair of `ADJUST` rows that sum to exactly zero, the heat
 * lot and cert follow the material onto the target row, and the source part is
 * left in the catalog at zero rather than deleted.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS HAND-BUILT RATHER THAN A SHARED DIALOG PRIMITIVE
 * ---------------------------------------------------------------------------
 * The same reasoning as its sibling `RenumberPartDialog`: `<ConfirmDialog>`
 * types `message` as a string and this screen renders two computed stock panels,
 * a lot table per side, a reservations table and a diagnostics list;
 * `<InputDialog>` is single-field by contract and this needs a quantity, a
 * reason and a checkbox per flagged part. The final go/no-go IS a
 * `<ConfirmDialog>` though — see the confirm step at the foot of the file.
 *
 * ---------------------------------------------------------------------------
 * NON-OPTIMISTIC, AND NOT NEGOTIABLE (the CLAUDE.md convention)
 * ---------------------------------------------------------------------------
 * This is a server-GATED action whose entire point is that the server may
 * refuse. Nothing is folded locally before the response; the caller receives the
 * SERVER's result object, never a locally patched pair of rows. A 409 keeps this
 * dialog open with the server's verbatim `detail` and every typed value intact,
 * because a refusal that costs the operator their reason text is a refusal they
 * will route around.
 *
 * ---------------------------------------------------------------------------
 * THE PREVIEW IS LOAD-BEARING HERE, WHICH IS THE ONE PLACE THIS DIFFERS FROM
 * RenumberPartDialog
 * ---------------------------------------------------------------------------
 * Renumber lets a failed impact read through — the server re-checks everything
 * anyway, and refusing to let someone TRY because the PREVIEW broke is the worse
 * failure. Not here. `expected_source_part_number` / `expected_target_part_number`
 * are the compare-and-swap preconditions (a `Part` maps no optimistic-lock
 * version column, so those strings are the only concurrency control there is),
 * and the only honest source for them is what the preview just READ. Inventing
 * them from the picker's own list would turn the CAS into a rubber stamp against
 * a value this dialog never confirmed. So a failed preview renders `<ErrorState>`
 * with Retry and there is nothing to submit.
 *
 * ---------------------------------------------------------------------------
 * WHY THE QUANTITY FIELD DOES NOT RE-PREVIEW ON EVERY KEYSTROKE
 * ---------------------------------------------------------------------------
 * `POST /inventory/combine/preview` accepts an optional `quantity`, so a
 * debounced re-read per keystroke is possible — and deliberately not done. Both
 * quantity-dependent refusals are already fully determined by numbers the first
 * preview returned: `quantity_exceeds_available` is `requested >
 * source.eligible_available`, and `open_work_order_reservation` is `requested >
 * max_combinable_quantity` (which is defined as the largest quantity that would
 * NOT strand an open tie). Re-fetching would put a spinner under the operator's
 * fingers on a field they are still typing to learn something the client can
 * already compute. The server re-runs every probe on the write regardless, so
 * nothing rests on the client's arithmetic.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowRightIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import {
  Button,
  ComboBox,
  ComboBoxOption,
  ConfirmDialog,
  DataTable,
  DataTableColumn,
  ErrorState,
  FormField,
  LoadingButton,
  Modal,
  Skeleton,
  StatusBadge,
  useToast,
} from '../ui';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { toDisplayString } from '../../utils/apiError';
import type {
  CombineDiagnostic,
  CombineFlaggedPart,
  CombineOpenReservation,
  CombinePartStockSummary,
  CombineStockLine,
  InventoryCombinePreview,
  InventoryCombineResult,
  Part,
} from '../../types';

/**
 * The shape this dialog needs out of a part row, and no more.
 *
 * Structural rather than `Part` so both callers can pass what they already hold:
 * the Inventory page's untyped `/parts` list and the Materials page's `Part[]`.
 * Nothing here reads a field the two lists do not share.
 */
export type CombinePartOption = Pick<Part, 'id' | 'part_number' | 'name' | 'unit_of_measure'>;

export interface CombineInventoryDialogProps {
  open: boolean;
  /** Every part/material the caller has loaded, for the two pickers. */
  parts: CombinePartOption[];
  /** Pre-select the source — e.g. opened from one material's edit form. */
  initialSourcePartId?: number | null;
  onClose: () => void;
  /**
   * Receives the SERVER's result — never a locally folded pair of rows. Closing
   * is the caller's job (via `open`), so a refusal can keep this dialog up.
   */
  onCombined: (result: InventoryCombineResult) => void;
}

/** Float slack. Quantities cross the wire as floats; `92 !== 92.00000000001`. */
const EPS = 1e-6;

/** Quantities read as a planner writes them: 92, 12.5, 0.0625 — never 92.000. */
function formatQty(value: number): string {
  if (!Number.isFinite(value)) return '0';
  return String(Number(value.toFixed(4)));
}

/**
 * A stocking unit, or a word that stands in for one.
 *
 * `unit_of_measure` is `Optional[str]` on the server and a part that states no
 * unit is a real, allowed row — the server treats a blank on either side as
 * AGREEMENT rather than a mismatch. Interpolating it raw would print "null"
 * into a sentence an operator is being asked to confirm.
 */
function unitLabel(unit?: string | null): string {
  return unit || 'units';
}

function formatMoney(value: number): string {
  if (!Number.isFinite(value)) return '$0.00';
  return `$${value.toFixed(2)}`;
}

/**
 * What to DO about each refusal, in the operator's words.
 *
 * Paired with — never instead of — the server's own `detail` sentence. The
 * server writes `detail` to read correctly inside the 409 the operator would
 * get, so that string stays the authoritative text and this map only adds the
 * next step it implies. An unrecognised code still renders its server sentence,
 * so a refusal this build has never heard of is disclosed rather than swallowed.
 */
const BLOCKER_NEXT_STEP: Record<string, string> = {
  same_part: 'Pick two different items. Combining is for two numbers that describe one physical material.',
  part_not_found: 'That item is not in this company’s catalog. Check the number, or pick it from the list again.',
  part_deleted: 'Restore the item first, or pick a different number. A deleted part cannot take or give stock.',
  unit_of_measure_mismatch:
    'These two are counted in different units, so the quantities do not mean the same thing. Correct the unit of measure on one of them first — folding them now would be adding sheets to pounds.',
  no_available_stock:
    'There is nothing left to move. Stock that is on hold, quarantined, rejected or fully allocated is never folded — the lot table above says which rows those are.',
  quantity_exceeds_available: 'Lower the quantity to what is actually available on the source.',
  open_work_order_reservation:
    'Open jobs are still tied to the source item for this material. Either move only the quantity shown as safe, or re-tie those jobs to the target item first.',
  flagged_part_not_acknowledged:
    'One of these items is named test or housing. Tick the acknowledgement below to confirm you mean this one.',
  expected_part_number_mismatch:
    'Somebody renumbered one of these parts while this was open. Close and reopen so you are looking at the current numbers.',
  source_still_has_stock:
    'The source will not land at zero, so it cannot be marked inactive as part of this. Untick that box, or move the rest of the stock first.',
};

/** One blocker or advisory. `detail` is the server's, verbatim. */
function DiagnosticRow({ diagnostic, tone }: { diagnostic: CombineDiagnostic; tone: 'blocking' | 'advisory' }) {
  const blocking = tone === 'blocking';
  const nextStep = BLOCKER_NEXT_STEP[diagnostic.code];
  return (
    <li
      className={`flex items-start gap-2 rounded-sm border px-2.5 py-2 text-xs ${
        blocking ? 'border-fd-red/40 bg-fd-red/10 text-red-200' : 'border-fd-amber/35 bg-fd-amber/10 text-amber-200'
      }`}
      role={blocking ? 'alert' : undefined}
    >
      <ExclamationTriangleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block">{diagnostic.detail}</span>
        {nextStep && <span className="mt-0.5 block opacity-90">{nextStep}</span>}
        <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wider opacity-60">
          {diagnostic.code}
        </span>
      </span>
    </li>
  );
}

/** Why a stock row is not going anywhere, when the server did not say. */
function ineligibleReasonText(line: CombineStockLine): string {
  return line.ineligible_reason || 'Not available to move';
}

/**
 * The per-lot table on each side.
 *
 * COLUMN ORDER IS DELIBERATE. Two of these panels sit side by side, so each
 * table is roughly half the dialog wide and the tail scrolls horizontally.
 * "Moves?" therefore comes BEFORE Allocated and Unit Cost: it is the column
 * that says a lot is staying behind on hold, and a signal that has to be
 * scrolled to is a signal an operator will discover after the fact instead.
 * Allocated and Unit Cost are also totalled in the summary above each table,
 * so they are the safe pair to push off the edge.
 */
const lineColumns: Array<DataTableColumn<CombineStockLine>> = [
  {
    key: 'location',
    header: 'Location',
    sortable: true,
    accessor: (line) => line.location ?? '',
    render: (line) => (
      <span className="font-mono text-xs">
        {line.location || '—'}
        {line.warehouse ? <span className="ml-1 text-slate-500">/ {line.warehouse}</span> : null}
      </span>
    ),
  },
  {
    key: 'lot',
    header: 'Lot / Serial',
    sortable: true,
    accessor: (line) => line.lot_number ?? '',
    render: (line) => (
      <span className="font-mono text-xs">
        {line.lot_number || '—'}
        {line.serial_number ? <span className="ml-1 text-slate-500">#{line.serial_number}</span> : null}
      </span>
    ),
  },
  {
    key: 'on_hand',
    header: 'On Hand',
    sortable: true,
    align: 'right',
    className: 'font-medium tabular-nums',
    accessor: (line) => line.quantity_on_hand,
    render: (line) => formatQty(line.quantity_on_hand),
  },
  {
    key: 'eligible',
    header: 'Moves?',
    sortable: true,
    accessor: (line) => (line.eligible ? 'Yes' : ineligibleReasonText(line)),
    render: (line) =>
      line.eligible ? (
        <span className="text-xs text-fd-green">Yes</span>
      ) : (
        // Stock that stays put is stated ROW BY ROW, not summarised. An operator
        // who folds two SKUs and finds material still sitting under the old
        // number has to be able to see, here, which lot it was and why.
        <span className="text-xs text-fd-amber">{ineligibleReasonText(line)}</span>
      ),
  },
  {
    key: 'allocated',
    header: 'Allocated',
    sortable: true,
    align: 'right',
    className: 'tabular-nums',
    accessor: (line) => line.quantity_allocated,
    render: (line) => formatQty(line.quantity_allocated),
  },
  {
    key: 'unit_cost',
    header: 'Unit Cost',
    sortable: true,
    align: 'right',
    className: 'tabular-nums',
    accessor: (line) => line.unit_cost,
    render: (line) => formatMoney(line.unit_cost),
  },
];

/**
 * One side of the fold.
 *
 * The prop is `side`, not `role`: jsx-a11y reads a `role` attribute on ANY JSX
 * element — a component included — as an ARIA role and fails the build on
 * "source"/"target". Renaming it is the fix; a per-line suppression would not be.
 */
function StockSummaryPanel({
  summary,
  side,
}: {
  summary: CombinePartStockSummary;
  side: 'source' | 'target';
}) {
  const isSource = side === 'source';
  return (
    <section
      className="rounded-sm border border-fd-line bg-fd-panel p-3"
      data-testid={`combine-${side}-summary`}
      aria-label={`${isSource ? 'Source' : 'Target'} item — ${summary.part_number}`}
    >
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            {isSource ? 'Moving out of' : 'Moving into'}
          </p>
          <p className="truncate font-mono text-sm font-medium text-white">{summary.part_number}</p>
          <p className="truncate text-xs text-slate-400">{summary.name}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* Only when the server actually sent one — `status` is Optional on
              the schema, and StatusBadge calls .replace() on it. */}
          {summary.status && <StatusBadge status={summary.status} />}
          {!summary.is_active && (
            <span className="rounded-sm border border-fd-line px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-slate-400">
              Inactive
            </span>
          )}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs sm:grid-cols-4">
        <div>
          <dt className="text-slate-500">On hand</dt>
          <dd className="font-medium tabular-nums text-white">{formatQty(summary.total_on_hand)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Allocated</dt>
          <dd className="tabular-nums text-slate-300">{formatQty(summary.total_allocated)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Available</dt>
          <dd className="tabular-nums text-slate-300">{formatQty(summary.total_available)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">{isSource ? 'Can move' : 'Unit'}</dt>
          <dd className="tabular-nums text-slate-300">
            {isSource ? formatQty(summary.eligible_available) : summary.unit_of_measure || '—'}
          </dd>
        </div>
      </dl>
      {isSource && (
        <p className="mt-1 text-[11px] text-slate-500">
          Counted in {summary.unit_of_measure || 'no stated unit'}.
        </p>
      )}

      <div className="mt-2">
        <DataTable
          columns={lineColumns}
          data={summary.lines}
          rowKey={(line) => line.inventory_item_id}
          dense
          defaultSort={{ key: 'location', dir: 'asc' }}
          empty={{
            title: 'No stock rows',
            description: isSource
              ? 'Nothing is on hand under this number, so there is nothing to move.'
              : 'Nothing is on hand under this number yet. The moved lots will create rows here.',
          }}
        />
      </div>
    </section>
  );
}

const reservationColumns: Array<DataTableColumn<CombineOpenReservation>> = [
  {
    key: 'work_order',
    header: 'Work Order',
    sortable: true,
    accessor: (row) => row.work_order_number,
    render: (row) => <span className="font-mono text-xs">{row.work_order_number}</span>,
  },
  {
    key: 'status',
    header: 'Status',
    sortable: true,
    accessor: (row) => row.work_order_status,
    render: (row) => <StatusBadge status={row.work_order_status} />,
  },
  {
    key: 'outstanding',
    header: 'Still Needs',
    sortable: true,
    align: 'right',
    className: 'tabular-nums',
    accessor: (row) => row.outstanding_quantity,
    render: (row) => formatQty(row.outstanding_quantity),
  },
];

export default function CombineInventoryDialog({
  open,
  parts,
  initialSourcePartId = null,
  onClose,
  onCombined,
}: CombineInventoryDialogProps) {
  const { showToast } = useToast();

  // ComboBox speaks strings; `''` is "nothing picked".
  const [sourceId, setSourceId] = useState('');
  const [targetId, setTargetId] = useState('');

  const [preview, setPreview] = useState<InventoryCombinePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  const [reloadToken, setReloadToken] = useState(0);

  const [quantity, setQuantity] = useState('');
  /** What the preview pre-filled, so "dirty" means the operator changed it. */
  const [prefilledQuantity, setPrefilledQuantity] = useState('');
  const [reason, setReason] = useState('');
  const [acknowledgedIds, setAcknowledgedIds] = useState<number[]>([]);
  const [deactivateSource, setDeactivateSource] = useState(false);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState('');

  // Full reset on close. Every field, including the pickers: this dialog moves
  // stock, and a stale source left selected from a previous session is exactly
  // the kind of thing somebody confirms without re-reading.
  useEffect(() => {
    if (open) return;
    setSourceId('');
    setTargetId('');
    setPreview(null);
    setPreviewError('');
    setQuantity('');
    setPrefilledQuantity('');
    setReason('');
    setAcknowledgedIds([]);
    setDeactivateSource(false);
    setConfirmOpen(false);
    setServerError('');
  }, [open]);

  // Seed the source when the caller opened this from a specific item.
  useEffect(() => {
    if (!open) return;
    setSourceId(initialSourcePartId != null ? String(initialSourcePartId) : '');
  }, [open, initialSourcePartId]);

  const sourcePartId = sourceId ? Number(sourceId) : null;
  const targetPartId = targetId ? Number(targetId) : null;
  const samePart = sourcePartId != null && sourcePartId === targetPartId;
  const pairReady = sourcePartId != null && targetPartId != null && !samePart;

  /**
   * The preview read. ONE implementation, reached from two places — selecting a
   * pair, and Retry after a failed read — because the second entry point is what
   * usually grows into a divergent copy that forgets to reset the acknowledgements.
   *
   * `cancelled` is the stale-response guard: the pickers are two independent
   * controls, so a fast second selection can land while the first read is still
   * in flight, and the loser must not overwrite the winner's preview.
   */
  useEffect(() => {
    if (!open || !pairReady) {
      setPreview(null);
      setPreviewError('');
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError('');
    api
      .previewInventoryCombine({ source_part_id: sourcePartId as number, target_part_id: targetPartId as number })
      .then((result) => {
        if (cancelled) return;
        setPreview(result);
        // Pre-fill the whole eligible pile, capped at what the open ties allow.
        // `default_quantity` alone would offer a number the server then refuses.
        const prefill = formatQty(Math.min(result.default_quantity, result.max_combinable_quantity));
        setQuantity(prefill);
        setPrefilledQuantity(prefill);
        // A new pair is a new decision: acknowledgements and the deactivate
        // choice must never carry over from the pair they were made about.
        setAcknowledgedIds([]);
        setDeactivateSource(false);
        setServerError('');
      })
      .catch((err) => {
        if (cancelled) return;
        setPreview(null);
        setPreviewError(
          toDisplayString((err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail) ||
            'Could not read what combining these two would do.'
        );
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, pairReady, sourcePartId, targetPartId, reloadToken]);

  const partOptions = useMemo<ComboBoxOption[]>(
    () =>
      parts.map((part) => ({
        value: String(part.id),
        label: part.part_number ? `${part.part_number} — ${part.name}` : part.name,
        hint: part.unit_of_measure || undefined,
      })),
    [parts]
  );

  /**
   * One checkbox per flagged PART, not per match.
   *
   * `flagged_parts` can carry two rows for one part when the token hit both
   * `part_number` and `name`, and `acknowledge_flagged_part_ids` is a list of
   * part ids — so rendering a checkbox per row would give two controls that
   * write the same value and one of them would look unticked forever.
   */
  const flaggedByPart = useMemo(() => {
    const grouped = new Map<number, { part: CombineFlaggedPart; matches: CombineFlaggedPart[] }>();
    for (const flagged of preview?.flagged_parts ?? []) {
      const existing = grouped.get(flagged.part_id);
      if (existing) existing.matches.push(flagged);
      else grouped.set(flagged.part_id, { part: flagged, matches: [flagged] });
    }
    return Array.from(grouped.values());
  }, [preview]);

  const requested = Number.parseFloat(quantity);
  const requestedValid = Number.isFinite(requested) && requested > 0;
  const eligibleAvailable = preview?.source.eligible_available ?? 0;
  const maxCombinable = preview?.max_combinable_quantity ?? 0;

  // The two quantity refusals, computed from the numbers the preview returned
  // rather than re-fetched — see the "no re-preview per keystroke" note above.
  const overAvailable = requestedValid && requested > eligibleAvailable + EPS;
  const overReservationCap = requestedValid && !overAvailable && requested > maxCombinable + EPS;

  // `deactivate_source` is refused unless the source lands at exactly zero
  // across ALL its rows, the ineligible ones included. Offering the tick while
  // that is false would be showing a fold the server would refuse, so the box
  // goes dead with the arithmetic spelled out beside it.
  const projectedSourceRemaining = preview ? preview.source.total_on_hand - (requestedValid ? requested : 0) : 0;
  const canDeactivateSource = Boolean(preview) && requestedValid && projectedSourceRemaining <= EPS;

  useEffect(() => {
    // Never leave a dead box ticked: the request would carry a flag the server
    // is going to 409 on, for a reason the operator can no longer see.
    if (!canDeactivateSource && deactivateSource) setDeactivateSource(false);
  }, [canDeactivateSource, deactivateSource]);

  const blockers = preview?.blockers ?? [];
  const advisories = preview?.advisories ?? [];

  /**
   * Unsaved-edit guard.
   *
   * "Dirty" here is the operator's own input — a typed reason, a quantity moved
   * off the server's pre-fill, an acknowledgement ticked — never the mere fact
   * that a preview loaded. Treating a loaded preview as dirty would put a
   * confirm in front of every Cancel, which is how a guard gets trained out of
   * people. In-app route blocking is unavailable in this app (the router is a
   * component `<BrowserRouter>`), so this covers refresh/tab-close plus the
   * explicit Cancel/Close gate below.
   */
  const isDirty =
    open &&
    !saving &&
    (reason.trim().length > 0 ||
      acknowledgedIds.length > 0 ||
      deactivateSource ||
      (quantity !== prefilledQuantity && quantity.trim().length > 0));
  const { confirmDiscard } = useUnsavedChanges(isDirty, 'Discard this combine without moving anything?');

  /** Cancel/Close. The successful path calls `onCombined` and never this. */
  const requestClose = () => {
    if (saving) return;
    if (!confirmDiscard()) return;
    onClose();
  };
  const unacknowledged = flaggedByPart.filter((entry) => !acknowledgedIds.includes(entry.part.part_id));
  const reasonTooShort = reason.trim().length < 5;

  /**
   * Why the button is dead, in one sentence — or `null` when it is live.
   *
   * The button IS disabled while a refusal stands (unlike `PartBackflushCard`,
   * which leaves its confirm live so the server can do the talking) because this
   * one moves stock in two places and writes 2N ledger rows: a hopeful click is
   * not a cheap experiment here. The trade that makes a disabled button
   * acceptable is that it is never mute — this string renders beside it.
   */
  const submitBlockedReason = useMemo<string | null>(() => {
    if (!preview) return 'Pick both items to see what would move.';
    if (blockers.length > 0) return 'The refusals above have to be cleared first.';
    if (!requestedValid) return 'Enter how much to move.';
    if (overAvailable) return `Only ${formatQty(eligibleAvailable)} is available to move.`;
    if (overReservationCap) return `Open jobs cap this at ${formatQty(maxCombinable)}.`;
    if (reasonTooShort) return 'A reason is required — it goes on the audit trail.';
    if (unacknowledged.length > 0) return 'Acknowledge the flagged items below.';
    return null;
  }, [
    preview,
    blockers.length,
    requestedValid,
    overAvailable,
    overReservationCap,
    eligibleAvailable,
    maxCombinable,
    reasonTooShort,
    unacknowledged.length,
  ]);

  const handleConfirm = async () => {
    if (!preview || submitBlockedReason || saving) return;
    setSaving(true);
    setServerError('');
    try {
      const result = await api.combineInventory({
        source_part_id: preview.source.part_id,
        target_part_id: preview.target.part_id,
        quantity: requested,
        reason: reason.trim(),
        // The compare-and-swap: what the PREVIEW read, not what the picker list
        // happens to hold. See the header note.
        expected_source_part_number: preview.source.part_number,
        expected_target_part_number: preview.target.part_number,
        // What the operator actually TICKED, narrowed to parts the server
        // flagged. Sending `flaggedByPart` wholesale would be equivalent today
        // — submit is blocked until every one is ticked — but it would turn
        // into a silent auto-acknowledgement the day that gate is relaxed, and
        // the acknowledgement is the entire control the owner asked for.
        acknowledge_flagged_part_ids: flaggedByPart
          .map((entry) => entry.part.part_id)
          .filter((partId) => acknowledgedIds.includes(partId)),
        deactivate_source: deactivateSource,
      });

      /**
       * SUCCESS versus WARNING.
       *
       * `warning` is the variant for an action that SUCCEEDED but did not do
       * everything asked — a `success` toast would hide the shortfall and an
       * `error` would claim a failure that did not happen, sending someone
       * looking for a combine that exists. Three ways this lands short:
       *
       *  1. Fewer units moved than asked for.
       *  2. `deactivate_source` was asked for and the source is still active.
       *  3. The operator asked to fold the WHOLE eligible pile and the source
       *     still holds stock — i.e. held/quarantined/rejected lots stayed put.
       *     Conditioned on "asked for everything" on purpose: someone who
       *     deliberately moved 50 of 92 has not been short-changed by anything.
       */
      const short = result.quantity_moved + EPS < requested;
      const deactivationMissed = deactivateSource && !result.source_deactivated;
      const askedForEverything = requested >= eligibleAvailable - EPS;
      const leftBehind = askedForEverything && result.source_quantity_after > EPS;

      const headline =
        `Moved ${formatQty(result.quantity_moved)} ${unitLabel(preview.source.unit_of_measure)} ` +
        `from ${result.source_part_number} to ${result.target_part_number} ` +
        `(${result.combine_number}). ${result.source_part_number} now ` +
        `${formatQty(result.source_quantity_after)}, ${result.target_part_number} now ` +
        `${formatQty(result.target_quantity_after)}.`;

      if (short || deactivationMissed || leftBehind) {
        const shortfalls: string[] = [];
        if (short) shortfalls.push(`${formatQty(requested - result.quantity_moved)} could not move`);
        if (leftBehind)
          shortfalls.push(
            `${formatQty(result.source_quantity_after)} stayed under the old number ` +
              '(held, quarantined, rejected or allocated lots are never folded)'
          );
        if (deactivationMissed) shortfalls.push(`${result.source_part_number} is still active`);
        showToast('warning', `${headline} ${shortfalls.join('; ')}.`);
      } else {
        showToast('success', headline);
      }

      setConfirmOpen(false);
      onCombined(result);
    } catch (err) {
      // Kept OPEN on failure so a 409 does not cost the operator their reason
      // text — and the confirm closes so the refusal is read, not clicked past.
      setConfirmOpen(false);
      setServerError(
        toDisplayString((err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail) ||
          'Could not combine these two items.'
      );
    } finally {
      setSaving(false);
    }
  };

  const toggleAcknowledged = (partId: number, checked: boolean) => {
    setAcknowledgedIds((prev) => (checked ? [...prev, partId] : prev.filter((id) => id !== partId)));
  };

  return (
    <>
      <Modal
        open={open}
        onClose={requestClose}
        closeOnBackdrop={!saving}
        closeOnEscape={!saving}
        size="5xl"
        padded={false}
        scroll={false}
        ariaLabelledBy="combine-inventory-title"
      >
        <div className="modal-header">
          <h3 id="combine-inventory-title" className="text-lg font-semibold text-white">
            Combine two item numbers
          </h3>
        </div>

        <div className="modal-body max-h-[72vh] space-y-4 overflow-y-auto">
          <p className="text-sm text-slate-300">
            Use this when two numbers describe the <strong>same physical material</strong> — a numbering recut
            that left the old number carrying stock. The on-hand moves from one number to the other and the
            shop&apos;s total does not change: nothing is received and nothing is issued. Heat lots, certs and
            supplier details travel with the material. The old number is <strong>never deleted</strong> — it
            stays in the catalog at zero so every traveler, MTR and closed PO bearing it still resolves.
          </p>

          <div className="grid gap-3 sm:grid-cols-[1fr_auto_1fr] sm:items-end">
            <FormField label="Move stock OUT of (source)" required>
              {(field) => (
                <ComboBox
                  id={field.id}
                  ariaDescribedBy={field['aria-describedby']}
                  options={partOptions}
                  value={sourceId}
                  onChange={setSourceId}
                  placeholder="Search item number or name…"
                  disabled={saving}
                />
              )}
            </FormField>
            <div className="hidden pb-2 text-slate-500 sm:block" aria-hidden="true">
              <ArrowRightIcon className="h-5 w-5" />
            </div>
            <FormField label="Move stock INTO (target)" required>
              {(field) => (
                <ComboBox
                  id={field.id}
                  ariaDescribedBy={field['aria-describedby']}
                  options={partOptions}
                  value={targetId}
                  onChange={setTargetId}
                  placeholder="Search item number or name…"
                  disabled={saving}
                />
              )}
            </FormField>
          </div>

          {samePart && (
            <p className="rounded-sm border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-xs text-red-200" role="alert">
              {BLOCKER_NEXT_STEP.same_part}
            </p>
          )}

          {!pairReady && !samePart && (
            <p className="flex items-start gap-1.5 text-xs text-slate-500">
              <InformationCircleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              <span>Pick both items to see every lot, location and open job this would touch.</span>
            </p>
          )}

          {pairReady && previewLoading && <Skeleton className="h-40 w-full" />}

          {/* A failed preview is fatal here, not survivable: without it there is
              no compare-and-swap value to send. See the header note. */}
          {pairReady && !previewLoading && previewError && (
            <ErrorState message={previewError} onRetry={() => setReloadToken((token) => token + 1)} />
          )}

          {pairReady && !previewLoading && preview && (
            <>
              <div className="grid gap-3 lg:grid-cols-2">
                <StockSummaryPanel summary={preview.source} side="source" />
                <StockSummaryPanel summary={preview.target} side="target" />
              </div>

              {blockers.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-red">
                    {blockers.length === 1 ? 'This is refused' : `${blockers.length} refusals`}
                  </p>
                  <ul className="space-y-1.5" data-testid="combine-blockers">
                    {/* Index-keyed: two diagnostics can legitimately share a code
                        (two lots, two flagged parts), so code alone is not unique. */}
                    {blockers.map((diagnostic, index) => (
                      <DiagnosticRow key={`blocker-${diagnostic.code}-${index}`} diagnostic={diagnostic} tone="blocking" />
                    ))}
                  </ul>
                </div>
              )}

              {advisories.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-amber">
                    Worth a look — not blocking
                  </p>
                  <ul className="space-y-1.5" data-testid="combine-advisories">
                    {advisories.map((diagnostic, index) => (
                      <DiagnosticRow
                        key={`advisory-${diagnostic.code}-${index}`}
                        diagnostic={diagnostic}
                        tone="advisory"
                      />
                    ))}
                  </ul>
                </div>
              )}

              {/* Units, side by side. A mismatch is the server's to refuse; this
                  panel exists so the operator sees WHY before they are refused. */}
              {!preview.unit_of_measure_match && (
                <div className="rounded-sm border border-fd-amber/35 bg-fd-amber/10 px-3 py-2 text-xs text-amber-200">
                  These two are not counted the same way: {preview.source.part_number} is in{' '}
                  <strong>{preview.source.unit_of_measure || 'no stated unit'}</strong> and{' '}
                  {preview.target.part_number} is in{' '}
                  <strong>{preview.target.unit_of_measure || 'no stated unit'}</strong>.
                </div>
              )}

              {/* Costs are never reblended — this is disclosure, not a control. */}
              <div className="rounded-sm border border-fd-line bg-fd-panel px-3 py-2 text-xs text-slate-300">
                <p className="mb-1 font-medium text-white">What this does to cost</p>
                <p className="tabular-nums">
                  {preview.source.part_number} {formatMoney(preview.cost.source_weighted_unit_cost)} ·{' '}
                  {preview.target.part_number} {formatMoney(preview.cost.target_weighted_unit_cost)}
                  {preview.cost.differs ? ' — these differ.' : ' — these match.'}
                </p>
                {preview.cost.note && <p className="mt-1 text-slate-400">{preview.cost.note}</p>}
              </div>

              {preview.open_source_reservations.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-slate-400">
                    Open jobs still tied to {preview.source.part_number} —{' '}
                    {formatQty(preview.reserved_quantity)} {unitLabel(preview.source.unit_of_measure)} reserved
                  </p>
                  <DataTable
                    columns={reservationColumns}
                    data={preview.open_source_reservations}
                    rowKey={(row) => row.work_order_id}
                    dense
                    defaultSort={{ key: 'work_order', dir: 'asc' }}
                    empty={{ title: 'No open ties' }}
                  />
                  <p className="mt-1 text-[11px] text-slate-500">
                    Stock these jobs still need cannot be folded away from under them. Re-tie them to{' '}
                    {preview.target.part_number} if the whole pile has to move.
                  </p>
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <FormField
                  label={`How much to move (${unitLabel(preview.source.unit_of_measure)})`}
                  required
                  help={`${formatQty(eligibleAvailable)} available on ${preview.source.part_number}${
                    maxCombinable < eligibleAvailable - EPS
                      ? `, of which ${formatQty(maxCombinable)} is not spoken for by an open job`
                      : ''
                  }.`}
                >
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      inputMode="decimal"
                      min={0}
                      step="any"
                      value={quantity}
                      onChange={(event) => setQuantity(event.target.value)}
                      className="input tabular-nums"
                      disabled={saving}
                      data-testid="combine-quantity"
                      required
                    />
                  )}
                </FormField>

                <FormField
                  label="Reason"
                  required
                  help="Recorded on the audit trail and kept on the combine record."
                >
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      className="input"
                      disabled={saving}
                      placeholder="e.g. Numbering recut — same 16ga 304 sheet under two numbers"
                      data-testid="combine-reason"
                      required
                    />
                  )}
                </FormField>
              </div>

              {/* OFFER the safe number rather than dead-ending. The server already
                  computed the largest quantity that clears the open ties; making
                  the operator guess at it is how a correct action gets abandoned. */}
              {(overAvailable || overReservationCap) && (
                <div
                  className="flex flex-wrap items-center gap-2 rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2 text-xs text-amber-200"
                  role="alert"
                >
                  <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
                  <span className="min-w-0">
                    {overAvailable
                      ? `Only ${formatQty(eligibleAvailable)} ${unitLabel(preview.source.unit_of_measure)} is available to move.`
                      : `Open jobs still need ${formatQty(preview.reserved_quantity)}, so at most ` +
                        `${formatQty(maxCombinable)} can move without stranding one.`}
                  </span>
                  {(overAvailable ? eligibleAvailable : maxCombinable) > 0 && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setQuantity(formatQty(overAvailable ? eligibleAvailable : maxCombinable))}
                      disabled={saving}
                      data-testid="combine-use-max"
                    >
                      Use {formatQty(overAvailable ? eligibleAvailable : maxCombinable)}
                    </Button>
                  )}
                </div>
              )}

              {flaggedByPart.length > 0 && (
                <fieldset className="rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2">
                  <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-fd-amber">
                    Confirm these on purpose
                  </legend>
                  <p className="mb-1.5 text-xs text-amber-200/90">
                    One of these numbers reads like test or fixture work. That is not a ban — &quot;housing&quot;
                    is an ordinary word on this floor — but the combine will be refused until you say you mean
                    this item.
                  </p>
                  <ul className="space-y-1.5">
                    {flaggedByPart.map((entry) => (
                      <li key={entry.part.part_id}>
                        <label className="flex items-start gap-2 text-xs text-amber-100">
                          <input
                            type="checkbox"
                            className="mt-0.5 rounded border-slate-600 text-werco-navy-600"
                            checked={acknowledgedIds.includes(entry.part.part_id)}
                            onChange={(event) => toggleAcknowledged(entry.part.part_id, event.target.checked)}
                            disabled={saving}
                            data-testid={`combine-ack-${entry.part.part_id}`}
                          />
                          <span>
                            Yes, include <span className="font-mono">{entry.part.part_number}</span> — matched{' '}
                            {entry.matches
                              .map((match) => `“${match.matched_token}” in ${match.field.replace(/_/g, ' ')}`)
                              .join(' and ')}
                            .
                          </span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </fieldset>
              )}

              <div className="rounded-sm border border-fd-line px-3 py-2">
                <label className="flex items-start gap-2 text-xs text-slate-300">
                  <input
                    type="checkbox"
                    className="mt-0.5 rounded border-slate-600 text-werco-navy-600"
                    checked={deactivateSource}
                    onChange={(event) => setDeactivateSource(event.target.checked)}
                    disabled={saving || !canDeactivateSource}
                    data-testid="combine-deactivate-source"
                  />
                  <span>
                    Also mark <span className="font-mono">{preview.source.part_number}</span> inactive when this
                    lands. It is not deleted — it stays in the catalog and every old document still resolves to
                    it.
                    {!canDeactivateSource && (
                      <span className="mt-0.5 block text-slate-500">
                        {requestedValid
                          ? `Not available: ${formatQty(projectedSourceRemaining)} would still be on hand ` +
                            'afterwards, and an item holding stock cannot be switched off here.'
                          : 'Enter a quantity first.'}
                      </span>
                    )}
                  </span>
                </label>
              </div>
            </>
          )}

          {/* The server's verbatim refusal. It names what changed or what stands
              in the way, which is the entire reason it renders in full rather
              than as a generic failure line. */}
          {serverError && (
            <div
              role="alert"
              data-testid="combine-error"
              className="rounded-sm border border-red-500/60 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300"
            >
              {serverError}
            </div>
          )}
        </div>

        <div className="modal-footer">
          {submitBlockedReason && (
            <span className="mr-auto text-xs text-slate-500" data-testid="combine-blocked-reason">
              {submitBlockedReason}
            </span>
          )}
          <Button variant="secondary" onClick={requestClose} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton
            type="button"
            loading={saving}
            loadingText="Combining…"
            disabled={Boolean(submitBlockedReason)}
            onClick={() => {
              setServerError('');
              setConfirmOpen(true);
            }}
            data-testid="combine-submit"
          >
            Combine…
          </LoadingButton>
        </div>
      </Modal>

      {/* The go/no-go. `pending` drives the spinner, the double-click guard, the
          dead Cancel and the refusal of backdrop/Escape dismissal, so an action
          already on the wire can never be visually "cancelled" while the server
          may still be applying it. */}
      <ConfirmDialog
        open={confirmOpen && !!preview}
        title="Combine these two item numbers?"
        message={
          preview
            ? `Move ${formatQty(requestedValid ? requested : 0)} ${unitLabel(preview.source.unit_of_measure)} from ` +
              `${preview.source.part_number} to ${preview.target.part_number}` +
              `${deactivateSource ? `, and mark ${preview.source.part_number} inactive` : ''}. ` +
              'The stock stays exactly where it is on the shelf — only the number on it changes. ' +
              'Undoing this is a new, reasoned combine in the other direction, not an edit.'
            : ''
        }
        confirmLabel="Combine"
        variant="warning"
        pending={saving}
        onConfirm={() => void handleConfirm()}
        onCancel={() => {
          if (!saving) setConfirmOpen(false);
        }}
      />
    </>
  );
}
