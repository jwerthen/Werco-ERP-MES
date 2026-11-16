import React, { useEffect, useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Button, ErrorState, Modal } from '../ui';
import PdfPreview from '../ui/PdfPreview';

interface CarPdfPreviewModalProps {
  carId: number;
  carNumber: string;
  onClose: () => void;
}

/**
 * The printable CAR (GET /quality/car/{id}/pdf) in the shared PDF preview, with its
 * Download link. Mount only while open: closing revokes the object URL and discards a
 * late load (the WorkOrderPdfPreviewModal pattern). The PDF is rendered from the saved
 * record, so unsaved edits in the detail form are not on it.
 */
export default function CarPdfPreviewModal({ carId, carNumber, onClose }: CarPdfPreviewModalProps) {
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
        const loadedUrl = await api.fetchCARPdf(carId);
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
  }, [carId, attempt]);

  const title = `Corrective Action Report ${carNumber}`;
  const fileName = `${carNumber}.pdf`;

  return (
    <Modal open onClose={onClose} size="7xl" padded={false} ariaLabel={`Preview ${title}`}>
      <div className="flex items-start justify-between gap-3 border-b border-fd-line px-4 py-3 sm:px-6">
        <div className="min-w-0">
          <h2 className="break-words text-lg font-semibold text-fd-ink">{title}</h2>
          <p className="break-words text-sm text-fd-mute">
            Printed from the saved record in the layout of the controlled CAR form.
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={onClose} aria-label="Close preview" className="shrink-0">
          <XMarkIcon className="h-5 w-5" aria-hidden="true" />
        </Button>
      </div>
      <div className="p-3 sm:p-4">
        {error ? (
          <ErrorState
            title="Could not load the CAR PDF"
            message="Try loading the printable CAR again."
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
