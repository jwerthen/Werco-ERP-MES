import React, { useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Modal } from '../ui/Modal';
import {
  laserNestManualSchema,
  LaserNestManualFormData,
  LaserNestManualFormInput,
} from '../../validation/schemas';
import { LaserNestInfo, LaserNestManualResponse } from '../../types';
import api from '../../services/api';

interface LaserNestManualModalProps {
  open: boolean;
  onClose: () => void;
  /** Parent work order id — manual create POSTs under this WO. */
  workOrderId: number;
  /** When set, the modal edits an existing nest (PATCH) instead of creating. */
  nest?: LaserNestInfo | null;
  /**
   * Called after a successful create/update so the parent can refresh.
   * On a partial create (nest saved, PDF attach failed) it is still called —
   * with a non-fatal warning the parent can surface — because the nest itself
   * was persisted and must show up in the list.
   */
  onSaved: (warning?: string) => void;
}

const FIELD_LABEL = 'text-xs font-medium text-fd-mute';
const ERR = 'mt-1 text-xs text-fd-red';

const PDF_ATTACH_FAILED_MESSAGE =
  "Nest created, but the PDF didn't attach — use Attach PDF on the nest row to retry.";

/**
 * Add a laser nest manually, or edit an existing nest's fields.
 *
 * Create path: POST the manual nest; if a PDF was chosen, upload it as a
 * DRAWING Document (scoped to this WO) and attach it to the new nest. Edit path:
 * PATCH the changed fields (PDF attach/detach is handled inline on the nest row,
 * not here).
 */
