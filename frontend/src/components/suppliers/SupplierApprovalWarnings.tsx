import React from 'react';
import { ExclamationTriangleIcon, NoSymbolIcon } from '@heroicons/react/24/outline';
import type { SupplierApprovalWarning } from '../../types/supplierApproval';

interface Props {
  warnings?: SupplierApprovalWarning[] | null;
  /** The Disqualified refusal (the exact 409 text). Shown red; the matching warning is folded into it. */
  refusal?: string | null;
  /**
   * The warnings belong to a purchase order that ALREADY EXISTS (the PO detail view), not to
   * one being raised. They are then the server's reading of the vendor's CURRENT Approved
   * Supplier List entry -- not what was recorded when the order was created -- and a
   * Disqualified vendor stops the order moving toward issue rather than stopping its creation.
   */
  existingOrder?: boolean;
  className?: string;
}

/**
 * Approved Supplier List findings for a purchase order.
 *
 * Warnings are ADVISORY (amber). The one hard stop is a Disqualified vendor (red): every
 * PO-create door refuses it, and an existing order to it cannot be sent, approved, submitted
 * for approval or given lines. A `vendor_disqualified` warning is therefore ALWAYS shown as
 * that red stop -- with the `refusal` text when one is passed, else its own message -- and
 * never in the amber list under "the order can still be raised".
 */
export function SupplierApprovalWarnings({ warnings, refusal, existingOrder = false, className = '' }: Props) {
  const all = warnings ?? [];
  const disqualified = all.find(warning => warning.code === 'vendor_disqualified');
  const hardStop = refusal || disqualified?.message || null;
  const advisory = all.filter(warning => warning.code !== 'vendor_disqualified');
  if (!hardStop && advisory.length === 0) return null;
  return (
    <div className={`space-y-2 ${className}`} data-testid="supplier-approval-warnings">
      {hardStop && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-sm border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm text-red-200"
        >
          <NoSymbolIcon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
          <div>
            <p>{hardStop}</p>
            {existingOrder && (
              <p className="mt-1 text-xs text-red-200/80">
                This order cannot be sent, approved, submitted for approval or given new lines. Cancel it, or record a
                new approval decision for the supplier first.
              </p>
            )}
          </div>
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
            {existingOrder
              ? 'Advisory, from the vendor’s current Approved Supplier List entry; these do not stop the order.'
              : 'Advisory: the order can still be raised, and these warnings are recorded with it.'}
          </p>
        </section>
      )}
    </div>
  );
}

export default SupplierApprovalWarnings;
