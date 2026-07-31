import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { Modal } from '../ui/Modal';
import {
  laserNestManualSchema,
  LaserNestManualFormData,
  LaserNestManualFormInput,
} from '../../validation/schemas';
import {
  LaserNestInfo,
  LaserNestManualInput,
  LaserNestManualResponse,
  LaserNestExtractionConfidence,
  MaterialAllocation,
  Part,
} from '../../types';
import api from '../../services/api';
import { toDisplayString } from '../../utils/apiError';

interface LaserNestManualModalProps {
  open: boolean;
  onClose: () => void;
  /** Parent work order id — manual create POSTs under this WO. */
  workOrderId: number;
  /** When set, the modal edits an existing nest (PATCH) instead of creating. */
  nest?: LaserNestInfo | null;
  /**
   * EDIT mode only: the work-order operation that backs this nest.
   *
   * `LaserNestInfo` carries no operation id, and a tie is addressed by
   * operation — so without this the modal cannot see or change the nest's
   * material tie, and the tie controls stay hidden. Ignored on the create path
   * (the backend ties the operation it creates).
   */
  workOrderOperationId?: number;
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

/**
 * Explicit page size for the sheet-part list: `api.getMaterials` falls into an
 * unbounded `while (true)` loop pulling 500 rows at a time when `limit` is
 * omitted.
 */
const MATERIAL_OPTION_LIMIT = 500;

/** A sheet-part choice in the picker. */
interface MaterialOption {
  id: number;
  label: string;
}

/**
 * Manual-create body plus the sheet-part tie fields. The shared
 * `LaserNestManualInput` is intersected rather than edited because that type is
 * owned elsewhere; the backend accepts both keys on the manual-create route.
 */
type ManualNestCreateBody = LaserNestManualInput & {
  material_part_id?: number;
  qty_per_run?: number;
};

const PDF_ATTACH_FAILED_MESSAGE =
  "Nest created, but the PDF didn't attach — use Attach PDF on the nest row to retry.";

/** Outcome of an auto-extract attempt, used to drive the inline hint banner. */
interface ExtractHint {
  confidence: LaserNestExtractionConfidence | null;
  source: 'ai' | 'filename';
  warning: string | null;
}

const CONFIDENCE_BADGE: Record<LaserNestExtractionConfidence, { label: string; className: string }> = {
  high: { label: 'High confidence', className: 'border-fd-green/40 bg-fd-green/10 text-fd-green' },
  medium: { label: 'Medium confidence', className: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber' },
  low: { label: 'Low confidence', className: 'border-fd-red/40 bg-fd-red/10 text-fd-red' },
};

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
  workOrderOperationId,
  onSaved,
}: LaserNestManualModalProps) {
  const isEdit = Boolean(nest);
  const [pdfFile, setPdfFile] = useState<File | null>(null);
  const [submitError, setSubmitError] = useState('');
  const [fileError, setFileError] = useState('');
  const [busy, setBusy] = useState(false);
  const [fileInputKey, setFileInputKey] = useState(0);
  // Auto-extraction (create path only): while a PDF is being read we show a
  // spinner and block submit; the resulting hint drives the dismissible banner.
  const [extracting, setExtracting] = useState(false);
  const [extractHint, setExtractHint] = useState<ExtractHint | null>(null);
  // Once a manual create succeeds we must never re-POST it, even if the PDF
  // attach step fails and the user re-submits. Holds the id of the nest this
  // modal session already created.
  const createdNestIdRef = useRef<number | null>(null);
  // Sheet-part tie: the pickable material parts, plus (edit mode) the OPEN
  // operation-scoped tie already on this nest's operation. Both are optional
  // chrome — a failed load hides the controls rather than blocking the nest.
  const [materials, setMaterials] = useState<Part[]>([]);
  const [existingTie, setExistingTie] = useState<MaterialAllocation | null>(null);

  const {
    register,
    handleSubmit,
    reset,
    setValue,
    getValues,
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
      material_part_id: '',
      qty_per_run: 1,
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
      // The tie is seeded asynchronously below; start from "untied" so a failed
      // read can never present someone else's tie as this nest's.
      material_part_id: '',
      qty_per_run: 1,
    });
    setPdfFile(null);
    setSubmitError('');
    setFileError('');
    setExtracting(false);
    setExtractHint(null);
    setFileInputKey((k) => k + 1);
    setExistingTie(null);
    createdNestIdRef.current = null;
  }, [open, nest, reset]);

  // Load the sheet-part options and — in edit mode, when the caller told us
  // which operation backs this nest — the tie already on it. Declared AFTER the
  // seed effect so its `reset` cannot clobber the values set here.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const parts = await api.getMaterials({ active_only: true, limit: MATERIAL_OPTION_LIMIT });
        if (!cancelled) setMaterials(parts ?? []);
      } catch {
        if (!cancelled) setMaterials([]);
      }
      if (!isEdit || workOrderOperationId == null) return;
      try {
        const ties = await api.getMaterialAllocations(workOrderId, false);
        if (cancelled) return;
        // OPEN + operation-scoped only: `closed` is never written (a fully
        // consumed tie stays `open`), and a work-order-scoped tie is not this
        // nest's tie.
        const tie =
          (ties ?? []).find((t) => t.status === 'open' && t.work_order_operation_id === workOrderOperationId) ?? null;
        setExistingTie(tie);
        if (tie) {
          setValue('material_part_id', tie.part_id);
          setValue('qty_per_run', tie.qty_per_run ?? 1);
        }
      } catch {
        if (!cancelled) setExistingTie(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, isEdit, workOrderId, workOrderOperationId, setValue]);

  const materialOptions = useMemo<MaterialOption[]>(() => {
    const options: MaterialOption[] = materials.map((part) => ({
      id: part.id,
      label: part.part_number ? `${part.part_number} — ${part.name}` : part.name,
    }));
    // Keep an existing tie's part selectable even when the (capped, filtered,
    // or failed) material load didn't return it — otherwise the picker reads as
    // untied while the tie is very much live.
    if (existingTie && !options.some((option) => option.id === existingTie.part_id)) {
      options.push({
        id: existingTie.part_id,
        label:
          [existingTie.part_number, existingTie.part_name].filter(Boolean).join(' — ') ||
          `Part ${existingTie.part_id}`,
      });
    }
    return options;
  }, [materials, existingTie]);

  // Ties are addressable on create (the backend ties the operation it creates)
  // and, in edit mode, only when the caller supplied the operation id.
  const showTieControls = materialOptions.length > 0 && (!isEdit || workOrderOperationId != null);

  /**
   * Fill the form from an extraction result WITHOUT clobbering anything the user
   * already typed: only empty text fields are filled. `cnc_number` is filled
   * whenever the response carries one (and the field is still empty).
   * `planned_runs` must stay an integer >= 1, so it is only adopted when the
   * field is still at its default and the response is a valid whole number.
   */
  const applyExtraction = (extracted: {
    cnc_number: string | null;
    material: string | null;
    thickness: string | null;
    sheet_size: string | null;
    planned_runs: number | null;
  }) => {
    const current = getValues();
    const fillIfEmpty = (field: 'cnc_number' | 'material' | 'thickness' | 'sheet_size', value: string | null) => {
      if (!value) return;
      const existing = String(current[field] ?? '').trim();
      if (existing) return; // never overwrite a value the user already typed
      setValue(field, value, { shouldDirty: true });
    };

    fillIfEmpty('cnc_number', extracted.cnc_number);
    fillIfEmpty('material', extracted.material);
    fillIfEmpty('thickness', extracted.thickness);
    fillIfEmpty('sheet_size', extracted.sheet_size);

    // planned_runs defaults to 1; adopt an extracted whole number >= 1 only when
    // the user hasn't changed it off the default.
    const runs = extracted.planned_runs;
    if (typeof runs === 'number' && Number.isInteger(runs) && runs >= 1 && Number(current.planned_runs) === 1) {
      setValue('planned_runs', runs, { shouldDirty: true });
    }
  };

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    setFileError('');
    setExtractHint(null);
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

    // Auto-extract only on the create path: edit keeps the existing nest's data.
    if (!file || isEdit) return;
    setExtracting(true);
    try {
      const extracted = await api.extractLaserNestFromPdf(file);
      applyExtraction(extracted);
      setExtractHint({
        confidence: extracted.confidence,
        source: extracted.source,
        warning: extracted.warning,
      });
    } catch {
      // Extraction is a convenience — a failure must not block manual entry.
      // The user can still type every field by hand and save as before.
      setExtractHint(null);
    } finally {
      setExtracting(false);
    }
  };

  /**
   * Bring the tie on this nest's operation in line with the form (edit path
   * only — the nest PATCH cannot carry a tie).
   *
   * `part_id` is fixed at creation, since changing what a tie points at after
   * consumption posted would rewrite genealogy — so a part swap is
   * untie-then-re-tie. Every verb is server-GATED (409 on untie after
   * consumption, 422 when the new plan sits under what was consumed), so this
   * stays strictly NON-optimistic: state moves only on what the server
   * returned, and a failure propagates to the caller's error banner. If the
   * untie lands and the re-tie is refused, the nest is genuinely untied — which
   * is what the banner then says.
   */
  const reconcileTie = async (data: LaserNestManualFormData) => {
    if (workOrderOperationId == null) return;
    const desiredPartId = data.material_part_id ?? null;
    const perRun = data.qty_per_run ?? 1;
    const planned = perRun * data.planned_runs;

    if (desiredPartId == null) {
      if (existingTie) {
        await api.deleteMaterialAllocation(workOrderId, existingTie.id);
        setExistingTie(null);
      }
      return;
    }

    if (existingTie && existingTie.part_id === desiredPartId) {
      if (existingTie.qty_per_run !== perRun || existingTie.qty_planned !== planned) {
        setExistingTie(
          await api.updateMaterialAllocation(workOrderId, existingTie.id, {
            qty_per_run: perRun,
            qty_planned: planned,
          })
        );
      }
      return;
    }

    // The swap is two verbs and there is no server-side swap (nor a RETURN verb
    // until PR 3), so the window between them is real. If the untie lands and
    // the re-tie is refused, the nest ends GENUINELY UNTIED and its material
    // will silently never deplete — the exact outcome this feature exists to
    // prevent. Say so explicitly rather than surfacing the bare server detail,
    // which would read as "nothing happened".
    const hadTie = existingTie !== null;
    if (existingTie) {
      await api.deleteMaterialAllocation(workOrderId, existingTie.id);
      setExistingTie(null);
    }
    try {
      setExistingTie(
        await api.createMaterialAllocation(workOrderId, {
          part_id: desiredPartId,
          work_order_operation_id: workOrderOperationId,
          source: 'nest',
          qty_per_run: perRun,
          qty_planned: planned,
        })
      );
    } catch (err: any) {
      if (!hadTie) throw err;
      const detail = toDisplayString(err?.response?.data?.detail) || 'the server refused it';
      throw Object.assign(
        new Error(
          `The previous sheet part was untied, but the new one could not be tied: ${detail} ` +
            'This nest is now UNTIED — its material will not deplete. Re-tie it before releasing the work.'
        ),
        // The submit handlers surface `response.data.detail` and fall back to a
        // generic string; a bare Error would be swallowed by that fallback and
        // this warning — the one that matters most — would never reach the user.
        { isTieSwapFailure: true }
      );
    }
  };

  const onSubmit = async (data: LaserNestManualFormData) => {
    setBusy(true);
    setSubmitError('');

    // --- Edit path: PATCH the changed fields, then reconcile the tie. ---
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
        if (showTieControls) await reconcileTie(data);
        onSaved();
        onClose();
      } catch (err: any) {
        setSubmitError(
          err?.isTieSwapFailure
            ? err.message
            : toDisplayString(err?.response?.data?.detail) || 'Failed to update laser nest'
        );
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
        // The tie keys ride on the create body — the backend ties the operation
        // it creates for this nest. Spread only when set, so an untied nest
        // POSTs exactly the body it did before this feature existed.
        const body: ManualNestCreateBody = {
          cnc_number: data.cnc_number,
          planned_runs: data.planned_runs,
          nest_name: data.nest_name,
          material: data.material,
          thickness: data.thickness,
          sheet_size: data.sheet_size,
          ...(data.material_part_id != null
            ? { material_part_id: data.material_part_id, qty_per_run: data.qty_per_run ?? 1 }
            : {}),
        };
        const created: LaserNestManualResponse = await api.createManualLaserNest(workOrderId, body);
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
      setSubmitError(
        err?.isTieSwapFailure
          ? err.message
          : toDisplayString(err?.response?.data?.detail) || 'Failed to add laser nest'
      );
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

          {showTieControls && (
            <>
              <label className="block">
                <span className={FIELD_LABEL}>Sheet part</span>
                <select {...register('material_part_id')} className="input mt-1 w-full">
                  <option value="">(none)</option>
                  {materialOptions.map((option) => (
                    <option key={option.id} value={String(option.id)}>
                      {option.label}
                    </option>
                  ))}
                </select>
                {errors.material_part_id && <p className={ERR}>{errors.material_part_id.message}</p>}
              </label>

              <label className="block">
                <span className={FIELD_LABEL}>Sheets per run</span>
                <input
                  type="number"
                  min={0}
                  step="any"
                  {...register('qty_per_run')}
                  className="input mt-1 w-full"
                />
                {errors.qty_per_run && <p className={ERR}>{errors.qty_per_run.message}</p>}
              </label>

              {/* Consumption fires when this nest's OPERATION completes, never
                  per run: reporting runs on a still-open nest deducts nothing,
                  because an IN_PROGRESS operation is still reducible and
                  consumption never auto-reverses. Say so plainly. */}
              <p className="text-xs text-fd-faint sm:col-span-2">
                Optional. Tied sheets leave inventory when this nest's operation completes, not per run. FIFO picks
                the lot at that moment.
              </p>
            </>
          )}

          {!isEdit && (
            <div className="block sm:col-span-2">
              {/* The label wraps ONLY the file input so the input's accessible
                  name stays "Reference PDF (optional)". The descriptive helper
                  text below must live OUTSIDE the label — it mentions "CNC
                  number", "material", and "size", and if it were inside the
                  label those words would leak into the file input's accessible
                  name and collide with the actual CNC/material/size fields. */}
              <label className="block">
                <span className={FIELD_LABEL}>Reference PDF (optional)</span>
                <input
                  key={fileInputKey}
                  type="file"
                  accept="application/pdf"
                  onChange={handleFileChange}
                  disabled={extracting}
                  aria-label="Reference PDF (optional)"
                  className="mt-1 block w-full text-sm text-fd-body file:mr-3 file:rounded file:border-0 file:bg-fd-raised file:px-3 file:py-2 file:text-sm file:font-semibold file:text-fd-ink hover:file:bg-fd-line-bright disabled:opacity-60"
                />
              </label>
              {pdfFile && <p className="mt-1 text-xs text-fd-mute">{pdfFile.name}</p>}
              {fileError && <p className={ERR}>{fileError}</p>}
              <p className="mt-1 text-xs text-fd-faint">
                Drop the nest report PDF here and we&rsquo;ll read the CNC number, material, and size for you.
              </p>
            </div>
          )}
        </div>

        {!isEdit && extracting && (
          <div
            className="flex items-center gap-2 rounded border border-fd-blue/40 bg-fd-blue/10 px-3 py-2 text-sm text-fd-body"
            role="status"
            aria-live="polite"
          >
            <svg className="h-4 w-4 animate-spin text-fd-blue" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
            </svg>
            Extracting fields from the PDF&hellip;
          </div>
        )}

        {!isEdit && !extracting && extractHint && (
          <ExtractionHint hint={extractHint} onDismiss={() => setExtractHint(null)} />
        )}

        {submitError && (
          <div className="rounded border border-fd-red/40 bg-fd-red/10 px-3 py-2 text-sm text-fd-red">
            {submitError}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" onClick={onClose} disabled={busy} className="btn-secondary">
            Cancel
          </button>
          <button type="submit" disabled={busy || extracting} className="btn-primary">
            {busy ? 'Saving…' : extracting ? 'Extracting…' : isEdit ? 'Save changes' : 'Add nest'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Dismissible banner shown after a successful auto-extract. Wording escalates
 * with how reliable the read was: a filename-only fallback or a low-confidence
 * read tells the planner to fill in material/size by hand; a clean AI read just
 * asks them to verify. Any model-supplied warning is appended.
 */
function ExtractionHint({ hint, onDismiss }: { hint: ExtractHint; onDismiss: () => void }) {
  const cautious = hint.source === 'filename' || hint.confidence === 'low';
  const badge = hint.confidence ? CONFIDENCE_BADGE[hint.confidence] : null;

  const message =
    hint.source === 'filename'
      ? 'Only the CNC number could be read from the filename — please fill in material and size.'
      : cautious
        ? 'Low-confidence AI read — double-check every field before saving.'
        : 'AI-filled from the PDF — verify before saving.';

  const tone = cautious
    ? 'border-fd-amber/40 bg-fd-amber/10'
    : 'border-fd-green/40 bg-fd-green/10';

  return (
    <div className={`rounded border px-3 py-2 text-sm text-fd-body ${tone}`} role="status">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-fd-ink">{message}</span>
            {badge && (
              <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badge.className}`}>
                {badge.label}
              </span>
            )}
          </div>
          {hint.warning && <p className="text-xs text-fd-mute">{hint.warning}</p>}
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="shrink-0 text-fd-mute hover:text-fd-ink"
          aria-label="Dismiss extraction hint"
        >
          &times;
        </button>
      </div>
    </div>
  );
}
