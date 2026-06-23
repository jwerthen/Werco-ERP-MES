import React, { useEffect, useRef, useState } from 'react';
import { DocumentTextIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';

interface LaserNestPdfPreviewProps {
  /** Nest id whose attached PDF is served inline by GET /laser-nests/{id}/document. */
  laserNestId: number;
  /** File name used as the iframe title / fallback label. */
  fileName?: string | null;
  /** Tailwind height utility for the embedded viewer (default a compact panel). */
  heightClassName?: string;
  className?: string;
}

/**
 * Authenticated inline PDF preview for a laser nest's attached drawing.
 *
 * GET /laser-nests/{id}/document requires the JWT, so a bare
 * `<iframe src="/api/...">` would NOT send the auth header. We fetch the PDF
 * through the authenticated Axios client as a blob (see api.fetchLaserNestDocument),
 * build a same-origin object URL, point the <iframe> at it, and revoke the URL
 * on unmount / nest change so we never leak blob URLs.
 *
 * Reused by the work-order-detail "View PDF" action and the operator kiosk /
 * shop-floor "Preview nest" surfaces.
 */
export default function LaserNestPdfPreview({
  laserNestId,
  fileName,
  heightClassName = 'h-[420px]',
  className = '',
}: LaserNestPdfPreviewProps) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  // Hold the live object URL in a ref so the cleanup always revokes the current
  // one regardless of render timing.
  const objectUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);

    const load = async () => {
      try {
        const url = await api.fetchLaserNestDocument(laserNestId);
        if (cancelled) {
          window.URL.revokeObjectURL(url);
          return;
        }
        objectUrlRef.current = url;
        setObjectUrl(url);
      } catch {
        if (!cancelled) {
          setError(true);
          setObjectUrl(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();

    return () => {
      cancelled = true;
      if (objectUrlRef.current) {
        window.URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = null;
      }
    };
  }, [laserNestId]);

  const frame = `rounded border border-fd-line bg-fd-sunken ${heightClassName} ${className}`.trim();

  if (loading) {
    return (
      <div
        data-testid="laser-nest-pdf-loading"
        className={`flex items-center justify-center text-sm text-fd-mute ${frame}`}
      >
        Loading nest PDF…
      </div>
    );
  }

  if (error || !objectUrl) {
    return (
      <div
        role="alert"
        className={`flex flex-col items-center justify-center gap-2 px-4 text-center text-sm text-fd-mute ${frame}`}
      >
        <ExclamationTriangleIcon className="h-8 w-8 text-fd-amber" />
        <span>Could not load the nest PDF.</span>
      </div>
    );
  }

  return (
    <object
      data={objectUrl}
      type="application/pdf"
      aria-label={fileName || 'Laser nest drawing PDF'}
      className={`w-full bg-white ${frame}`}
    >
      <div className="flex flex-col items-center justify-center gap-2 px-4 text-center text-sm text-fd-mute">
        <DocumentTextIcon className="h-8 w-8" />
        <a href={objectUrl} target="_blank" rel="noreferrer" className="text-fd-blue underline">
          Open {fileName || 'nest PDF'}
        </a>
      </div>
    </object>
  );
}
