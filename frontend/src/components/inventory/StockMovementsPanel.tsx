/**
 * Stock Movements — the inventory ledger, read-only.
 *
 * This panel exists to answer one question the rest of the app could not:
 * **"what took material off the shelf, and when?"** Everything else in the
 * Inventory section is a CURRENT-STATE snapshot (on-hand by part, on-hand by
 * location), so a quantity that dropped had no explanation attached to it and a
 * quantity that never dropped was indistinguishable from a quantity that was
 * never supposed to. `GET /inventory/transactions` has always carried the
 * answer; nothing rendered it.
 *
 * Four things about the data shape it is built on:
 *
 * 1. **The sign convention is not uniform, so never sum the column blindly.**
 *    `receive` is positive, `issue` is negative, `adjust`/`count` carry the
 *    signed delta — but `transfer` carries a POSITIVE quantity representing a
 *    ZERO net change in on-hand (it names both `from_location` and
 *    `to_location`). The totals below therefore EXCLUDE transfer rows, and they
 *    are labelled as covering the visible page only, because the endpoint is
 *    offset-paged with no aggregate.
 *
 * 2. **`reference_number` is what names the job, not `reference_id`.** Material
 *    consumed against a work order posts under one of three reference shapes,
 *    and on the headline one (`work_order_operation`, per-operation tie
 *    consumption — the laser-nest case) `reference_id` is an OPERATION id, not a
 *    work order id. Every shape stamps the work order NUMBER into
 *    `reference_number`, so that is the field the Source column reads. Deriving
 *    the job from `reference_id` would silently mislabel exactly the rows this
 *    panel was built for.
 *
 * 3. **Date filters are compared against UTC-stored timestamps.** A bare
 *    `YYYY-MM-DD` would be read as UTC midnight and push second-shift movements
 *    into the wrong day, so both bounds go through `centralWallClockToUtcISO` —
 *    the shop is Central and the filter has to mean a Central day.
 *
 * 4. **Soft-deleted work orders keep their rows.** The endpoint deliberately
 *    applies no `is_deleted` filter: a voided work order's movements are still
 *    real ledger facts. Don't add client-side hiding.
 *
 * Strictly a READ. No mutation lives here and none should — correcting a posted
 * movement is a reasoned, audited compensating transaction owned by the
 * receiving-correction and material-return verbs, never a ledger edit.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowsRightLeftIcon, ArrowDownTrayIcon, ArrowUpTrayIcon, QueueListIcon } from '@heroicons/react/24/outline';

import api from '../../services/api';
import { DataTable, FormField, MobileDataCard, SelectField } from '../ui';
import type { DataTableColumn, SelectOption } from '../ui';
import { MiniStat, MiniStatStrip } from '../cockpit';
import { centralWallClockToUtcISO, formatCentralDateTime, getCentralDateStamp } from '../../utils/centralTime';
import type { InventoryTransaction, InventoryTransactionParams } from '../../types';

const PAGE_SIZE = 50;

/**
 * Movement types, in the order a stockroom reads them. Values match
 * `models/inventory.TransactionType`; the label is the shop's word for it.
 */
const TYPE_OPTIONS: SelectOption<string>[] = [
  { value: '', label: 'All movement types' },
  { value: 'issue', label: 'Issue — consumed by a job' },
  { value: 'receive', label: 'Receive — into stock' },
  { value: 'return', label: 'Return — back to stock' },
  { value: 'adjust', label: 'Adjust — correction' },
  { value: 'count', label: 'Count — cycle count' },
  { value: 'scrap', label: 'Scrap' },
  { value: 'transfer', label: 'Transfer — between locations' },
  { value: 'ship', label: 'Ship — to customer' },
];

/**
 * `reference_type` → what a human calls it. Only the shapes that actually reach
 * the ledger are named; anything else is humanized rather than hidden, so a new
 * reference shape shows up as itself instead of a blank cell.
 */
