import React, { useId, useMemo, useState } from 'react';
import { CheckCircleIcon, ComputerDesktopIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline';

export interface KioskWorkstationOption {
  id: number;
  code: string;
  name: string;
}

interface KioskWorkstationPickerProps {
  workCenters: KioskWorkstationOption[];
  selectedId: number | null;
  loading: boolean;
  error: string | null;
  saving?: boolean;
  onSelect: (id: number) => void;
  onRetry: () => void;
  onCancel?: () => void;
}

export default function KioskWorkstationPicker({
  workCenters, selectedId, loading, error, saving = false, onSelect, onRetry, onCancel,
}: KioskWorkstationPickerProps) {
  const [search, setSearch] = useState('');
  const headingId = useId();
  const searchId = useId();
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return workCenters.filter((center) => `${center.code} ${center.name}`.toLowerCase().includes(query));
  }, [search, workCenters]);

  return (
    <section aria-labelledby={headingId} className="mx-auto w-full max-w-4xl px-5 py-8 sm:py-12">
      <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="mb-2 flex items-center gap-2 font-mono text-sm uppercase tracking-widest text-fd-blue">
            <ComputerDesktopIcon className="h-6 w-6" aria-hidden="true" /> Kiosk setup
          </p>
          <h1 id={headingId} className="text-3xl font-bold text-fd-ink sm:text-4xl">Choose workstation</h1>
          <p className="mt-3 text-lg text-fd-body">Tap where this kiosk will work. Your selection is remembered.</p>
        </div>
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={saving}
            className="min-h-14 rounded border border-fd-line bg-fd-panel px-6 text-lg font-semibold text-fd-body disabled:opacity-40">
            Cancel
          </button>
        )}
      </div>

      <label htmlFor={searchId} className="mb-2 block text-lg font-semibold text-fd-body">Find a workstation</label>
      <div className="relative mb-6">
        <MagnifyingGlassIcon className="pointer-events-none absolute left-4 top-4 h-6 w-6 text-fd-mute" aria-hidden="true" />
        <input id={searchId} type="search" value={search} disabled={saving}
          onChange={(event) => setSearch(event.target.value)} placeholder="Search by name or code"
          className="min-h-14 w-full rounded border border-fd-line-bright bg-fd-panel py-3 pl-12 pr-4 text-xl text-fd-ink focus:outline-none focus:ring-2 focus:ring-fd-blue" />
      </div>

      {error && (
        <div role="alert" className="mb-6 rounded border border-fd-red bg-fd-red/10 p-4 text-lg text-fd-ink">
          <p>{error}</p>
          <button type="button" onClick={onRetry} disabled={loading || saving}
            className="mt-3 min-h-12 rounded border border-fd-line-bright px-5 font-bold disabled:opacity-40">Try again</button>
        </div>
      )}
      {loading ? <p role="status" className="py-8 text-xl text-fd-body">Loading workstations…</p> : (
        <>
          {saving && <p role="status" className="mb-4 text-xl text-fd-blue">Changing workstation…</p>}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2" aria-busy={saving}>
            {filtered.map((center) => (
              <button key={center.id} type="button" onClick={() => onSelect(center.id)} disabled={saving}
                aria-pressed={center.id === selectedId}
                className={`flex min-h-28 items-center justify-between gap-4 rounded border-2 bg-fd-panel p-5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-fd-blue disabled:opacity-40 ${center.id === selectedId ? 'border-fd-blue' : 'border-fd-line hover:border-fd-blue'}`}>
                <span className="min-w-0">
                  <span className="block break-words font-mono text-2xl font-bold text-fd-ink">{center.code}</span>
                  <span className="mt-1 block break-words text-lg text-fd-body">{center.name}</span>
                </span>
                {center.id === selectedId && <span className="flex shrink-0 flex-col items-center gap-1 text-sm text-fd-blue"><CheckCircleIcon className="h-7 w-7" aria-hidden="true" />Current</span>}
              </button>
            ))}
          </div>
          {!error && filtered.length === 0 && <p role="status" className="py-8 text-xl text-fd-body">
            {workCenters.length ? 'No matching workstations. Try another name or code.' : 'No active workstations are available. Ask your supervisor to add one in Work Centers.'}
          </p>}
        </>
      )}
    </section>
  );
}
