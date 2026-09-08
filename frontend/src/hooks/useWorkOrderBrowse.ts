import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { WorkOrderSummary } from '../types';
import { WorkOrderBrowseParams, WorkOrderBrowseResponse } from '../types/workOrderBrowse';
import { workspaceIdentity } from './useWorkspaceRecords';

const PAGE_SIZE = 50;
const empty: WorkOrderBrowseResponse = {
  items: [],
  total: 0,
  skip: 0,
  limit: PAGE_SIZE,
  has_next: false,
  stats: { overdue: 0, in_progress: 0, due_today: 0 },
  customers: [],
  customers_truncated: false,
  group_totals: {},
};

/** Fetch only requested windows. A scope/generation guard prevents stale filters or sessions replacing rows. */
export function useWorkOrderBrowse(params: WorkOrderBrowseParams) {
  const identity = workspaceIdentity();
  const scope = JSON.stringify([identity, params]);
  const current = useRef(scope);
  current.current = scope;
  const sequence = useRef(0);
  const facets = useRef<{ identity: string; customers: string[] }>({ identity, customers: [] });
  const busy = useRef(false);
  const [state, setState] = useState({ scope, response: empty, loading: true, error: false, nextSkip: 0 });
  const latest = useRef(state);
  latest.current = state;
  const query = useRef(params);
  query.current = params;
  const fetchPage = useCallback(
    async (skip: number, append = false, windows = 1) => {
      const seq = ++sequence.current;
      busy.current = true;
      setState(previous => ({
        scope,
        response: previous.scope === scope ? previous.response : empty,
        loading: true,
        error: false,
        nextSkip: previous.scope === scope ? previous.nextSkip : 0,
      }));
      try {
        const selectedQuery = query.current;
        const responses: WorkOrderBrowseResponse[] = [];
        for (let start = 0; start < windows; start += 4) {
          if (sequence.current !== seq || current.current !== scope) return;
          responses.push(
            ...(await Promise.all(
              Array.from({ length: Math.min(4, windows - start) }, (_, index) =>
                api.browseWorkOrders({ ...selectedQuery, skip: skip + (start + index) * PAGE_SIZE, limit: PAGE_SIZE })
              )
            ))
          );
        }
        const first = responses[0];
        const last = responses[responses.length - 1];
        const response = {
          ...first,
          items: responses.flatMap(row => row.items),
          has_next: last.has_next,
          group_totals: Object.assign({}, ...responses.map(row => row.group_totals)),
        };
        if (sequence.current !== seq || current.current !== scope || identity !== workspaceIdentity()) return;
        facets.current = { identity, customers: response.customers };
        setState(previous => {
          const keep = append && previous.scope === scope && response.skip === skip;
          const items = keep ? [...previous.response.items, ...response.items] : response.items;
          const unique = Array.from(new Map(items.map(row => [row.id, row])).values());
          return {
            scope,
            loading: false,
            error: false,
            nextSkip: last.skip + last.items.length,
            response: {
              ...response,
              items: unique,
              skip: keep ? previous.response.skip : response.skip,
              group_totals: keep
                ? { ...previous.response.group_totals, ...response.group_totals }
                : response.group_totals,
            },
          };
        });
      } catch {
        if (sequence.current === seq && current.current === scope)
          setState(previous => ({ ...previous, loading: false, error: true }));
      } finally {
        if (sequence.current === seq) busy.current = false;
      }
    },
    [scope, identity]
  );
  const reload = useCallback(() => {
    // Refetch only the windows already requested, replacing every loaded row so
    // removed/status-changed jobs do not survive in earlier mobile pages.
    const existing = latest.current.scope === scope ? latest.current.response : empty;
    return fetchPage(existing.skip, false, Math.max(1, Math.ceil(existing.items.length / PAGE_SIZE)));
  }, [scope, fetchPage]);
  useEffect(
    () => () => {
      ++sequence.current;
      busy.current = false;
    },
    [scope]
  );
  const visible = state.scope === scope ? state : { scope, response: empty, loading: true, error: false, nextSkip: 0 };
  const setItems = useCallback(
    (change: React.SetStateAction<WorkOrderSummary[]>) => {
      setState(previous =>
        previous.scope !== scope
          ? previous
          : {
              ...previous,
              response: {
                ...previous.response,
                items: typeof change === 'function' ? change(previous.response.items) : change,
              },
            }
      );
    },
    [scope]
  );
  return {
    scope,
    ...visible.response,
    customers:
      visible.loading && facets.current.identity === identity ? facets.current.customers : visible.response.customers,
    workOrders: visible.response.items,
    setWorkOrders: setItems,
    loading: visible.loading && visible.response.items.length === 0,
    refreshing: visible.loading,
    loadError: visible.error,
    loadWorkOrders: reload,
    page: Math.floor(visible.response.skip / PAGE_SIZE) + 1,
    setPage: (page: number) => {
      if (!busy.current) void fetchPage((page - 1) * PAGE_SIZE);
    },
    loadMore: () => {
      if (!busy.current && visible.response.has_next) void fetchPage(visible.nextSkip, true);
    },
  };
}