export default function LaserNestManualModal({
  open,
  onClose,
  workOrderId,
  nest,
  onSaved,
}: LaserNestManualModalProps) {
  const isEdit = Boolean(nest);
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [submitError, setSubmitError] = useState('');
  const [fileError, setFileError] = useState('');
  const [busy, setBusy] = useState(false);
  const [fileInputKey, setFileInputKey] = useState(0);
  // Once a manual create succeeds we must never re-POST it, even if the PDF
  // attach step fails and the user re-submits. Holds the id of the nest this
  // modal session already created.
  const createdNestIdRef = useRef<number | null>(null);

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<LaserNestManualFormInput, unknown, LaserNestManualFormData>({
    resolver: zodResolver(laserNestManualSchema),
    defaultValues: {
      cnc_number: '',
      planned_runs: 1,
      nest_name: '',
      material: '',
      thickness: '',
      sheet_size: '',
    },
  });

  // Seed the form whenever the modal opens (or the target nest changes).
  useEffect(() => {
    if (!open) return;
    reset({
      cnc_number: nest?.cnc_number ?? '',
      planned_runs: nest?.planned_runs ?? 1,
      nest_name: nest?.nest_name ?? '',
      material: nest?.material ?? '',
      thickness: nest?.thickness ?? '',
      sheet_size: nest?.sheet_size ?? '',
    });
    setPdfFile(null);
    setSubmitError('');
    setFileError('');
    setFileInputKey((k) => k + 1);
    createdNestIdRef.current = null;
  }, [open, nest, reset]);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    setFileError('');
    const file = event.target.files?.[0] || null;
    if (file) {
      const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
      if (!isPdf) {
        setFileError('Only PDF files can be attached.');
        setPdfFile(null);
        return;
      }
    }
    setPdfFile(file);
  };

  const onSubmit = async (data: LaserNestManualFormData) => {
    setBusy(true);
    setSubmitError('');

    // --- Edit path: PATCH the changed fields, unchanged behavior. ---
    if (isEdit && nest) {
      try {
        await api.updateLaserNest(nest.id, {
          cnc_number: data.cnc_number,
          planned_runs: data.planned_runs,
          nest_name: data.nest_name,
          material: data.material,
          thickness: data.thickness,
          sheet_size: data.sheet_size,
        });
        onSaved();
        onClose();
      } catch (err: any) {
        setSubmitError(err?.response?.data?.detail || 'Failed to update laser nest');
      } finally {
        setBusy(false);
      }
      return;
    }

    // --- Create path: the nest POST and the PDF attach are two independent
    // steps. Once the nest is created we treat the create as done — a failed
    // PDF attach must NOT lose the nest or trigger a second create on retry. ---
    try {
      let nestId = createdNestIdRef.current;
      if (nestId === null) {
        const created: LaserNestManualResponse = await api.createManualLaserNest(workOrderId, {
          cnc_number: data.cnc_number,
          planned_runs: data.planned_runs,
          nest_name: data.nest_name,
          material: data.material,
          thickness: data.thickness,
          sheet_size: data.sheet_size,
        });
        nestId = created.id;
        createdNestIdRef.current = nestId;
      }

      // No reference PDF chosen — done.
      if (!pdfFile) {
        onSaved();
        onClose();
        return;
      }

      // Optional reference PDF: upload as a DRAWING Document scoped to the
      // parent WO, then attach it to the freshly-created nest. If this fails,
      // the nest still exists, so refresh + close with a non-fatal warning and
      // let the operator retry via "Attach PDF" on the nest row.
      try {
        const formData = new FormData();
        formData.append('file', pdfFile);
        formData.append('title', data.cnc_number);
        formData.append('document_type', 'drawing');
        formData.append('revision', 'A');
        formData.append('work_order_id', String(workOrderId));
        const uploaded = await api.uploadDocument(formData);
        await api.attachLaserNestDocument(nestId, uploaded.id);
        onSaved();
        onClose();
      } catch {
        onSaved(PDF_ATTACH_FAILED_MESSAGE);
        onClose();
      }
    } catch (err: any) {
      setSubmitError(err?.response?.data?.detail || 'Failed to add laser nest');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} size="lg" ariaLabelledBy="laser-nest-modal-title">
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        <div>
          <h2 id="laser-nest-modal-title" className="text-lg font-semibold text-fd-ink">
            {isEdit ? 'Edit laser nest' : 'Add nest manually'}
          </h2>
          <p className="mt-1 text-sm text-fd-mute">
            {isEdit
              ? 'Update the CNC number, runs, and material for this nest.'
              : 'Key one laser nest onto this work order, optionally with a reference PDF.'}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label className="block">
            <span className={FIELD_LABEL}>CNC number *</span>
            <input
              type="text"
              autoFocus
              {...register('cnc_number')}
              className="input mt-1 w-full"
              placeholder="e.g. 12345"
            />
            {errors.cnc_number && <p className={ERR}>{errors.cnc_number.message}</p>}
          </label>

          <label className="block">
            <span className={FIELD_LABEL}>Qty to cut / runs *</span>
            <input
              type="number"
              min={1}
              step={1}
              {...register('planned_runs')}
              className="input mt-1 w-full"
            />
            {errors.planned_runs && <p className={ERR}>{errors.planned_runs.message}</p>}
          </label>

          <label className="block sm:col-span-2">
            <span className={FIELD_LABEL}>Nest name</span>
            <input
              type="text"
              {...register('nest_name')}
              className="input mt-1 w-full"
              placeholder="Defaults to the CNC number"
            />
            {errors.nest_name && <p className={ERR}>{errors.nest_name.message}</p>}
          </label>

          <label className="block">
            <span className={FIELD_LABEL}>Material</span>
            <input type="text" {...register('material')} className="input mt-1 w-full" placeholder="e.g. 304 SS" />
          </label>

          <label className="block">
            <span className={FIELD_LABEL}>Thickness</span>
            <input type="text" {...register('thickness')} className="input mt-1 w-full" placeholder='e.g. 0.125"' />
          </label>

          <label className="block sm:col-span-2">
            <span className={FIELD_LABEL}>Sheet size</span>
            <input type="text" {...register('sheet_size')} className="input mt-1 w-full" placeholder='e.g. 48" x 96"' />
          </label>

          {!isEdit && (
            <label className="block sm:col-span-2">
              <span className={FIELD_LABEL}>Reference PDF (optional)</span>
              <input
                key={fileInputKey}
                type="file"
                accept="application/pdf"
                onChange={handleFileChange}
                className="mt-1 block w-full text-sm text-fd-body file:mr-3 file:rounded file:border-0 file:bg-fd-raised file:px-3 file:py-2 file:text-sm file:font-semibold file:text-fd-ink hover:file:bg-fd-line-bright"
              />
              {pdfFile && <p className="mt-1 text-xs text-fd-mute">{pdfFile.name}</p>}
              {fileError && <p className={ERR}>{fileError}</p>}
            </label>
          )}
        </div>

        {submitError && (
          <div className="rounded border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-sm text-fd-red">
            {submitError}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} disabled={busy} className="btn-secondary">
            Cancel
          </button>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? 'Saving…' : isEdit ? 'Save changes' : 'Add nest'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
