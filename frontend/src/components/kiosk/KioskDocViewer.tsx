import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeftIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  MinusIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import { formatCentralDate } from '../../utils/centralTime';
import type { OperationCriticalDim, OperationDocumentsResponse } from '../../types';
import { kioskErrorMessage } from './kioskConstants';

/**
 * Transport seam (the StepsTransport pattern) so BOTH kiosks share one viewer:
 *  - OperatorKiosk binds api.getOperationDocuments / api.fetchShopFloorDocumentBlob
 *    (logged-in session; the /kiosk interceptor guard means a dead session
 *    rejects instead of navigating).
 *  - CrewStationKiosk binds kioskStationClient with a badge-minted operator
 *    token. Neither transport EVER navigates — errors render inline here.
 */
export interface KioskDocTransport {
  fetchOperationDocuments(operationId: number): Promise<OperationDocumentsResponse>;
  /** Resolves the document PDF to an object URL. The viewer revokes it. */
  fetchDocumentBlob(documentId: number): Promise<string>;
}

export type KioskDocTab = 'drawing' | 'nest';

interface KioskDocViewerProps {
  operationId: number;
  initialTab: KioskDocTab;
  transport: KioskDocTransport;
  onBack: () => void;
  /** Crew override for 401-ish failures ("Badge session expired — rescan to view"). */
  sessionExpiredMessage?: string;
}

const ZOOM_MIN = 50;
const ZOOM_MAX = 300;
const ZOOM_STEP = 25;
const GENERIC_ERROR = 'Could not load the document.';

// ---------------------------------------------------------------------------
// pdf.js is LAZY — the kiosk bundle must not pay for it until a viewer opens.
// The worker rides the same dynamic import via Vite's `?url` asset handling.
// ---------------------------------------------------------------------------
let pdfjsPromise: Promise<typeof import('pdfjs-dist')> | null = null;
function loadPdfJs(): Promise<typeof import('pdfjs-dist')> {
  if (!pdfjsPromise) {
    pdfjsPromise = (async () => {
      const [lib, worker] = await Promise.all([
        import('pdfjs-dist'),
        import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
      ]);
      lib.GlobalWorkerOptions.workerSrc = worker.default;
      return lib;
    })();
    // A failed load must stay retriable on the next viewer open.
    pdfjsPromise.catch(() => {
      pdfjsPromise = null;
    });
  }
  return pdfjsPromise;
}

function isAuthExpiry(err: unknown): boolean {
  const status =
    (err as { status?: unknown })?.status ?? (err as { response?: { status?: unknown } })?.response?.status;
  return status === 401;
}

/** "0.498 – 0.502 in" / "≥ 0.498 in" / "0.5 in" — honest limits from SPC fields. */
function dimSpec(dim: OperationCriticalDim): string {
  const unit = dim.unit_of_measure ? ` ${dim.unit_of_measure}` : '';
  if (dim.lsl != null && dim.usl != null) return `${dim.lsl} – ${dim.usl}${unit}`;
  if (dim.lsl != null) return `≥ ${dim.lsl}${unit}`;
  if (dim.usl != null) return `≤ ${dim.usl}${unit}`;
  if (dim.nominal != null) return `${dim.nominal}${unit}`;
  return '—';
}

/**
 * Full-screen drawing / nest viewer (Foundry 1h). Renders the released part
 * drawing and/or the live nest PDF through pdf.js (canvas, zoom 50–300%, FIT,
 * pager) with an `<object>` embed fallback, plus the DOCUMENT / CRITICAL DIMS
 * right rail (no ECO, no invented material — decision 3). All failures render
 * INLINE; the component never navigates.
 */
