import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { useCompany } from '../../context/CompanyContext';
import { Button, DataTable, FormField, Modal } from '../ui';
import type { DataTableColumn } from '../ui';
import { formatCentralDateTime } from '../../utils/centralTime';
import { STOCK_PIECE_ADVISORY } from '../../types/stockPiece';
import type { StockPieceDetail, StockPiecePage, StockPieceSummary } from '../../types/stockPiece';
import { shapeSummary } from '../../validation/stockPiece';
import StockPieceObservationEditor from './StockPieceObservationEditor';
import { StockPieceGeometryPreview } from './StockPieceGeometry';
import { SourceSnapshot } from './StockPieceSourcePicker';
import {
  checkObservation,
  checkObservationDetail,
  checkObservationPage,
  driftLabel,
  observationError,
} from './stockPieceHelpers';

function ObservationHistory({
  companyId,
  pieceId,
  number,
  onClose,
  onSelect,
  onEdit,
}: {
  companyId: number;
  pieceId: number;
  number: number;
  onClose: () => void;
  onSelect: (number: number) => void;
  onEdit: (value: StockPieceDetail, withdraw: boolean, allowed: boolean) => void;
}) {
  const [page, setPage] = useState(1),
    [refresh, setRefresh] = useState(0);
  const [history, setHistory] = useState<StockPiecePage<StockPieceSummary> | null>(null),
    [detail, setDetail] = useState<StockPieceDetail | null>(null);
  const [error, setError] = useState(''),
    [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setDetail(null);
    setHistory(null);
    Promise.all([
      api.getStockPieceObservation(pieceId, number, controller.signal),
      api.getStockPieceHistory(pieceId, page, controller.signal),
    ])
      .then(([value, revisions]) => {
        if (controller.signal.aborted) return;
        checkObservationDetail(value, companyId);
        checkObservationPage(revisions, companyId);
        if (value.piece_id !== pieceId || value.observation_number !== number)
          throw new Error('A different observation was returned.');
        revisions.items.forEach(item => {
          checkObservation(item, companyId);
          if (item.piece_id !== pieceId) throw new Error('History belongs to a different piece.');
        });
        setDetail(value);
        setHistory(revisions);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(observationError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [companyId, pieceId, number, page, refresh]);
  return (
    <Modal open onClose={onClose} size="5xl" ariaLabel="Piece observation history">
      <div className="space-y-4 text-slate-200">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold">{detail?.label ?? 'Piece observation history'}</h2>
            <p className="text-sm text-amber-300">{STOCK_PIECE_ADVISORY}</p>
          </div>
          <Button variant="secondary" onClick={onClose}>
            Close history
          </Button>
        </div>
        {loading && <p role="status">Loading observation history…</p>}
        {error && (
          <p role="alert" className="text-red-300">
            {error}
          </p>
        )}
        <Button variant="secondary" size="sm" onClick={() => setRefresh(n => n + 1)}>
          Refresh source drift and history
        </Button>
        {detail && history && (
          <>
            <div className="border border-slate-700 p-3 space-y-2">
              <p className="font-medium">
                Observation {detail.observation_number} ·{' '}
                {detail.state === 'WITHDRAWN' ? 'Withdrawn observation' : 'Recorded observation'}
              </p>
              <p className={detail.source_status === 'unchanged' ? 'text-slate-300' : 'text-amber-300'}>
                {driftLabel(detail.source_status)}
              </p>
              <p className="text-xs text-slate-400">
                This is a read-time comparison. No detected change does not establish that the piece remains present.
              </p>
              {detail.review_issues.map((issue, i) => (
                <p key={i} className="text-sm text-amber-300">
                  {issue}
                </p>
              ))}
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <StockPieceGeometryPreview shape={detail.evidence.geometry} zones={detail.evidence.unavailable_zones} />
              <div className="space-y-2 text-sm">
                <p>{shapeSummary(detail.evidence.geometry)}</p>
                <p>Reported thickness: {detail.evidence.thickness ? `${detail.evidence.thickness} in` : 'Unknown'}</p>
                <p>Reported grade: {detail.evidence.grade ?? 'Unknown'}</p>
                <p>Reported grain: {detail.evidence.grain_axis?.toUpperCase() ?? 'Unknown'}</p>
                <p>Measurement method: {detail.evidence.measurement_method}</p>
                <p>Original units: {detail.evidence.source_units}</p>
                <p>
                  Observed by {detail.observer_name} · {formatCentralDateTime(detail.observed_at)} Central
                </p>
                <p>
                  Recorded by user #{detail.created_by} · {formatCentralDateTime(detail.created_at)} Central
                  {detail.submitted_api_token_id ? ` · API credential #${detail.submitted_api_token_id}` : ''}
                </p>
                <p className="whitespace-pre-wrap">Reason: {detail.reason}</p>
                <p>Location note: {detail.evidence.location_note ?? 'Unknown'}</p>
                <p>Ownership note: {detail.evidence.ownership_note ?? 'Unknown'}</p>
                <p>Certification note: {detail.evidence.certification_note ?? 'Unknown'} — unverified</p>
              </div>
            </div>
            <SourceSnapshot snapshot={detail.source_snapshot} />
            <details>
              <summary className="cursor-pointer text-blue-300">Exact stored evidence and hashes</summary>
              <p className="text-xs text-slate-400 mt-2 break-all">
                Evidence SHA-256: {detail.payload_sha256}
                <br />
                Recorded source: {detail.source_sha256}
                <br />
                Current source: {detail.current_source_sha256 ?? 'Missing'}
                <br />
                Request key: {detail.request_key}
              </p>
              <pre className="text-xs text-slate-300 overflow-auto max-h-64 whitespace-pre-wrap break-all mt-2">
                {JSON.stringify(detail.evidence, null, 2)}
              </pre>
            </details>
            {history.can_record && (
              <div className="flex flex-wrap gap-3">
                <Button onClick={() => onEdit(detail, false, history.can_record)}>Record correction</Button>
                {detail.state !== 'WITHDRAWN' && (
                  <Button variant="secondary" onClick={() => onEdit(detail, true, history.can_record)}>
                    Withdraw observation
                  </Button>
                )}
              </div>
            )}
            <section className="space-y-2 border-t border-slate-700 pt-3">
              <h3 className="font-semibold">Immutable history</h3>
              <ul className="space-y-2">
                {history.items.map(item => (
                  <li key={item.observation_number}>
                    <Button
                      variant={item.observation_number === number ? 'primary' : 'secondary'}
                      size="sm"
                      onClick={() => onSelect(item.observation_number)}
                    >
                      Observation {item.observation_number} · {item.state === 'WITHDRAWN' ? 'Withdrawn' : 'Recorded'} ·{' '}
                      {formatCentralDateTime(item.created_at)}
                    </Button>
                  </li>
                ))}
              </ul>
              <div className="flex gap-3 items-center text-sm">
                <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage(n => n - 1)}>
                  Previous history
                </Button>
                <span>Page {page}</span>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={page * history.per_page >= history.total}
                  onClick={() => setPage(n => n + 1)}
                >
                  Next history
                </Button>
              </div>
            </section>
          </>
        )}
      </div>
    </Modal>
  );
}

function PieceRegister({ companyId }: { companyId: number }) {
  const [params, setParams] = useSearchParams();
  const pageValue = Number(params.get('piece_page') ?? 1),
    page = Number.isSafeInteger(pageValue) && pageValue > 0 ? pageValue : 1;
  const [result, setResult] = useState<StockPiecePage<StockPieceSummary> | null>(null),
    [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<{ id: number; number: number } | null>(null);
  const [editor, setEditor] = useState<{ previous?: StockPieceDetail; withdraw: boolean; allowed: boolean } | null>(
    null
  );
  const [filter, setFilter] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    setResult(null);
    api
      .getStockPieces(page, controller.signal)
      .then(response => {
        if (controller.signal.aborted) return;
        checkObservationPage(response, companyId);
        response.items.forEach(item => checkObservation(item, companyId));
        setResult(response);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(observationError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [companyId, page, refresh]);
  const columns: DataTableColumn<StockPieceSummary>[] = [
    {
      key: 'label',
      header: 'Piece label',
      render: row => (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setSelected({ id: row.piece_id, number: row.observation_number })}
        >
          {row.label}
        </Button>
      ),
    },
    {
      key: 'source',
      header: 'Source reference',
      render: row => `Part #${row.source_part_id} · inventory row #${row.source_inventory_item_id}`,
    },
    {
      key: 'state',
      header: 'Observation',
      render: row => `${row.state === 'WITHDRAWN' ? 'Withdrawn' : 'Recorded'} · revision ${row.observation_number}`,
    },
    {
      key: 'drift',
      header: 'Source comparison',
      render: row => (
        <span className={row.source_status !== 'unchanged' ? 'text-amber-300' : 'text-slate-300'}>
          {driftLabel(row.source_status)}
        </span>
      ),
    },
    {
      key: 'observed',
      header: 'Observed (Central)',
      render: row => `${formatCentralDateTime(row.observed_at)} · ${row.observer_name}`,
    },
  ];
  return (
    <section aria-label="Piece observations" className="space-y-4 text-slate-200">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-slate-100">Piece observations</h2>
          <p className="text-sm text-amber-300">{STOCK_PIECE_ADVISORY}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => setRefresh(n => n + 1)}>
            Refresh register
          </Button>
          {result?.can_record && (
            <Button
              onClick={() => {
                setNotice('');
                setEditor({ withdraw: false, allowed: true });
              }}
            >
              Record piece observation
            </Button>
          )}
        </div>
      </div>
      <p className="text-sm text-slate-400">
        Record individually labeled measurements linked to ERP source evidence. These records do not change inventory
        quantities, reserve material, create nesting stock or assign remnant credit.
      </p>
      {notice && (
        <p role="status" className="text-blue-300">
          {notice}
        </p>
      )}
      <FormField
        label="Filter labels on this page"
        help="This filters the loaded page only. Use page controls for the rest of the register."
      >
        {field => (
          <input
            {...field}
            className="input sm:max-w-sm"
            value={filter}
            maxLength={120}
            onChange={e => setFilter(e.target.value)}
          />
        )}
      </FormField>
      <DataTable
        columns={columns}
        data={(result?.items ?? []).filter(item => item.label.toLowerCase().includes(filter.toLowerCase()))}
        rowKey={row => row.piece_id}
        loading={loading}
        error={error || false}
        onRetry={() => setRefresh(n => n + 1)}
        manualSorting
        empty={{
          title: 'No observations on this page',
          description:
            'An authorized recorder can explicitly record a measured piece. No records are generated from inventory quantities or nesting output.',
        }}
        serverPagination={{
          page,
          pageSize: result?.per_page ?? 20,
          hasNext: !!result && page * result.per_page < result.total,
          onPageChange: next => {
            const updated = new URLSearchParams(params);
            updated.set('piece_page', String(next));
            setParams(updated);
          },
          loading,
        }}
      />
      {result && (
        <p className="text-xs text-slate-400">
          {result.total} observation records, including withdrawn records. This is not an available sheet count.
        </p>
      )}
      {selected && !editor && (
        <ObservationHistory
          companyId={companyId}
          pieceId={selected.id}
          number={selected.number}
          onClose={() => setSelected(null)}
          onSelect={number => setSelected({ ...selected, number })}
          onEdit={(previous, withdraw, allowed) => setEditor({ previous, withdraw, allowed })}
        />
      )}
      {editor && (
        <StockPieceObservationEditor
          companyId={companyId}
          canRecord={editor.allowed}
          previous={editor.previous}
          withdraw={editor.withdraw}
          onClose={() => setEditor(null)}
          onSaved={value => {
            setEditor(null);
            setSelected({ id: value.piece_id, number: value.observation_number });
            setNotice(
              `Saved ${value.label}, observation ${value.observation_number}. Inventory quantities are unchanged.`
            );
            setRefresh(n => n + 1);
          }}
        />
      )}
    </section>
  );
}

export default function StockPieceObservationsPanel() {
  const { user } = useAuth(),
    { currentCompany } = useCompany();
  const companyId = currentCompany?.id ?? user?.company_id;
  return companyId && user ? (
    <PieceRegister key={`${user.id}:${companyId}`} companyId={companyId} />
  ) : (
    <p className="text-slate-400">Select an authenticated company to view observations.</p>
  );
}
