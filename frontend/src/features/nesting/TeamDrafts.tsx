import React, { useEffect, useRef, useState } from 'react';
import { Cloud, History } from 'lucide-react';
import api from '../../services/api';
import type {
  NestingDraftPage,
  NestingDraftRevision,
  NestingDraftSave,
  NestingDraftSummary,
} from '../../types/nestingDraft';
import { formatCentralDateTime } from '../../utils/centralTime';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import { projectFromFile, projectToFile, type QuoteProject } from './lib/quote-project';
import { clearCatalogPricing } from './lib/material-binding';
import { nestingApiMessage } from './useNestingCatalog';

type PendingSave = { request: NestingDraftSave; signature: string };

function checkReceipt(value: NestingDraftSummary, companyId: number) {
  if (
    value.company_id !== companyId ||
    value.status !== 'DRAFT' ||
    !Number.isSafeInteger(value.draft_id) ||
    value.draft_id < 1 ||
    !Number.isSafeInteger(value.revision_number) ||
    value.revision_number < 1 ||
    value.draft_version !== value.revision_number ||
    !/^[a-f0-9]{64}$/.test(value.content_sha256)
  )
    throw new Error('The saved draft receipt is invalid or belongs to another company.');
}

export default function TeamDrafts({
  companyId,
  project,
  canSave,
  disabled,
  dirty,
  onOpen,
  onSaved,
}: {
  companyId: number;
  project: QuoteProject;
  canSave: boolean;
  disabled: boolean;
  dirty: boolean;
  onOpen: (project: QuoteProject) => void;
  onSaved: (signature: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<NestingDraftPage | null>(null);
  const [history, setHistory] = useState<NestingDraftSummary | null>(null);
  const [linked, setLinked] = useState<NestingDraftSummary | null>(null);
  const [replace, setReplace] = useState<NestingDraftSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState<PendingSave | null>(null);
  const readController = useRef<AbortController | null>(null);
  const writeController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const savingRef = useRef(false);
  const projectRef = useRef(project);
  projectRef.current = project;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      readController.current?.abort();
      writeController.current?.abort();
    };
  }, []);

  async function list(target: NestingDraftSummary | null, number = 1) {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true);
    setError('');
    setPage(null);
    setHistory(target);
    setReplace(null);
    try {
      const result = target
        ? await api.listNestingDraftRevisions(target.draft_id, number, controller.signal)
        : await api.listNestingDrafts(number, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      if (result.schema_version !== 1 || !Array.isArray(result.items)) throw new Error('Invalid draft list response.');
      result.items.forEach(item => checkReceipt(item, companyId));
      setPage(result);
    } catch (cause) {
      if (mounted.current && !controller.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }

  async function load(item: NestingDraftSummary) {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true);
    setError('');
    const signature = JSON.stringify(projectRef.current);
    try {
      const result = await api.getNestingDraftRevision(item.draft_id, item.revision_number, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      checkReceipt(result, companyId);
      if (result.draft_id !== item.draft_id || result.revision_number !== item.revision_number)
        throw new Error('The server returned a different draft revision.');
      if (signature !== JSON.stringify(projectRef.current))
        throw new Error('Your estimate changed while the draft loaded. Open it again to replace the current estimate.');
      const stored = projectFromFile(result.estimate);
      // Saved inputs are not a current catalog review or a validated nest result.
      const loaded = {
        ...stored,
        groups: stored.groups.map(group => ({ ...group, quote: clearCatalogPricing(group.quote) })),
      };
      if (
        loaded.groups.some(group => group.quote.materialBinding && group.quote.materialBinding.companyId !== companyId)
      )
        throw new Error('This estimate contains material sources from another company.');
      onOpen(loaded);
      setLinked(result);
      setReplace(null);
      setMessage(
        `Opened draft #${result.draft_id}, revision ${result.revision_number}. Compare sheets and review pricing again.`
      );
      setOpen(false);
    } catch (cause) {
      if (mounted.current && !controller.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }

  async function save(copy = false) {
    if (!canSave || savingRef.current || disabled) return;
    setError('');
    setMessage('');
    let operation: PendingSave;
    try {
      operation = pending ?? {
        request: {
          companyId,
          requestKey: crypto.randomUUID(),
          estimateJson: JSON.stringify(projectToFile(projectRef.current)),
          ...(!copy && linked ? { target: { draftId: linked.draft_id, expectedVersion: linked.draft_version } } : {}),
        },
        signature: JSON.stringify(projectRef.current),
      };
      if (new Blob([operation.request.estimateJson]).size > 5 * 1024 * 1024)
        throw new Error('Team draft limit: 5 MiB. Save a smaller estimate.');
    } catch (cause) {
      setError(nestingApiMessage(cause));
      return;
    }
    readController.current?.abort();
    setLoading(false);
    setReplace(null);
    savingRef.current = true;
    setSaving(true);
    setPending(operation);
    const controller = new AbortController();
    writeController.current = controller;
    try {
      const result: NestingDraftRevision = await api.saveNestingDraft(operation.request, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      checkReceipt(result, companyId);
      setPending(null);
      setLinked(result);
      onSaved(operation.signature);
      setMessage(
        `Saved draft #${result.draft_id}, revision ${result.revision_number}. Earlier revisions are retained.`
      );
      await list(null);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted) return;
      const status = (cause as { response?: { status?: number } })?.response?.status;
      // A definite 4xx refusal did not save this request. For uncertain network
      // failures retain the exact request/key; retry cannot create a duplicate.
      if (status && status >= 400 && status < 500 && status !== 408) setPending(null);
      setError(nestingApiMessage(cause));
    } finally {
      savingRef.current = false;
      if (mounted.current && !controller.signal.aborted) setSaving(false);
    }
  }

  function choose(item: NestingDraftSummary) {
    if (dirty) setReplace(item);
    else void load(item);
  }

  const unavailable = loading || saving || disabled;
  return (
    <>
      <button
        className="secondary compact"
        title={
          linked ? `Team draft #${linked.draft_id}, revision ${linked.revision_number}` : 'Save or open a team draft'
        }
        onClick={() => {
          setOpen(true);
          void list(null);
        }}
        disabled={disabled}
      >
        <Cloud size={16} /> Team drafts
      </button>
      <Dialog
        open={open}
        onOpenChange={value => {
          if (saving) return;
          if (!value) {
            readController.current?.abort();
            setLoading(false);
          }
          setOpen(value);
        }}
      >
        <DialogContent className="app-dialog team-drafts-dialog" showCloseButton={!saving}>
          <DialogHeader>
            <DialogTitle>Team nesting drafts</DialogTitle>
            <DialogDescription>
              Save inputs for another estimator or another day. Open a draft explicitly; the workspace always starts
              empty.
            </DialogDescription>
          </DialogHeader>
          <div className="team-draft-current">
            <strong>{project.name}</strong>
            <p>
              {linked
                ? `Draft #${linked.draft_id} · opened revision ${linked.revision_number}`
                : 'New, unsaved team draft'}
            </p>
            <p className="helper inset-free">Draft only. Recalculate the nest and review pricing after opening.</p>
            <div className="team-draft-actions">
              {canSave && (
                <button className="primary" disabled={unavailable} onClick={() => void save()}>
                  {saving
                    ? 'Saving…'
                    : pending
                      ? 'Retry previous save'
                      : linked
                        ? 'Save next revision'
                        : 'Save team draft'}
                </button>
              )}
              {canSave && linked && !pending && (
                <button className="secondary" disabled={unavailable} onClick={() => void save(true)}>
                  Save as new draft
                </button>
              )}
              {!canSave && <p>You can open drafts. Saving requires purchasing create permission.</p>}
            </div>
            {pending && !saving && (
              <p role="status">
                The previous save is unconfirmed. Retry checks that same snapshot without creating a duplicate.
              </p>
            )}
          </div>
          {message && (
            <p className="team-draft-message" role="status">
              {message}
            </p>
          )}
          {error && (
            <p className="team-draft-error" role="alert">
              {error}
            </p>
          )}
          {replace && (
            <div className="team-draft-replace">
              <p>
                Open “{replace.name}”, revision {replace.revision_number}, and replace your current unsaved inputs?
              </p>
              <div className="team-draft-actions">
                <button className="primary" disabled={unavailable} onClick={() => void load(replace)}>
                  Replace current estimate
                </button>
                <button className="secondary" disabled={unavailable} onClick={() => setReplace(null)}>
                  Keep current estimate
                </button>
              </div>
            </div>
          )}
          <div className="team-draft-list-heading">
            <h3>{history ? `Revision history · draft #${history.draft_id}` : 'Saved team drafts'}</h3>
            <button className="secondary compact" disabled={unavailable} onClick={() => void list(null)}>
              {history ? 'All drafts' : 'Refresh list'}
            </button>
          </div>
          {loading && <p role="status">Loading drafts…</p>}
          {page && !page.items.length && <p>No saved team drafts yet.</p>}
          <div className="team-draft-list">
            {page?.items.map(item => (
              <article className="team-draft-item" key={`${item.draft_id}:${item.revision_number}`}>
                <div>
                  <strong>{item.name}</strong>
                  <p>
                    #{item.draft_id} · revision {item.revision_number} · {formatCentralDateTime(item.created_at)}{' '}
                    Central · user #{item.created_by}
                  </p>
                  {item.review_issues.length > 0 && (
                    <details>
                      <summary>Review notes ({item.review_issues.length})</summary>
                      <ul>
                        {item.review_issues.map((issue, i) => (
                          <li key={`${issue.code}:${i}`}>{issue.message}</li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
                <div className="team-draft-actions">
                  <button
                    className="secondary"
                    disabled={unavailable || !!pending}
                    onClick={() => choose(item)}
                    aria-label={`Open ${item.name} revision ${item.revision_number}`}
                  >
                    Open revision {item.revision_number}
                  </button>
                  {!history && (
                    <button
                      className="secondary"
                      disabled={unavailable}
                      onClick={() => void list(item)}
                      aria-label={`History for ${item.name}`}
                    >
                      <History size={16} /> History
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
          {page && page.total > page.per_page && (
            <nav className="team-draft-actions" aria-label="Draft pages">
              <button
                className="secondary"
                disabled={unavailable || page.page <= 1}
                onClick={() => void list(history, page.page - 1)}
              >
                Previous drafts
              </button>
              <span>
                Page {page.page} of {Math.ceil(page.total / page.per_page)}
              </span>
              <button
                className="secondary"
                disabled={unavailable || page.page * page.per_page >= page.total}
                onClick={() => void list(history, page.page + 1)}
              >
                Next drafts
              </button>
            </nav>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
