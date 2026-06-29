import React from 'react';
import { statusColor, UNKNOWN_STATUS_CLASS } from '../../utils/statusColors';

interface StatusBadgeProps {
  status: string;
  colorMap?: Record<string, string>;
  className?: string;
}

export function StatusBadge({ status, colorMap, className = '' }: StatusBadgeProps) {
  // Default coloring comes from the central statusColors source of truth so a
  // given status looks identical across every page. An optional `colorMap`
  // override still wins for genuinely page-specific labels.
  const colorClass = colorMap ? colorMap[status] || UNKNOWN_STATUS_CLASS : statusColor(status);

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium capitalize ${colorClass} ${className}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}
