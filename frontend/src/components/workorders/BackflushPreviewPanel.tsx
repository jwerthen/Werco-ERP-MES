/**
 * Backflush dry run — what completing THIS work order would take out of stock.
 *
 * The office-side answer to "if I close this job, what leaves the shelf, and
 * from which heat?" — asked and answered BEFORE anything moves.
 *
 * Five facts shape everything below:
 *
 * 1. **It is a PURE READ.** `GET /work-orders/{id}/backflush-preview` writes
 *    nothing at all — no ledger row, no audit row, no operational event. That is
 *    structural on the server (the resolution layer takes no AuditService), not
 *    a convention, which is why this panel can fetch on demand and re-fetch
 *    freely. A poll is not an actor and records no reason.
 *
 * 2. **It models the ISSUE LOOP, not just the demand resolver.** Both legs in
 *    the real order (work-order-scoped ties first, so a tie's lot pin gets first
 *    claim), the legacy one-shot fence, the reconcile-to-target delta, and the
 *    actual FIFO lot pick. So the lots listed here are the lots a completion
 *    draws — not a plausible guess at them. Showing a different heat than the
 *    engine will consume is the one failure this panel exists to prevent, since
 *    that lot number lands on the as-built genealogy record.
 *
 * 3. **Lines appear whether or not the part has opted in.** Someone reading this
 *    is often deciding whether to opt in, and a preview that showed nothing
 *    until afterwards could not inform that decision. Each line therefore
 *    carries `requires_opt_in`, and the panel labels those rows explicitly
 *    rather than letting them read as "this will happen".
 *
 * 4. **Basis 0 means no BOM lines, and that is real.** `basis` is
 *    `quantity_complete + operation scrap`; the resolver returns nothing for a
 *    work order that has produced nothing. The panel says so instead of showing
 *    an empty table that reads like a bug.
 *
 * 5. **Suppressed is normal, not an error.** The leg reconciles to target and
 *    never auto-reverses, so a line whose ledger already holds the whole target
 *    (`converged`) is the healthy steady state. Two reasons are worth alarm and
 *    are coloured for it: `already_issued` (the permanent legacy one-shot fence)
 *    and `blocking_diagnostic` (the completion refuses that component over a
 *    blocking BOM/routing problem rather than issuing a quantity it cannot
 *    trust, and records the refusal on the audit trail).
 *
 * Loaded on demand rather than on mount: it is one more request per page, and
 * the answer only matters when someone is about to close a job or is auditing a
 * BOM. Nothing about an untied, non-backflushing work order changes by leaving
 * it unopened.
 */

import React, { useCallback, useState } from 'react';
import { BeakerIcon, CubeIcon, ExclamationTriangleIcon, InformationCircleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, DataTable, ErrorState, LoadingButton } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatTieQty } from '../../utils/materialTie';
import { toDisplayString } from '../../utils/apiError';
import type { BackflushDiagnostic, BackflushPreviewLine, BackflushPreviewResponse } from '../../types';

export interface BackflushPreviewPanelProps {
  workOrderId: number;
}

/**
 * Plain-language gloss for each `suppression_reason`, plus whether it deserves
 * alarm. The raw key is shown alongside so the vocabulary stays greppable
 * against the server's own — it uses the same word for the same fact on the
 * audit-chain row a completion later writes.
 */
const SUPPRESSION_COPY: Record<string, { label: string; help: string; alarming: boolean }> = {
  converged: {
    label: 'Already covered',
    help: 'The ledger already holds this component’s whole target. Nothing is wrong — the leg reconciles to target, so there is nothing left to post.',
    alarming: false,
  },
  already_issued: {
    label: 'Fenced out (legacy)',
    help: 'An older one-shot issue row fences this work order out of the reconciling engine for this component, permanently. Nothing further will ever post for it on this job.',
    alarming: true,
  },
  ledger_consumed: {
    label: 'Drawn by a tie',
    help: 'An operation-scoped material tie already drew this component against this job, so the BOM’s demand for it is dropped — the material cannot leave twice.',
    alarming: false,
  },
  open_operation_tie: {
    label: 'Owned by a tie',
    help: 'An open operation-scoped tie owns this component’s demand. The material still moves — on the per-run engine, when that operation completes, rather than on this leg.',
    alarming: false,
  },
  blocking_diagnostic: {
    label: 'Refused (BOM problem)',
    help: 'A blocking problem below stands against this component, so the completion refuses to issue it rather than move a quantity it cannot trust — and records that refusal on the audit trail. The material stays on the shelf; fix the BOM or routing line and it resolves normally.',
    alarming: true,
  },
};

