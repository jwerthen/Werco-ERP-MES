import React, { useEffect, useRef, useState } from 'react';
import { useForm, Controller } from 'react-hook-form';
import { z } from 'zod';
import api from '../../services/api';
import { Button, FormField, Modal } from '../ui';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { centralWallClockToUtcISO, getCentralNowDateTimeLocal } from '../../utils/centralTime';
import { STOCK_PIECE_ADVISORY } from '../../types/stockPiece';
import type {
  AppendStockPieceObservation,
  CreateStockPiece,
  StockPieceDetail,
  StockPieceEvidence,
  StockPieceSource,
} from '../../types/stockPiece';
import { emptyEvidence, observationFieldsSchema, stockPieceEvidenceSchema } from '../../validation/stockPiece';
import StockPieceSourcePicker from './StockPieceSourcePicker';
import { StockPieceGeometryPreview, StockPieceShapeEditor } from './StockPieceGeometry';
import { checkSavedObservation, observationError } from './stockPieceHelpers';

type Fields = {
  label: string;
  reason: string;
  observer_name: string;
  observed_at: string;
  evidence: StockPieceEvidence;
};
type Pending = { pieceId?: number; command: CreateStockPiece | AppendStockPieceObservation };
export default function StockPieceObservationEditor({
  companyId,
  canRecord,
  previous,
  withdraw = false,
  onClose,
  onSaved,
}: {
  companyId: number;
  canRecord: boolean;
  previous?: StockPieceDetail;
  withdraw?: boolean;
  onClose: () => void;
  onSaved: (value: StockPieceDetail) => void;
}) {
  const {
    register,
    control,
    handleSubmit,
    formState: { isDirty },
  } = useForm<Fields>({
    defaultValues: {
      label: previous?.label ?? '',
      reason: '',
      observer_name: '',
      observed_at: '',
      evidence: previous ? (JSON.parse(JSON.stringify(previous.evidence)) as StockPieceEvidence) : emptyEvidence(),
    },
  });
  const [source, setSource] = useState<StockPieceSource | null>(null);
  const [sourceAllowed, setSourceAllowed] = useState(withdraw);
  const [pending, setPending] = useState<Pending | null>(null);
  const [saving, setSaving] = useState(false),
    [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const controller = useRef<AbortController | null>(null),
    live = useRef(true),
    busy = useRef(false);
  const { confirmDiscard, markSaved } = useUnsavedChanges(
    isDirty || source !== null || pending !== null,
    pending
      ? 'This save may have completed. Retry the same request to recover its receipt before leaving. Leave anyway?'
      : undefined
  );
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      controller.current?.abort();
    };
  }, []);
  const close = () => {
    if (!saving && confirmDiscard()) onClose();
  };
  const title = withdraw ? 'Withdraw observation' : previous ? 'Record correction' : 'Record piece observation';

  async function save(request: Pending) {
    if (busy.current || !canRecord) return;
    busy.current = true;
    setSaving(true);
    setError('');
    setConflict(false);
    const abort = new AbortController();
    controller.current = abort;
    try {
      const result =
        request.pieceId === undefined
          ? await api.createStockPiece(request.command as CreateStockPiece, abort.signal)
          : await api.appendStockPieceObservation(
              request.pieceId,
              request.command as AppendStockPieceObservation,
              abort.signal
            );
      if (!live.current || abort.signal.aborted) return;
      checkSavedObservation(result, request.command, companyId, previous);
      markSaved();
      onSaved(result);
    } catch (cause) {
      if (live.current && !abort.signal.aborted) {
        setError(observationError(cause));
        const status = (cause as { response?: { status?: number } })?.response?.status;
        setConflict(status === 409 || status === 422 || status === 400);
      }
    } finally {
      busy.current = false;
      if (live.current && !abort.signal.aborted) setSaving(false);
    }
  }
  const submit = handleSubmit(values => {
    try {
      if (pending) {
        void save(pending);
        return;
      }
      const fields = observationFieldsSchema.parse(values);
      const instant = centralWallClockToUtcISO(fields.observed_at);
      if (!instant || getCentralNowDateTimeLocal(instant) !== fields.observed_at)
        throw new Error('Enter a valid observed date and time in Central time.');
      const base = {
        expected_company_id: companyId,
        request_key: crypto.randomUUID(),
        reason: fields.reason,
        observed_at: instant,
        observer_name: fields.observer_name,
      };
      let command: CreateStockPiece | AppendStockPieceObservation;
      if (withdraw && previous) command = { ...base, state: 'WITHDRAWN', expected_version: previous.piece_version };
      else {
        if (!source || !sourceAllowed)
          throw new Error('Explicitly select a current ERP source before recording this observation.');
        const recorded = {
          ...base,
          state: 'RECORDED' as const,
          source_inventory_item_id: source.inventory_item_id,
          source_part_id: source.part_id,
          expected_source_sha256: source.source_sha256,
          evidence: stockPieceEvidenceSchema.parse(values.evidence),
        };
        command = previous
          ? { ...recorded, expected_version: previous.piece_version }
          : { ...recorded, label: fields.label };
      }
      const next = { pieceId: previous?.piece_id, command };
      setPending(next);
      void save(next);
    } catch (cause) {
      setError(
        cause instanceof z.ZodError
          ? cause.issues.map(issue => `${issue.path.join('.')}: ${issue.message}`).join(' ')
          : observationError(cause)
      );
    }
  });

  return (
    <Modal open onClose={close} size="5xl" ariaLabel={title}>
      <form onSubmit={submit} className="space-y-5 text-slate-200">
        <div>
          <h2 className="text-xl font-semibold text-slate-100">{title}</h2>
          <p className="text-sm text-amber-300 mt-1">{STOCK_PIECE_ADVISORY}</p>
        </div>
        {previous && (
          <p className="text-sm text-slate-400">
            {previous.label} · based on observation {previous.observation_number}. A newer revision will cause a
            conflict; open the latest history before trying again.
          </p>
        )}
        {withdraw && (
          <p className="text-sm text-slate-300">
            The existing measurement and source snapshot will be retained unchanged. Withdrawal only withdraws this
            observation; it does not dispose of or move material.
          </p>
        )}
        <fieldset disabled={saving || pending !== null || !canRecord} className="space-y-5 min-w-0">
          {!previous && (
            <FormField
              label="Physical piece label"
              required
              help="Enter the actual tag or label. Labels are case-sensitive and must be unique in this company."
            >
              {field => <input {...field} {...register('label')} className="input" maxLength={120} />}
            </FormField>
          )}
          {!withdraw && (
            <StockPieceSourcePicker
              companyId={companyId}
              value={source}
              onChange={setSource}
              initialItemId={previous?.source_inventory_item_id}
              onCapability={setSourceAllowed}
            />
          )}
          {!withdraw && (
            <Controller
              control={control}
              name="evidence"
              render={({ field: { value, onChange } }) => {
                const set = <K extends keyof StockPieceEvidence>(key: K, next: StockPieceEvidence[K]) =>
                  onChange({ ...value, [key]: next });
                return (
                  <div className="space-y-4 border-t border-slate-700 pt-4">
                    <h3 className="font-semibold text-slate-100">Reported physical measurements</h3>
                    <p className="text-sm text-slate-400">
                      Enter all geometry in inches. Decimals and exact fractions are accepted. Empty optional fields
                      mean unknown. ERP source values do not populate measurements.
                    </p>
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
                      <StockPieceShapeEditor
                        value={value.geometry}
                        onChange={shape => set('geometry', shape)}
                        unknownDisabled={value.unavailable_zones.length > 0}
                      />
                      <StockPieceGeometryPreview shape={value.geometry} zones={value.unavailable_zones} />
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <FormField label="Measurement method" required>
                        {f => (
                          <input
                            {...f}
                            className="input"
                            maxLength={120}
                            value={value.measurement_method}
                            onChange={e => set('measurement_method', e.target.value)}
                            placeholder="Describe how this piece was measured"
                          />
                        )}
                      </FormField>
                      <FormField
                        label="Original measurement units"
                        help="This records evidence provenance. Entries on this form are always inches."
                      >
                        {f => (
                          <select
                            {...f}
                            className="input"
                            value={value.source_units}
                            onChange={e => set('source_units', e.target.value as StockPieceEvidence['source_units'])}
                          >
                            <option value="unknown">Unknown</option>
                            <option value="in">Inches</option>
                            <option value="mm">Millimeters, explicitly converted to inches</option>
                          </select>
                        )}
                      </FormField>
                      <FormField label="Reported thickness (in)">
                        {f => (
                          <input
                            {...f}
                            className="input"
                            maxLength={80}
                            value={value.thickness ?? ''}
                            onChange={e => set('thickness', e.target.value || null)}
                            placeholder="Unknown"
                          />
                        )}
                      </FormField>
                      <FormField label="Reported grade">
                        {f => (
                          <input
                            {...f}
                            className="input"
                            maxLength={120}
                            value={value.grade ?? ''}
                            onChange={e => set('grade', e.target.value || null)}
                            placeholder="Unknown"
                          />
                        )}
                      </FormField>
                      <FormField label="Reported grain axis">
                        {f => (
                          <select
                            {...f}
                            className="input"
                            value={value.grain_axis ?? ''}
                            onChange={e =>
                              set('grain_axis', e.target.value === 'x' ? 'x' : e.target.value === 'y' ? 'y' : null)
                            }
                          >
                            <option value="">Unknown</option>
                            <option value="x">Horizontal X in reported outline</option>
                            <option value="y">Vertical Y in reported outline</option>
                          </select>
                        )}
                      </FormField>
                    </div>
                    <details className="space-y-3">
                      <summary className="cursor-pointer text-blue-300">
                        Reported location, ownership and certification notes
                      </summary>
                      {(['location_note', 'ownership_note', 'certification_note'] as const).map((key, i) => (
                        <FormField
                          key={key}
                          label={['Observed location note', 'Ownership note', 'Certification evidence note'][i]}
                          help={
                            key === 'certification_note'
                              ? 'A note is not a verified certificate or approval.'
                              : undefined
                          }
                        >
                          {f => (
                            <textarea
                              {...f}
                              className="input"
                              maxLength={1000}
                              rows={2}
                              value={value[key] ?? ''}
                              onChange={e => set(key, e.target.value || null)}
                              placeholder="Unknown"
                            />
                          )}
                        </FormField>
                      ))}
                    </details>
                    <details className="space-y-3">
                      <summary className="cursor-pointer text-blue-300">
                        Reported unavailable zones ({value.unavailable_zones.length}/16)
                      </summary>
                      <p className="text-sm text-slate-400">
                        Enter measured areas to flag for review. Containment and geometry have not been independently
                        verified.
                      </p>
                      {value.unavailable_zones.map((zone, index) => {
                        const update = (next: typeof zone) =>
                          set(
                            'unavailable_zones',
                            value.unavailable_zones.map((old, i) => (i === index ? next : old))
                          );
                        return (
                          <section key={zone.id} className="border border-slate-700 p-3 space-y-3">
                            <h4 className="font-medium">Zone {index + 1}</h4>
                            <FormField label={`Zone ${index + 1} label`} required>
                              {f => (
                                <input
                                  {...f}
                                  className="input"
                                  maxLength={120}
                                  value={zone.label}
                                  onChange={e => update({ ...zone, label: e.target.value })}
                                />
                              )}
                            </FormField>
                            <FormField label={`Zone ${index + 1} reason`} required>
                              {f => (
                                <textarea
                                  {...f}
                                  className="input"
                                  rows={2}
                                  maxLength={1000}
                                  value={zone.reason}
                                  onChange={e => update({ ...zone, reason: e.target.value })}
                                />
                              )}
                            </FormField>
                            <StockPieceShapeEditor
                              zone
                              value={
                                zone.outline.kind === 'circle'
                                  ? zone.outline
                                  : { kind: 'polygon', outer: zone.outline.pts, holes: [] }
                              }
                              onChange={shape => {
                                if (shape.kind === 'circle' || shape.kind === 'polygon')
                                  update({
                                    ...zone,
                                    outline: shape.kind === 'circle' ? shape : { kind: 'polygon', pts: shape.outer },
                                  });
                              }}
                            />
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                set(
                                  'unavailable_zones',
                                  value.unavailable_zones.filter((_, i) => i !== index)
                                )
                              }
                            >
                              Remove zone {index + 1}
                            </Button>
                          </section>
                        );
                      })}
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={value.geometry.kind === 'unknown' || value.unavailable_zones.length >= 16}
                        onClick={() =>
                          set('unavailable_zones', [
                            ...value.unavailable_zones,
                            {
                              id: crypto.randomUUID(),
                              label: '',
                              reason: '',
                              outline: { kind: 'circle', cx: '0', cy: '0', r: '' },
                            },
                          ])
                        }
                      >
                        Add unavailable zone
                      </Button>
                    </details>
                  </div>
                );
              }}
            />
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <FormField
              label="Observer name"
              required
              help="The person who made this observation. The authenticated recorder is stored separately."
            >
              {field => <input {...field} {...register('observer_name')} className="input" maxLength={120} />}
            </FormField>
            <FormField label="Observed at (Central time)" required>
              {field => <input {...field} {...register('observed_at')} type="datetime-local" className="input" />}
            </FormField>
          </div>
          <FormField label={withdraw ? 'Withdrawal reason' : 'Evidence / correction reason'} required>
            {field => <textarea {...field} {...register('reason')} className="input" rows={3} maxLength={1000} />}
          </FormField>
        </fieldset>
        {!withdraw && pending?.command.state === 'RECORDED' && (
          <p className="text-xs text-slate-400">
            Exact inch values have been captured for this request. Retry sends the same evidence and request key.
          </p>
        )}
        {error && (
          <p role="alert" className="text-red-300 whitespace-pre-wrap">
            {error}
          </p>
        )}
        {pending && (
          <div className="border border-amber-600/50 bg-amber-950/20 p-3 text-sm space-y-2">
            <p className="text-amber-200">
              {saving
                ? 'Saving this observation…'
                : 'This request is retained. Retry to recover its receipt; an interrupted response does not prove that nothing was saved.'}
            </p>
            <p className="text-xs break-all text-slate-400">Request key: {pending.command.request_key}</p>
            {conflict && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setPending(null);
                  setConflict(false);
                  setError(
                    'Review the existing history and refresh/reselect the source before sending changed evidence. The original version remains in force.'
                  );
                }}
              >
                Review and edit rejected request
              </Button>
            )}
          </div>
        )}
        <div className="flex flex-wrap justify-end gap-3">
          <Button variant="secondary" disabled={saving} onClick={close}>
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={saving || !canRecord || (!withdraw && !pending && (!source || !sourceAllowed))}
          >
            {saving ? 'Saving…' : pending ? 'Retry same request' : withdraw ? 'Record withdrawal' : 'Save observation'}
          </Button>
        </div>
        {!withdraw && (
          <p className="text-xs text-slate-400">
            Up to 2,000 source vertices across the outline, holes and zones; 128 KiB evidence limit. No physical
            availability, reservation, balance, valuation or remnant credit is created.
          </p>
        )}
      </form>
    </Modal>
  );
}