export default function KioskDocViewer({
  operationId,
  initialTab,
  transport,
  onBack,
  sessionExpiredMessage,
}: KioskDocViewerProps) {
  const transportRef = useRef(transport);
  transportRef.current = transport;

  const [docs, setDocs] = useState<OperationDocumentsResponse | null>(null);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [docsLoading, setDocsLoading] = useState(true);
  const [tab, setTab] = useState<KioskDocTab>(initialTab);

  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [blobError, setBlobError] = useState<string | null>(null);
  /** Bumped by the retry button to re-run a failed blob fetch. */
  const [blobAttempt, setBlobAttempt] = useState(0);
  // Object URLs by document id — revoked in one sweep on unmount/op change.
  const blobCacheRef = useRef<Map<number, string>>(new Map());

  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [numPages, setNumPages] = useState(0);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState<number | 'fit'>('fit');
  const [displayPct, setDisplayPct] = useState<number | null>(null);
  /** pdf.js unavailable/failed → plain <object> embed of the same blob. */
  const [useFallback, setUseFallback] = useState(false);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const renderTaskRef = useRef<{ cancel: () => void } | null>(null);

  const errorFor = useCallback(
    (err: unknown) => (isAuthExpiry(err) && sessionExpiredMessage ? sessionExpiredMessage : kioskErrorMessage(err, GENERIC_ERROR)),
    [sessionExpiredMessage]
  );

  // --- Discovery read ---------------------------------------------------------
  const loadDocs = useCallback(async () => {
    setDocsLoading(true);
    setDocsError(null);
    try {
      const res = await transportRef.current.fetchOperationDocuments(operationId);
      setDocs(res);
    } catch (err) {
      setDocsError(errorFor(err));
    } finally {
      setDocsLoading(false);
    }
  }, [operationId, errorFor]);

  useEffect(() => {
    setDocs(null);
    setTab(initialTab);
    void loadDocs();
    const cache = blobCacheRef.current;
    return () => {
      cache.forEach((url) => window.URL.revokeObjectURL(url));
      cache.clear();
    };
    // initialTab is deliberately NOT a dependency — it is the OPENING tab only,
    // and re-running on its change would reset the operator's in-viewer choice.
  }, [operationId, loadDocs]);

  const hasDrawing = docs?.drawing != null;
  const hasNestDoc = docs?.nest?.document_id != null;

  // Settle onto a tab that actually exists once discovery lands.
  useEffect(() => {
    if (!docs) return;
    if (tab === 'nest' && !hasNestDoc && hasDrawing) setTab('drawing');
    if (tab === 'drawing' && !hasDrawing && hasNestDoc) setTab('nest');
  }, [docs, tab, hasDrawing, hasNestDoc]);

  const currentDocumentId = tab === 'drawing' ? (docs?.drawing?.document_id ?? null) : (docs?.nest?.document_id ?? null);

  // --- Blob fetch (cached per document id) ------------------------------------
  useEffect(() => {
    if (currentDocumentId == null) {
      setBlobUrl(null);
      return undefined;
    }
    let cancelled = false;
    const cached = blobCacheRef.current.get(currentDocumentId);
    if (cached) {
      setBlobUrl(cached);
      setBlobError(null);
      return undefined;
    }
    setBlobUrl(null);
    setBlobError(null);
    (async () => {
      try {
        const url = await transportRef.current.fetchDocumentBlob(currentDocumentId);
        if (cancelled) {
          window.URL.revokeObjectURL(url);
          return;
        }
        blobCacheRef.current.set(currentDocumentId, url);
        setBlobUrl(url);
      } catch (err) {
        if (!cancelled) setBlobError(errorFor(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [currentDocumentId, errorFor, blobAttempt]);

  // --- pdf.js document load ---------------------------------------------------
  useEffect(() => {
    if (!blobUrl) {
      setPdf(null);
      setNumPages(0);
      return undefined;
    }
    let cancelled = false;
    let loaded: PDFDocumentProxy | null = null;
    setPdf(null);
    setUseFallback(false);
    setPage(1);
    setZoom('fit');
    (async () => {
      try {
        const lib = await loadPdfJs();
        const doc = await lib.getDocument({ url: blobUrl }).promise;
        if (cancelled) {
          void doc.loadingTask.destroy();
          return;
        }
        loaded = doc;
        setPdf(doc);
        setNumPages(doc.numPages);
      } catch {
        // pdf.js could not load/parse — fall back to the browser's PDF embed.
        if (!cancelled) setUseFallback(true);
      }
    })();
    return () => {
      cancelled = true;
      if (loaded) void loaded.loadingTask.destroy();
    };
  }, [blobUrl]);

  // --- Page render ------------------------------------------------------------
  const renderPage = useCallback(async () => {
    const doc = pdf;
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!doc || !canvas || !container) return;
    try {
      const pdfPage = await doc.getPage(page);
      const baseViewport = pdfPage.getViewport({ scale: 1 });
      const scale =
        zoom === 'fit'
          ? Math.max(0.1, (container.clientWidth - 48) / baseViewport.width)
          : zoom / 100;
      const outputScale = window.devicePixelRatio || 1;
      const viewport = pdfPage.getViewport({ scale });
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      const context = canvas.getContext('2d');
      if (!context) return;
      renderTaskRef.current?.cancel();
      const task = pdfPage.render({
        canvasContext: context,
        viewport,
        transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : undefined,
      } as unknown as Parameters<typeof pdfPage.render>[0]);
      renderTaskRef.current = task;
      await task.promise;
      setDisplayPct(Math.round(scale * 100));
    } catch (err) {
      // A cancelled render is routine (rapid zoom/page taps), and a destroyed
      // document (fast tab switch / unmount tears down the loadingTask while a
      // render is in flight) rejects with AbortException — neither is a real
      // failure, and flagging one would kick the NEXT document into the embed
      // fallback. Anything else drops to the fallback rather than a dead canvas.
      const name = (err as { name?: string })?.name;
      if (name !== 'RenderingCancelledException' && name !== 'AbortException') setUseFallback(true);
    }
  }, [pdf, page, zoom]);

  useEffect(() => {
    void renderPage();
  }, [renderPage]);

  // FIT tracks the container across orientation changes.
  useEffect(() => {
    if (zoom !== 'fit') return undefined;
    const handleResize = () => void renderPage();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, [zoom, renderPage]);

  const stepZoom = (direction: 1 | -1) => {
    const current = zoom === 'fit' ? (displayPct ?? 100) : zoom;
    const snapped = Math.round(current / ZOOM_STEP) * ZOOM_STEP;
    setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, snapped + direction * ZOOM_STEP)));
  };

  const partNumber = docs?.part?.part_number ?? null;
  const revision = docs?.part?.revision ?? docs?.drawing?.revision ?? null;
  const rawNestLabel = docs?.nest ? docs.nest.nest_name || docs.nest.cnc_number || 'Nest' : null;
  // Nest names commonly already start with "NEST" (e.g. "NEST 2231-A") — strip
  // it so the tab / open-nest button never read "NEST NEST 2231-A".
  const nestLabel = rawNestLabel ? rawNestLabel.replace(/^nest\s+/i, '') : rawNestLabel;
  const criticalDims = docs?.critical_dims ?? [];
  const noDocuments = docs != null && !hasDrawing && !hasNestDoc;

  const barButton =
    'inline-flex h-10 items-center gap-2 rounded-[3px] border border-fd-line px-3.5 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40';

  return (
    // Height-capped (not min-h): the canvas pans inside its own overflow area
    // so the rail's pinned button and the watermark stay on-screen.
    <div className="flex h-screen flex-col overflow-hidden bg-fd-canvas" data-testid="kiosk-doc-viewer">
      {/* Top bar */}
      <div className="flex h-[60px] shrink-0 flex-wrap items-center gap-3 border-b border-fd-line bg-fd-panel px-4">
        <button type="button" onClick={onBack} className={barButton} data-testid="kiosk-viewer-back">
          <ArrowLeftIcon className="h-[15px] w-[15px]" aria-hidden="true" />
          Back
        </button>
        <span className="font-mono text-sm font-bold uppercase text-fd-ink">
          {partNumber || docs?.nest?.cnc_number || '—'}
          {revision ? <span className="font-normal text-fd-mute"> Rev {revision}</span> : null}
        </span>
        {(hasDrawing || hasNestDoc) && (
          <div className="flex overflow-hidden rounded-[3px] border border-fd-line" role="tablist" aria-label="Document">
            {hasDrawing && (
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'drawing'}
                data-testid="kiosk-viewer-tab-drawing"
                onClick={() => setTab('drawing')}
                className={`flex h-[38px] items-center px-4 font-mono text-[11px] uppercase tracking-[0.08em] transition-colors duration-150 ease-out ${
                  tab === 'drawing'
                    ? 'bg-fd-blue/12 font-bold text-fd-blue shadow-[inset_0_-2px_0_var(--fd-blue)]'
                    : 'font-semibold text-fd-mute'
                }`}
              >
                Drawing
              </button>
            )}
            {hasDrawing && hasNestDoc && <div className="w-px bg-fd-line" aria-hidden="true" />}
            {hasNestDoc && (
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'nest'}
                data-testid="kiosk-viewer-tab-nest"
                onClick={() => setTab('nest')}
                className={`flex h-[38px] items-center px-4 font-mono text-[11px] uppercase tracking-[0.08em] transition-colors duration-150 ease-out ${
                  tab === 'nest'
                    ? 'bg-fd-blue/12 font-bold text-fd-blue shadow-[inset_0_-2px_0_var(--fd-blue)]'
                    : 'font-semibold text-fd-mute'
                }`}
              >
                Nest {nestLabel}
              </button>
            )}
          </div>
        )}
        <div className="flex-1" />
        {!useFallback && pdf && (
          <>
            <div className="flex items-center overflow-hidden rounded-[3px] border border-fd-line">
              <button
                type="button"
                aria-label="Zoom out"
                onClick={() => stepZoom(-1)}
                className="flex h-[38px] w-12 items-center justify-center bg-fd-raised text-fd-body transition-transform duration-150 ease-out active:scale-[0.98]"
              >
                <MinusIcon className="h-[17px] w-[17px]" aria-hidden="true" />
              </button>
              <span className="flex h-[38px] w-[70px] items-center justify-center border-x border-fd-line font-mono text-xs tabular-nums text-fd-ink">
                {displayPct != null ? `${displayPct}%` : '—'}
              </span>
              <button
                type="button"
                aria-label="Zoom in"
                onClick={() => stepZoom(1)}
                className="flex h-[38px] w-12 items-center justify-center bg-fd-raised text-fd-body transition-transform duration-150 ease-out active:scale-[0.98]"
              >
                <PlusIcon className="h-[17px] w-[17px]" aria-hidden="true" />
              </button>
            </div>
            <button
              type="button"
              onClick={() => setZoom('fit')}
              className={`${barButton} ${zoom === 'fit' ? 'border-fd-blue/40 text-fd-blue' : ''}`}
            >
              Fit
            </button>
            {numPages > 1 && (
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  aria-label="Previous page"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  className="flex h-10 w-11 items-center justify-center rounded-[3px] border border-fd-line text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
                >
                  <ChevronLeftIcon className="h-4 w-4" aria-hidden="true" />
                </button>
                <span className="font-mono text-xs uppercase text-fd-mute">
                  Page <span className="tabular-nums text-fd-ink">{page}/{numPages}</span>
                </span>
                <button
                  type="button"
                  aria-label="Next page"
                  disabled={page >= numPages}
                  onClick={() => setPage((p) => Math.min(numPages, p + 1))}
                  className="flex h-10 w-11 items-center justify-center rounded-[3px] border border-fd-line text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
                >
                  <ChevronRightIcon className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Body */}
      <div className="flex min-h-0 flex-1 flex-col min-[1100px]:flex-row">
        {/* Viewer area */}
        <div
          ref={containerRef}
          className="relative min-h-[320px] flex-1 overflow-auto bg-fd-sunken [background-image:linear-gradient(rgba(36,48,68,0.14)_1px,transparent_1px),linear-gradient(90deg,rgba(36,48,68,0.14)_1px,transparent_1px)] [background-size:28px_28px]"
        >
          {docsLoading ? (
            <p className="flex h-full items-center justify-center py-16 font-mono text-sm uppercase tracking-[0.14em] text-fd-mute">
              Loading document…
            </p>
          ) : docsError ? (
            <div role="alert" className="flex h-full flex-col items-center justify-center gap-4 px-6 py-16 text-center">
              <p className="text-xl font-semibold text-fd-red">{docsError}</p>
              <button type="button" onClick={() => void loadDocs()} className={`${barButton} h-11`}>
                Retry
              </button>
            </div>
          ) : noDocuments ? (
            <p className="flex h-full items-center justify-center px-6 py-16 text-center text-xl text-fd-mute">
              No released drawing or nest PDF is attached to this operation.
            </p>
          ) : blobError ? (
            <div role="alert" className="flex h-full flex-col items-center justify-center gap-4 px-6 py-16 text-center">
              <p className="text-xl font-semibold text-fd-red">{blobError}</p>
              <button type="button" onClick={() => setBlobAttempt((n) => n + 1)} className={`${barButton} h-11`}>
                Retry
              </button>
            </div>
          ) : !blobUrl ? (
            <p className="flex h-full items-center justify-center py-16 font-mono text-sm uppercase tracking-[0.14em] text-fd-mute">
              Loading PDF…
            </p>
          ) : useFallback ? (
            <object
              data={blobUrl}
              type="application/pdf"
              aria-label="Document PDF"
              className="h-full min-h-[480px] w-full bg-white"
            >
              <p className="flex h-full items-center justify-center px-6 text-center text-lg text-fd-mute">
                This browser cannot display the PDF inline.
              </p>
            </object>
          ) : (
            <div className="flex min-h-full items-center justify-center p-6">
              <canvas
                ref={canvasRef}
                role="img"
                aria-label={`${tab === 'drawing' ? 'Drawing' : 'Nest'} PDF page ${page} of ${numPages || 1}`}
                className="rounded-[4px] border border-fd-line-bright bg-white shadow-[0_12px_40px_rgba(0,0,0,0.5)]"
              />
            </div>
          )}
          <p className="pointer-events-none absolute bottom-3.5 left-4 font-mono text-[10px] uppercase tracking-[0.12em] text-fd-faint">
            Controlled copy · uncontrolled if printed · ITAR
          </p>
        </div>

        {/* Right rail */}
        <div className="flex w-full shrink-0 flex-col gap-3 border-t border-fd-line bg-fd-panel p-4 min-[1100px]:w-[280px] min-[1100px]:border-l min-[1100px]:border-t-0">
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute">Document</p>
          <dl className="flex flex-col gap-2 font-mono text-xs">
            <div className="flex justify-between gap-3">
              <dt className="uppercase text-fd-mute">Part</dt>
              <dd className="font-semibold text-fd-ink">{partNumber || '—'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="uppercase text-fd-mute">Rev</dt>
              <dd className="font-semibold text-fd-ink">{revision || '—'}</dd>
            </div>
            {docs?.drawing?.released_at && (
              <div className="flex justify-between gap-3">
                <dt className="uppercase text-fd-mute">Released</dt>
                <dd className="text-fd-ink">{formatCentralDate(docs.drawing.released_at)}</dd>
              </div>
            )}
            {docs?.material && (
              <div className="flex justify-between gap-3">
                <dt className="uppercase text-fd-mute">Material</dt>
                <dd className="text-fd-ink">{docs.material}</dd>
              </div>
            )}
            {docs?.nest?.cnc_number && (
              <div className="flex justify-between gap-3">
                <dt className="uppercase text-fd-mute">CNC#</dt>
                <dd className="text-fd-ink">{docs.nest.cnc_number}</dd>
              </div>
            )}
          </dl>

          {criticalDims.length > 0 && (
            <>
              <div className="h-px bg-fd-line" aria-hidden="true" />
              <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-fd-mute">Critical dims</p>
              <ul className="flex flex-col gap-2 font-mono text-xs">
                {criticalDims.map((dim) => (
                  <li key={dim.id} className="flex justify-between gap-3">
                    <span className="min-w-0 truncate text-fd-body">{dim.name}</span>
                    <span className="shrink-0 text-fd-cyan">{dimSpec(dim)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          <div className="flex-1" />
          {hasDrawing && hasNestDoc && (
            <button
              type="button"
              data-testid="kiosk-viewer-open-other"
              onClick={() => setTab(tab === 'drawing' ? 'nest' : 'drawing')}
              className="h-[52px] rounded-[3px] border border-fd-blue/40 bg-fd-blue/10 font-mono text-xs font-bold uppercase tracking-[0.08em] text-fd-blue transition-transform duration-150 ease-out active:scale-[0.98]"
            >
              {tab === 'drawing' ? `Open nest ${nestLabel}` : 'Open drawing'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
