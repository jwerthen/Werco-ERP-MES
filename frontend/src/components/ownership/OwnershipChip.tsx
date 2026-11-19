import React from 'react';
import type { OwnershipInfo } from '../../types/ownership';
import { ownershipLabel, ownershipOf } from '../../utils/ownership';

/**
 * "Customer: {owner}" / "Supplier: {owner}" on a stock row or tool that is not the
 * company's property (AS9100D 8.5.3). Company ownership renders NOTHING: it is the
 * default, and a chip on every row would bury the few that matter.
 */
export function OwnershipChip({
  record,
  className = '',
}: {
  record: OwnershipInfo | null | undefined;
  className?: string;
}) {
  const label = ownershipLabel(record);
  if (!label) return null;
  const tone =
    ownershipOf(record) === 'customer'
      ? 'border-fd-blue/40 bg-fd-blue/10 text-fd-link'
      : 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber';
  return (
    <span
      className={`inline-flex max-w-full items-center rounded-sm border px-1.5 py-0.5 text-xs font-medium ${tone} ${className}`}
      title={label}
    >
      <span className="truncate">{label}</span>
    </span>
  );
}

export default OwnershipChip;
