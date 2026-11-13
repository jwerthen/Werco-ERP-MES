import React from 'react';
import { ExclamationTriangleIcon, NoSymbolIcon } from '@heroicons/react/24/outline';
import type { SupplierApprovalWarning } from '../../types/supplierApproval';

interface Props {
  warnings?: SupplierApprovalWarning[] | null;
  /** The Disqualified refusal (the exact 409 text). Shown red; the matching warning is folded into it. */
  refusal?: string | null;
  className?: string;
}

/**
 * Approved Supplier List findings for a purchase order.
 *
 * Warnings are ADVISORY (amber): the order can still be raised — they are what the
 * create audit row records. The one hard stop is a Disqualified vendor (red), which every
 * PO-create door refuses with the text passed as `refusal`.
 */
export function SupplierApprovalWarnings({ warnings, refusal, className = '' }: Props) {
  const advisory = (warnings ?? []).filter(warning => !(refusal && warning.code === 'vendor_disqualified'));
  if (!refusal && advisory.length === 0) return null;
  return (
    <div className={`space-y-2 ${className}`} data-testid="supplier-approval-warnings">
      {refusal && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-sm border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200"
        >
          <NoSymbolIcon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
          <p>{refusal}</p>
        </div>
      )}
      {advisory.length > 0 && (
        <section
          aria-label="Approved Supplier List warnings"
          className="rounded-sm border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
        >
          <div className="flex items-center gap-2 font-medium">
            <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
            <span>Approved Supplier List</span>
          </div>
          <ul className="mt-1 list-disc space-y-0.5 pl-9">
            {advisory.map((warning, index) => (
              <li key={`${warning.code}-${warning.part_id ?? 'vendor'}-${index}`}>{warning.message}</li>
            ))}
          </ul>
          <p className="mt-1 pl-7 text-xs text-amber-200/80">
            Advisory: the order can still be raised, and these warnings are recorded with it.
          </p>
        </section>
      )}
    </div>
  );
}

export default SupplierApprovalWarnings;
