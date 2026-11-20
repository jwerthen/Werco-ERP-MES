import { useCallback, useEffect, useRef, useState } from 'react';
import type { MaintenanceLogEntry } from '../../types/maintenance';
import { toDisplayString } from '../../utils/apiError';

export interface MaintenanceLogsState {
  logs: MaintenanceLogEntry[];
  loading: boolean;
  /** Display-safe text when the last read failed, else null. */
  error: string | null;
  /** Re-read the same log (after an entry is added, or from Retry). */
  reload: () => void;
}

/**
 * Reads a list of maintenance log entries for whatever `key` names (`null` = nothing open, nothing
 * read). A new key clears the list before its read, so one target's entries never show under
 * another's; a reload of the same key keeps the list on screen while it re-reads. Only the latest
 * read's answer is applied, so a slow answer for a dialog since closed or re-pointed is dropped.
 * The list is shown as the server orders it (newest first).
 */
export function useMaintenanceLogs(
  key: string | null,
  load: () => Promise<MaintenanceLogEntry[]>
): MaintenanceLogsState {
  const [logs, setLogs] = useState<MaintenanceLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const loadRef = useRef(load);
  const loadedKey = useRef<string | null>(null);

  useEffect(() => {
    loadRef.current = load;
  });

  const read = useCallback((target: string | null) => {
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
      .then(() => loadRef.current())
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
