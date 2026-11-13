import React from 'react';
import type { AslStatus } from '../../types/supplierApproval';
import { aslStatusClass, aslStatusLabel } from '../../utils/supplierApproval';

/** The vendor's Approved Supplier List status as a pill; "Not on the ASL" when unset. */
export function AslStatusPill({ status, className = '' }: { status?: AslStatus | string | null; className?: string }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium whitespace-nowrap ${aslStatusClass(
        status
      )} ${className}`}
    >
      {aslStatusLabel(status)}
    </span>
  );
}

export default AslStatusPill;
