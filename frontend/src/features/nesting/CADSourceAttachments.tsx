import React, { useCallback, useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { useCompany } from '../../context/CompanyContext';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import type { NestingDraftSummary } from '../../types/nestingDraft';
import type { NestingSourceIntent, NestingSourcePage, NestingSourceRequest } from '../../types/nestingSource';
import { formatCentralDateTime } from '../../utils/centralTime';
import { sha256 } from './lib/provenance';
import { nestingApiMessage } from './useNestingCatalog';
import { createSourcePacer, sourceRetryAt } from './cadSourceTransport';
import {
  checkSourceIntent,
  checkSourcePage,
  fingerprintOriginal,
  savedSourceParts,
  sourceTargetKey,
  type SavedSourcePart,
} from './cadSourceEvidence';

type Props = {
  target: NestingDraftSummary;
  onBack: () => void;
  registerCloseGuard: (guard: (() => boolean) | null) => void;
};
type FileRow = {
  id: string;
  file: File;
  hash: string;
  matches: SavedSourcePart[];
  selected: string[];
  state: 'ready' | 'intent' | 'sending' | 'attached' | 'uncertain' | 'resend' | 'rejected' | 'paused';
  message: string;
  request?: NestingSourceRequest;
  intent?: NestingSourceIntent;
};

export default function CADSourceAttachments(props: Props) {
  const { user } = useAuth();
  const { currentCompany } = useCompany();
  if (!user || currentCompany?.id !== props.target.company_id)
    return <p role="alert">Select the original company to view this saved revision’s attachments.</p>;
  return (
    <AttachmentPanel
      key={`${user.id}:${currentCompany.id}:${props.target.draft_id}:${props.target.revision_number}:${props.target.content_sha256}`}
      {...props}
      actorId={user.id}
    />
  );
}

function AttachmentPanel({ target, actorId, onBack, registerCloseGuard }: Props & { actorId: number }) {
  const [parts, setParts] = useState<SavedSourcePart[]>([]);
  const [page, setPage] = useState<NestingSourcePage | null>(null);
  const [rows, setRows] = useState<FileRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [recovering, setRecovering] = useState<NestingSourceIntent | null>(null);
  const [retryAt, setRetryAt] = useState(0);
  const [clock, setClock] = useState(Date.now());
  const pacer = useRef(createSourcePacer());
  const live = useRef(true);
  const busyRef = useRef(false);
  const stop = useRef(false);
  const read = useRef<AbortController | null>(null);
  const action = useRef<AbortController | null>(null);
  const reselect = useRef<HTMLInputElement>(null);
  const dirty = busy || rows.some(row => !['attached', 'rejected'].includes(row.state));
  const { confirmDiscard } = useUnsavedChanges(
    dirty,
    'Leave original DXF attachments? Local selections will be cleared. Submitted attachments may continue and can be checked in this saved revision.'
  );
  useEffect(() => {
    registerCloseGuard(confirmDiscard);
    return () => registerCloseGuard(null);
  }, [confirmDiscard, registerCloseGuard]);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      read.current?.abort();
      action.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (!retryAt) return;
    const timer = setInterval(() => {
      setClock(Date.now());
      if (Date.now() >= retryAt) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [retryAt]);

  const refresh = useCallback(
    async (number = 1, knownParts?: SavedSourcePart[]) => {
      read.current?.abort();
      const controller = new AbortController();
      read.current = controller;
      setLoading(true);
      setError('');
      try {
        const saved =
          knownParts ??
          savedSourceParts(
            await api.getNestingDraftRevision(target.draft_id, target.revision_number, controller.signal),
            target
          );
        if (!live.current || controller.signal.aborted) return;
        const result = await checkSourcePage(
          await api.listNestingSources(target.draft_id, target.revision_number, number, controller.signal),
          target,
          saved
        );
        if (!live.current || controller.signal.aborted) return;
        setParts(saved);
        setPage(result);
      } catch (cause) {
        if (live.current && !controller.signal.aborted) setError(nestingApiMessage(cause));
      } finally {
        if (live.current && !controller.signal.aborted) setLoading(false);
      }
    },
    [target]
  );
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const update = (id: string, changes: Partial<FileRow>) =>
    setRows(previous => previous.map(row => (row.id === id ? { ...row, ...changes } : row)));
  const valid = (controller: AbortController) => live.current && !controller.signal.aborted;
  const remember = (intent: NestingSourceIntent) =>
    setPage(previous =>
      previous
        ? {
            ...previous,
            items: previous.items.some(item => item.id === intent.id)
              ? previous.items.map(item => (item.id === intent.id ? intent : item))
              : previous.items,
          }
        : previous
    );

  async function choose(files: File[], resumed?: NestingSourceIntent) {
    if (busyRef.current || !page || (!resumed && !page.can_attach)) return;
    if (files.length > 100 || (resumed && files.length !== 1)) {
      setError(
        resumed
          ? 'Select one matching original for this attachment.'
          : 'Select up to 100 original DXFs. No files were read or uploaded.'
      );
      return;
    }
    if (rows.some(row => !['attached', 'rejected'].includes(row.state)) && !confirmDiscard()) return;
    busyRef.current = true;
    stop.current = false;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    action.current = controller;
    const selected: FileRow[] = [];
    const hashes = new Set<string>();
    try {
      for (const file of files) {
        if (stop.current) break;
        if (!valid(controller)) return;
        const row: FileRow = {
          id: crypto.randomUUID(),
          file,
          hash: '',
          matches: [],
          selected: [],
          state: 'ready',
          message: '',
        };
        try {
          row.hash = await fingerprintOriginal(file);
          if (!valid(controller)) return;
          if (hashes.has(row.hash))
            throw new Error(
              'These exact bytes are already selected in this batch. Use the first file’s profile selections.'
            );
          hashes.add(row.hash);
          row.matches = parts.filter(
            part => part.provenance?.sourceHashBasis === 'original-bytes' && part.provenance.sourceSha256 === row.hash
          );
          if (!row.matches.length)
            throw new Error('No saved profile has this original-byte hash. Names alone do not establish a match.');
          if (resumed) {
            if (row.hash !== resumed.source_sha256 || file.size !== resumed.byte_count)
              throw new Error('Choose the exact original bytes recorded for this pending attachment.');
            row.matches = row.matches.filter(part =>
              resumed.targets.some(t => sourceTargetKey(t) === sourceTargetKey(part))
            );
            row.intent = resumed;
            row.state = 'uncertain';
            row.message =
              'Check the existing attachment before resending. Filename changes do not change the recorded command.';
          }
          row.selected = row.matches.map(sourceTargetKey);
        } catch (cause) {
          row.state = 'rejected';
          row.message = nestingApiMessage(cause);
        }
        selected.push(row);
        if (valid(controller)) setRows([...selected]);
      }
    } finally {
      busyRef.current = false;
      if (valid(controller)) {
        setBusy(false);
        setRecovering(null);
      }
    }
  }

  async function send(row: FileRow, controller: AbortController, resend = false) {
    let intent = row.intent;
    const request =
      row.request ??
      (intent
        ? {
            expected_company_id: target.company_id,
            expected_input_sha256: intent.input_sha256,
            request_key: intent.request_key,
            source_sha256: intent.source_sha256,
            byte_count: intent.byte_count,
            source_name: intent.source_name,
            mime_type: intent.mime_type,
            targets: intent.targets.map(part => ({ group_id: part.group_id, part_id: part.part_id })),
          }
        : {
            expected_company_id: target.company_id,
            expected_input_sha256: target.content_sha256,
            request_key: crypto.randomUUID(),
            source_sha256: row.hash,
            byte_count: row.file.size,
            source_name: row.file.name,
            mime_type: 'application/dxf',
            targets: row.matches
              .filter(part => row.selected.includes(sourceTargetKey(part)))
              .map(part => ({ group_id: part.group_id, part_id: part.part_id })),
          });
    update(row.id, { request, state: 'intent', message: 'Recording attachment intent…' });
    try {
      if (!intent) {
        await pacer.current(controller.signal);
        const created = await checkSourceIntent(
          await api.createNestingSourceIntent(target.draft_id, target.revision_number, request, controller.signal),
          target,
          parts,
          request
        );
        if (created.created_by !== actorId) throw new Error('The upload intent belongs to a different estimator.');
        if (!valid(controller)) return;
        intent = created;
        update(row.id, { intent });
      }
      if (intent.state !== 'ATTACHED') {
        if (!intent.can_resume) throw new Error('This credential cannot resume the attachment.');
        // An uncertain intent-creation retry checks completion before any body upload.
        if (row.request && !resend) {
          await pacer.current(controller.signal);
          intent = await checkSourceIntent(
            await api.finalizeNestingSource(
              target.draft_id,
              target.revision_number,
              intent.id,
              target.company_id,
              controller.signal
            ),
            target,
            parts,
            undefined,
            intent
          );
          if (!valid(controller)) return;
          if (intent.state !== 'ATTACHED')
            throw new Error('Attachment remains pending. Check it, then explicitly resend the original if needed.');
        } else {
          update(row.id, {
            state: 'sending',
            message: 'Sending original bytes; the server verifies storage before retaining a receipt…',
          });
          const bytes = await row.file.arrayBuffer();
          if (bytes.byteLength !== intent.byte_count || (await sha256(bytes)) !== intent.source_sha256)
            throw new Error('The selected bytes do not match the frozen upload command.');
          if (!valid(controller)) return;
          await pacer.current(controller.signal);
          intent = await checkSourceIntent(
            await api.uploadNestingSource(
              target.draft_id,
              target.revision_number,
              intent.id,
              target.company_id,
              bytes,
              controller.signal
            ),
            target,
            parts,
            undefined,
            intent
          );
        }
      }
      if (!valid(controller)) return;
      if (intent.state !== 'ATTACHED')
        throw new Error('No completed byte-verification receipt was returned. Check attachment before retrying.');
      remember(intent);
      update(row.id, {
        intent,
        state: 'attached',
        message: 'Original bytes retained. Geometry and reported revision remain unapproved.',
      });
    } catch (cause) {
      if (valid(controller)) {
        const until = sourceRetryAt(cause);
        if (until) {
          setRetryAt(until);
          setClock(Date.now());
          stop.current = true;
        }
        update(row.id, {
          ...(intent ? { intent } : {}),
          state: until ? 'paused' : 'uncertain',
          message: until
            ? 'API rate limit paused this batch. The exact command and remaining selections are retained. Resume explicitly after the wait.'
            : nestingApiMessage(cause),
        });
      }
    }
  }

  async function batch(only?: FileRow, resend = false) {
    if (busyRef.current || Date.now() < retryAt || (!only && !page?.can_attach)) return;
    const selected = only
      ? [only]
      : rows.filter(row => ['ready', 'paused'].includes(row.state) && row.selected.length > 0);
    busyRef.current = true;
    stop.current = false;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    action.current = controller;
    try {
      for (const row of selected) {
        if (stop.current || !valid(controller)) break;
        await send(row, controller, resend || row.state === 'paused');
      }
      if (valid(controller))
        setNotice(
          stop.current
            ? 'Stopped before the next file. Submitted attachments may already be retained; check their receipts.'
            : 'Batch finished. Review each file’s receipt or recovery message.'
        );
    } finally {
      busyRef.current = false;
      if (valid(controller)) setBusy(false);
    }
  }

  async function recover(intent: NestingSourceIntent, row?: FileRow) {
    if (busyRef.current || Date.now() < retryAt || !intent.can_resume) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    action.current = controller;
    try {
      await pacer.current(controller.signal);
      const result = await checkSourceIntent(
        await api.finalizeNestingSource(
          target.draft_id,
          target.revision_number,
          intent.id,
          target.company_id,
          controller.signal
        ),
        target,
        parts,
        undefined,
        intent
      );
      if (!valid(controller)) return;
      remember(result);
      if (row)
        update(row.id, {
          intent: result,
          state: result.state === 'ATTACHED' ? 'attached' : 'resend',
          message:
            result.state === 'ATTACHED'
              ? 'Original bytes retained. No resend was needed.'
              : 'Still pending. Resend is a separate explicit action.',
        });
      else
        setNotice(
          result.state === 'ATTACHED'
            ? `Attachment #${result.id} retained without resending.`
            : 'No completed attachment. Reselect the original if you want to resend.'
        );
    } catch (cause) {
      if (!valid(controller)) return;
      const until = sourceRetryAt(cause);
      if (until) {
        setRetryAt(until);
        setClock(Date.now());
      }
      if (row)
        update(row.id, {
          state: 'resend',
          message: `${nestingApiMessage(cause)} No bytes were resent. Resend only if you want another attempt.`,
        });
      else setError(`${nestingApiMessage(cause)} No bytes were resent.`);
    } finally {
      busyRef.current = false;
      if (valid(controller)) setBusy(false);
    }
  }

  async function download(intent: NestingSourceIntent) {
    if (busyRef.current || !intent.receipt) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    const controller = new AbortController();
    action.current = controller;
    try {
      const blob = await api.downloadNestingSource(
        target.draft_id,
        target.revision_number,
        intent.id,
        controller.signal
      );
      if (!valid(controller)) return;
      if (blob.size !== intent.byte_count || (await sha256(await blob.arrayBuffer())) !== intent.source_sha256)
        throw new Error('Downloaded bytes differ from the retained receipt. Nothing was downloaded.');
      if (!valid(controller)) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = intent.source_name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      if (valid(controller)) setError(nestingApiMessage(cause));
    } finally {
      busyRef.current = false;
      if (valid(controller)) setBusy(false);
    }
  }

  const ineligible = parts.filter(part => part.provenance?.sourceHashBasis !== 'original-bytes').length;
  return (
    <section className="cad-source-panel" aria-label="Original DXF attachments">
      <div className="team-draft-actions">
        <button
          className="secondary"
          onClick={() => {
            if (confirmDiscard()) onBack();
          }}
        >
          Back to drafts
        </button>
        <button className="secondary" disabled={busy || loading} onClick={() => void refresh(page?.page ?? 1, parts)}>
          Refresh attachments
        </button>
      </div>
      <h3>Original DXFs · revision {target.revision_number}</h3>
      <p>
        {target.name} · draft #{target.draft_id}. Attachments belong to this saved revision; current workspace edits are
        unchanged.
      </p>
      <details>
        <summary>Saved input fingerprint</summary>
        <code className="cad-source-hash">{target.content_sha256}</code>
      </details>
      <p className="cad-source-disclosure">
        Retained bytes are unapproved evidence. They do not certify geometry, customer revision, manufacturing readiness
        or present storage availability.
      </p>
      {error && (
        <p className="team-draft-error" role="alert">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {retryAt > 0 && (
        <p role="status">
          {clock < retryAt
            ? `Rate-limit pause: wait ${Math.ceil((retryAt - clock) / 1000)} seconds before resuming.`
            : 'The rate-limit wait has ended. Resume the paused batch explicitly.'}
        </p>
      )}
      {loading && <p role="status">Loading saved profiles and attachments…</p>}
      {ineligible > 0 && (
        <p>
          {ineligible} saved profiles lack an original-byte hash. Text-based hashes and missing provenance cannot be
          upgraded here.
        </p>
      )}
      {page?.can_attach && (
        <label className="cad-source-file">
          Choose original DXFs (up to 100; each smaller than 5 MB)
          <input
            type="file"
            accept=".dxf"
            multiple
            disabled={busy || loading}
            onChange={event => {
              const files = Array.from(event.target.files ?? []);
              event.target.value = '';
              if (files.length) void choose(files);
            }}
          />
        </label>
      )}
      {page && !page.can_attach && (
        <p>You can view retained evidence. Attaching requires purchasing create permission in this company.</p>
      )}
      <p className="helper inset-free">
        Files stay local until you choose Attach selected originals. Batches are paced to respect API limits and may
        take several minutes. Matching uses exact bytes, including line endings. The server checks existing bindings
        before completion.
      </p>
      <div className="cad-source-files">
        {rows.map(row => (
          <article className="cad-source-file-row" key={row.id}>
            <strong>{row.file.name}</strong>
            <p>
              {row.file.size.toLocaleString()} bytes ·{' '}
              {row.state === 'attached'
                ? 'Retained'
                : row.state === 'rejected'
                  ? 'Not eligible'
                  : 'Pending selection or attachment'}
            </p>
            {row.hash && (
              <details>
                <summary>Original-byte SHA-256</summary>
                <code className="cad-source-hash">{row.hash}</code>
              </details>
            )}
            {row.matches.length > 0 && (
              <fieldset disabled={busy || !!row.request || !!row.intent}>
                <legend>Matching saved profiles ({row.matches.length})</legend>
                {row.matches.map(part => (
                  <label className="cad-source-target" key={sourceTargetKey(part)}>
                    <input
                      type="checkbox"
                      checked={row.selected.includes(sourceTargetKey(part))}
                      onChange={event =>
                        update(row.id, {
                          selected: event.target.checked
                            ? [...row.selected, sourceTargetKey(part)]
                            : row.selected.filter(key => key !== sourceTargetKey(part)),
                        })
                      }
                    />
                    <span>
                      {part.name}
                      {Object.prototype.hasOwnProperty.call(part, 'revision')
                        ? ` · reported revision ${part.revision ?? 'unknown'}`
                        : ''}
                      <small>
                        {part.groupName} · {part.part_id}
                      </small>
                    </span>
                  </label>
                ))}
              </fieldset>
            )}
            {row.message && <p role="status">{row.message}</p>}
            <div className="team-draft-actions">
              {row.state === 'uncertain' && (
                <button
                  className="secondary"
                  disabled={busy || (row.intent ? !row.intent.can_resume : !page?.can_attach)}
                  onClick={() => (row.intent ? void recover(row.intent, row) : void batch(row))}
                >
                  Check attachment
                </button>
              )}
              {row.state === 'resend' && row.intent && (
                <button
                  className="secondary"
                  disabled={busy || !row.intent.can_resume || row.intent.attempt_count >= 8}
                  onClick={() => void batch(row, true)}
                >
                  Resend original
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
      {rows.length > 0 && (
        <div className="team-draft-actions">
          <button
            className="primary"
            disabled={
              busy ||
              clock < retryAt ||
              !page?.can_attach ||
              !rows.some(row => ['ready', 'paused'].includes(row.state) && row.selected.length > 0)
            }
            onClick={() => void batch()}
          >
            {rows.some(row => row.state === 'paused') ? 'Resume paused batch' : 'Attach selected originals'}
          </button>
          {busy && (
            <button
              className="secondary"
              onClick={() => {
                stop.current = true;
                setNotice('Stopping after the current file. Already submitted work is not rolled back.');
              }}
            >
              Stop after this file
            </button>
          )}
          {!busy && (
            <button
              className="secondary"
              onClick={() => {
                if (confirmDiscard()) setRows([]);
              }}
            >
              Clear local selection
            </button>
          )}
        </div>
      )}
      <h4>Saved attachment history</h4>
      {page && !page.items.length && <p>No attachment intents are recorded on this page.</p>}
      <div className="cad-source-history">
        {page?.items.map(intent => (
          <article className="cad-source-file-row" key={intent.id}>
            <strong>{intent.source_name}</strong>
            <p>
              #{intent.id} · {intent.target_count} profiles ·{' '}
              {intent.state === 'ATTACHED' ? 'Original bytes retained' : `Pending · ${intent.attempt_count}/8 attempts`}
            </p>
            <details>
              <summary>Retained command details</summary>
              <code className="cad-source-hash">{intent.source_sha256}</code>
              <p>
                {intent.byte_count.toLocaleString()} bytes · user #{intent.created_by} ·{' '}
                {formatCentralDateTime(intent.created_at)} Central
              </p>
              {intent.targets.map(evidence => (
                <p key={sourceTargetKey(evidence)}>
                  {parts.find(part => sourceTargetKey(part) === sourceTargetKey(evidence))?.name} · {evidence.part_id}
                </p>
              ))}
            </details>
            {intent.receipt && (
              <p>Verified {formatCentralDateTime(intent.receipt.verified_at)} Central. Unapproved source evidence.</p>
            )}
            <div className="team-draft-actions">
              {intent.state === 'ATTACHED' ? (
                <button className="secondary" disabled={busy} onClick={() => void download(intent)}>
                  Download original
                </button>
              ) : intent.can_resume ? (
                <>
                  <button className="secondary" disabled={busy} onClick={() => void recover(intent)}>
                    Check and finish attachment
                  </button>
                  <button
                    className="secondary"
                    disabled={busy || intent.attempt_count >= 8}
                    onClick={() => {
                      setRecovering(intent);
                      reselect.current?.click();
                    }}
                  >
                    Reselect matching original
                  </button>
                </>
              ) : (
                <p>Only the original actor and credential with write permission can resume this command.</p>
              )}
            </div>
          </article>
        ))}
      </div>
      <input
        ref={reselect}
        className="sr-only"
        type="file"
        accept=".dxf"
        aria-label="Reselect matching original DXF"
        onChange={event => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = '';
          if (recovering && files.length) void choose(files, recovering);
        }}
      />
      {page && page.total > page.per_page && (
        <nav className="team-draft-actions" aria-label="Attachment pages">
          <button
            className="secondary"
            disabled={busy || loading || page.page <= 1}
            onClick={() => void refresh(page.page - 1, parts)}
          >
            Previous attachments
          </button>
          <span>
            Page {page.page} of {Math.ceil(page.total / page.per_page)}
          </span>
          <button
            className="secondary"
            disabled={busy || loading || page.page * page.per_page >= page.total}
            onClick={() => void refresh(page.page + 1, parts)}
          >
            Next attachments
          </button>
        </nav>
      )}
    </section>
  );
}
