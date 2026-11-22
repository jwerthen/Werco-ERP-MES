import { useCallback, useEffect, useRef, useState } from 'react';
import type { MaintenanceLogEntry } from '../../types/maintenance';
import { toDisplayString } from '../../utils/apiError';

export interface MaintenanceLogsState {
  logs: MaintenanceLogEntry[];
  loading: boolean;
  /** Display-safe text when the last read failed, else null. */
  error: string | null;
  /** Re-read the same log (after an entry is added, or from Retry). Ignored once `key` has moved on. */
  reload: () => void;
}

/**
 * Reads a list of maintenance log entries for whatever `key` names (`null` = nothing open, nothing
 * read). A new key clears the list before its read, so one target's entries never show under
 * another's; a reload of the same key keeps the list on screen while it re-reads. Only the latest
 * read's answer is applied, so a slow answer for a dialog since closed or re-pointed is dropped.
 * The loader is tied to its key: a `reload` handed out for an earlier key (an add that finishes
 * after the dialog moved to another record) is ignored rather than re-reading the new key under
 * the old one. The list is shown as the server orders it (newest first).
 */
export function useMaintenanceLogs(
  key: string | null,
  load: () => Promise<MaintenanceLogEntry[]>
): MaintenanceLogsState {
  const [logs, setLogs] = useState<MaintenanceLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  // The current key and the loader rendered with it, always together.
  const latest = useRef({ key, load });
  const loadedKey = useRef<string | null>(null);

  useEffect(() => {
    latest.current = { key, load };
  });

  const read = useCallback((target: string | null) => {
    // Only the current key may be read: a read asked for any other key is stale.
    if (target !== latest.current.key) return;
    const loader = latest.current.load;
    const current = ++seq.current;
    if (target !== loadedKey.current) setLogs([]);
    loadedKey.current = target;
    setError(null);
    if (target === null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    Promise.resolve()
      .then(() => loader())
      .then(rows => {
        if (current === seq.current) setLogs(Array.isArray(rows) ? rows : []);
      })
      .catch((err: any) => {
        if (current !== seq.current) return;
        setError(toDisplayString(err?.response?.data?.detail) || 'The maintenance log could not be loaded.');
      })
      .finally(() => {
        if (current === seq.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    read(key);
  }, [key, read]);

  const reload = useCallback(() => read(key), [key, read]);
  return { logs, loading, error, reload };
}
