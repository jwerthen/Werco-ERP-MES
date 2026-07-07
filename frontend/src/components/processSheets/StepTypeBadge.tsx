import React from 'react';

/**
 * Instrument-panel chips for the typed step kinds. TYPES, not statuses — so
 * deliberately outside the canonical statusColors map. Shared by the Process
 * Sheets library page and the WO detail "Process steps" records panel.
 */
const TYPE_BADGE: Record<string, string> = {
  measurement: 'bg-blue-500/20 text-blue-300',
  checkbox: 'bg-emerald-500/20 text-emerald-300',
  list: 'bg-cyan-500/20 text-cyan-300',
  value: 'bg-indigo-500/20 text-indigo-300',
  photo: 'bg-purple-500/20 text-purple-300',
  file: 'bg-slate-500/20 text-slate-300',
  instruction: 'bg-amber-500/20 text-amber-300',
};

export default function StepTypeBadge({ type }: { type: string }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium capitalize ${
        TYPE_BADGE[type] || 'bg-slate-800/50 text-slate-400'
      }`}
    >
      {type}
    </span>
  );
}