const SOURCE_LABELS: Record<string, string> = {
  work_order_operation: 'Operation completed',
  work_order_backflush: 'Backflush / material tie',
  work_order: 'Work order',
  po_receipt: 'PO receipt',
  purchase_order: 'Purchase order',
  shipment: 'Shipment',
  ncr: 'Nonconformance',
};

/**
 * The Source sub-label. Reads the reference SHAPE, with one correction for the
 * movement type: a reasoned material RETURN posts under the same
 * `work_order_operation` shape as the consumption it compensates, so the plain
 * shape label ("Operation completed") would describe a give-back as a draw.
 * That is not cosmetic — a return is the audited reversal of a completion, and
 * labelling it as the completion inverts the direction of the record.
 */
const humanizeSource = (referenceType?: string | null, transactionType?: string | null): string => {
  if (!referenceType) return '—';
  if (referenceType === 'work_order_operation' && transactionType === 'return') {
    return 'Operation — material returned';
  }
  return SOURCE_LABELS[referenceType] ?? referenceType.replace(/_/g, ' ');
};

/** Tailwind chrome per movement type, matching the app's semantic status palette. */
const TYPE_TONE: Record<string, string> = {
  issue: 'border-fd-blue/40 bg-fd-blue/10 text-fd-blue',
  receive: 'border-fd-green/40 bg-fd-green/10 text-fd-green',
  return: 'border-fd-green/40 bg-fd-green/10 text-fd-green',
  adjust: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber',
  count: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber',
  scrap: 'border-fd-red/40 bg-fd-red/10 text-fd-red',
  ship: 'border-fd-blue/40 bg-fd-blue/10 text-fd-blue',
  transfer: 'border-fd-line bg-fd-panel text-slate-300',
};

/** Quick date ranges, as a count of days back from today (null = no bound). */
const QUICK_RANGES: { id: string; label: string; days: number | null }[] = [
  { id: 'all', label: 'All time', days: null },
  { id: 'today', label: 'Today', days: 0 },
  { id: '7', label: 'Last 7 days', days: 7 },
  { id: '30', label: 'Last 30 days', days: 30 },
];

const daysAgoStamp = (days: number): string => {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return getCentralDateStamp(d);
};

const formatQty = (value: number): string => {
  const rounded = Math.round(value * 1000) / 1000;
  return `${rounded > 0 ? '+' : ''}${rounded}`;
};

export interface StockMovementsPanelProps {
  /**
   * Parts for the part filter. Passed in rather than re-fetched — the Inventory
   * page already holds this list, and a second identical fetch on tab switch is
   * pure waste. Optional: with no list the filter simply doesn't render.
   */
  parts?: Array<{ id: number; part_number?: string; name?: string }>;
  /** Preselect a part (e.g. drilled in from a summary row). */
  initialPartId?: number | null;
}

