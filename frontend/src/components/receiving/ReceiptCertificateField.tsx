import React, { useEffect, useRef, useState } from 'react';
import api from '../../services/api';
import { workspaceIdentity } from '../../hooks/useWorkspaceRecords';
import { Button, FormField } from '../ui';
import { ReceivingCertificate } from '../../types/receivingDelivery';

export function CertificateDownload({
  documentId,
  fileName = 'certificate.pdf',
}: {
  documentId: number;
  fileName?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const download = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const [blob, document] = await Promise.all([api.downloadDocument(documentId), api.getDocument(documentId)]);
      const url = URL.createObjectURL(blob);
      const link = documentCreateLink(url, document.file_name || fileName);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setError('Certificate unavailable. Retry the download.');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <Button type="button" variant="secondary" onClick={download} disabled={busy}>
        {busy ? 'Downloading…' : 'Download certificate'}
      </Button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
function documentCreateLink(url: string, name: string) {
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  return link;
}

export default function ReceiptCertificateField({
  lineId,
  receiptId,
  certificate,
  onChange,
  disabled,
  onBusy,
}: {
  lineId: number;
  receiptId?: number;
  certificate?: ReceivingCertificate | null;
  onChange: (value: ReceivingCertificate) => void;
  disabled?: boolean;
  onBusy?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const upload = async (file?: File) => {
    if (!file || busy) return;
    const identity = workspaceIdentity();
    setBusy(true);
    onBusy?.(true);
    setError('');
    try {
      const result = await api.uploadReceivingCertificate(lineId, file, receiptId);
      if (alive.current && workspaceIdentity() === identity) onChange(result);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      if (alive.current && workspaceIdentity() === identity)
        setError(typeof detail === 'string' ? detail : 'Certificate upload failed. Choose the file again to retry.');
    } finally {
      if (alive.current) {
        setBusy(false);
        onBusy?.(false);
      }
    }
  };
  return (
    <div className="space-y-2 min-w-0">
      <FormField label="Certificate file (PDF, PNG or JPEG; up to 20 MB)">
        {field => (
          <input
            {...field}
            type="file"
            accept=".pdf,.png,.jpg,.jpeg"
            disabled={disabled || busy}
            className="block max-w-full text-sm"
            onChange={event => {
              void upload(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
        )}
      </FormField>
      {busy && <p role="status">Uploading certificate…</p>}
      {error && (
        <p role="alert" className="text-red-300">
          {error}
        </p>
      )}
      {certificate && (
        <>
          <p className="text-sm break-all">Stored: {certificate.file_name}</p>
          <CertificateDownload documentId={certificate.id} fileName={certificate.file_name} />
        </>
      )}
    </div>
  );
}
