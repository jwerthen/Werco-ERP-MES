import { useEffect, useRef, useState } from 'react';
import { DataTableColumn, SortDir } from '../components/ui/DataTable';
import { useWorkspaceRecords, workspaceError } from './useWorkspaceRecords';

export interface TableLayout {
  order: string[];
  hidden: string[];
  dense: boolean;
  sort: { key: string; dir: SortDir } | null;
}
export interface TableView {
  table: string;
  layout: TableLayout;
  filters: Record<string, string>;
}

export function normalizeLayout<T>(
  value: Partial<TableLayout> | undefined,
  columns: DataTableColumn<T>[],
  defaultSort: TableLayout['sort']
): TableLayout {
  const keys = columns.map(column => column.key);
  const required = new Set([keys[0], ...keys.filter(key => /action/i.test(key))]);
  const order = Array.isArray(value?.order) ? value.order.filter(key => keys.includes(key)) : [];
  const hidden = Array.isArray(value?.hidden)
    ? value.hidden.filter(key => keys.includes(key) && !required.has(key))
    : [];
  const sort =
    value?.sort === null
      ? null
      : value?.sort &&
          columns.some(column => column.key === value.sort?.key && column.sortable) &&
          ['asc', 'desc'].includes(value.sort.dir)
        ? value.sort
        : defaultSort;
  return { order: Array.from(new Set([...order, ...keys])), hidden, dense: value?.dense === true, sort };
}

export function useTableWorkspace<T>(
  namespace: string,
  table: string,
  columns: DataTableColumn<T>[],
  filters: Record<string, string>,
  applyFilters: (filters: Record<string, string>) => void,
  defaultSort: TableLayout['sort'] = null
) {
  const records = useWorkspaceRecords<TableView>(namespace, 'view');
  const team = useWorkspaceRecords<TableView>(namespace, 'view', true);
  const allViews = [
    ...records.rows.map(row => ({ ...row, visibility: 'private' as const })),
    ...team.rows.map(row => ({ ...row, key: `team:${row.key}`, visibility: 'team' as const })),
  ];
  const findView = (key: string) => allViews.find(row => row.key === key && row.data.table === table);
  const canEditView = (key: string) => findView(key)?.visibility === 'private' || team.canManage;
  const [layout, setLayout] = useState<TableLayout>(() => normalizeLayout(undefined, columns, defaultSort));
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const loaded = useRef('');
  const edited = useRef(false);
  const identity = `${records.scope}:${table}`;
  const activeIdentity = useRef(identity);
  activeIdentity.current = identity;
  const safeMessage = (value: string) => {
    if (activeIdentity.current === identity) setMessage(value);
  };
  useEffect(() => {
    busyRef.current = false;
    setBusy(false);
  }, [identity]);
  const currentKey = `${table}-layout`;
  const current = records.rows.find(row => row.key === currentKey);
  const values = useRef({ columns, defaultSort });
  values.current = { columns, defaultSort };
  useEffect(() => {
    if (loaded.current && !loaded.current.startsWith(`${identity}:`)) {
      setLayout(normalizeLayout(undefined, values.current.columns, values.current.defaultSort));
      edited.current = false;
      loaded.current = '';
      setMessage('');
      setError('');
    }
    if (!records.loading && !records.error && loaded.current !== `${identity}:loaded`) {
      if (!edited.current)
        setLayout(normalizeLayout(current?.data.layout, values.current.columns, values.current.defaultSort));
      loaded.current = `${identity}:loaded`;
    }
  }, [identity, records.loading, records.error, current]);
  const change = (next: TableLayout) => {
    edited.current = true;
    setLayout(next);
    setMessage('');
  };
  const normalized = normalizeLayout(layout, columns, defaultSort);
  const displayColumns = (source: DataTableColumn<T>[]) =>
    normalized.order
      .filter(key => !normalized.hidden.includes(key))
      .map(key => source.find(column => column.key === key))
      .filter((column): column is DataTableColumn<T> => !!column);
  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await action();
    } catch (reason) {
      if (activeIdentity.current === identity)
        setError(workspaceError(reason, 'Could not save this view. Retry when connected.'));
    } finally {
      if (activeIdentity.current === identity) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };
  return {
    columns,
    layout: normalized,
    change,
    displayColumns,
    busy,
    error: error || records.error || team.error,
    loading: records.loading || team.loading,
    canManageTeam: team.canManage,
    canEditView,
    enabled: !!records.identity,
    message,
    views: allViews.filter(row => row.data?.table === table && row.key !== currentKey),
    tableProps: {
      dense: normalized.dense,
      sort: normalized.sort,
      sortColumns: columns,
      onSortChange: (sort: TableLayout['sort']) => change({ ...normalized, sort }),
    },
    sortRows: (rows: T[]) => {
      const sort = normalized.sort;
      const column = sort && columns.find(item => item.key === sort.key);
      if (!sort || !column?.accessor) return rows;
      return [...rows].sort((a, b) => {
        const left = column.accessor!(a),
          right = column.accessor!(b);
        const comparison =
          left === right
            ? 0
            : left === null
              ? -1
              : right === null
                ? 1
                : typeof left === 'number' && typeof right === 'number'
                  ? left - right
                  : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: 'base' });
        return sort.dir === 'asc' ? comparison : -comparison;
      });
    },
    apply: (key: string) => {
      const row = findView(key);
      if (!row || row.data?.table !== table) return;
      change(normalizeLayout(row.data.layout, columns, defaultSort));
      applyFilters(row.data.filters || {});
      safeMessage(`Applied “${row.name}”.`);
    },
    saveView: (name: string, visibility: 'private' | 'team' = 'private', savedFilters = filters) =>
      run(async () => {
        if (visibility === 'team' && !team.canManage) throw new Error('Only managers can change team views.');
        const target = visibility === 'team' ? team : records;
        if (
          target.rows.some(
            row =>
              row.data?.table === table &&
              row.key !== currentKey &&
              row.name.toLocaleLowerCase() === name.toLocaleLowerCase()
          )
        ) {
          setError('A view with this name already exists. Apply and update it, or choose a different name.');
          return;
        }
        const key = crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        await target.save(`${table}-${key}`, name, { table, layout: normalized, filters: savedFilters }, 0);
        safeMessage(visibility === 'team' ? 'Team view saved.' : 'View saved to your account.');
      }),
    updateView: (key: string) =>
      run(async () => {
        const row = findView(key);
        if (row) {
          if (!canEditView(key)) throw new Error('Only managers can change team views.');
          await (row.visibility === 'team' ? team : records).save(
            row.key.replace(/^team:/, ''),
            row.name,
            { table, layout: normalized, filters },
            row.version
          );
          safeMessage('Saved view updated.');
        }
      }),
    saveLayout: () =>
      run(async () => {
        await records.save(
          currentKey,
          'Table layout',
          { table, layout: normalized, filters: {} },
          current?.version || 0
        );
        safeMessage('Layout saved for your next visit.');
      }),
    remove: (key: string) =>
      run(async () => {
        const row = findView(key);
        if (row) {
          if (!canEditView(key)) throw new Error('Only managers can change team views.');
          await (row.visibility === 'team' ? team : records).remove({ ...row, key: row.key.replace(/^team:/, '') });
          safeMessage('Saved view removed.');
        }
      }),
    reload: () =>
      run(async () => {
        await Promise.all([records.reload(), team.reload()]);
        safeMessage('Saved views reloaded. Your current layout is unchanged.');
      }),
    reset: () => change(normalizeLayout(undefined, columns, defaultSort)),
  };
}

export type TableWorkspace<T> = ReturnType<typeof useTableWorkspace<T>>;
