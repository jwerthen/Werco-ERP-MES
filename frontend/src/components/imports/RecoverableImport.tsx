import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import api from '../../services/api';
import { ImportBatch, ImportBatchHistory } from '../../types/importBatch';
import { Button } from '../ui/Button';
import { FormField } from '../ui/FormField';
import { formatCentralDateTime } from '../../utils/centralTime';
import { useCompany } from '../../context/CompanyContext';

const message = (error: unknown) =>
  error instanceof Error ? error.message : 'Import request failed. Check the receipt before continuing.';
const RECORD_LINKS: Record<string, (id: number) => string> = {
  users: id => `/users?id=${id}`,
  parts: id => `/parts/${id}`,
  materials: id => `/parts/${id}`,
  customers: id => `/customers?id=${id}`,
  vendors: id => `/purchasing?vendor=${id}`,
  'work-orders': id => `/work-orders/${id}`,
  'purchase-orders': id => `/purchasing?po=${id}`,
};

export function saveImportDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export default function RecoverableImport({ entity, canImport }: { entity: string; canImport: boolean }) {
  const [params, setParams] = useSearchParams();
  const batchId = Number(params.get('batch')) || 0;
  const { currentCompany } = useCompany();
  const [file, setFile] = useState<File | null>(null);
  const [correction, setCorrection] = useState<File | null>(null);
  const [password, setPassword] = useState('');
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [history, setHistory] = useState<ImportBatchHistory | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [authEpoch, setAuthEpoch] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState('');
  const [historyError, setHistoryError] = useState(false);
  const generation = useRef(0);
  const requestKey = useRef('');
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const reset = () => {
      // Token replacement occurs before CompanyContext finishes its request.
      // Invalidate a queued chunk synchronously, before it can use the new token.
      generation.current += 1;
      setBatch(null);
      setHistory(null);
      setFile(null);
      setCorrection(null);
      setPassword('');
      setBusy(false);
      setReviewed(false);
      setUncertain(false);
      setError('');
      requestKey.current = '';
      setAuthEpoch(value => value + 1);
    };
    window.addEventListener('werco:auth-token-changed', reset);
    return () => window.removeEventListener('werco:auth-token-changed', reset);
  }, []);
  useEffect(() => {
    generation.current += 1;
    setBusy(false);
    setUncertain(false);
    return () => {
      generation.current += 1;
    };
  }, [batchId]);
  useEffect(() => {
    generation.current += 1;
    setBatch(null);
    setFile(null);
    setCorrection(null);
    setPassword('');
    setError('');
    setUncertain(false);
    setBusy(false);
    setHistoryOffset(0);
    requestKey.current = '';
    return () => {
      generation.current += 1;
    };
  }, [entity, currentCompany?.id]);
  useEffect(() => {
    setReviewed(false);
  }, [batch?.id, batch?.version]);
  useEffect(() => {
    let active = true;
    setHistory(null);
    setHistoryError(false);
    api
      .listImportBatches(entity, historyOffset)
      .then(data => {
        if (active) setHistory(data);
      })
      .catch(() => {
        if (active) setHistoryError(true);
      });
    return () => {
      active = false;
    };
  }, [entity, historyOffset, refresh, currentCompany?.id, authEpoch]);
  useEffect(() => {
    let active = true;
    if (!batchId) {
      setBatch(null);
      return;
    }
    setLoading(true);
    setError('');
    api
      .getImportBatch(batchId)
      .then(data => {
        if (active && data.entity === entity) {
          setBatch(data);
          setUncertain(false);
        } else if (active) setError('This receipt belongs to another import type. Open it from that type’s history.');
      })
      .catch(error => {
        if (active) setError(message(error));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [batchId, entity, currentCompany?.id, authEpoch]);
  const selectBatch = (id: number) => {
    const next = new URLSearchParams(params);
    next.set('batch', String(id));
    setParams(next);
  };
  const readReceipt = useCallback(
    async (offset = 0) => {
      if (!batchId) return;
      const stamp = generation.current;
      setLoading(true);
      try {
        const data = await api.getImportBatch(batchId, offset);
        if (stamp === generation.current) {
          setBatch(data);
          setUncertain(false);
          setError('');
        }
      } catch (error) {
        if (stamp === generation.current) setError(message(error));
      } finally {
        if (stamp === generation.current) setLoading(false);
      }
    },
    [batchId]
  );
  const prepare = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file || busy || !canImport) return;
    const stamp = generation.current;
    if (!requestKey.current) requestKey.current = crypto.randomUUID();
    setBusy(true);
    setError('');
    try {
      const data = await api.prepareImportBatch(entity, file, requestKey.current, password);
      if (stamp !== generation.current) return;
      setBatch(data);
      selectBatch(data.id);
      setUncertain(false);
      setRefresh(value => value + 1);
    } catch (error) {
      if (stamp === generation.current) setError(message(error));
    } finally {
      if (stamp === generation.current) setBusy(false);
    }
  };
  const commit = async () => {
    if (!batch || busy || uncertain || !reviewed || !canImport) return;
    const stamp = generation.current;
    setBusy(true);
    setError('');
    let current = batch;
    try {
      while (current.counts.ready > 0) {
        const next = await api.commitImportBatch(
          current.id,
          current.version,
          entity === 'users' ? file : null,
          password
        );
        if (stamp !== generation.current) return;
        setBatch(next);
        if (next.version === current.version) break;
        current = next;
      }
      setRefresh(value => value + 1);
      if (!current.counts.ready) {
        setPassword('');
        setFile(null);
        if (fileInput.current) fileInput.current.value = '';
      }
    } catch (error) {
      if (stamp === generation.current) {
        setUncertain(true);
        setError(`${message(error)} Refresh the receipt to see which rows committed.`);
      }
    } finally {
      if (stamp === generation.current) setBusy(false);
    }
  };
  const correct = async () => {
    if (!batch || !correction || busy || !canImport) return;
    const stamp = generation.current;
    setBusy(true);
    setError('');
    try {
      const data = await api.correctImportBatch(batch.id, batch.version, correction, password);
      if (stamp !== generation.current) return;
      setBatch(data);
      setFile(entity === 'users' ? correction : file);
      setCorrection(null);
      setRefresh(value => value + 1);
    } catch (error) {
      if (stamp === generation.current) setError(message(error));
    } finally {
      if (stamp === generation.current) setBusy(false);
    }
  };
  const download = async () => {
    if (!batch) return;
    const stamp = generation.current;
    try {
      const blob = await api.downloadFailedImportRows(batch.id);
      if (stamp === generation.current) saveImportDownload(blob, `import-${batch.id}-failed-rows.csv`);
    } catch (error) {
      if (stamp === generation.current) setError(message(error));
    }
  };
  return (
    <div className="space-y-5">
      <form onSubmit={prepare} className="card space-y-3">
        <FormField label="Import file">
          {field => (
            <input
              {...field}
              ref={fileInput}
              type="file"
              accept=".csv,.xlsx"
              disabled={busy || !canImport}
              onChange={event => {
                setFile(event.target.files?.[0] || null);
                requestKey.current = '';
              }}
            />
          )}
        </FormField>
        {entity === 'users' && (
          <>
            <FormField label="Default password">
              {field => (
                <input
                  {...field}
                  className="input"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  disabled={busy}
                  onChange={event => setPassword(event.target.value)}
                />
              )}
            </FormField>
            <p className="text-sm text-fd-mute">
              Passwords stay out of saved batches and downloads. When resuming employee imports, reattach the reviewed
              file or enter a default password. Operators can omit it.
            </p>
          </>
        )}
        <Button disabled={!file || busy || !canImport} type="submit">
          {busy ? 'Working…' : 'Validate file (dry run)'}
        </Button>
        <p className="text-sm text-fd-mute">
          Validation saves a review receipt. It creates no business records. Uploading the same input opens its existing
          receipt.
        </p>
        {!canImport && <p role="status">Your role can view this workflow but cannot import this record type.</p>}
      </form>
      {error && (
        <div role="alert" className="border border-amber-500 p-3">
          {error}
        </div>
      )}
      {loading && <p role="status">Loading receipt…</p>}
      {batch && (
        <section className="card space-y-4" aria-label="Import receipt">
          <div className="flex flex-wrap justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold">Import receipt #{batch.id}</h2>
              <p>{batch.filename}</p>
              <p className="text-xs text-fd-mute">Updated {formatCentralDateTime(batch.updated_at)}</p>
            </div>
            <Button variant="secondary" disabled={busy || loading} onClick={() => readReceipt()}>
              Refresh receipt
            </Button>
          </div>
          <p role="status">
            {batch.total_rows} rows · {batch.counts.ready || 0} ready · {batch.created_records} records created ·{' '}
            {(batch.counts.invalid || 0) + (batch.counts.failed || 0)} rows need correction
          </p>
          <div className="space-y-2">
            {batch.rows.map(row => (
              <div key={row.row_key} className="border border-fd-line p-3">
                <div className="flex flex-wrap justify-between gap-2">
                  <strong>
                    Row {row.source_row}:{' '}
                    {row.data.part_number ||
                      row.data.name ||
                      row.data.employee_id ||
                      row.data.code ||
                      row.data.po_number ||
                      'Record'}
                  </strong>
                  <span>{row.status}</span>
                </div>
                <p className="text-sm text-fd-mute">
                  {Object.entries(row.data)
                    .filter(([, value]) => value !== '')
                    .map(([key, value]) => `${key.replace(/_/g, ' ')}: ${value}`)
                    .join(' · ')}
                </p>
                {row.error && <p className="text-sm text-amber-300">{row.error}</p>}
                {row.status === 'created' && row.result?.record_id && !RECORD_LINKS[entity] && (
                  <span className="text-sm">Created record #{row.result.record_id}</span>
                )}
                {row.status === 'created' && row.result?.record_id && RECORD_LINKS[entity] && (
                  <Link className="text-fd-blue underline" to={RECORD_LINKS[entity](row.result.record_id)}>
                    Open created record
                  </Link>
                )}
              </div>
            ))}
          </div>
          {(batch.row_offset > 0 || batch.has_more_rows) && (
            <nav aria-label="Import row pages" className="flex gap-3">
              <Button
                variant="secondary"
                disabled={loading || busy || batch.row_offset === 0}
                onClick={() => readReceipt(Math.max(0, batch.row_offset - 200))}
              >
                Previous rows
              </Button>
              <Button
                variant="secondary"
                disabled={loading || busy || !batch.has_more_rows}
                onClick={() => readReceipt(batch.row_offset + 200)}
              >
                Next rows
              </Button>
            </nav>
          )}
          <label className="flex gap-2 items-start">
            <input
              type="checkbox"
              checked={reviewed}
              disabled={busy || uncertain}
              onChange={event => setReviewed(event.target.checked)}
            />
            <span>
              I reviewed the ready rows and authorize their import
              {entity === 'work-orders'
                ? ' as open production jobs'
                : entity === 'purchase-orders'
                  ? ' as issued purchase orders'
                  : ''}
              .
            </span>
          </label>
          <Button
            disabled={busy || loading || uncertain || !reviewed || !batch.counts.ready || !canImport}
            onClick={commit}
          >
            {busy ? 'Importing…' : 'Commit ready rows'}
          </Button>
          {!batch.counts.ready && (
            <p className="text-sm text-fd-mute">
              No ready rows remain. Created records are preserved; correct failed rows to continue.
            </p>
          )}
          {(batch.counts.invalid || 0) + (batch.counts.failed || 0) > 0 && (
            <div className="border-t border-fd-line pt-3 space-y-3">
              <Button variant="secondary" onClick={download}>
                Download failed rows CSV
              </Button>
              <p className="text-sm text-fd-mute">
                Correct only these rows. Keep _import_row_id unchanged and all lines of a failed purchase order
                together.
              </p>
              <FormField label="Corrected failed-row file">
                {field => (
                  <input
                    {...field}
                    type="file"
                    accept=".csv,.xlsx"
                    disabled={busy || !canImport}
                    onChange={event => setCorrection(event.target.files?.[0] || null)}
                  />
                )}
              </FormField>
              <Button variant="secondary" disabled={busy || uncertain || !correction || !canImport} onClick={correct}>
                Validate corrections
              </Button>
            </div>
          )}
        </section>
      )}
      <section className="card space-y-3" aria-label="Import history">
        <h2 className="text-xl font-semibold">Saved import batches</h2>
        {historyError && (
          <div role="alert">
            Batch history is unavailable.{' '}
            <Button variant="secondary" onClick={() => setRefresh(value => value + 1)}>
              Retry history
            </Button>
          </div>
        )}
        {!history && !historyError && <p role="status">Loading history…</p>}
        {history?.batches.map(item => (
          <Button
            variant="ghost"
            key={item.id}
            className="block w-full text-left border border-fd-line p-3 hover:bg-white/5"
            disabled={busy}
            onClick={() => selectBatch(item.id)}
          >
            <strong>
              #{item.id} {item.filename}
            </strong>
            <span className="block text-sm">
              {item.created_records} created · {item.counts.ready || 0} ready · {formatCentralDateTime(item.created_at)}
            </span>
          </Button>
        ))}
        {history?.batches.length === 0 && <p>No saved batches for this record type.</p>}
        {(historyOffset > 0 || history?.has_more) && (
          <nav aria-label="Import history pages" className="flex gap-3">
            <Button
              variant="secondary"
              disabled={historyOffset === 0 || busy}
              onClick={() => setHistoryOffset(value => Math.max(0, value - 25))}
            >
              Newer batches
            </Button>
            <Button
              variant="secondary"
              disabled={!history?.has_more || busy}
              onClick={() => setHistoryOffset(value => value + 25)}
            >
              Older batches
            </Button>
          </nav>
        )}
      </section>
    </div>
  );
}
