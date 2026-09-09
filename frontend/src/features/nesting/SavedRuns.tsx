import React, { useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import type { NestingDraftSummary } from '../../types/nestingDraft';
import type {
  NestingRunDetail,
  NestingRunPage,
  NestingRunRequest,
  NestingRunSummary,
  NestingRuntime,
} from '../../types/nestingRun';
import { formatCentralDateTime } from '../../utils/centralTime';
import { nestingApiMessage } from './useNestingCatalog';
import { validateRemnantReport } from './lib/remnant-review';
import SavedRunPreview from './SavedRunPreview';

const active = (run: NestingRunSummary) => run.status === 'QUEUED' || run.status === 'RUNNING';
const labels = {
  QUEUED: 'Queued',
  RUNNING: 'Calculating',
  COMPLETED: 'Finished',
  PARTIAL: 'Partially evaluated',
  CANCELLED: 'Cancelled',
  FAILED: 'Stopped with an error',
};
export function checkRun(run: NestingRunSummary, target: NestingDraftSummary) {
  if (
    !Number.isSafeInteger(run.id) ||
    run.id < 1 ||
    run.company_id !== target.company_id ||
    run.draft_id !== target.draft_id ||
    run.revision_number !== target.revision_number ||
    run.input_sha256 !== target.content_sha256 ||
    !Object.prototype.hasOwnProperty.call(labels, run.status)
  ) {
    throw new Error('This calculation does not belong to the selected company and draft revision.');
  }
}

export default function SavedRuns({
  target,
  canStart,
  onBack,
}: {
  target: NestingDraftSummary;
  canStart: boolean;
  onBack: () => void;
}) {
  const [page, setPage] = useState<NestingRunPage | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [runtime, setRuntime] = useState<NestingRuntime | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [run, setRun] = useState<NestingRunDetail | null>(null);
  const [preview, setPreview] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<NestingRunRequest | null>(null);
  const live = useRef(true);
  const action = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      action.current?.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    void api
      .listNestingRuns(target.draft_id, target.revision_number, pageNumber, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.schema_version !== 1 || !Array.isArray(result.items))
          throw new Error('Invalid calculation history.');
        result.items.forEach(item => checkRun(item, target));
        setPage(result);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(nestingApiMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    void api
      .getNestingRuntime(controller.signal)
      .then(result => {
        if (!controller.signal.aborted) setRuntime(result.schema_version === 1 ? result : null);
      })
      .catch(() => {
        if (!controller.signal.aborted) setRuntime(null);
      });
    return () => controller.abort();
  }, [target, pageNumber, refresh]);

  useEffect(() => {
    setRun(null);
    setPreview(null);
    if (selectedId === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const result = await api.getNestingRun(selectedId!, controller.signal);
        if (controller.signal.aborted) return;
        checkRun(result, target);
        if (result.id !== selectedId) throw new Error('The server returned a different calculation.');
        if (result.schema_version !== 1 || !Array.isArray(result.checkpoints))
          throw new Error('Invalid saved calculation.');
        setRun(result);
        setPage(previous =>
          previous
            ? { ...previous, items: previous.items.map(item => (item.id === result.id ? result : item)) }
            : previous
        );
        if (active(result)) timer = setTimeout(() => void poll(), 3000);
      } catch (cause) {
        if (!controller.signal.aborted) setError(nestingApiMessage(cause));
      }
    }
    void poll();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [selectedId, target, refresh]);

  async function start() {
    if (!canStart || busyRef.current || (!pending && !runtime?.available)) return;
    const request = pending ?? {
      draft_id: target.draft_id,
      revision_number: target.revision_number,
      input_sha256: target.content_sha256,
      expected_company_id: target.company_id,
      request_key: crypto.randomUUID(),
    };
    setPending(request);
    setError('');
    setBusy(true);
    busyRef.current = true;
    const controller = new AbortController();
    action.current = controller;
    try {
      const result = await api.startNestingRun(request, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      checkRun(result, target);
      setPending(null);
      setSelectedId(result.id);
      setPageNumber(1);
      setRefresh(value => value + 1);
    } catch (cause) {
      if (!live.current || controller.signal.aborted) return;
      const status = (cause as { response?: { status?: number } })?.response?.status;
      if (status && status >= 400 && status < 500 && status !== 408) setPending(null);
      setError(nestingApiMessage(cause));
    } finally {
      busyRef.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }

  async function cancel() {
    if (!canStart || !run || !active(run) || busyRef.current) return;
    setBusy(true);
    busyRef.current = true;
    setError('');
    const controller = new AbortController();
    action.current = controller;
    try {
      const result = await api.cancelNestingRun(run.id, target.company_id, run.version, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      checkRun(result, target);
      setRefresh(value => value + 1);
    } catch (cause) {
      if (live.current && !controller.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      busyRef.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }

  async function download() {
    if (!run || busyRef.current) return;
    setBusy(true);
    busyRef.current = true;
    setError('');
    const controller = new AbortController();
    action.current = controller;
    try {
      const report = await api.getNestingRunReport(run.id, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      checkRun(report.run, target);
      if (
        report.run.id !== run.id ||
        report.schema_version !== 1 ||
        report.status !== 'UNAPPROVED' ||
        !/^[a-f0-9]{64}$/.test(report.content_sha256)
      )
        throw new Error('The saved report does not match this calculation.');
      await validateRemnantReport(report);
      if (!live.current || controller.signal.aborted) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `nest-draft-${target.draft_id}-revision-${target.revision_number}-run-${run.id}.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      if (live.current && !controller.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      busyRef.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <section className="saved-runs" aria-label="Saved server calculations">
      <div className="team-draft-list-heading">
        <h3>Saved calculations</h3>
        <button className="secondary compact" onClick={onBack} disabled={busy}>
          Back to drafts
        </button>
      </div>
      <p>
        <strong>{target.name}</strong> · draft #{target.draft_id}, revision {target.revision_number}
      </p>
      <p className="helper inset-free">
        Calculate these saved inputs and retain the results for team review. Current workspace edits are separate. These
        are unapproved quote layouts, not NC programs.
      </p>
      <div className="team-draft-actions">
        {canStart && (
          <button className="primary" disabled={busy || (!pending && !runtime?.available)} onClick={() => void start()}>
            {busy ? 'Working…' : pending ? 'Retry calculation request' : 'Calculate saved revision'}
          </button>
        )}
        <button className="secondary" disabled={busy || loading} onClick={() => setRefresh(value => value + 1)}>
          Refresh calculations
        </button>
      </div>
      {!runtime?.available && (
        <p role="status">The saved-calculation service is unavailable or starting. Refresh to check again.</p>
      )}
      {pending && !busy && (
        <p role="status">The request is unconfirmed. Retry recovers the same request without creating another run.</p>
      )}
      {error && (
        <p className="team-draft-error" role="alert">
          {error}
        </p>
      )}
      {loading && <p role="status">Loading calculations…</p>}
      {page && !page.items.length && <p>No calculations saved for this revision.</p>}
      <div className="team-draft-list">
        {page?.items.map(item => (
          <article className="team-draft-item" key={item.id}>
            <div>
              <strong>
                Run #{item.id} · {labels[item.status]}
              </strong>
              <p>
                {formatCentralDateTime(item.created_at)} Central · {item.evaluated_count} options evaluated
              </p>
            </div>
            <button
              className="secondary"
              onClick={() => {
                setSelectedId(item.id);
                setError('');
              }}
            >
              Review run #{item.id}
            </button>
          </article>
        ))}
      </div>
      {page && page.total > page.per_page && (
        <nav className="team-draft-actions" aria-label="Calculation pages">
          <button
            className="secondary"
            disabled={loading || pageNumber <= 1}
            onClick={() => setPageNumber(value => value - 1)}
          >
            Previous calculations
          </button>
          <span>
            Page {pageNumber} of {Math.ceil(page.total / page.per_page)}
          </span>
          <button
            className="secondary"
            disabled={loading || pageNumber * page.per_page >= page.total}
            onClick={() => setPageNumber(value => value + 1)}
          >
            Next calculations
          </button>
        </nav>
      )}
      {selectedId && !run && <p role="status">Loading run #{selectedId}…</p>}
      {run && (
        <div className="saved-run-review">
          <h3>
            Run #{run.id} · {labels[run.status]}
          </h3>
          <p>
            {run.evaluated_count} options evaluated · {run.completed_count} options fit their complete material group.
          </p>
          <p className="helper inset-free">
            Each option is a separate sheet-size scenario. Do not add alternative sheet counts together. Finished means
            the planned search ended; an option may still have unplaced parts.
          </p>
          {run.cancel_requested && active(run) && (
            <p role="status">Cancellation requested. Completed results will be retained.</p>
          )}
          {run.error_message && <p className="team-draft-error">{run.error_message}</p>}
          <div className="team-draft-actions">
            {canStart && active(run) && (
              <button className="secondary" disabled={busy || run.cancel_requested} onClick={() => void cancel()}>
                Cancel calculation
              </button>
            )}
            <button className="secondary" disabled={busy} onClick={() => void download()}>
              Download saved report
            </button>
          </div>
          <div className="saved-run-options">
            {run.checkpoints.map(checkpoint => (
              <article className="team-draft-item" key={checkpoint.sequence}>
                <div>
                  <strong>
                    {checkpoint.group_id} ·{' '}
                    {checkpoint.stage_kind === 'recorded_piece'
                      ? 'Recorded piece'
                      : checkpoint.stage_kind === 'residual'
                        ? `Remaining sheets · ${checkpoint.source_option_id}`
                        : checkpoint.stage_kind === 'baseline'
                          ? `Full-sheet baseline · ${checkpoint.source_option_id}`
                          : checkpoint.option_id}
                  </strong>
                  <p>
                    {checkpoint.sheets ?? '—'}{' '}
                    {checkpoint.stage_kind === 'recorded_piece' ? 'reported piece' : 'full sheets'} ·{' '}
                    {checkpoint.placed} placed · {checkpoint.unplaced} unplaced ·{' '}
                    {checkpoint.complete
                      ? checkpoint.stage_kind === 'residual'
                        ? 'Conditional remainder fits'
                        : 'Full group fits'
                      : 'Review incomplete layout'}
                  </p>
                </div>
                <button className="secondary" onClick={() => setPreview(checkpoint.sequence)}>
                  View {checkpoint.stage_kind ? 'stage' : 'option'} {checkpoint.sequence}
                </button>
              </article>
            ))}
          </div>
          {preview !== null && <SavedRunPreview key={`${run.id}:${preview}`} run={run} sequence={preview} />}
          <details>
            <summary>Calculation identity, limits and review notes</summary>
            <p>Input {run.input_sha256}</p>
            <p>
              Solver {run.solver_version ?? 'Pending'} · runtime {run.node_version ?? 'Pending'}
            </p>
            <p>Release {run.release_identity ?? 'Pending'}</p>
            <p>Bundle {run.bundle_sha256 ?? 'Pending'}</p>
            <pre>{JSON.stringify(run.settings, null, 2)}</pre>
            <ul>
              {run.warnings.map((warning, index) => (
                <li key={`${warning.code}:${index}`}>{warning.message}</li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </section>
  );
}
