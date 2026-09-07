import React from 'react';
import type { PendingProductionReport } from './useProductionReportRequest';

export default function ProductionRecoveryNotice({
  report,
  busy,
  onRetry,
}: {
  report: PendingProductionReport | null;
  busy: boolean;
  onRetry: () => void;
}) {
  if (!report) return null;
  return (
    <div role="alert" className="rounded border border-fd-amber bg-fd-amber/10 p-4 text-fd-body">
      <p className="font-semibold">Production report needs confirmation</p>
      <p className="mt-1 text-sm">
        {report.body.quantity_complete_delta || 0} good · {report.body.quantity_scrapped_delta || 0} scrap · operation{' '}
        {report.operationId}. Check this original report before entering another. The same request cannot add quantities
        twice.
      </p>
      <button type="button" className="btn-secondary mt-3" disabled={busy} onClick={onRetry}>
        Check original report
      </button>
    </div>
  );
}
