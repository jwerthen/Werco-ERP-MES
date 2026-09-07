import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { WorkspaceKind, WorkspaceRecord } from '../types/workspace';

export function workspaceIdentity(): string {
  try {
    const user = JSON.parse(sessionStorage.getItem('user') || 'null');
    return user && typeof user.id === 'number' && typeof user.company_id === 'number'
      ? `${user.company_id}:${user.id}`
      : '';
  } catch {
    return '';
  }
}

export function workspaceError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : fallback;
}

export function useWorkspaceRecords<T>(namespace: string, kind: WorkspaceKind) {
  const [, render] = useState(0);
  const identity = workspaceIdentity();
  const scope = `${identity}:${namespace}:${kind}`;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const request = useRef(0);
  const [state, setState] = useState<{ scope: string; rows: WorkspaceRecord<T>[]; loading: boolean; error: string }>({
    scope,
    rows: [],
    loading: !!identity,
    error: '',
  });
  useEffect(() => {
    const changed = () => render(n => n + 1);
    window.addEventListener('werco:auth-token-changed', changed);
    window.addEventListener('storage', changed);
    return () => {
      window.removeEventListener('werco:auth-token-changed', changed);
      window.removeEventListener('storage', changed);
    };
  }, []);
  const reload = useCallback(async () => {
    const seq = ++request.current;
    if (!identity) {
      setState({ scope, rows: [], loading: false, error: '' });
      return [];
    }
    setState(previous => ({ scope, rows: previous.scope === scope ? previous.rows : [], loading: true, error: '' }));
    try {
      const rows = await api.listWorkspaceRecords<T>(namespace, kind);
      if (request.current === seq && currentScope.current === scope && identity === workspaceIdentity())
        setState({ scope, rows: Array.isArray(rows) ? rows : [], loading: false, error: '' });
      return rows || [];
    } catch (error) {
      if (request.current === seq && currentScope.current === scope && identity === workspaceIdentity())
        setState(previous => ({
          ...previous,
          loading: false,
          error: workspaceError(error, 'Saved work could not be loaded. Retry to reconnect.'),
        }));
      throw error;
    }
  }, [identity, scope, namespace, kind]);
  useEffect(() => {
    void reload().catch(() => undefined);
    return () => {
      ++request.current;
    };
  }, [reload]);
  const save = useCallback(
    async (key: string, name: string, data: T, version: number) => {
      if (!identity || identity !== workspaceIdentity()) throw new Error('Your session changed. Reload this page.');
      const row = await api.saveWorkspaceRecord(namespace, key, { kind, name, data, version });
      if (currentScope.current === scope && identity === workspaceIdentity()) {
        ++request.current;
        setState(previous =>
          previous.rows.some(item => item.key === row.key && item.version > row.version)
            ? previous
            : { ...previous, loading: false, rows: [...previous.rows.filter(item => item.key !== row.key), row] }
        );
      }
      return row;
    },
    [identity, scope, namespace, kind]
  );
  const remove = useCallback(
    async (row: WorkspaceRecord<T>) => {
      if (!identity || identity !== workspaceIdentity()) throw new Error('Your session changed. Reload this page.');
      await api.deleteWorkspaceRecord(namespace, row.key, kind, row.version);
      if (currentScope.current === scope && identity === workspaceIdentity()) {
        ++request.current;
        setState(previous => ({
          ...previous,
          loading: false,
          rows: previous.rows.filter(item => item.key !== row.key),
        }));
      }
    },
    [identity, scope, namespace, kind]
  );
  return {
    identity,
    scope,
    rows: state.scope === scope ? state.rows : [],
    loading: state.scope === scope ? state.loading : !!identity,
    error: state.scope === scope ? state.error : '',
    reload,
    save,
    remove,
  };
}