const qtyWithUom = (value: number | null | undefined, uom: string | null): string =>
  `${formatTieQty(Number(value || 0))}${uom ? ` ${uom}` : ''}`;

const partLabel = (line: BackflushPreviewLine): string =>
  (line.component_part_number || '').trim() || `Part #${line.component_part_id}`;

/** One diagnostic, rendered from its `detail` sentence — never a prettified code. */
function DiagnosticRow({ diagnostic, tone }: { diagnostic: BackflushDiagnostic; tone: 'blocking' | 'advisory' }) {
  const blocking = tone === 'blocking';
  return (
    <li
      className={`flex items-start gap-2 rounded-sm border px-2.5 py-2 text-xs ${
        blocking ? 'border-fd-red/40 bg-fd-red/10 text-red-200' : 'border-fd-amber/35 bg-fd-amber/10 text-amber-200'
      }`}
    >
      <ExclamationTriangleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
      <span className="min-w-0">
        <span className="block">{diagnostic.detail}</span>
        <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wider opacity-60">
          {diagnostic.code}
          {diagnostic.component_part_number ? ` · ${diagnostic.component_part_number}` : ''}
        </span>
      </span>
    </li>
  );
}

export default function BackflushPreviewPanel({ workOrderId }: BackflushPreviewPanelProps) {
  const [preview, setPreview] = useState<BackflushPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [opened, setOpened] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setPreview(await api.getWorkOrderBackflushPreview(workOrderId));
    } catch (err) {
      setPreview(null);
      const detail = toDisplayString((err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail);
      setError(detail.trim() || 'Could not read the backflush preview for this work order');
    } finally {
      setLoading(false);
    }
  }, [workOrderId]);

  const openAndLoad = () => {
    setOpened(true);
    void load();
  };

  const columns: DataTableColumn<BackflushPreviewLine>[] = [
    {
      key: 'component',
      header: 'Component',
      sortable: true,
      accessor: (line) => partLabel(line),
      render: (line) => (
        <div className="min-w-0">
          <p className="truncate font-mono text-sm font-semibold text-slate-100">{partLabel(line)}</p>
          {line.component_part_name && <p className="truncate text-xs text-slate-400">{line.component_part_name}</p>}
        </div>
      ),
    },
    {
      key: 'source',
      header: 'From',
      sortable: true,
      accessor: (line) => line.source,
      csv: (line) => (line.source === 'work_order_tie' ? 'Material tie' : 'BOM / routing'),
      render: (line) =>
        line.source === 'work_order_tie' ? (
          <span
            className="font-mono text-[11px] uppercase tracking-wider text-slate-300"
            title="A work-order-scoped material tie. The tie IS its own opt-in — it consumes whether or not the part opted into automatic backflush."
          >
            Tie
          </span>
        ) : (
          <span
            className="font-mono text-[11px] uppercase tracking-wider text-slate-400"
            title="Resolved from the finished part's BOM and routing. Moves only while the part's automatic backflush is on."
          >
            BOM
          </span>
        ),
    },
    {
      key: 'required',
      header: 'Target',
      align: 'right',
      sortable: true,
      accessor: (line) => Number(line.required_quantity || 0),
      csv: (line) => qtyWithUom(line.required_quantity, line.unit_of_measure),
      render: (line) => (
        <span
          className="font-mono text-sm tabular-nums text-slate-300"
          title="What the ledger should hold in total for this component on this job."
        >
          {qtyWithUom(line.required_quantity, line.unit_of_measure)}
        </span>
      ),
    },
    {
      key: 'issued',
      header: 'Already posted',
      align: 'right',
      sortable: true,
      accessor: (line) => Number(line.already_issued || 0),
      csv: (line) => qtyWithUom(line.already_issued, line.unit_of_measure),
      render: (line) => (
        <span
          className="font-mono text-sm tabular-nums text-slate-400"
          title="Signed ledger net for this component on this job — issues minus returns. The ledger is the authoritative figure."
        >
          {qtyWithUom(line.already_issued, line.unit_of_measure)}
        </span>
      ),
    },
    {
      key: 'delta',
      header: 'Would post now',
      align: 'right',
      sortable: true,
      accessor: (line) => Number(line.delta_quantity || 0),
      csv: (line) => qtyWithUom(line.delta_quantity, line.unit_of_measure),
      render: (line) => (
        <span
          className={`font-mono text-sm font-semibold tabular-nums ${
            line.suppressed ? 'text-slate-500' : line.would_go_negative ? 'text-fd-red' : 'text-slate-100'
          }`}
        >
          {qtyWithUom(line.delta_quantity, line.unit_of_measure)}
        </span>
      ),
    },
    {
      key: 'lots',
      header: 'Lots it would draw',
      // Not sortable: this is the column the whole panel exists for, and a list
      // of heats has no meaningful ordering key that isn't a lie.
      csv: (line) =>
        line.lots
          .map(
            (lot) =>
              `${lot.lot_number || 'no lot'}:${formatTieQty(lot.quantity)}${lot.is_shortfall ? ' (short)' : ''}`
          )
          .join(' | '),
      render: (line) => {
        if (line.suppressed) return <span className="text-xs text-slate-600">—</span>;
        if (line.lots.length === 0) {
          return (
            <span
              className="text-xs text-fd-red"
              title={
                line.shortfall_creates_placeholder
                  ? 'No stock row for this part exists at all, so the completion mints a lot-less placeholder row and posts the whole draw against it. It names no heat and carries no cert.'
                  : 'No lot the policy permits can cover this draw.'
              }
            >
              {line.shortfall_creates_placeholder ? 'no stock — placeholder row' : 'no eligible lot'}
            </span>
          );
        }
        return (
          <div className="flex flex-wrap items-center gap-1">
            {line.lots.map((lot, index) => (
              <span
                // Index-keyed: a shortfall row legitimately repeats the lot it was
                // anchored to, so `inventory_item_id` is not unique within a line.
                key={`${lot.inventory_item_id}-${index}`}
                className={`whitespace-nowrap rounded-sm border px-1.5 py-0.5 font-mono text-[11px] ${
                  lot.is_shortfall ? 'border-fd-red/50 bg-fd-red/10 text-fd-red' : 'border-fd-line text-slate-200'
                }`}
                title={
                  lot.is_shortfall
                    ? `${formatTieQty(lot.quantity)}${
                        line.unit_of_measure ? ` ${line.unit_of_measure}` : ''
                      } more than this part has. The completion still posts it, against this lot, driving it negative — and that lot number lands on the as-built record.`
                    : `${lot.location || 'no location'} · ${formatTieQty(lot.quantity)}${
                        line.unit_of_measure ? ` ${line.unit_of_measure}` : ''
                      } from this lot`
                }
              >
                {lot.lot_number || 'no lot'}
                <span className="ml-1 opacity-70">{formatTieQty(lot.quantity)}</span>
                {lot.is_shortfall && <span className="ml-1 uppercase tracking-wider">short</span>}
              </span>
            ))}
            {line.shortfall_creates_placeholder && (
              <span
                className="whitespace-nowrap rounded-sm border border-fd-red/50 bg-fd-red/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-red"
                title="This part has no stock row at all, so the completion mints a lot-less placeholder row and posts against it."
              >
                placeholder row
              </span>
            )}
            {line.pinned_lot_number && (
              <span
                className="whitespace-nowrap rounded-sm border border-fd-blue/45 bg-fd-blue/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-blue"
                title="Pinned by a tie: the draw is directed at this lot only and is driven negative rather than spilling onto another heat."
              >
                pinned
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: 'flags',
      header: 'Flags',
      csv: (line) => {
        const flags: string[] = [];
        if (line.suppressed) flags.push(SUPPRESSION_COPY[line.suppression_reason || '']?.label || 'Suppressed');
        if (line.would_go_negative) flags.push(`Short ${formatTieQty(line.shortfall)}`);
        if (line.held_quantity_skipped > 0) flags.push(`${formatTieQty(line.held_quantity_skipped)} held`);
        if (line.pinned_lot_is_held) flags.push('Pinned lot is HELD');
        if (line.requires_opt_in) flags.push('Needs opt-in');
        return flags.join(' | ');
      },
      render: (line) => {
        const suppression = line.suppressed ? SUPPRESSION_COPY[line.suppression_reason || ''] : undefined;
        return (
          <div className="flex flex-wrap items-center gap-1">
            {line.suppressed && (
              <span
                data-testid={`backflush-suppressed-${line.component_part_id}`}
                title={
                  suppression?.help ||
                  `Suppressed (${line.suppression_reason || 'reason not stated'}) — nothing will post for this component.`
                }
                className={`whitespace-nowrap rounded-sm border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                  suppression?.alarming
                    ? 'border-fd-amber/45 bg-fd-amber/10 text-fd-amber'
                    : 'border-fd-line text-slate-400'
                }`}
              >
                {suppression?.label || line.suppression_reason || 'suppressed'}
              </span>
            )}
            {line.would_go_negative && (
              <span
                data-testid={`backflush-short-${line.component_part_id}`}
                title="Stock the policy permits cannot cover this draw — the lot is driven negative and flagged. A shortage never blocks production."
                className="whitespace-nowrap rounded-sm border border-fd-red/50 bg-fd-red/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-red"
              >
                short {formatTieQty(line.shortfall)}
              </span>
            )}
            {line.held_quantity_skipped > 0 && (
              <span
                title={
                  `${formatTieQty(line.held_quantity_skipped)} is on hand but segregated (hold / quarantine / rejected / inactive) ` +
                  `and will NOT be drawn${
                    line.held_lot_numbers.length > 0 ? `: lot ${line.held_lot_numbers.join(', ')}` : ''
                  }. That is an MRB question, not a purchasing one.`
                }
                className="whitespace-nowrap rounded-sm border border-fd-amber/45 bg-fd-amber/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-amber"
              >
                {formatTieQty(line.held_quantity_skipped)} held
              </span>
            )}
            {line.pinned_lot_is_held && (
              <span
                data-testid={`backflush-pinned-held-${line.component_part_id}`}
                title={
                  `The tie pins lot ${line.pinned_lot_number || '(unnamed)'}, and that lot has since been put on ` +
                  'hold / quarantined / rejected. A pin is a lot-directed instruction, so the completion consumes ' +
                  'it ANYWAY and records HELD_MATERIAL_CONSUMED. Clear the pin or release the lot before completing.'
                }
                className="whitespace-nowrap rounded-sm border border-fd-red/50 bg-fd-red/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-fd-red"
              >
                pinned lot held
              </span>
            )}
            {line.requires_opt_in && (
              <span
                title="Resolved from the BOM/routing. It moves only once this part's automatic backflush is turned on — today it is a forecast, not a commitment."
                className="whitespace-nowrap rounded-sm border border-fd-line px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-slate-500"
              >
                needs opt-in
              </span>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div className="card card-compact" data-testid="wo-backflush-preview">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="card-title flex items-center gap-2">
            <BeakerIcon className="h-5 w-5 flex-shrink-0 text-slate-400" aria-hidden="true" />
            Backflush Preview
          </h2>
          <p className="card-subtitle">
            A dry run of what completing this work order would take out of stock, per component and per lot. It
            models the same draw the completion performs — nothing here moves anything.
          </p>
        </div>
        {!opened ? (
          <Button variant="secondary" size="sm" className="shrink-0" onClick={openAndLoad}>
            Run dry run
          </Button>
        ) : (
          <LoadingButton
            variant="secondary"
            size="sm"
            className="shrink-0"
            loading={loading}
            loadingText="Reading…"
            onClick={() => void load()}
          >
            Refresh
          </LoadingButton>
        )}
      </div>

      {!opened ? (
        <p className="text-xs text-slate-500">
          Not loaded. This is one extra read against the BOM, the routing and the stock ledger, so it runs when you
          ask for it rather than on every page view.
        </p>
      ) : error ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : (
        <div className="space-y-3">
          {preview && (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              <div className="rounded-sm border border-fd-line px-3 py-2">
                <p className="text-[11px] uppercase tracking-wider text-slate-500">Finished part</p>
                <p className="truncate font-mono text-sm text-slate-200">
                  {preview.part_number || (preview.part_id ? `Part #${preview.part_id}` : 'None (nest package)')}
                </p>
              </div>
              <div className="rounded-sm border border-fd-line px-3 py-2">
                <p className="text-[11px] uppercase tracking-wider text-slate-500">Automatic backflush</p>
                <p
                  data-testid="backflush-preview-flag"
                  className={`font-mono text-sm ${preview.backflush_components ? 'text-fd-green' : 'text-slate-400'}`}
                >
                  {preview.backflush_components ? 'On' : 'Off'}
                </p>
              </div>
              <div className="rounded-sm border border-fd-line px-3 py-2">
                <p
                  className="text-[11px] uppercase tracking-wider text-slate-500"
                  title="Completed quantity plus operation scrap — a scrapped run still used its material."
                >
                  Demand basis
                </p>
                <p className="font-mono text-sm tabular-nums text-slate-200">{formatTieQty(preview.basis)}</p>
              </div>
            </div>
          )}

          {preview && !preview.backflush_components && preview.lines.some((line) => line.requires_opt_in) && (
            <p className="flex items-start gap-1.5 rounded-sm border border-fd-line bg-fd-sunken/40 px-2.5 py-2 text-xs text-slate-400">
              <InformationCircleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              <span>
                Automatic backflush is <strong className="text-slate-300">off</strong> for this part, so the BOM
                rows below are a forecast, not a commitment — they show what would happen if it were turned on.
                Rows marked <em>Tie</em> are unaffected: a material tie is its own opt-in and consumes either way.
              </span>
            </p>
          )}

          {preview && preview.basis <= 0 && (
            <p className="flex items-start gap-1.5 text-xs text-slate-500">
              <InformationCircleIcon className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
              <span>
                Nothing has been produced on this work order yet, so the demand basis is 0 and the BOM resolves to
                no component demand at all. That is the engine’s real answer, not a gap in this preview — report
                production and run it again.
              </span>
            </p>
          )}

          {preview && preview.blockers.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-red">
                {preview.blockers.length} problem{preview.blockers.length === 1 ? '' : 's'} resolving this demand
              </p>
              <ul className="space-y-1.5" data-testid="backflush-preview-blockers">
                {preview.blockers.map((diagnostic, index) => (
                  <DiagnosticRow key={`${diagnostic.code}-${index}`} diagnostic={diagnostic} tone="blocking" />
                ))}
              </ul>
            </div>
          )}

          {preview && preview.advisories.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-fd-amber">
                {preview.advisories.length} advisor{preview.advisories.length === 1 ? 'y' : 'ies'} (not blocking)
              </p>
              <ul className="space-y-1.5" data-testid="backflush-preview-advisories">
                {preview.advisories.map((diagnostic, index) => (
                  <DiagnosticRow key={`${diagnostic.code}-${index}`} diagnostic={diagnostic} tone="advisory" />
                ))}
              </ul>
            </div>
          )}

          <DataTable<BackflushPreviewLine>
            columns={columns}
            data={preview?.lines ?? []}
            rowKey={(line) => `${line.source}-${line.allocation_id ?? 'bom'}-${line.component_part_id}`}
            loading={loading}
            dense
            rowClassName={(line) => (line.suppressed ? 'opacity-60' : '')}
            empty={{
              icon: CubeIcon,
              title: 'Nothing would be consumed',
              description:
                'This work order resolves to no component demand — no BOM/routing components and no work-order-scoped material ties. Completing it moves no material.',
            }}
          />

          <p className="text-xs text-slate-500">
            Read-only. Running this writes nothing — no stock movement, no ledger row, no audit entry. The lots
            shown are the lots the completion would actually draw, in the order it walks them — including the
            row it posts for any quantity stock cannot cover, against the lot it drives negative — so this and
            the outcome cannot disagree about which heat gets consumed.
          </p>
        </div>
      )}
    </div>
  );
}
