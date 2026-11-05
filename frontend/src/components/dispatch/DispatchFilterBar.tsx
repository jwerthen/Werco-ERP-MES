/**
 * Dispatch Board search + filter chips.
 *
 * Every value lives in the URL (`q`, `late`, `running`, `held`, `machine`), owned
 * by the page shell, so a filtered board survives a reload and can be shared.
 * This component is presentational: it renders the current values and reports
 * changes. The search box writes `q` on every keystroke (replace-navigation);
 * the shell debounces what the views filter by.
 */

import React from 'react';
import { MagnifyingGlassIcon } from '@heroicons/react/24/outline';
import { Button } from '../ui';

export interface DispatchMachineOption {
  id: number;
  code: string;
  name: string;
  /** false = deactivated (still listed while work sits there). */
  isActive: boolean;
}

export type DispatchChipKey = 'late' | 'running' | 'held';

export interface DispatchFilterBarProps {
  query: string;
  onQueryChange: (value: string) => void;
  chips: Record<DispatchChipKey, boolean>;
  onToggleChip: (key: DispatchChipKey) => void;
  machine: number | null;
  machineOptions: DispatchMachineOption[];
  onMachineChange: (id: number | null) => void;
  onClear: () => void;
}

const CHIPS: Array<{ key: DispatchChipKey; label: string; title: string }> = [
  { key: 'late', label: 'Late', title: 'Past its promise date (must ship by, else due date)' },
  { key: 'running', label: 'Running', title: 'Someone is clocked in on it right now' },
  { key: 'held', label: 'Held', title: 'The work order, or one of its operations, is on hold' },
];

export default function DispatchFilterBar({
  query,
  onQueryChange,
  chips,
  onToggleChip,
  machine,
  machineOptions,
  onMachineChange,
  onClear,
}: DispatchFilterBarProps) {
  const anyActive = !!query.trim() || chips.late || chips.running || chips.held || machine != null;
  const machineKnown = machine == null || machineOptions.some(option => option.id === machine);
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="dispatch-filter-bar">
      <div className="relative min-w-[14rem] flex-1 sm:max-w-sm">
        <label htmlFor="dispatch-search" className="sr-only">
          Search work orders
        </label>
        <MagnifyingGlassIcon
          className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"
          aria-hidden="true"
        />
        <input
          id="dispatch-search"
          type="search"
          value={query}
          onChange={event => onQueryChange(event.target.value)}
          placeholder="Search WO #, part, customer, unit"
          className="w-full rounded-sm border border-fd-line bg-fd-sunken py-1.5 pl-8 pr-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-blue-400 focus:outline-none"
        />
      </div>
      <div role="group" aria-label="Filters" className="flex flex-wrap items-center gap-1.5">
        {CHIPS.map(chip => {
          const pressed = chips[chip.key];
          return (
            <button
              key={chip.key}
              type="button"
              aria-pressed={pressed}
              title={chip.title}
              onClick={() => onToggleChip(chip.key)}
              className={`rounded-sm border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider transition-colors ${
                pressed
                  ? 'border-blue-400/70 bg-blue-500/15 text-blue-200'
                  : 'border-fd-line text-slate-400 hover:border-slate-500 hover:text-slate-200'
              }`}
            >
              {chip.label}
            </button>
          );
        })}
        <label htmlFor="dispatch-machine-filter" className="sr-only">
          Machine
        </label>
        <select
          id="dispatch-machine-filter"
          value={machine == null ? '' : String(machine)}
          onChange={event => {
            const id = Number(event.target.value);
            onMachineChange(event.target.value && Number.isFinite(id) ? id : null);
          }}
          className={`rounded-sm border bg-transparent px-1.5 py-1 font-mono text-[11px] uppercase tracking-wider ${
            machine != null ? 'border-blue-400/70 text-blue-200' : 'border-fd-line text-slate-400'
          }`}
        >
          <option value="">All machines</option>
          {!machineKnown && machine != null && <option value={String(machine)}>Machine #{machine}</option>}
          {machineOptions.map(option => (
            <option key={option.id} value={String(option.id)}>
              {option.code}
              {option.name && option.name !== option.code ? ` · ${option.name}` : ''}
              {option.isActive ? '' : ' (deactivated)'}
            </option>
          ))}
        </select>
        {anyActive && (
          <Button variant="ghost" size="sm" onClick={onClear}>
            Clear filters
          </Button>
        )}
      </div>
    </div>
  );
}
