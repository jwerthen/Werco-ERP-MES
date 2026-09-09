import React, { useEffect, useId, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { useCompany } from '../../context/CompanyContext';
import type { StockPiecePage, StockPieceSummary } from '../../types/stockPiece';
import { REMNANT_PLANNING_ADVISORY, type RemnantPlan, type RemnantResolution } from '../../types/remnantPlanning';
import { checkObservation, checkObservationPage, observationError } from '../../components/inventory/stockPieceHelpers';
import { StockPieceGeometryPreview } from '../../components/inventory/StockPieceGeometry';
import {
  buildRemnantPlan,
  canonicalRemnantEvidence,
  defaultZoneClearance,
  remnantTarget,
  validateRemnantResolution,
} from './lib/remnant-evidence';

type Props = {
  companyId: number;
  groupId: string;
  /** Exact quoteToFile output. The picker never rewrites or converts this fingerprint input. */
  quote: unknown;
  onSelect: (plan: RemnantPlan) => void;
  onCancel: () => void;
};
const fields = z.object({
  family: z.enum(['Carbon steel', 'Stainless steel', 'Aluminum'], { error: 'Assign the material family explicitly.' }),
  requiredGrade: z.string().trim().min(1, 'Enter the grade required for this entire group.').max(120),
  reason: z.string().trim().min(1, 'Record the basis for this planning assignment.').max(1000),
  zoneClearanceIn: z.string().min(1, 'Enter additional zone clearance in inches.').max(80),
});
type FormValues = z.infer<typeof fields>;

/** Isolated read-only picker. It does not mutate inventory or expose a working comparison. */
export default function RemnantPiecePicker(props: Props) {
  const { user } = useAuth();
  const { currentCompany } = useCompany();
  if (!user || currentCompany?.id !== props.companyId)
    return <p role="alert">Select this nest’s company to review recorded pieces.</p>;
  let signature: string;
  try {
    signature = canonicalRemnantEvidence({ groupId: props.groupId, quote: props.quote });
    remnantTarget(props.quote);
  } catch (error) {
    return <p role="alert">{observationError(error)}</p>;
  }
  return <PiecePicker key={`${user.id}:${props.companyId}:${props.groupId}`} {...props} signature={signature} />;
}

function PiecePicker({ companyId, groupId, quote, onSelect, onCancel, signature }: Props & { signature: string }) {
  const fieldId = useId();
  const [page, setPage] = useState(1),
    [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<StockPiecePage<StockPieceSummary> | null>(null);
  const [resolved, setResolved] = useState<{ value: RemnantResolution; signature: string } | null>(null);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  const singleFlight = useRef(false);
  const lifetime = useRef({ mounted: true, signature });
  lifetime.current.signature = signature;
  const initialClearance = defaultZoneClearance(quote);
  const form = useForm<FormValues>({
    resolver: zodResolver(fields),
    defaultValues: {
      requiredGrade: '',
      reason: '',
      zoneClearanceIn: defaultZoneClearance(quote),
    },
  });
  const { reset } = form;
  const target = remnantTarget(quote);
  const current = resolved?.signature === signature ? resolved.value : null;

  useEffect(() => {
    lifetime.current.mounted = true;
    return () => {
      lifetime.current.mounted = false;
      pending.current?.abort();
    };
  }, []);
  useEffect(() => {
    pending.current?.abort();
    singleFlight.current = false;
    setResolved(null);
    setBusy(false);
    setError('');
    reset({ requiredGrade: '', reason: '', zoneClearanceIn: initialClearance });
  }, [signature, initialClearance, reset]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setResult(null);
    setError('');
    api
      .getStockPieces(page, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        checkObservationPage(value, companyId);
        if (value.page !== page || value.per_page !== 20 || value.items.length > 20 || value.total < value.items.length)
          throw new Error('The recorded-piece page is inconsistent. Refresh to retry.');
        value.items.forEach(item => checkObservation(item, companyId));
        if (new Set(value.items.map(item => item.piece_id)).size !== value.items.length)
          throw new Error('The recorded-piece page contains duplicate identities.');
        setResult(value);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(observationError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [companyId, page, refresh]);

  const valid = (controller: AbortController, frozen: string) =>
    lifetime.current.mounted && !controller.signal.aborted && lifetime.current.signature === frozen;
  async function resolve(
    pieceId: number,
    observation: number,
    payload: string,
    source: string,
    controller: AbortController
  ) {
    const request = {
      expected_company_id: companyId,
      expected_payload_sha256: payload,
      expected_source_sha256: source,
    };
    const response = await api.resolveRemnantPlanningSnapshot(pieceId, observation, request, controller.signal);
    return validateRemnantResolution(response, { ...request, pieceId, observationNumber: observation });
  }
  async function inspect(row: StockPieceSummary) {
    if (singleFlight.current) return;
    singleFlight.current = true;
    pending.current?.abort();
    const controller = new AbortController(),
      frozen = signature;
    pending.current = controller;
    setBusy(true);
    setError('');
    setResolved(null);
    try {
      const response = await resolve(
        row.piece_id,
        row.observation_number,
        row.payload_sha256,
        row.source_sha256,
        controller
      );
      if (valid(controller, frozen)) {
        setResolved({ value: response, signature: frozen });
        reset({ requiredGrade: '', reason: '', zoneClearanceIn: defaultZoneClearance(quote) });
      }
    } catch (cause) {
      if (valid(controller, frozen)) setError(observationError(cause));
    } finally {
      if (pending.current === controller) {
        singleFlight.current = false;
        if (valid(controller, frozen)) setBusy(false);
      }
    }
  }
  async function select(values: FormValues) {
    if (!current || singleFlight.current) return;
    singleFlight.current = true;
    const controller = new AbortController(),
      frozen = signature;
    pending.current = controller;
    setBusy(true);
    setError('');
    try {
      const snapshot = current.snapshot;
      // Selection requires another fresh as-of check; the first preview is not a lasting availability claim.
      const response = await resolve(
        snapshot.pieceId,
        snapshot.observationNumber,
        snapshot.payloadSha256,
        snapshot.sourceSha256,
        controller
      );
      const plan = await buildRemnantPlan({ resolution: response, companyId, groupId, quote, ...values });
      if (valid(controller, frozen)) onSelect(plan);
    } catch (cause) {
      if (valid(controller, frozen)) setError(observationError(cause));
    } finally {
      if (pending.current === controller) {
        singleFlight.current = false;
        if (valid(controller, frozen)) setBusy(false);
      }
    }
  }
  const fieldError = (name: keyof FormValues) => form.formState.errors[name]?.message;
  return (
    <section className="cad-source-panel" aria-label="Recorded piece planning">
      <div className="team-draft-actions">
        <button
          type="button"
          className="secondary"
          onClick={() => {
            pending.current?.abort();
            onCancel();
          }}
        >
          Cancel selection
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy || loading}
          onClick={() => {
            setResolved(null);
            setRefresh(value => value + 1);
          }}
        >
          Refresh recorded pieces
        </button>
      </div>
      <h3>Plan with one recorded piece</h3>
      <p className="cad-source-disclosure">
        {REMNANT_PLANNING_ADVISORY} The piece may be used once in a conditional plan; the full-sheet baseline stays
        available. This does not reserve or consume material.
      </p>
      <p>
        Target group: {target.family} · {target.thicknessIn} in thick. Geometry must pass the nesting checks before a
        layout can use it.
      </p>
      {error && (
        <p role="alert" className="team-draft-error">
          {error}
        </p>
      )}
      {loading && <p role="status">Loading recorded pieces…</p>}
      {busy && <p role="status">Checking the exact observation and source…</p>}
      {result && !result.items.length && (
        <p>No recorded pieces on this page. Record measured evidence in Inventory first.</p>
      )}
      {result && (
        <ul className="cad-source-list">
          {result.items.map(row => (
            <li key={row.piece_id} className="cad-source-row">
              <strong>{row.label}</strong>
              <span>
                Observation {row.observation_number} · {row.observed_at}
              </span>
              <p>
                {row.state === 'WITHDRAWN'
                  ? 'Withdrawn — history only.'
                  : row.source_status !== 'unchanged'
                    ? `Source ${row.source_status} — record and review a current observation.`
                    : 'Reported evidence; inspect specification and shape.'}
              </p>
              <button
                type="button"
                className="secondary"
                disabled={busy || row.state !== 'RECORDED' || row.source_status !== 'unchanged'}
                onClick={() => void inspect(row)}
              >
                Review {row.label}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="team-draft-actions">
        <button
          type="button"
          className="secondary"
          disabled={busy || loading || page <= 1}
          onClick={() => setPage(value => value - 1)}
        >
          Previous pieces
        </button>
        <span>Page {page}</span>
        <button
          type="button"
          className="secondary"
          disabled={busy || loading || !result || page * result.per_page >= result.total}
          onClick={() => setPage(value => value + 1)}
        >
          Next pieces
        </button>
      </div>
      {current && (
        <form onSubmit={form.handleSubmit(select)}>
          <h4>
            {current.snapshot.label} · observation {current.snapshot.observationNumber}
          </h4>
          <p>
            Reported grade: {current.snapshot.evidence.grade}; thickness: {current.snapshot.evidence.thickness} in;
            grain: {current.snapshot.evidence.grain_axis ?? 'unknown'}.
          </p>
          <StockPieceGeometryPreview
            shape={current.snapshot.evidence.geometry}
            zones={current.snapshot.evidence.unavailable_zones}
          />
          <p>
            Source checked {current.checked_at}. Matching text and geometry do not certify material or its physical
            presence.
          </p>
          <details>
            <summary>Source identity and review notes</summary>
            <p>
              Inventory source #{current.snapshot.sourceInventoryItemId} · Part #{current.snapshot.sourcePartId} ·{' '}
              {current.snapshot.sourceEvidence.part.part_number}
            </p>
            <code className="cad-source-hash">{current.snapshot_sha256}</code>
            <ul>
              {current.review_issues.map((issue, index) => (
                <li key={index}>{issue}</li>
              ))}
            </ul>
          </details>
          <fieldset disabled={busy}>
            <label className="field-label" htmlFor={`${fieldId}-family`}>
              Assign material family
              <select id={`${fieldId}-family`} {...form.register('family')} defaultValue="">
                <option value="" disabled>
                  Choose explicitly
                </option>
                {['Carbon steel', 'Stainless steel', 'Aluminum'].map(value => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            {fieldError('family') && <p role="alert">{fieldError('family')}</p>}
            <label className="field-label" htmlFor={`${fieldId}-grade`}>
              Required grade for this entire group
              <input id={`${fieldId}-grade`} maxLength={120} {...form.register('requiredGrade')} />
            </label>
            {fieldError('requiredGrade') && <p role="alert">{fieldError('requiredGrade')}</p>}
            <label className="field-label" htmlFor={`${fieldId}-reason`}>
              Assignment reason
              <textarea id={`${fieldId}-reason`} maxLength={1000} {...form.register('reason')} />
            </label>
            {fieldError('reason') && <p role="alert">{fieldError('reason')}</p>}
            <label className="field-label" htmlFor={`${fieldId}-clearance`}>
              Additional unavailable-zone clearance (in)
              <input id={`${fieldId}-clearance`} inputMode="decimal" {...form.register('zoneClearanceIn')} />
            </label>
            {fieldError('zoneClearanceIn') && <p role="alert">{fieldError('zoneClearanceIn')}</p>}
            <p>
              The default is the group edge margin, rounded upward only when needed to represent nine decimal places.
              Outer and physical-hole edges retain the group margin; zone clearance is separately editable.
            </p>
            <button type="submit" className="primary">
              Select this piece for the group
            </button>
          </fieldset>
        </form>
      )}
    </section>
  );
}
