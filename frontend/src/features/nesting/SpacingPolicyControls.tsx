import React, { useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import api from '../../services/api';
import { formatCentralDateTime } from '../../utils/centralTime';
import type { NestingPolicyResolution } from '../../types/nestingPolicy';
import type { Quote } from './lib/quoting';
import { inToMm, mmToIn } from './lib/units';
import { policyThicknessIn, validatePolicySnapshot, type PolicyMaterial } from './lib/spacing-policy';
import { canonicalJSON } from './lib/provenance';
import { nestingApiMessage } from './useNestingCatalog';
import SpacingPolicyManager from './SpacingPolicyManager';

const reasonSchema = z.object({ reason: z.string().trim().min(1, 'Enter an estimator reason.').max(1000) });

export default function SpacingPolicyControls({
  quote,
  companyId,
  canManage,
  onChange,
}: {
  quote: Quote;
  companyId?: number;
  canManage: boolean;
  onChange: (patch: Partial<Quote>) => void;
}) {
  const [resolution, setResolution] = useState<NestingPolicyResolution | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [custom, setCustom] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const form = useForm<{ reason: string }>({ resolver: zodResolver(reasonSchema), defaultValues: { reason: '' } });
  const identity = JSON.stringify([companyId, quote.material, quote.thickness]);
  useEffect(() => {
    setResolution(null);
    setError('');
    setBusy(false);
    setCustom(false);
    controller.current?.abort();
    return () => controller.current?.abort();
  }, [identity]);

  async function resolve() {
    if (!companyId || busy) return;
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setBusy(true);
    setError('');
    setResolution(null);
    try {
      const result = await api.resolveNestingSpacingPolicy(
        { material: quote.material as PolicyMaterial, thickness_in: policyThicknessIn(mmToIn(quote.thickness)) },
        request.signal
      );
      if (request.signal.aborted) return;
      if (
        result.schema_version !== 1 ||
        !['resolved', 'unmatched', 'unavailable'].includes(result.status) ||
        (result.status === 'resolved') !== !!result.policy
      )
        throw new Error('Invalid spacing policy response.');
      if (result.policy) {
        if (result.policy.company_id !== companyId) throw new Error('The policy belongs to another company.');
        validatePolicySnapshot(
          result.policy,
          quote.material,
          quote.thickness,
          inToMm(Number(result.policy.gap_in)),
          inToMm(Number(result.policy.margin_in))
        );
      }
      setResolution(result);
    } catch (cause) {
      if (!request.signal.aborted) setError(nestingApiMessage(cause));
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }

  const snapshot = quote.spacingPolicy;
  const source = resolution?.policy;
  const checked =
    !!snapshot && !!source && canonicalJSON({ ...snapshot, resolved_at: source.resolved_at }) === canonicalJSON(source);
  return (
    <section className="spacing-policy-controls" aria-label="Quote spacing policy">
      <strong>
        {snapshot
          ? `Policy revision ${snapshot.revision_number} applied`
          : quote.spacingOverride
            ? 'Custom quoting allowances'
            : 'Unreviewed starting allowances'}
      </strong>
      {snapshot && (
        <p className="helper inset-free">
          {checked
            ? 'Current publication checked.'
            : 'Historical snapshot; check the current policy before saving or starting a new server calculation.'}{' '}
          Family: {snapshot.band.material}. Thickness {snapshot.band.thickness_min_in} to less than{' '}
          {snapshot.band.thickness_max_in} in.
        </p>
      )}
      {quote.spacingOverride && (
        <p className="helper inset-free">
          Estimator reason: {quote.spacingOverride.reason}. Policy conformance is cleared.
        </p>
      )}
      <button className="secondary compact" disabled={!companyId || busy} onClick={() => void resolve()}>
        {busy ? 'Checking policy…' : 'Check current spacing policy'}
      </button>
      {resolution && (
        <p role="status" className="helper inset-free">
          {resolution.explanation}
        </p>
      )}
      {source && (
        <div className="policy-resolution">
          <strong>
            Gap {source.gap_in} in · edge margin {source.margin_in} in
          </strong>
          <p className="helper inset-free">
            Gap = max({source.band.minimum_gap_in} in, thickness × {source.band.gap_thickness_multiplier}). Margin =
            max({source.band.minimum_margin_in} in, thickness × {source.band.margin_thickness_multiplier}).
          </p>
          <p className="helper inset-free">
            Revision {source.revision_number} · checked {formatCentralDateTime(source.resolved_at)} Central
          </p>
          <button
            className="primary compact"
            onClick={() =>
              onChange({
                spacingMode: 'policy',
                spacingPolicy: source,
                spacingOverride: undefined,
                gap: inToMm(Number(source.gap_in)),
                margin: inToMm(Number(source.margin_in)),
              })
            }
          >
            Apply these allowances
          </button>
        </div>
      )}
      {snapshot && !custom && (
        <button
          className="secondary compact"
          onClick={() => {
            form.reset({ reason: '' });
            setCustom(true);
          }}
        >
          Use custom spacing
        </button>
      )}
      {custom && (
        <form
          onSubmit={form.handleSubmit(values => {
            onChange({
              spacingPolicy: undefined,
              spacingMode: 'manual',
              spacingOverride: { schema_version: 1, reason: values.reason, changed_at: new Date().toISOString() },
            });
            setCustom(false);
          })}
        >
          <label className="field-label">
            Estimator reason
            <textarea {...form.register('reason')} maxLength={1000} rows={3} />
          </label>
          {form.formState.errors.reason && <p role="alert">{form.formState.errors.reason.message}</p>}
          <p className="helper inset-free">
            This clears policy conformance and unlocks material, thickness and custom spacing. Save a new revision to
            retain the reason.
          </p>
          <button className="primary compact" type="submit">
            Use custom allowances
          </button>
          <button className="secondary compact" type="button" onClick={() => setCustom(false)}>
            Keep applied policy
          </button>
        </form>
      )}
      {error && (
        <p className="team-draft-error" role="alert">
          {error}
        </p>
      )}
      {companyId && (
        <SpacingPolicyManager
          companyId={companyId}
          canManage={canManage}
          onChanged={() => {
            controller.current?.abort();
            setBusy(false);
            setResolution(null);
            setError('');
          }}
        />
      )}
      <p className="helper inset-free">
        Quoting policy only. Grade, certification, inventory and quote approval are separate.
      </p>
    </section>
  );
}
