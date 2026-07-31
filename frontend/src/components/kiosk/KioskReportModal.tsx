import React, { useMemo, useState } from 'react';
import KioskKeypad from './KioskKeypad';
import KioskModal, { KioskModalClose } from './KioskModal';
import {
  activeScrapCodes,
  isQualityRelatedScrapSelection,
  resolveScrapSelection,
  scrapReasonTiles,
} from './scrapReasonOptions';
import type { ScrapReasonCodeOption } from '../../types/scrapReason';

export type ReportTab = 'good' | 'scrap';

interface KioskReportModalProps {
  workOrderNumber: string;
  operationNumber: string | number | null;
  /** Reported-so-far / ordered totals for the GOOD tab wells. */
  reportedGood: number;
  quantityOrdered: number;
  /** Active op's component_quantity — renders `FULL NEST {n}` only when > 1 (decision 4). */
  fullNestQuantity?: number | null;
  scrapCodes?: ScrapReasonCodeOption[] | null;
  busy: boolean;
  online: boolean;
  offlineHintId?: string;
  initialTab: ReportTab;
  onCancel: () => void;
  onConfirmGood: (good: number) => void;
  onConfirmScrap: (
    scrap: number,
    reason: string | null,
    codeId: number | null,
    openNcr: boolean,
    ncrDescription: string | null
  ) => void;
}

/**
 * REPORT PRODUCTION overlay (Foundry 1c/1d): ONE modal, segmented
 * GOOD PCS | SCRAP / NCR tabs. Each tab is a separate entry — GOOD confirms an
 * additive good delta, SCRAP confirms a scrap delta with its REQUIRED reason
 * (company codes when the tenant has them, else the legacy vocabulary — the
 * exact KioskQuantityScreen contract via scrapReasonOptions) plus the OPEN NCR
 * toggle (decision 5). Non-optimistic: the host owns the POST; on refusal the
 * modal stays open with everything entered.
 */
