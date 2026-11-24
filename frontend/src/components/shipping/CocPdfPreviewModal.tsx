import React, { useEffect, useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, ErrorState, Modal } from '../ui';
import PdfPreview from '../ui/PdfPreview';

interface CocPdfPreviewModalProps {
  shipmentId: number;
  cocNumber: string;
  onClose: () => void;
}

/**
 * The issued, numbered Certificate of Conformance (GET /shipping/{id}/coc/pdf) in the
 * shared PDF preview, with Download and an Open-to-print link. This PDF, rendered by the
 * server from the certificate's frozen snapshot, is the only certificate the app prints;
 * the browser no longer draws a look-alike. Mount only while open: unmounting revokes
 * the object URL and discards a late load (the CarPdfPreviewModal pattern).
 */
export default function CocPdfPreviewModal({ shipmentId, cocNumber, onClose }: CocPdfPreviewModalProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setError(false);

    void (async () => {
      try {
        const loadedUrl = await api.fetchCocPdf(shipmentId);
        if (cancelled) {
          window.URL.revokeObjectURL(loadedUrl);
          return;
        }
        objectUrl = loadedUrl;
        setUrl(loadedUrl);
      } catch {
        if (!cancelled) setError(true);
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [shipmentId, attempt]);

  const title = `Certificate of Conformance ${cocNumber}`;
  const fileName = `${cocNumber}.pdf`;

  return (
    <Modal open onClose={onClose} size="7xl" padded={false} ariaLabel={`Preview ${title}`}>
      <div className="flex items-start justify-between gap-3 border-b border-fd-line px-4 py-3 sm:px-6">
        <div className="min-w-0">
          <h2 className="break-words text-lg font-semibold text-fd-ink">{title}</h2>
          <p className="break-words text-sm text-fd-mute">The issued certificate, printed from its frozen record.</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {url && (
            <a href={url} target="_blank" rel="noreferrer" className="btn-secondary btn-sm">
              Open to print
            </a>
          )}
          <Button variant="secondary" size="sm" onClick={onClose} aria-label="Close preview">
            <XMarkIcon className="h-5 w-5" aria-hidden="true" />
          </Button>
        </div>
      </div>
      <div className="p-3 sm:p-4">
        {error ? (
          <ErrorState
            title="Could not load the certificate PDF"
            message="Try loading the Certificate of Conformance again."
            onRetry={() => setAttempt(value => value + 1)}
          />
        ) : url ? (
          <PdfPreview url={url} fileName={fileName} title={title} />
        ) : (
          <div role="status" className="flex h-[65vh] items-center justify-center text-sm text-fd-mute">
            Loading PDF preview…
          </div>
        )}
      </div>
    </Modal>
  );
}
