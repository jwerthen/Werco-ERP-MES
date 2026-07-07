import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircleIcon,
  ClipboardDocumentCheckIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { EmptyState, ErrorState, SkeletonText } from '../ui';
import { formatCentralDateTime } from '../../utils/centralTime';
import StepTypeBadge from './StepTypeBadge';
import type { OperationStepRecord, OperationStepWithState, OperationStepsView } from '../../types/processSheet';

/**
 * Tooltip text for the warn-and-record qualification marker: the exception
 * messages frozen on the record at capture time (supervision signal — the
 * record itself was never blocked).
 */
function qualificationTitle(record: OperationStepRecord): string {
  const messages = (record.qualification_snapshot?.exceptions ?? [])
    .map((exc) => (typeof exc.message === 'string' ? exc.message : ''))
    .filter((m) => m.length > 0);
  return messages.length > 0
    ? messages.join('; ')
    : 'Operator qualification exceptions were recorded at capture time';
}

interface OperationStepsPanelProps {
  operationId: number;
}

/** "LSL 0.4980 · NOM 0.5000 · USL 0.5020 in" for measurement steps. */
function limitsLine(step: OperationStepWithState): string | null {
  if (step.step_type !== 'measurement' || !step.config) return null;
  const { lsl, nominal, usl, unit } = step.config;
  const parts: string[] = [];
  if (lsl != null) parts.push(`LSL ${lsl}`);
  if (nominal != null) parts.push(`NOM ${nominal}`);
  if (usl != null) parts.push(`USL ${usl}`);
  if (parts.length === 0) return null;
  return `${parts.join(' · ')}${unit ? ` ${unit}` : ''}`;
}

function recordValue(step: OperationStepWithState, record: OperationStepRecord): string {
  switch (step.step_type) {
    case 'measurement': {
      if (record.value_numeric == null) return '—';
      const unit = step.config?.unit;
      return `${record.value_numeric}${unit ? ` ${unit}` : ''}`;
    }
    case 'checkbox':
      return record.value_bool ? 'Done' : 'Not done';
    case 'photo':
      return 'Photo attached';
    case 'file':
      return 'File attached';
    default:
      return record.value_text ?? '—';
  }
}

/**
 * Read-only "Process steps" evidence panel for the WO detail operation view —
 * the same steps-view data the kiosk records against, so office staff see the
 * captured trail (value · recorder · Central time, per serial) without kiosk
 * access. Deliberately NO record/correct affordances here: capture stays on
 * the shop floor while the operation runs.
 */
export default function OperationStepsPanel({ operationId }: OperationStepsPanelProps) {
  const [view, setView] = useState<OperationStepsView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.getOperationSteps(operationId);
      setView(res);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setError(typeof detail === 'string' && detail.trim() ? detail : 'Failed to load process steps');
    } finally {
      setLoading(false);
    }
  }, [operationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="p-4" data-testid="operation-steps-loading">
        <SkeletonText lines={3} />
      </div>
    );
  }

  if (error || !view) {
    return (
      <ErrorState
        title="Couldn't load process steps"
        message={error || 'Failed to load process steps'}
        onRetry={() => void load()}
      />
    );
  }

  if (view.steps.length === 0) {
    return (
      <EmptyState
        icon={ClipboardDocumentCheckIcon}
        title="No process steps"
        description="This operation's traveler has no process-sheet steps."
      />
    );
  }

  return (
    <div className="p-4" data-testid="operation-steps-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-slate-400">
          Process steps
          {view.steps[0]?.source_sheet_revision ? (
            <span className="ml-2 font-mono normal-case text-slate-500">
              Rev {view.steps[0].source_sheet_revision}
            </span>
          ) : null}
        </h3>
        <span className="font-mono text-sm tabular-nums text-slate-300">
          {view.steps_recorded}/{view.steps_total} required recorded
          {view.is_serialized ? ` · ${view.serial_numbers.length} serials` : ''}
        </span>
      </div>

      <ol className="mt-3 space-y-2">
        {view.steps.map((step) => {
          const isInstruction = step.step_type === 'instruction';
          const limits = limitsLine(step);
          return (
            <li key={step.id} className="rounded-sm border border-fd-line bg-slate-900/40 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs text-slate-500">{step.sequence}</span>
                <span className="text-sm font-medium text-slate-100">{step.label}</span>
                <StepTypeBadge type={step.step_type} />
                {step.is_required && !isInstruction && (
                  <span className="rounded border border-fd-amber/50 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-fd-amber">
                    Required
                  </span>
                )}
                <span className="ml-auto">
                  {isInstruction ? (
                    <span className="text-xs text-slate-500">Display only</span>
                  ) : step.complete ? (
                    <span className="inline-flex items-center gap-1 text-xs font-semibold text-fd-green">
                      <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
                      Complete
                    </span>
                  ) : (
                    <span className="text-xs font-semibold text-fd-amber">
                      {view.is_serialized && step.missing_serials.length > 0
                        ? `Missing: ${step.missing_serials.join(', ')}`
                        : 'Not recorded'}
                    </span>
                  )}
                </span>
              </div>
              {limits && <p className="mt-1 font-mono text-xs text-slate-400">{limits}</p>}
              {step.instruction_text && <p className="mt-1 text-xs text-slate-400">{step.instruction_text}</p>}

              {step.records.length > 0 && (
                <ul aria-label={`Records for ${step.label}`} className="mt-2 space-y-1">
                  {step.records.map((record) => (
                    <li
                      key={record.id}
                      className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-sm bg-slate-800/50 px-2 py-1.5 text-xs"
                    >
                      <LockClosedIcon className="h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden="true" />
                      <span className="font-mono font-semibold text-slate-100">{recordValue(step, record)}</span>
                      {record.is_conforming === false && (
                        <span className="rounded border border-fd-red/50 px-1 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-fd-red">
                          Not satisfied
                        </span>
                      )}
                      {record.qualification_snapshot?.qualified === false && (
                        <span
                          data-testid={`record-qualification-warning-${record.id}`}
                          title={qualificationTitle(record)}
                          className="inline-flex items-center gap-1 rounded border border-fd-amber/50 px-1 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest text-fd-amber"
                        >
                          <ExclamationTriangleIcon className="h-3.5 w-3.5" aria-hidden="true" />
                          Qual
                        </span>
                      )}
                      {view.is_serialized && record.serial_number && (
                        <span className="font-mono text-slate-400">SN {record.serial_number}</span>
                      )}
                      {record.gauge && (
                        <span data-testid={`record-gauge-${record.id}`} className="text-slate-400">
                          Gauge {record.gauge.name} ({record.gauge.equipment_code})
                        </span>
                      )}
                      <span className="text-slate-400">
                        {record.recorded_by_name || 'Operator'} · {formatCentralDateTime(record.recorded_at)}
                      </span>
                      {record.source && (
                        <span className="ml-auto font-mono uppercase tracking-wider text-slate-500">
                          {record.source}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
