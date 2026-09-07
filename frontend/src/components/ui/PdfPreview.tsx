import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from 'pdfjs-dist';

let rendererPromise: Promise<typeof import('./pdfRenderer')> | null = null;

// Keep the renderer and its worker out of routes that do not open a PDF.
function loadRenderer() {
  if (!rendererPromise) {
    rendererPromise = import('./pdfRenderer');
    void rendererPromise.catch(() => {
      rendererPromise = null;
    });
  }
  return rendererPromise;
}

export interface PdfPreviewProps {
  /** An already authorized PDF URL. The caller owns and revokes object URLs. */
  url: string;
  fileName?: string;
  title?: string;
  className?: string;
}

export default function PdfPreview({
  url,
  fileName = 'document.pdf',
  title = 'Document PDF',
  className = '',
}: PdfPreviewProps) {
  const [attempt, setAttempt] = useState(0);
  return (
    <div className={`min-w-0 overflow-hidden rounded border border-fd-line bg-fd-sunken ${className}`}>
      <PdfDocument
        key={`${url}:${attempt}`}
        url={url}
        fileName={fileName}
        title={title}
        onRetry={() => setAttempt(value => value + 1)}
      />
    </div>
  );
}

function PdfDocument({
  url,
  fileName,
  title,
  onRetry,
}: Required<Pick<PdfPreviewProps, 'url' | 'fileName' | 'title'>> & { onRetry: () => void }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [width, setWidth] = useState(0);
  const [status, setStatus] = useState<'loading' | 'rendering' | 'ready' | 'error'>('loading');
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measure = () => setWidth(Math.max(0, Math.floor(container.clientWidth)));
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(container);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    void (async () => {
      try {
        const renderer = await loadRenderer();
        if (cancelled) return;
        loadingTask = renderer.getDocument({ url });
        const pdf = await loadingTask.promise;
        if (cancelled) return;
        if (!pdf.numPages) throw new Error('PDF has no pages');
        setDocument(pdf);
        setStatus('rendering');
      } catch {
        if (!cancelled) setStatus('error');
      }
    })();
    return () => {
      cancelled = true;
      if (loadingTask) void loadingTask.destroy().catch(() => undefined);
    };
  }, [url]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!document || !canvas || !width) return;
    let cancelled = false;
    let renderTask: { cancel: () => void; promise: Promise<void> } | undefined;
    setStatus('rendering');
    void (async () => {
      try {
        const page = await document.getPage(pageNumber);
        if (cancelled) return;
        const natural = page.getViewport({ scale: 1 });
        const viewport = page.getViewport({ scale: width / natural.width });
        const density = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.ceil(viewport.width * density);
        canvas.height = Math.ceil(viewport.height * density);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        const context = canvas.getContext('2d');
        if (!context) throw new Error('Canvas is unavailable');
        renderTask = page.render({
          canvas,
          canvasContext: context,
          viewport,
          transform: density === 1 ? undefined : [density, 0, 0, density, 0, 0],
        });
        await renderTask.promise;
        if (!cancelled) setStatus('ready');
      } catch {
        if (!cancelled) setStatus('error');
      }
    })();
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [document, pageNumber, width]);

  const buttonClass = 'btn-secondary px-2 py-1 text-sm disabled:opacity-40';
  const changePage = (next: number) => {
    setStatus('rendering');
    setPageNumber(next);
    containerRef.current?.scrollTo?.({ top: 0 });
  };
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-fd-line p-2">
        <div className="flex items-center gap-2" aria-label="PDF pages">
          <button
            type="button"
            className={buttonClass}
            aria-label="Previous PDF page"
            disabled={!document || pageNumber === 1 || status === 'error'}
            onClick={() => changePage(pageNumber - 1)}
          >
            Previous
          </button>
          <span className="text-sm text-fd-body" aria-live="polite">
            {document ? `Page ${pageNumber} of ${document.numPages}` : 'PDF preview'}
          </span>
          <button
            type="button"
            className={buttonClass}
            aria-label="Next PDF page"
            disabled={!document || pageNumber >= document.numPages || status === 'error'}
            onClick={() => changePage(pageNumber + 1)}
          >
            Next
          </button>
        </div>
        <a href={url} download={fileName} className="text-sm text-fd-link underline">
          Download PDF
        </a>
      </div>
      <div
        ref={containerRef}
        className="relative max-h-[65vh] min-h-[240px] overflow-y-auto"
        aria-busy={status === 'loading' || status === 'rendering'}
      >
        {status === 'error' ? (
          <div
            role="alert"
            className="flex min-h-[240px] flex-col items-center justify-center gap-3 p-4 text-center text-sm text-fd-body"
          >
            <p>Could not display this PDF. Retry the preview or download the document.</p>
            <button type="button" onClick={onRetry} className={buttonClass}>
              Retry preview
            </button>
          </div>
        ) : (
          <>
            {status !== 'ready' && (
              <div
                role="status"
                className="absolute inset-0 flex min-h-[240px] items-center justify-center p-4 text-sm text-fd-mute"
              >
                {status === 'loading' ? 'Loading PDF…' : 'Rendering page…'}
              </div>
            )}
            <canvas
              // A fresh canvas prevents an in-flight cancelled render from painting a new page.
              key={`${pageNumber}:${width}`}
              ref={canvasRef}
              role="img"
              aria-label={`${title} — page ${pageNumber}${document ? ` of ${document.numPages}` : ''}`}
              className={`block bg-white ${status === 'ready' ? '' : 'invisible'}`}
            />
          </>
        )}
      </div>
    </>
  );
}
