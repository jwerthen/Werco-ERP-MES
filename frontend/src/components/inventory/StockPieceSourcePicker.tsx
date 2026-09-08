import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { Button, FormField } from '../ui';
import type { StockPiecePage, StockPieceSource } from '../../types/stockPiece';
import {
  checkObservationPage,
  checkSource,
  observationError,
  sourceLabel,
  snapshotObject,
  snapshotText,
} from './stockPieceHelpers';

export function SourceSnapshot({
  snapshot,
  heading = 'Recorded ERP source snapshot',
}: {
  snapshot: Record<string, unknown>;
  heading?: string;
}) {
  const item = snapshotObject(snapshot, 'item'),
    part = snapshotObject(snapshot, 'part');
  const rows = [
    ['Part', `${snapshotText(part.part_number)} · ${snapshotText(part.name)}`],
    ['Lot / heat', `${snapshotText(item.lot_number)} / ${snapshotText(item.heat_lot)}`],
    ['Location / warehouse', `${snapshotText(item.location)} / ${snapshotText(item.warehouse)}`],
    ['ERP status / UOM', `${snapshotText(item.status)} / ${snapshotText(part.unit_of_measure)}`],
    ['Aggregate on hand', snapshotText(item.quantity_on_hand)],
    ['Certificate text', snapshotText(item.cert_number)],
  ];
  return (
    <section className="border border-slate-700 p-3 space-y-2">
      <h4 className="font-semibold text-slate-200">{heading}</h4>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {rows.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt className="text-slate-400">{label}</dt>
            <dd className="min-w-0 break-words text-slate-200">{value}</dd>
          </React.Fragment>
        ))}
      </dl>
      <p className="text-xs text-slate-400">
        Aggregate quantity does not establish a physical piece count. ERP fields are source evidence, not measured
        dimensions or eligibility.
      </p>
      <details>
        <summary className="cursor-pointer text-sm text-blue-300">All source fields and movement watermark</summary>
        <pre className="text-xs text-slate-300 overflow-auto max-h-48 whitespace-pre-wrap break-all mt-2">
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      </details>
    </section>
  );
}

export default function StockPieceSourcePicker({
  companyId,
  value,
  onChange,
  initialItemId,
  onCapability,
}: {
  companyId: number;
  value: StockPieceSource | null;
  onChange: (source: StockPieceSource) => void;
  initialItemId?: number;
  onCapability: (allowed: boolean) => void;
}) {
  const [query, setQuery] = useState('');
  const [exact, setExact] = useState<number | undefined>(initialItemId);
  const search = useDebouncedValue(query, 250);
  const [page, setPage] = useState(1),
    [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<StockPiecePage<StockPieceSource> | null>(null);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setResult(null);
    api
      .getStockPieceSources({ page, per_page: 10, q: search || undefined, inventory_item_id: exact }, controller.signal)
      .then(response => {
        if (controller.signal.aborted) return;
        checkObservationPage(response, companyId);
        response.items.forEach(source => checkSource(source, companyId));
        setResult(response);
        onCapability(response.can_record);
      })
      .catch(cause => {
        if (!controller.signal.aborted) {
          setError(observationError(cause));
          onCapability(false);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [companyId, exact, page, search, refresh, onCapability]);
  return (
    <section className="space-y-3">
      <h3 className="font-semibold text-slate-100">Select the ERP source</h3>
      <FormField label="Search source part number, name, or lot">
        {field => (
          <input
            {...field}
            value={query}
            className="input"
            maxLength={100}
            onChange={e => {
              setQuery(e.target.value);
              setExact(undefined);
              setPage(1);
            }}
          />
        )}
      </FormField>
      <div className="flex gap-2 flex-wrap">
        <Button size="sm" variant="secondary" onClick={() => setRefresh(n => n + 1)}>
          Refresh source choices
        </Button>
        {exact !== undefined && (
          <Button size="sm" variant="ghost" onClick={() => setExact(undefined)}>
            Show all source choices
          </Button>
        )}
      </div>
      {loading && (
        <p role="status" className="text-sm text-slate-400">
          Loading source choices…
        </p>
      )}
      {error && (
        <p role="alert" className="text-red-300">
          {error}
        </p>
      )}
      {result && (
        <>
          <ul className="max-h-48 overflow-auto divide-y divide-slate-700 border border-slate-700">
            {result.items.map(source => (
              <li key={source.inventory_item_id}>
                <button
                  type="button"
                  className="w-full text-left px-3 py-2 hover:bg-slate-800 text-sm text-slate-200"
                  aria-pressed={
                    value?.inventory_item_id === source.inventory_item_id &&
                    value?.source_sha256 === source.source_sha256
                  }
                  onClick={() => onChange(source)}
                >
                  {sourceLabel(source)}
                  {value?.inventory_item_id === source.inventory_item_id &&
                  value?.source_sha256 === source.source_sha256
                    ? ' · Selected'
                    : ''}
                </button>
              </li>
            ))}
          </ul>
          {!result.items.length && (
            <p className="text-sm text-slate-400">No matching source rows. No source will be inferred.</p>
          )}
          <div className="flex items-center gap-3 text-sm">
            <Button variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage(n => n - 1)}>
              Previous sources
            </Button>
            <span className="text-slate-400">
              Page {page} · {result.total} source rows
            </span>
            <Button
              variant="secondary"
              size="sm"
              disabled={page * result.per_page >= result.total}
              onClick={() => setPage(n => n + 1)}
            >
              Next sources
            </Button>
          </div>
        </>
      )}
      {value && (
        <>
          <SourceSnapshot snapshot={value.snapshot} heading="Source selected for this observation" />
          <p className="text-xs text-slate-400 break-all">Source hash: {value.source_sha256}</p>
          {value.review_issues.map((issue, i) => (
            <p key={i} className="text-sm text-amber-300">
              {issue}
            </p>
          ))}
        </>
      )}
    </section>
  );
}
