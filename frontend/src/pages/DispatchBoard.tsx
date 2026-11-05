/**
 * Dispatch Board — page shell (`/dispatch`, gated on `work_orders:edit`).
 *
 * Dispatch v2 is WORK-ORDER FIRST (owner decision 2026-11-04): the planner sets
 * one shop-wide order of work orders, every machine queue follows it, and within
 * a work order the planner orders that work order's operations. The board has
 * two views over the same plan, switched by the `view` URL param:
 *
 *  - `work-orders` (default) — `components/dispatch/DispatchWorkOrderView`: the
 *    plan itself, one compact lane per live work order, reordered by drag or by
 *    keyboard (Move up / down / to top, Set position…, Add / Remove).
 *  - `machines` — `components/dispatch/DispatchMachineView`: the original
 *    column-per-machine board, now banded by the plan.
 *
 * The run order is ADVISORY on both: it orders and labels work for the floor,
 * it never decides what may start, clock in or complete.
 *
 * The shell owns what must survive a reload — the view, the search (`q`) and the
 * filter chips (`late`, `running`, `held`, `machine`) — in URL params, and
 * debounces the search before either view filters by it. Each view owns its own
 * data, polling (every 10 s while the tab is visible, and on focus; only the
 * mounted view polls), writes and its aria-live status line, which announces the
 * user's own actions, conflicts and failures — never a background poll.
 */

import React, { useCallback, useMemo, useState } from 'react';
import { useQueuedSearchParams } from '../hooks/useQueuedSearchParams';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import DispatchMachineView from '../components/dispatch/DispatchMachineView';
import DispatchWorkOrderView from '../components/dispatch/DispatchWorkOrderView';
import DispatchFilterBar, {
  type DispatchChipKey,
  type DispatchMachineOption,
} from '../components/dispatch/DispatchFilterBar';
import type { DispatchFilters } from '../utils/dispatchPlan';

// The machine view's pure helpers keep their historical import path.
export {
  extractDispatchQueue,
  insertionIndexFromPointer,
  jobLabel,
  reorderRows,
} from '../components/dispatch/DispatchMachineView';

export type DispatchView = 'work-orders' | 'machines';

const VIEWS: Array<{ id: DispatchView; label: string }> = [
  { id: 'work-orders', label: 'Work orders' },
  { id: 'machines', label: 'Machines' },
];

const SEARCH_DEBOUNCE_MS = 250;

export default function DispatchBoard() {
  const [params, setParams] = useQueuedSearchParams();
  const view: DispatchView = params.get('view') === 'machines' ? 'machines' : 'work-orders';
  const query = params.get('q') ?? '';
  const debouncedQuery = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS);
  const machineParam = Number(params.get('machine'));
  const machine = params.get('machine') && Number.isFinite(machineParam) && machineParam > 0 ? machineParam : null;
  const chips: Record<DispatchChipKey, boolean> = {
    late: params.get('late') === '1',
    running: params.get('running') === '1',
    held: params.get('held') === '1',
  };
  const [machineOptions, setMachineOptions] = useState<DispatchMachineOption[]>([]);

  const filters: DispatchFilters = useMemo(
    () => ({ q: debouncedQuery, late: chips.late, running: chips.running, held: chips.held, machine }),
    // Primitive deps keep the object stable across re-renders (and across polls).
    [debouncedQuery, chips.late, chips.running, chips.held, machine]
  );

  /** Copy-and-set: an empty value deletes the param, so the default board keeps a clean URL. */
  const setParam = useCallback(
    (key: string, value: string | null, options?: { replace?: boolean }) =>
      setParams(previous => {
        const next = new URLSearchParams(previous);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      }, options),
    [setParams]
  );

  const clearFilters = useCallback(
    () =>
      setParams(previous => {
        const next = new URLSearchParams(previous);
        ['q', 'late', 'running', 'held', 'machine'].forEach(key => next.delete(key));
        return next;
      }),
    [setParams]
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-bold text-white">Dispatch Board</h1>
        <div role="group" aria-label="Board view" className="inline-flex rounded-sm border border-fd-line">
          {VIEWS.map(option => {
            const active = view === option.id;
            return (
              <button
                key={option.id}
                type="button"
                aria-pressed={active}
                onClick={() => setParam('view', option.id === 'work-orders' ? null : option.id)}
                className={`px-3 py-1.5 font-mono text-xs font-semibold uppercase tracking-wider transition-colors first:border-r first:border-fd-line ${
                  active ? 'bg-blue-500/15 text-blue-200' : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      <DispatchFilterBar
        query={query}
        onQueryChange={value => setParam('q', value, { replace: true })}
        chips={chips}
        onToggleChip={key => setParam(key, chips[key] ? null : '1')}
        machine={machine}
        machineOptions={machineOptions}
        onMachineChange={id => setParam('machine', id == null ? null : String(id))}
        onClear={clearFilters}
      />

      {view === 'machines' ? (
        <DispatchMachineView filters={filters} onMachineOptions={setMachineOptions} />
      ) : (
        <DispatchWorkOrderView filters={filters} onMachineOptions={setMachineOptions} />
      )}
    </div>
  );
}
