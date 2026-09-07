import { useCallback, useEffect, useRef, useState } from 'react';
import { WorkspaceRecord } from '../types/workspace';
import { useWorkspaceRecords, workspaceError, workspaceIdentity } from './useWorkspaceRecords';

interface DraftOptions<T> {
  namespace: string;
  value: T;
  dirty: boolean;
  enabled: boolean;
  restore: (data: T) => void;
  valid: (data: unknown) => data is T;
}

/** Private server drafts. Writes serialize and use compare-and-swap versions;
 * a conflict pauses autosave until the user reloads the other saved draft. */
export function useResumableDraft<T>({ namespace, value, dirty, enabled, restore, valid }: DraftOptions<T>) {
  const records = useWorkspaceRecords<T>(namespace, 'draft');
  const [candidate, setCandidate] = useState<WorkspaceRecord<T> | null>(null);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [settled, setSettled] = useState(false);
  const row = useRef<WorkspaceRecord<T> | null>(null);
  const initialized = useRef('');
  const generation = useRef(0);
  const stopped = useRef(false);
  const chain = useRef<Promise<unknown>>(Promise.resolve());
  const lastSaved = useRef('');
  const current = useRef({ value, restore, valid, records, enabled, dirty });
  current.current = { value, restore, valid, records, enabled, dirty };
  const scope = records.scope;
  const serialized = JSON.stringify(value);

  useEffect(() => {
    const revision = ++generation.current;
    initialized.current = '';
    row.current = null;
    lastSaved.current = '';
    stopped.current = !enabled;
    setReady(false);
    setBusy(false);
    setSettled(false);
    setCandidate(null);
    setError('');
    setStatus('');
    // A modal may reopen before its previous autosave has finished. Wait for
    // that write to publish the observed row/version before offering recovery.
    void chain.current
      .catch(() => undefined)
      .then(() => {
        if (revision === generation.current) setSettled(true);
      });
  }, [scope, enabled]);

  useEffect(() => {
    if (!enabled || !settled || !records.identity || records.loading || records.error || initialized.current === scope)
      return;
    initialized.current = scope;
    const saved = records.rows.find(item => item.key === 'new');
    row.current = saved || null;
    stopped.current = false;
    if (saved) {
      setCandidate(saved);
      setReady(false);
      if (!current.current.valid(saved.data))
        setError('This draft uses an unsupported format. Remove it to start a new draft.');
    } else setReady(true);
  }, [enabled, settled, records.identity, records.loading, records.error, records.rows, scope]);

  const persist = useCallback((data: T) => {
    const revision = generation.current;
    const capturedScope = current.current.records.scope;
    const run = chain.current
      .catch(() => undefined)
      .then(async () => {
        const api = current.current.records;
        if (
          stopped.current ||
          revision !== generation.current ||
          api.scope !== capturedScope ||
          api.identity !== workspaceIdentity()
        )
          return;
        const snapshot = JSON.stringify(data);
        if (snapshot === lastSaved.current) return;
        setBusy(true);
        setStatus('Saving draft…');
        setError('');
        try {
          const saved = await api.save('new', 'Unfinished draft', data, row.current?.version || 0);
          if (revision === generation.current && current.current.records.scope === capturedScope) {
            row.current = saved;
            lastSaved.current = snapshot;
            setStatus(
              `Draft saved ${new Date(saved.updated_at.endsWith('Z') ? saved.updated_at : `${saved.updated_at}Z`).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`
            );
          }
        } catch (reason) {
          if (revision === generation.current) {
            stopped.current = true;
            setError(workspaceError(reason, 'Draft could not be saved. Keep this page open and retry.'));
            setStatus('Draft not saved');
          }
          throw reason;
        } finally {
          if (revision === generation.current) setBusy(false);
        }
      });
    chain.current = run;
    return run;
  }, []);

  useEffect(() => {
    // Once a draft exists, clearing the form is an edit too. Initial pristine
    // forms still create nothing. Include rows so an in-flight first save can
    // finish before a later blank edit is persisted with its returned version.
    if (!enabled || (!dirty && !row.current) || !ready || stopped.current || !records.identity) return;
    const timer = window.setTimeout(() => void persist(current.current.value).catch(() => undefined), 800);
    return () => window.clearTimeout(timer);
  }, [enabled, dirty, ready, serialized, records.identity, records.rows, persist]);

  useEffect(
    () => () => {
      stopped.current = true;
      ++generation.current;
    },
    []
  );

  const clear = useCallback(async (): Promise<boolean> => {
    // Stop queued writes before waiting for the in-flight save, then delete its
    // returned version. A late save cannot recreate a completed draft.
    const revision = generation.current;
    const api = current.current.records;
    stopped.current = true;
    await chain.current.catch(() => undefined);
    if (
      revision !== generation.current ||
      api.scope !== current.current.records.scope ||
      api.identity !== workspaceIdentity()
    )
      return false;
    const saved = row.current;
    try {
      if (saved) await api.remove(saved);
      if (revision !== generation.current) return false;
      row.current = null;
      lastSaved.current = '';
      setCandidate(null);
      setStatus('');
      return true;
    } catch (reason) {
      setError(workspaceError(reason, 'Saved draft could not be removed. Reload it before retrying.'));
      return false;
    }
  }, []);

  return {
    candidate,
    ready,
    busy,
    status,
    enabled,
    available: records.rows.some(item => item.key === 'new'),
    loading: enabled && (records.loading || !settled),
    error: enabled ? error || records.error : '',
    blocked: enabled && !!candidate,
    canResume: !!candidate && valid(candidate.data),
    resume: () => {
      if (!candidate || !valid(candidate.data)) return;
      restore(candidate.data);
      lastSaved.current = JSON.stringify(candidate.data);
      setCandidate(null);
      setReady(true);
      setError('');
      stopped.current = false;
      setStatus('Saved draft restored. Review it before creating the record.');
    },
    discard: async () => {
      setBusy(true);
      const removed = await clear();
      if (removed) {
        stopped.current = false;
        setReady(true);
        setError('');
        setStatus('Previous draft removed.');
      }
      setBusy(false);
    },
    retry: async () => {
      // Re-read before any retry. An ambiguous network response may already
      // have saved, and a 409 must never blindly overwrite another tab.
      const revision = generation.current;
      stopped.current = true;
      setBusy(true);
      await chain.current.catch(() => undefined);
      if (revision !== generation.current) return;
      try {
        const rows = await records.reload();
        if (
          revision !== generation.current ||
          records.scope !== current.current.records.scope ||
          records.identity !== workspaceIdentity()
        )
          return;
        const saved = rows.find(item => item.key === 'new');
        row.current = saved || null;
        setError('');
        if (saved) {
          setCandidate(saved);
          setReady(false);
        } else {
          stopped.current = false;
          setReady(true);
          await persist(current.current.value);
        }
      } catch (reason) {
        setError(workspaceError(reason, 'Draft could not be loaded. Keep this page open and retry.'));
      } finally {
        setBusy(false);
      }
    },
    saveNow: () => (ready && !stopped.current ? persist(current.current.value) : Promise.resolve()),
    clear,
  };
}

export type ResumableDraft<T> = ReturnType<typeof useResumableDraft<T>>;
