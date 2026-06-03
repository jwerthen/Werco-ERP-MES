import React, { useState } from 'react';
import { ChevronDownIcon, ChevronRightIcon } from '@heroicons/react/24/outline';

interface WhyThisSuggestionProps {
  rationale?: string;
  evidence?: Array<Record<string, unknown>>;
  impact?: Record<string, unknown>;
}

const formatValue = (value: unknown) => {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

export function WhyThisSuggestion({ rationale, evidence = [], impact = {} }: WhyThisSuggestionProps) {
  const [open, setOpen] = useState(false);
  const hasDetails = Boolean(rationale) || evidence.length > 0 || Object.keys(impact).length > 0;
  if (!hasDetails) return null;

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="inline-flex items-center gap-1 text-xs font-medium text-cyan-200 hover:text-cyan-100"
      >
        {open ? <ChevronDownIcon className="h-4 w-4" /> : <ChevronRightIcon className="h-4 w-4" />}
        Why this suggestion
      </button>
      {open && (
        <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-300">
          {rationale && <p>{rationale}</p>}
          {evidence.length > 0 && (
            <div className="mt-2 space-y-1">
              {evidence.slice(0, 3).map((item, index) => (
                <div key={index} className="rounded border border-slate-800 bg-slate-900/70 px-2 py-1">
                  {Object.entries(item).map(([key, value]) => {
                    const formatted = formatValue(value);
                    if (!formatted) return null;
                    return (
                      <span key={key} className="mr-3">
                        <span className="text-slate-500">{key}:</span> {formatted}
                      </span>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
          {Object.keys(impact).length > 0 && (
            <div className="mt-2 text-slate-400">
              {Object.entries(impact).map(([key, value]) => {
                const formatted = formatValue(value);
                if (!formatted) return null;
                return (
                  <div key={key}>
                    <span className="text-slate-500">{key}:</span> {formatted}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