export default function KioskReportModal({
  workOrderNumber,
  operationNumber,
  reportedGood,
  quantityOrdered,
  fullNestQuantity,
  scrapCodes,
  busy,
  online,
  offlineHintId,
  initialTab,
  onCancel,
  onConfirmGood,
  onConfirmScrap,
}: KioskReportModalProps) {
  const [tab, setTab] = useState<ReportTab>(initialTab);
  const [goodValue, setGoodValue] = useState('');
  const [scrapValue, setScrapValue] = useState('');
  const [scrapReason, setScrapReason] = useState<string | null>(null);
  const [scrapDetail, setScrapDetail] = useState('');
  // null = follow the reason-derived default; boolean = operator's explicit choice.
  const [ncrOverride, setNcrOverride] = useState<boolean | null>(null);

  const codes = activeScrapCodes(scrapCodes);
  const reasonTiles = useMemo(() => scrapReasonTiles(codes), [codes]);

  const goodQty = Number(goodValue || 0);
  const scrapQty = Number(scrapValue || 0);
  const afterEntry = reportedGood + goodQty;
  const solidPct = quantityOrdered > 0 ? Math.min(100, (reportedGood / quantityOrdered) * 100) : 0;
  const pendingPct = quantityOrdered > 0 ? Math.min(100 - solidPct, (goodQty / quantityOrdered) * 100) : 0;

  const openNcr = ncrOverride ?? isQualityRelatedScrapSelection(codes, scrapReason);

  const confirmDisabled =
    busy || (tab === 'good' ? goodQty <= 0 : scrapQty <= 0 || !scrapReason);

  const handleConfirm = () => {
    if (confirmDisabled) return;
    if (tab === 'good') {
      onConfirmGood(goodQty);
      return;
    }
    const { reason, codeId } = resolveScrapSelection(codes, scrapReason, scrapDetail);
    // The detail field doubles as the NCR description (decision 5).
    onConfirmScrap(scrapQty, reason, codeId, openNcr, openNcr ? scrapDetail.trim() || null : null);
  };

  const quickAdds: Array<{ label: string; amount: number }> = [
    { label: '+1', amount: 1 },
    { label: '+5', amount: 5 },
    { label: '+25', amount: 25 },
    ...(fullNestQuantity != null && Number(fullNestQuantity) > 1
      ? [{ label: `Full nest ${Number(fullNestQuantity)}`, amount: Number(fullNestQuantity) }]
      : []),
  ];

  const mono = "font-mono";

  return (
    <KioskModal onClose={onCancel} widthClassName="max-w-[620px]" ariaLabelledBy="kiosk-report-title">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-fd-line px-5 py-4">
        <h2
          id="kiosk-report-title"
          className={`${mono} text-[13px] font-bold uppercase tracking-[0.1em] text-fd-ink`}
        >
          Report production
        </h2>
        <span className={`${mono} text-[11px] uppercase text-fd-mute`}>
          {workOrderNumber} · Op {operationNumber ?? '—'}
        </span>
        <div className="flex-1" />
        <KioskModalClose onClose={onCancel} disabled={busy} />
      </div>

      {/* Segmented tabs */}
      <div className="flex border-b border-fd-line" role="tablist" aria-label="Report type">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'good'}
          data-testid="kiosk-report-tab-good"
          disabled={busy}
          onClick={() => setTab('good')}
          className={`${mono} h-[52px] flex-1 text-[13px] uppercase tracking-[0.1em] transition-colors duration-150 ease-out disabled:opacity-40 ${
            tab === 'good'
              ? 'bg-fd-green/10 font-bold text-fd-green shadow-[inset_0_-2px_0_var(--fd-green)]'
              : 'font-semibold text-fd-mute'
          }`}
        >
          Good pcs
        </button>
        <div className="w-px bg-fd-line" aria-hidden="true" />
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'scrap'}
          data-testid="kiosk-report-tab-scrap"
          disabled={busy}
          onClick={() => setTab('scrap')}
          className={`${mono} h-[52px] flex-1 text-[13px] uppercase tracking-[0.1em] transition-colors duration-150 ease-out disabled:opacity-40 ${
            tab === 'scrap'
              ? 'bg-fd-red/10 font-bold text-fd-red shadow-[inset_0_-2px_0_var(--fd-red)]'
              : 'font-semibold text-fd-mute'
          }`}
        >
          Scrap / NCR
        </button>
      </div>

      {/* Body: entry column + numpad */}
      <div className="flex flex-col gap-4 p-5 sm:flex-row">
        {tab === 'good' ? (
          <div className="flex min-w-0 flex-1 flex-col gap-2.5">
            <div
              data-testid="kiosk-report-qty"
              className={`${mono} flex h-[88px] items-center justify-end rounded-[4px] border border-fd-line-bright bg-fd-sunken px-4 text-[52px] font-bold tabular-nums text-fd-green [text-shadow:0_0_16px_rgba(63,185,80,0.3)]`}
            >
              {goodQty}
            </div>
            <div className="flex gap-2">
              {quickAdds.map((qa) => (
                <button
                  key={qa.label}
                  type="button"
                  disabled={busy}
                  onClick={() => setGoodValue(String(Math.min(99999, goodQty + qa.amount)))}
                  className={`${mono} h-11 min-w-0 flex-1 rounded-[3px] border border-fd-line bg-fd-raised px-1 text-[13px] font-semibold uppercase tracking-[0.04em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40`}
                >
                  {qa.label}
                </button>
              ))}
            </div>
            <div className="flex flex-col gap-2 rounded-[4px] border border-fd-line bg-fd-sunken px-3.5 py-3">
              <div className={`${mono} flex justify-between text-xs uppercase tracking-[0.04em]`}>
                <span className="text-fd-mute">Reported so far</span>
                <span className="font-semibold tabular-nums text-fd-ink">
                  {reportedGood} / {quantityOrdered}
                </span>
              </div>
              <div className={`${mono} flex justify-between text-xs uppercase tracking-[0.04em]`}>
                <span className="text-fd-mute">After this entry</span>
                <span className="font-bold tabular-nums text-fd-green">
                  {afterEntry} / {quantityOrdered}
                </span>
              </div>
              <div className="flex h-2 overflow-hidden rounded-[2px] border border-fd-line bg-fd-canvas">
                <div className="h-full bg-fd-green" style={{ width: `${solidPct}%` }} />
                <div className="h-full bg-fd-green/45" style={{ width: `${pendingPct}%` }} />
              </div>
            </div>
          </div>
        ) : (
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            <div
              data-testid="kiosk-report-qty"
              className={`${mono} flex h-[76px] items-center justify-end rounded-[4px] border border-fd-red/40 bg-fd-sunken px-4 text-[44px] font-bold tabular-nums text-fd-red`}
            >
              {scrapQty}
            </div>
            <div>
              <p className={`${mono} mb-2 text-[10px] uppercase tracking-[0.16em] text-fd-mute`}>
                Scrap reason — required
              </p>
              <div className="grid grid-cols-2 gap-2" role="group" aria-label="Scrap reason">
                {reasonTiles.map((reason) => {
                  const selected = scrapReason === reason.value;
                  return (
                    <button
                      key={reason.value}
                      type="button"
                      aria-pressed={selected}
                      disabled={busy}
                      onClick={() => setScrapReason(reason.value)}
                      className={`${mono} min-h-12 rounded-[3px] border px-2 py-1 text-xs uppercase tracking-[0.06em] transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 ${
                        selected
                          ? 'border-fd-red bg-fd-red/10 font-bold text-fd-red'
                          : 'border-fd-line bg-fd-raised font-semibold text-fd-body'
                      }`}
                    >
                      {reason.label}
                    </button>
                  );
                })}
              </div>
            </div>
            {codes && (
              <div>
                <label
                  htmlFor="kiosk-scrap-detail"
                  className={`${mono} mb-1 block text-[10px] uppercase tracking-[0.16em] text-fd-mute`}
                >
                  Detail — optional
                </label>
                <input
                  id="kiosk-scrap-detail"
                  data-testid="kiosk-scrap-detail"
                  type="text"
                  maxLength={255}
                  disabled={busy}
                  value={scrapDetail}
                  onChange={(e) => setScrapDetail(e.target.value)}
                  placeholder="What happened?"
                  className={`${mono} w-full rounded-[3px] border border-fd-line bg-fd-sunken px-3 py-2.5 text-base text-fd-ink placeholder:text-fd-mute focus:border-fd-red focus:outline-none disabled:opacity-40`}
                />
              </div>
            )}
            {/* OPEN NCR toggle row (decision 5) */}
            <div className="flex items-center gap-3 rounded-[4px] border border-fd-red/35 bg-fd-red/5 px-3.5 py-3">
              <button
                type="button"
                role="switch"
                aria-checked={openNcr}
                aria-label="Open NCR"
                data-testid="kiosk-report-ncr-toggle"
                disabled={busy}
                onClick={() => setNcrOverride(!openNcr)}
                className={`relative h-[26px] w-[46px] shrink-0 rounded-full border transition-colors duration-150 ease-out disabled:opacity-40 ${
                  openNcr ? 'border-fd-red bg-fd-red' : 'border-fd-line-bright bg-fd-sunken'
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`absolute top-[2px] h-5 w-5 rounded-full bg-white transition-all duration-150 ease-out ${
                    openNcr ? 'right-[2px]' : 'left-[2px]'
                  }`}
                />
              </button>
              <div className="min-w-0">
                <p className={`${mono} text-xs font-bold uppercase tracking-[0.06em] text-fd-red`}>Open NCR</p>
                <p className="mt-0.5 text-xs text-fd-body">
                  {openNcr
                    ? `An NCR will be filed · Quality notified · quarantines ${scrapQty} pcs`
                    : 'No NCR will be filed with this scrap entry'}
                </p>
              </div>
            </div>
          </div>
        )}

        <div className="w-full shrink-0 sm:w-[250px]">
          <KioskKeypad
            value={tab === 'good' ? goodValue : scrapValue}
            onChange={tab === 'good' ? setGoodValue : setScrapValue}
            maxLength={5}
            disabled={busy}
            size="sm"
          />
        </div>
      </div>

      {/* Footer */}
      <div className="flex gap-2.5 px-5 pb-5">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className={`${mono} h-[60px] w-[140px] rounded-[4px] border border-fd-line text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40 sm:w-[170px]`}
        >
          Cancel
        </button>
        <button
          type="button"
          data-testid="kiosk-qty-confirm"
          onClick={handleConfirm}
          disabled={confirmDisabled}
          aria-describedby={!online ? offlineHintId : undefined}
          className={`${mono} h-[60px] flex-1 rounded-[4px] text-[15px] font-bold uppercase tracking-[0.1em] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 ${
            tab === 'good' ? 'bg-fd-green text-[#04140b]' : 'bg-fd-red text-[#1a0505]'
          }`}
        >
          {!online
            ? 'Offline'
            : busy
              ? 'Saving…'
              : tab === 'good'
                ? `Confirm +${goodQty} good`
                : `Confirm ${scrapQty} scrap${openNcr ? ' + open NCR' : ''}`}
        </button>
      </div>
      {tab === 'scrap' && scrapQty > 0 && !scrapReason && (
        <p className="px-5 pb-4 text-center text-sm text-fd-red">Choose a scrap reason to continue.</p>
      )}
    </KioskModal>
  );
}
