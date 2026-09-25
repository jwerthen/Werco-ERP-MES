import React from 'react';
import type { ProductionSavePhase, ShopFloorPendingProduction, ShopFloorPendingCorrection } from '../../hooks/useShopFloorProduction';

/** Persistent feedback: a lost response is never presented as saved or safe to resend. */
export default function ProductionSaveNotice({ phase, message, online, unconfirmed, onRetry, unconfirmedCorrection, onReviewCorrection }: {
  phase: ProductionSavePhase;
  message: string;
  online: boolean;
  unconfirmed: ShopFloorPendingProduction | null;
  onRetry: () => void;
  unconfirmedCorrection?: ShopFloorPendingCorrection | null;
  /** Opens an explicit supervisor-history-review acknowledgement in the page. */
  onReviewCorrection?: () => void;
}) {
  if (phase === 'idle' && online && !message) return null;
  const uncertain = phase === 'not-confirmed';
  const problem = !online || uncertain || phase === 'not-saved';
  const title = !online ? 'Offline' : phase === 'saving' ? 'Saving…' : phase === 'saved' ? 'Saved' : uncertain ? 'Not confirmed' : 'Not saved';
  return (
    <div role={problem ? 'alert' : 'status'} aria-live={problem ? 'assertive' : 'polite'} className={`rounded-lg border p-3 ${problem ? 'border-amber-400/50 bg-amber-500/10' : 'border-emerald-500/40 bg-emerald-500/10'}`}>
      <p className="font-semibold text-white">{title}</p>
      {(!online || message !== title) && <p className="mt-1 text-sm text-slate-200">{!online ? 'Your entry is kept on this device. Reconnect before saving.' : message}</p>}
      {unconfirmed && (
        <>
          <p className="mt-1 text-sm text-slate-200">{unconfirmed.body.quantity_complete_delta || 0} complete · {unconfirmed.body.quantity_scrapped_delta || 0} scrap. Check this original report before entering another.</p>
          <button type="button" className="btn-secondary mt-3 min-h-11" disabled={!online || phase === 'saving'} onClick={onRetry}>
            Check original report
          </button>
        </>
      )}
      {unconfirmedCorrection && (
        <>
          <p className="mt-1 text-sm text-slate-200">Remove {unconfirmedCorrection.body.quantity_delta} complete · operation {unconfirmedCorrection.operationId}. Reason: {unconfirmedCorrection.body.reason}</p>
          <p className="mt-1 text-sm text-slate-200">This removal cannot be retried safely. Ask your supervisor to review the correction history before continuing.</p>
          {onReviewCorrection && <button type="button" className="btn-secondary mt-3 min-h-11" disabled={!online || phase === 'saving'} onClick={onReviewCorrection}>Correction reviewed with supervisor</button>}
        </>
      )}
    </div>
  );
}
