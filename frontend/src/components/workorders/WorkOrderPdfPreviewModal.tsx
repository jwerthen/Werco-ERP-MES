import React, { useEffect, useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, ErrorState, Modal } from '../ui';
import PdfPreview from '../ui/PdfPreview';

interface WorkOrderPdfPreviewModalProps {
  source: 'document' | 'nest';
  sourceId: number;
  title: string;
  fileName: string;
  description?: string;
  onClose: () => void;
}

/** Mount only while open so closing releases the PDF and discards late loads. */
export default function WorkOrderPdfPreviewModal({
  source,
  sourceId,
  title,
  fileName,
  description,
  onClose,
}: WorkOrderPdfPreviewModalProps) {
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
        const loadedUrl =
          source === 'nest'
            ? await api.fetchLaserNestDocument(sourceId)
            : window.URL.createObjectURL(new Blob([await api.downloadDocument(sourceId)], { type: 'application/pdf' }));
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
  }, [source, sourceId, attempt]);

  return (
    <Modal open onClose={onClose} size="7xl" padded={false} ariaLabel={`Preview ${title}`}>
      <div className="flex items-start justify-between gap-3 border-b border-fd-line px-4 py-3 sm:px-6">
        <div className="min-w-0">
          <h2 className="break-words text-lg font-semibold text-fd-ink">{title}</h2>
          <p className="break-words text-sm text-fd-mute">{description || fileName}</p>
        </div>
        <Button variant="secondary" size="sm" onClick={onClose} aria-label="Close preview" className="shrink-0">
          <XMarkIcon className="h-5 w-5" aria-hidden="true" />
        </Button>
      </div>
      <div className="p-3 sm:p-4">
        {error ? (
          <ErrorState
            title="Could not load PDF preview"
            message="Try loading the drawing again."
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
