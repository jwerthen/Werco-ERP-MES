import React from 'react';

interface StatusBadgeProps {
  status: string;
  colorMap?: Record<string, string>;
  className?: string;
}

const defaultColors: Record<string, string> = {
  active: 'bg-green-100 text-green-800',
  draft: 'bg-yellow-100 text-yellow-800',
  released: 'bg-green-100 text-green-800',
  obsolete: 'bg-gray-100 text-gray-600',
  pending_approval: 'bg-amber-100 text-amber-800',
};

export function StatusBadge({ status, colorMap, className = '' }: StatusBadgeProps) {
  const colors = colorMap || defaultColors;
  const colorClass = colors[status] || 'bg-gray-100 text-gray-800';

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium capitalize ${colorClass} ${className}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}
