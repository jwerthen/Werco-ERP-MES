import { useRef, useState } from 'react';
import { productionRequestId } from '../../utils/productionRequestId';
import { isDefinitiveHttpRefusal } from './useOneTapPieces';

export interface ProductionReportBody {
  request_id?: string;
  quantity_complete_delta?: number;
  quantity_scrapped_delta?: number;
  scrap_reason?: string;
  scrap_reason_code_id?: number;
  source: string;
  open_ncr?: boolean;
  ncr_description?: string;
}

export interface PendingProductionReport {
  operatorId: number;
  operationId: number;
  body: ProductionReportBody;
}

const fingerprint = (body: ProductionReportBody) =>
  JSON.stringify(
    Object.entries(body)
      .filter(([key, value]) => key !== 'request_id' && value !== undefined)
      .sort(([a], [b]) => a.localeCompare(b))
  );

/** Keeps the original report immutable until a known response resolves it. */
export function useProductionReportRequest(storageKey?: string) {
  const read = (): PendingProductionReport | null => {
    try {
      const value = storageKey ? JSON.parse(sessionStorage.getItem(storageKey) || 'null') : null;
      return value &&
        Number.isInteger(value.operatorId) &&
        Number.isInteger(value.operationId) &&
        typeof value.body?.request_id === 'string'
        ? value
        : null;
    } catch {
      return null;
    }
  };
  const attempt = useRef<PendingProductionReport | null>(read());
  const persist = (value: PendingProductionReport | null) => {
    if (!storageKey) return;
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch {
      /* The mounted recovery control remains available. */
    }
  };
  const sending = useRef(false);
  const [unconfirmed, setUnconfirmed] = useState<PendingProductionReport | null>(attempt.current);

  const submit = async (
    operatorId: number,
    operationId: number,
    body: ProductionReportBody,
    post: (data: ProductionReportBody) => Promise<unknown>
  ): Promise<unknown> => {
    if (sending.current) throw new Error('This report is already being checked.');
    const old = attempt.current;
    if (
      old &&
      (old.operatorId !== operatorId || old.operationId !== operationId || fingerprint(old.body) !== fingerprint(body))
    ) {
      throw new Error(
        'An earlier production report is unconfirmed. Check that original report before entering another.'
      );
    }
    const report = old ?? { operatorId, operationId, body: { ...body, request_id: productionRequestId() } };
    attempt.current = report;
    persist(report);
    sending.current = true;
    try {
      const response = await post(report.body);
      attempt.current = null;
      persist(null);
      setUnconfirmed(null);
      return response;
    } catch (error) {
      const reason = error as { status?: number; response?: { status?: number } };
      if (isDefinitiveHttpRefusal(reason.status ?? reason.response?.status)) {
        // An initial explicit refusal wrote nothing. If an ambiguous attempt
        // preceded it, retain that attempt: a now-expired badge proves nothing
        // about whether its earlier report committed.
        if (!old) {
          attempt.current = null;
          persist(null);
        }
      } else {
        setUnconfirmed(report);
      }
      throw error;
    } finally {
      sending.current = false;
    }
  };

  const retry = (operatorId: number, post: (operationId: number, body: ProductionReportBody) => Promise<unknown>) => {
    const original = attempt.current;
    if (!original || original.operatorId !== operatorId)
      return Promise.reject(new Error('The original operator must check this report.'));
    return submit(operatorId, original.operationId, original.body, body => post(original.operationId, body));
  };
  return { submit, retry, unconfirmed };
}
