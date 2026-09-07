import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import useUnsavedChanges from '../../hooks/useUnsavedChanges';
import { RecordHeader } from '../ui/PageHeader';
import { Modal } from '../ui/Modal';
import { Button, ErrorState, FormField } from '../ui';
export default function DocumentDetail({
  id,
  onClose,
  onSaved,
  onSelect,
}: {
  id: number;
  onClose: () => void;
  onSaved: () => void;
  onSelect: (id: number) => void;
}) {
  const [record, setRecord] = useState<any>(null);
  const [revisions, setRevisions] = useState<any[]>([]);
  const [preview, setPreview] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [previewError, setPreviewError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [revision, setRevision] = useState('');
  const [notes, setNotes] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [showRevision, setShowRevision] = useState(false);
  const { confirmDiscard, markSaved } = useUnsavedChanges(showRevision && (!!file || !!notes || !!revision));
  const selectRevision = (nextId: number) => {
    if (!uploading && confirmDiscard()) onSelect(nextId);
  };
  useEffect(() => {
    let active = true;
    let objectUrl = '';
    setLoading(true);
    setRecord(null);
    setError('');
    setPreviewError('');
    setPreview('');
    setShowRevision(false);
    Promise.all([api.getDocument(id), api.getDocumentRevisions(id)])
      .then(async ([doc, history]) => {
        if (!active) return;
        setRecord(doc);
        setRevisions(history);
        if (doc.mime_type === 'application/pdf' || /^image\/(png|jpeg|gif|webp)$/.test(doc.mime_type || '')) {
          try {
            const blob = await api.downloadDocument(id);
            if (active) {
              objectUrl = URL.createObjectURL(new Blob([blob], { type: doc.mime_type }));
              setPreview(objectUrl);
            }
          } catch {
            if (active) setPreviewError('Unable to load preview. Retry or download the document.');
          }
        }
      })
      .catch(err => {
        if (active) setError(err.response?.data?.detail || 'Unable to load document');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [id, attempt]);
  const download = async () => {
    try {
      const blob = await api.downloadDocument(id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = record.file_name || record.document_number;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setPreviewError('Unable to download document. Please retry.');
    }
  };
  const uploadRevision = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || uploading) return;
    setUploading(true);
    setError('');
    const data = new FormData();
    data.append('file', file);
    data.append('title', record.title);
    data.append('document_type', record.document_type);
    data.append('description', record.description || '');
    data.append('previous_revision_id', String(id));
    data.append('revision', revision.trim());
    data.append('revision_notes', notes.trim());
    ['part_id', 'work_order_id', 'vendor_id'].forEach(key => {
      if (record[key]) data.append(key, String(record[key]));
    });
    try {
      const next = await api.uploadDocument(data);
      setFile(null);
      setNotes('');
      setRevision('');
      markSaved();
      onSaved();
      onSelect(next.id);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === 'string' ? detail : 'Unable to upload revision');
    } finally {
      setUploading(false);
    }
  };
  const close = () => {
    if (!uploading && confirmDiscard()) onClose();
  };
  return (
    <Modal open ariaLabel="Document preview and revision history" onClose={close} size="2xl" closeOnBackdrop={false}>
      <RecordHeader
        title={record?.title || 'Document'}
        closeLabel="Close document"
        onClose={close}
        closeDisabled={uploading}
        fields={
          record
            ? [
                { label: 'Document', value: record.document_number },
                { label: 'Revision', value: record.revision },
                { label: 'Status', value: record.status },
                { label: 'File', value: record.file_name },
              ]
            : undefined
        }
      />
      {loading ? (
        <p role="status">Loading document and revision history…</p>
      ) : !record ? (
        <ErrorState message={error} onRetry={() => setAttempt(n => n + 1)} />
      ) : (
        <div className="space-y-4">
          <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{record.description}</p>
          <div className="flex min-w-0 flex-wrap gap-3">
            <Button variant="secondary" onClick={download}>
              Download file
            </Button>
            {revisions.length > 0 && revisions[0].id !== id ? (
              <Button variant="secondary" onClick={() => selectRevision(revisions[0].id)}>
                Open latest revision to upload
              </Button>
            ) : (
              <Button variant="secondary" onClick={() => setShowRevision(value => !value)}>
                Upload new revision
              </Button>
            )}
          </div>
          {previewError && (
            <p role="alert" className="text-red-300">
              {previewError}{' '}
              <button className="underline" onClick={() => setAttempt(n => n + 1)}>
                Retry
              </button>
            </p>
          )}
          {preview ? (
            record.mime_type === 'application/pdf' ? (
              <iframe
                title={`Preview ${record.title} revision ${record.revision}`}
                src={preview}
                className="w-full h-[50vh] bg-white"
              />
            ) : (
              <img
                src={preview}
                alt={`${record.title} revision ${record.revision}`}
                className="max-h-[50vh] object-contain mx-auto"
              />
            )
          ) : (
            !previewError && (
              <p className="text-sm text-slate-400">
                Preview is available for PDF and common images. Download this file to view it in its application.
              </p>
            )
          )}
          <h3 className="font-semibold">Revision history ({revisions.length})</h3>
          <ul className="space-y-2">
            {revisions.map(doc => (
              <li key={doc.id} className="border border-fd-line p-2">
                <button
                  className="text-werco-primary underline"
                  disabled={doc.id === id}
                  onClick={() => selectRevision(doc.id)}
                >
                  {doc.document_number} · Rev {doc.revision}
                  {doc.id === id ? ' (viewing)' : ''}
                </button>
                <p className="text-xs text-slate-400">{doc.created_at}</p>
                <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{doc.revision_notes || 'Initial upload'}</p>
              </li>
            ))}
          </ul>
          {showRevision && (
            <form onSubmit={uploadRevision} className="space-y-3 border-t border-fd-line pt-4">
              <p className="text-sm text-slate-400">
                The new revision retains this document’s linked records. Previous files remain available in history.
              </p>
              <FormField label="New revision" required>
                {field => (
                  <input
                    {...field}
                    className="input"
                    required
                    maxLength={20}
                    value={revision}
                    onChange={e => setRevision(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="What changed" required>
                {field => (
                  <textarea
                    {...field}
                    className="input"
                    required
                    value={notes}
                    onChange={e => setNotes(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Revision file" required>
                {field => (
                  <input {...field} type="file" required onChange={e => setFile(e.target.files?.[0] || null)} />
                )}
              </FormField>
              {error && (
                <p role="alert" className="text-red-300">
                  {error}
                </p>
              )}
              <Button type="submit" disabled={uploading || !file}>
                {uploading ? 'Uploading revision…' : 'Save revision'}
              </Button>
            </form>
          )}
        </div>
      )}
    </Modal>
  );
}