export default function StockMovementsPanel({ parts, initialPartId = null }: StockMovementsPanelProps) {
  const [rows, setRows] = useState<InventoryTransaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [page, setPage] = useState(0);
  const [hasNext, setHasNext] = useState(false);

  const [partId, setPartId] = useState<number | ''>(initialPartId ?? '');
  const [typeFilter, setTypeFilter] = useState('');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [activeRange, setActiveRange] = useState('all');

  const partOptions = useMemo<SelectOption<string>[]>(() => {
    const opts: SelectOption<string>[] = [{ value: '', label: 'All parts' }];
    (parts ?? []).forEach((p) => {
      opts.push({
        value: String(p.id),
        label: p.part_number || `Part #${p.id}`,
        description: p.name || undefined,
      });
    });
    return opts;
  }, [parts]);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const params: InventoryTransactionParams = {
        // Over-fetch one row to infer `hasNext` — the endpoint returns no total
        // count, exactly like GET /audit/.
        limit: PAGE_SIZE + 1,
        offset: page * PAGE_SIZE,
      };
      if (partId !== '') params.part_id = Number(partId);
      if (typeFilter) params.transaction_type = typeFilter;
      // Central day boundaries → UTC instants. A bare date would be read as UTC
      // midnight and mis-bucket second-shift movements.
      const startUtc = startDate ? centralWallClockToUtcISO(`${startDate}T00:00:00`) : null;
      const endUtc = endDate ? centralWallClockToUtcISO(`${endDate}T23:59:59`) : null;
      if (startUtc) params.start_date = startUtc;
      if (endUtc) params.end_date = endUtc;

      const res = await api.getInventoryTransactions(params);
      const list = Array.isArray(res) ? res : [];
      setHasNext(list.length > PAGE_SIZE);
      setRows(list.slice(0, PAGE_SIZE));
    } catch {
      setLoadError(true);
      setRows([]);
      setHasNext(false);
    } finally {
      setLoading(false);
    }
  }, [endDate, page, partId, startDate, typeFilter]);

  useEffect(() => {
    load();
  }, [load]);

  /** Changing any filter returns to the first page — page 3 of the old query is meaningless. */
  const resetToFirstPage = useCallback(() => setPage(0), []);

  const applyQuickRange = useCallback(
    (rangeId: string) => {
      const range = QUICK_RANGES.find((r) => r.id === rangeId);
      if (!range) return;
      setActiveRange(rangeId);
      if (range.days === null) {
        setStartDate('');
        setEndDate('');
      } else {
        setStartDate(daysAgoStamp(range.days));
        setEndDate(getCentralDateStamp(new Date()));
      }
      resetToFirstPage();
    },
    [resetToFirstPage]
  );

  /**
   * Page totals. TRANSFER ROWS ARE EXCLUDED — they carry a positive quantity for
   * a zero net on-hand change, so including them would invent stock. Labelled
   * "on this page" because the endpoint is offset-paged with no aggregate: a
   * total presented as if it covered the whole filtered set would be a lie.
   */
  const pageTotals = useMemo(() => {
    let out = 0;
    let inn = 0;
    rows.forEach((r) => {
      if (r.transaction_type === 'transfer') return;
      const q = Number(r.quantity) || 0;
      if (q < 0) out += q;
      else inn += q;
    });
    return { out, inn, net: inn + out };
  }, [rows]);

  const columns: DataTableColumn<InventoryTransaction>[] = useMemo(
    () => [
      {
        key: 'created_at',
        header: 'When',
        sortable: true,
        accessor: (r) => r.created_at ?? '',
        render: (r) => (
          <span className="whitespace-nowrap text-slate-300">
            {r.created_at ? formatCentralDateTime(r.created_at) : '—'}
          </span>
        ),
        csv: (r) => (r.created_at ? formatCentralDateTime(r.created_at) : ''),
      },
      {
        key: 'part',
        header: 'Part',
        sortable: true,
        accessor: (r) => r.part?.part_number ?? '',
        render: (r) => (
          <div className="min-w-0">
            <div className="font-medium text-white truncate">{r.part?.part_number ?? `#${r.part_id}`}</div>
            {r.part?.name && <div className="text-xs text-slate-400 truncate">{r.part.name}</div>}
          </div>
        ),
        csv: (r) => r.part?.part_number ?? String(r.part_id),
      },
      {
        key: 'transaction_type',
        header: 'Movement',
        sortable: true,
        accessor: (r) => r.transaction_type,
        render: (r) => (
          <span
            className={`inline-flex items-center rounded-sm border px-2 py-0.5 text-xs font-medium capitalize ${
              TYPE_TONE[r.transaction_type] ?? 'border-fd-line bg-fd-panel text-slate-300'
            }`}
          >
            {r.transaction_type}
          </span>
        ),
        csv: (r) => r.transaction_type,
      },
      {
        key: 'quantity',
        header: 'Qty',
        align: 'right',
        sortable: true,
        accessor: (r) => Number(r.quantity) || 0,
        render: (r) => {
          const q = Number(r.quantity) || 0;
          // A transfer is net-zero on-hand; showing it signed would imply a
          // change that did not happen.
          const isTransfer = r.transaction_type === 'transfer';
          return (
            <span
              className={`font-mono tabular-nums ${
                isTransfer ? 'text-slate-400' : q < 0 ? 'text-fd-red' : 'text-fd-green'
              }`}
            >
              {isTransfer ? Math.abs(q) : formatQty(q)}
              {r.part?.unit_of_measure ? <span className="ml-1 text-xs text-slate-400">{r.part.unit_of_measure}</span> : null}
            </span>
          );
        },
        csv: (r) => Number(r.quantity) || 0,
      },
      {
        key: 'lot_number',
        header: 'Lot',
        sortable: true,
        accessor: (r) => r.lot_number ?? '',
        render: (r) => <span className="font-mono text-xs text-slate-300">{r.lot_number || '—'}</span>,
        csv: (r) => r.lot_number ?? '',
      },
      {
        key: 'location',
        header: 'Location',
        accessor: (r) => r.from_location ?? r.to_location ?? '',
        render: (r) => {
          if (r.from_location && r.to_location) {
            return (
              <span className="whitespace-nowrap text-xs text-slate-300">
                {r.from_location} <span className="text-slate-400">→</span> {r.to_location}
              </span>
            );
          }
          return <span className="text-xs text-slate-300">{r.from_location || r.to_location || '—'}</span>;
        },
        csv: (r) =>
          r.from_location && r.to_location ? `${r.from_location} -> ${r.to_location}` : r.from_location || r.to_location || '',
      },
      {
        key: 'source',
        header: 'Source',
        sortable: true,
        // `reference_number` carries the WORK ORDER NUMBER on every work-order
        // shape — see the module docstring on why `reference_id` must not be used.
        accessor: (r) => r.reference_number ?? '',
        render: (r) => (
          <div className="min-w-0">
            <div className="truncate text-slate-200">{r.reference_number || '—'}</div>
            <div className="truncate text-xs text-slate-400">{humanizeSource(r.reference_type, r.transaction_type)}</div>
          </div>
        ),
        csv: (r) => `${r.reference_number ?? ''} (${humanizeSource(r.reference_type, r.transaction_type)})`,
      },
      {
        key: 'notes',
        header: 'Detail',
        accessor: (r) => r.notes ?? '',
        className: 'max-w-md',
        render: (r) => (
          <span className="block truncate text-xs text-slate-400" title={r.notes || undefined}>
            {r.notes || '—'}
          </span>
        ),
        csv: (r) => r.notes ?? '',
      },
    ],
    []
  );

  const filtersActive = partId !== '' || !!typeFilter || !!startDate || !!endDate;

  return (
    <div className="space-y-4">
      {/* What this screen is. Worth the two lines: the sign rules and the
          operation-completion timing are the two things that make a reader
          think the data is wrong when it isn't. */}
      <p className="text-sm text-slate-400">
        Every movement in and out of stock, newest first. Material tied to a job leaves stock when its{' '}
        <span className="text-slate-300">operation is completed</span> — not per reported run — and shows up here as an{' '}
        <span className="text-slate-300">issue</span> against that work order.
      </p>

      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={QueueListIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Movements on this page"
          value={rows.length}
        />
        <MiniStat
          icon={ArrowUpTrayIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Out (this page)"
          value={Math.abs(Math.round(pageTotals.out * 1000) / 1000)}
        />
        <MiniStat
          icon={ArrowDownTrayIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="In (this page)"
          value={Math.round(pageTotals.inn * 1000) / 1000}
        />
        <MiniStat
          icon={ArrowsRightLeftIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Net (this page)"
          value={formatQty(pageTotals.net)}
          valueColor={pageTotals.net < 0 ? 'text-fd-red' : 'text-fd-green'}
        />
      </MiniStatStrip>

      {/* Filters */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3 space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {/* SelectField takes `ariaLabel` rather than the FormField render-prop
              wiring (it renders a button+listbox, not a native control), so both
              of these use FormField's plain-node form — the VisitorLog idiom. */}
          {parts && parts.length > 0 && (
            <FormField label="Part">
              <SelectField
                searchable
                ariaLabel="Filter movements by part"
                value={partId === '' ? '' : String(partId)}
                options={partOptions}
                onChange={(value) => {
                  setPartId(value === '' ? '' : Number(value));
                  resetToFirstPage();
                }}
              />
            </FormField>
          )}
          <FormField label="Movement type">
            <SelectField
              ariaLabel="Filter movements by type"
              value={typeFilter}
              options={TYPE_OPTIONS}
              onChange={(value) => {
                setTypeFilter(String(value));
                resetToFirstPage();
              }}
            />
          </FormField>
          <FormField label="From">
            {(field) => (
              <input
                {...field}
                type="date"
                className="input"
                value={startDate}
                onChange={(e) => {
                  setStartDate(e.target.value);
                  setActiveRange('');
                  resetToFirstPage();
                }}
              />
            )}
          </FormField>
          <FormField label="To">
            {(field) => (
              <input
                {...field}
                type="date"
                className="input"
                value={endDate}
                onChange={(e) => {
                  setEndDate(e.target.value);
                  setActiveRange('');
                  resetToFirstPage();
                }}
              />
            )}
          </FormField>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          {QUICK_RANGES.map((range) => (
            <button
              key={range.id}
              type="button"
              onClick={() => applyQuickRange(range.id)}
              className={`rounded-full border px-3 py-1 font-medium transition ${
                activeRange === range.id
                  ? 'border-werco-500 bg-werco-500/10 text-werco-700'
                  : 'border-slate-700 text-slate-400 hover:border-werco-300'
              }`}
            >
              {range.label}
            </button>
          ))}
          {filtersActive && (
            <button
              type="button"
              onClick={() => {
                setPartId('');
                setTypeFilter('');
                setStartDate('');
                setEndDate('');
                setActiveRange('all');
                resetToFirstPage();
              }}
              className="ml-auto rounded-sm border border-fd-line px-3 py-1 font-medium text-slate-400 hover:border-fd-line-bright"
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      <DataTable
        columns={columns}
        data={rows}
        rowKey={(r) => r.id}
        loading={loading}
        error={loadError}
        onRetry={load}
        defaultSort={{ key: 'created_at', dir: 'desc' }}
        serverPagination={{
          page: page + 1,
          pageSize: PAGE_SIZE,
          hasNext,
          onPageChange: (nextPage) => setPage(nextPage - 1),
        }}
        csvExport={{ filename: 'stock-movements' }}
        stickyHeader
        empty={{
          icon: ArrowsRightLeftIcon,
          title: filtersActive ? 'No movements match these filters' : 'No stock movements recorded yet',
          description: filtersActive
            ? 'Widen the date range or clear the part/type filter.'
            : 'Movements appear here as material is received, issued to jobs, transferred, or adjusted. If jobs are completing but nothing shows up, the work orders may not have material tied to them.',
        }}
        mobileCards={(r) => (
          <MobileDataCard
            title={r.part?.part_number ?? `#${r.part_id}`}
            subtitle={r.created_at ? formatCentralDateTime(r.created_at) : undefined}
            fields={[
              { label: 'Movement', value: r.transaction_type },
              { label: 'Qty', value: r.transaction_type === 'transfer' ? Math.abs(Number(r.quantity) || 0) : formatQty(Number(r.quantity) || 0) },
              { label: 'Lot', value: r.lot_number || '—' },
              { label: 'Source', value: `${r.reference_number || '—'} · ${humanizeSource(r.reference_type, r.transaction_type)}` },
            ]}
          />
        )}
      />
    </div>
  );
}
