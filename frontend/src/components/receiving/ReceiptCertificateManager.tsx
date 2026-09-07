import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { Button, ErrorState } from '../ui';
import { Modal } from '../ui/Modal';
import ReceiptCertificateField, { CertificateDownload } from './ReceiptCertificateField';
interface Receipt {
  receipt_number: string;
  po_line_id: number;
  part_number?: string;
  lot_number: string;
  certificate_document_id?: number | null;
}
export default function ReceiptCertificateManager({
  receiptId,
  canUpload,
  onClose,
}: {
  receiptId: number;
  canUpload: boolean;
  onClose: () => void;
}) {
  const [record, setRecord] = useState<Receipt | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    setRecord(null);
    setError(false);
    api
      .getReceiptDetail(receiptId)
      .then(row => {
        if (active) setRecord(row);
      })
      .catch(() => {
        if (active) setError(true);
      });
    return () => {
      active = false;
    };
  }, [receiptId, attempt]);
  return (
    <Modal
      open
      ariaLabel="Receipt certificate"
      onClose={() => {
        if (!busy) onClose();
      }}
      size="lg"
      closeOnBackdrop={false}
    >
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Receipt certificate</h2>
        {error ? (
          <ErrorState message="Unable to load receipt" onRetry={() => setAttempt(n => n + 1)} />
        ) : !record ? (
          <p role="status">Loading certificate details…</p>
        ) : (
          <>
            <p>
              {record.receipt_number} · {record.part_number} · Lot {record.lot_number}
            </p>
            {record.certificate_document_id ? (
              <CertificateDownload documentId={record.certificate_document_id} />
            ) : (
              <>
                <p>No certificate file is linked to this receipt.</p>
                {canUpload && (
                  <ReceiptCertificateField
                    lineId={record.po_line_id}
                    receiptId={receiptId}
                    onBusy={setBusy}
                    onChange={document =>
                      setRecord(previous => previous && { ...previous, certificate_document_id: document.id })
                    }
                  />
                )}
              </>
            )}
          </>
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="secondary" disabled={busy} onClick={() => setAttempt(n => n + 1)}>
            Refresh certificate
          </Button>
          <Button type="button" disabled={busy} onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </Modal>
  );
}
