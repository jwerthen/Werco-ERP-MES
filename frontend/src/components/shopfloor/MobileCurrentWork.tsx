import React from 'react';
import { ActiveJob } from '../../types';
import { UnitBadge } from '../ui';
import { formatOperationLabel } from '../../utils/operationLabel';
import LaserNestOperatorPanel from '../laser/LaserNestOperatorPanel';

interface Props {
  jobs: ActiveJob[];
  selected: ActiveJob | null;
  onSelect: (job: ActiveJob) => void;
  onQueue: () => void;
  onReport: () => void;
  onAddOne: () => void;
  onDrawing: () => void;
  onNest: () => void;
  onInstructions: () => void;
  onHold: () => void;
  onCheckOut: () => void;
  onComplete: () => void;
  disabled: boolean;
  operationAvailable: boolean;
  elapsed: string;
}

/** The operator's own work is separate from the manager's ordered queue. */
export default function MobileCurrentWork({ jobs, selected, onSelect, onQueue, onReport, onAddOne, onDrawing,
  onNest, onInstructions, onHold, onCheckOut, onComplete, disabled, operationAvailable, elapsed }: Props) {
  if (!selected) {
    return (
      <section className="card space-y-4 text-center" aria-label="My current work">
        <h2 className="text-lg font-bold text-white">No active jobs</h2>
        <p className="text-sm text-slate-300">Choose a ready operation to start working.</p>
        <button type="button" onClick={onQueue} className="btn-primary min-h-12 w-full">Find ready work</button>
      </section>
    );
  }

  const complete = Number(selected.quantity_complete || 0);
  const target = Number(selected.quantity_ordered || 0);
  const remaining = Math.max(0, target - complete);
  return (
    <section aria-label="My current work" className="space-y-3">
      {jobs.length > 1 && (
        <div>
          <label htmlFor="mobile-active-job" className="mb-1 block text-sm font-semibold text-slate-300">Switch active job ({jobs.length})</label>
          <select id="mobile-active-job" className="input min-h-12 w-full" value={selected.time_entry_id}
            onChange={event => { const job = jobs.find(item => item.time_entry_id === Number(event.target.value)); if (job) onSelect(job); }}>
            {jobs.map(job => <option key={job.time_entry_id} value={job.time_entry_id}>
              {job.work_order_number} · {formatOperationLabel(job.operation_number)} — {job.operation_name}
            </option>)}
          </select>
        </div>
      )}
      <div className="rounded-sm border border-emerald-500/40 bg-emerald-500/10 p-4 space-y-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-300">Checked in · {elapsed}</p>
          <h2 className="mt-2 break-words text-xl font-bold text-white">{selected.work_order_number || 'Current job'}</h2>
          <UnitBadge unitNumber={selected.unit_number} className="mt-1" />
          <p className="mt-1 text-lg font-semibold text-white">{formatOperationLabel(selected.operation_number)} — {selected.operation_name || 'Operation'}</p>
          <p className="mt-1 text-sm text-slate-300">{selected.work_center_name}</p>
          <p className="mt-2 break-words text-sm text-slate-300">{selected.part_number} {selected.part_name && `· ${selected.part_name}`}</p>
        </div>
        <div className="flex items-end justify-between gap-3">
          <div><p className="text-sm text-slate-300">Completed</p><p className="text-3xl font-bold text-white">{complete}<span className="text-lg text-slate-400"> / {target}</span></p></div>
          <p className="pb-1 text-sm text-emerald-200">{remaining} remaining</p>
        </div>
        {remaining > 0 ? (
          <button type="button" onClick={onAddOne} disabled={disabled || !operationAvailable}
            className="btn-success min-h-14 w-full text-lg disabled:opacity-50">+1 Complete</button>
        ) : (
          <button type="button" onClick={onComplete} disabled={disabled || !operationAvailable || target <= 0}
            className="btn-success min-h-14 w-full disabled:opacity-50">Complete operation</button>
        )}
        {!operationAvailable && <p role="status" className="text-sm text-amber-300">Refresh to load this operation before recording work.</p>}
      </div>
      {selected.laser_nest && <LaserNestOperatorPanel nest={selected.laser_nest} allowPreview={false} />}
      <div className="grid grid-cols-2 gap-2">
        <button type="button" onClick={onInstructions} disabled={!operationAvailable} className="btn-secondary min-h-12 disabled:opacity-50">Instructions</button>
        {selected.laser_nest ? <button type="button" onClick={onNest} disabled={!operationAvailable} className="btn-secondary min-h-12 disabled:opacity-50">Nest PDF</button>
          : <button type="button" onClick={onQueue} className="btn-secondary min-h-12">Find next job</button>}
      </div>
      <div className="fixed inset-x-0 bottom-0 z-40 border-t border-slate-700 bg-fd-panel p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] shadow-xl" aria-label="Current job actions">
        <p className="mb-2 truncate text-xs text-slate-300">{selected.work_order_number} · {formatOperationLabel(selected.operation_number)} — {selected.operation_name}</p>
        <div className="grid grid-cols-4 gap-2">
          <button type="button" onClick={onReport} disabled={!operationAvailable} className="btn-primary min-h-12 px-1 text-xs disabled:opacity-50">Report quantity</button>
          <button type="button" onClick={onDrawing} disabled={!operationAvailable} className="btn-secondary min-h-12 px-1 text-xs disabled:opacity-50">Drawing</button>
          <button type="button" onClick={onHold} disabled={disabled || !operationAvailable} className="btn-secondary min-h-12 px-1 text-xs disabled:opacity-50">Hold</button>
          <button type="button" onClick={onCheckOut} disabled={disabled} className="btn-secondary min-h-12 px-1 text-xs disabled:opacity-50">Check Out</button>
        </div>
      </div>
    </section>
  );
}
