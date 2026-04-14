import React from 'react';

interface StatusBadgeProps {
  status: string;
  colorMap?: Record<string, string>;
  className?: string;
}

const defaultColors: Record<string, string> = {
  active: 'bg-green-500/20 text-green-300',
  draft: 'bg-yellow-500/20 text-yellow-300',
  released: 'bg-green-500/20 text-green-300',
  obsolete: 'bg-slate-700 text-slate-400',
  pending_approval: 'bg-amber-500/20 text-amber-300',
};

export function StatusBadge({ status, colorMap, className = '' }: StatusBadgeProps) {
  const colors = colorMap || defaultColors;
  const colorClass = colors[status] || 'bg-slate-700 text-slate-300';

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium capitalize ${colorClass} ${className}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}
