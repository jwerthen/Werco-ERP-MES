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
  const [layout, setLayout] = useState<TableLayout>(() => normalizeLayout(undefined, columns, defaultSort));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const loaded = useRef('');
  const edited = useRef(false);
  const identity = `${records.scope}:${table}`;
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
    if (busy) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await action();
    } catch (reason) {
      setError(workspaceError(reason, 'Could not save this view. Retry when connected.'));
    } finally {
      setBusy(false);
    }
  };
  return {
    columns,
    layout: normalized,
    change,
    displayColumns,
    busy,
    error: error || records.error,
    loading: records.loading,
    enabled: !!records.identity,
    message,
    views: records.rows.filter(row => row.data?.table === table && row.key !== currentKey),
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
      const row = records.rows.find(item => item.key === key);
      if (!row || row.data?.table !== table) return;
      change(normalizeLayout(row.data.layout, columns, defaultSort));
      applyFilters(row.data.filters || {});
      setMessage(`Applied “${row.name}”.`);
    },
    saveView: (name: string) =>
      run(async () => {
        if (
          records.rows.some(
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
        await records.save(`${table}-${key}`, name, { table, layout: normalized, filters }, 0);
        setMessage('View saved to your account.');
      }),
    updateView: (key: string) =>
      run(async () => {
        const row = records.rows.find(item => item.key === key && item.data?.table === table);
        if (row) {
          await records.save(row.key, row.name, { table, layout: normalized, filters }, row.version);
          setMessage('Saved view updated.');
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
        setMessage('Layout saved for your next visit.');
      }),
    remove: (key: string) =>
      run(async () => {
        const row = records.rows.find(item => item.key === key);
        if (row) {
          await records.remove(row);
          setMessage('Saved view removed.');
        }
      }),
    reload: () =>
      run(async () => {
        await records.reload();
        setMessage('Saved views reloaded. Your current layout is unchanged.');
      }),
    reset: () => change(normalizeLayout(undefined, columns, defaultSort)),
  };
}

export type TableWorkspace<T> = ReturnType<typeof useTableWorkspace<T>>;
