import React, { useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';
import { canPublishDocuments } from '../../utils/recordWriteAccess';
import { hankDocumentSchema, hankPdfError, HankDocumentFields } from '../../validation/hankDocument';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from './hankSession';

export interface HankUploadedDocument {
  id: number;
  document_number: string;
  title: string;
  revision: string;
}

interface HankDocumentUploadProps {
  onUploaded: (document: HankUploadedDocument) => void;
  onCancel: () => void;
  workOrderId?: number;
  onBusyChange?: (busy: boolean) => void;
}

/** Files go through the existing audited document endpoint, never the chat model. */
export function HankDocumentUpload({ onUploaded, onCancel, workOrderId, onBusyChange }: HankDocumentUploadProps) {
  const { role, isSuperuser } = usePermissions();
  const canPublish = canPublishDocuments({ role, is_superuser: isSuperuser }) && !isHankReadOnlySession();
  const [sessionScope] = useState(getHankSessionScope);
  // Pin the proposed destination for this form even if navigation changes behind the drawer.
  const [targetId] = useState(workOrderId);
  const [workOrderNumber, setWorkOrderNumber] = useState('');
  const [attachToJob, setAttachToJob] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState('');
  const [types, setTypes] = useState<Array<{ value: string; label: string }>>([]);
  const [typeLoadFailed, setTypeLoadFailed] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const uploadController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const {
    register,
    handleSubmit,
    getValues,
    setValue,
    formState: { errors },
  } = useForm<HankDocumentFields>({
    resolver: zodResolver(hankDocumentSchema),
    defaultValues: { title: '', document_type: '', revision: 'A', description: '' },
  });

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeHankSession(() => {
      if (getHankSessionScope() !== sessionScope) uploadController.current?.abort();
    });
    return () => {
      mounted.current = false;
      uploadController.current?.abort();
      unsubscribe();
    };
  }, [sessionScope]);

  useEffect(() => {
    let active = true;
    setTypeLoadFailed(false);
    api
      .getDocumentTypes()
      .then((result: Array<{ value: string; label: string }>) => {
        if (active && getHankSessionScope() === sessionScope) setTypes(result);
      })
      .catch(() => {
        if (active) setTypeLoadFailed(true);
      });
    return () => {
      active = false;
    };
  }, [loadAttempt, sessionScope]);

  useEffect(() => {
    if (!targetId || !Number.isSafeInteger(targetId) || targetId <= 0) return;
    let active = true;
    api
      .getWorkOrder(targetId)
      .then(result => {
        if (active && getHankSessionScope() === sessionScope && result.id === targetId) {
          setWorkOrderNumber(result.work_order_number);
        }
      })
      .catch(() => {
        // Filing to the library remains available if the current job cannot be resolved.
      });
    return () => {
      active = false;
    };
  }, [targetId, sessionScope]);

  const chooseFile = (next: File | undefined) => {
    if (inFlight.current) return;
    setError('');
    if (!next) {
      setFile(null);
      setFileError('');
      return;
    }
    const problem = hankPdfError(next);
    setFileError(problem || '');
    setFile(problem ? null : next);
    if (!problem && !getValues('title').trim()) {
      setValue(
        'title',
        next.name
          .replace(/\.pdf$/i, '')
          .replace(/[_-]+/g, ' ')
          .slice(0, 255)
      );
    }
  };

  const upload = async (values: HankDocumentFields) => {
    if (!canPublish || inFlight.current) return;
    if (getHankSessionScope() !== sessionScope) {
      setError('Your company or session changed. Reopen Upload PDF before filing this document.');
      return;
    }
    if (!file) {
      setFileError('Choose a PDF to upload.');
      return;
    }
    if (!types.some(type => type.value === values.document_type)) {
      setError('Choose an available document type.');
      return;
    }
    inFlight.current = true;
    const controller = new AbortController();
    uploadController.current = controller;
    setBusy(true);
    onBusyChange?.(true);
    setError('');
    const data = new FormData();
    data.append('file', file);
    data.append('title', values.title);
    data.append('document_type', values.document_type);
    data.append('revision', values.revision);
    data.append('description', values.description);
    if (attachToJob && workOrderNumber && targetId) data.append('work_order_id', String(targetId));
    try {
      const saved: HankUploadedDocument = await api.uploadDocument(data, controller.signal);
      if (mounted.current && getHankSessionScope() === sessionScope) onUploaded(saved);
    } catch (err: unknown) {
      if (mounted.current) {
        const detail: unknown = isAxiosError(err) ? err.response?.data?.detail : undefined;
        setError(
          typeof detail === 'string' ? detail : 'The upload was not confirmed. Check Documents before trying again.'
        );
      }
    } finally {
      inFlight.current = false;
      uploadController.current = null;
      if (mounted.current) {
        setBusy(false);
        onBusyChange?.(false);
      }
    }
  };

  if (!canPublish)
    return <p className="text-sm text-fd-body">Document publishing requires an Admin, Manager, or Quality role.</p>;

  return (
    <form onSubmit={handleSubmit(upload)} className="space-y-3" aria-label="File a PDF with Hank" aria-busy={busy}>
      <div>
        <h3 className="text-sm font-semibold text-fd-ink">Let’s file that PDF</h3>
        <p className="mt-1 text-xs text-fd-mute">
          Choose the file and check its details. Hank will save it to Documents and give you a link.
        </p>
      </div>
      <fieldset disabled={busy} className="space-y-3 min-w-0">
        <FormField
          label="PDF file"
          required
          error={fileError}
          help="Up to 25 MB. File contents are stored in the ERP; this does not read or extract the PDF."
        >
          {field => (
            <input
              {...field}
              type="file"
              accept=".pdf,application/pdf"
              onChange={event => chooseFile(event.target.files?.[0])}
              className="block w-full text-xs text-fd-body file:mr-2 file:px-2 file:py-1.5 file:rounded-[3px] file:border file:border-slate-600 file:bg-transparent file:text-fd-ink"
            />
          )}
        </FormField>
        {file && (
          <p className="text-xs text-fd-mute break-all">
            {file.name} · {(file.size / 1_000_000).toFixed(2)} MB
          </p>
        )}
        <FormField label="Document title" required error={errors.title?.message}>
          {field => <input {...field} {...register('title')} className="input w-full" maxLength={255} />}
        </FormField>
        <div className="grid grid-cols-[minmax(0,1fr)_5rem] gap-2">
          <FormField label="Document type" required error={errors.document_type?.message}>
            {field => (
              <select
                {...field}
                {...register('document_type')}
                className="input w-full"
                disabled={!types.length || busy}
              >
                <option value="">{types.length ? 'Choose a type' : 'Loading types…'}</option>
                {types.map(type => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField label="Revision" required error={errors.revision?.message}>
            {field => <input {...field} {...register('revision')} className="input w-full" maxLength={20} />}
          </FormField>
        </div>
        {typeLoadFailed && (
          <div role="alert" className="text-xs text-fd-red">
            Document types could not be loaded.{' '}
            <button type="button" className="underline" onClick={() => setLoadAttempt(attempt => attempt + 1)}>
              Retry loading types
            </button>
          </div>
        )}
        <FormField label="Notes (optional)" error={errors.description?.message}>
          {field => (
            <textarea {...field} {...register('description')} className="input w-full" rows={2} maxLength={4000} />
          )}
        </FormField>
        {workOrderNumber && (
          <label className="flex items-start gap-2 text-xs text-fd-body">
            <input
              type="checkbox"
              checked={attachToJob}
              onChange={event => setAttachToJob(event.target.checked)}
              className="mt-0.5"
            />
            <span>Also attach to {workOrderNumber}</span>
          </label>
        )}
      </fieldset>
      <p className="text-xs text-fd-mute">
        Upload creates a new, released document under your name. To replace an existing revision, open that document’s
        revision history.
      </p>
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <LoadingButton
          type="submit"
          size="sm"
          loading={busy}
          loadingText="Filing PDF…"
          disabled={!file || !types.length}
        >
          Upload and release PDF
        </LoadingButton>
        <button type="button" className="btn text-xs" disabled={busy} onClick={onCancel}>
          Cancel upload
        </button>
      </div>
    </form>
  );
}

export default HankDocumentUpload;
