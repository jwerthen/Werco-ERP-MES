import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankRoutineRun, HankRoutineStepKind, HankWorkQueue, HankWorkState } from '../../types/hankWork';
import { formatCentralDateTime } from '../../utils/centralTime';
import { FormField } from '../ui/FormField';
import type { HankRecordContext } from './hankContext';
import { HankDocumentIntake } from './HankDocumentIntake';
import { HankEvidencePanel } from './HankEvidencePanel';
import { HankOperationalTask } from './HankOperationalTask';
import { HankHandoffs } from './HankHandoffs';
import { HankRoutines } from './HankRoutines';
import { HankJobScan, HankScannedJob } from './HankJobScan';
import { useHankSessionGuard } from './useHankSessionGuard';

export type HankWorkArea =
  | 'overview'
  | 'intake'
  | 'readiness'
  | 'receiving'
  | 'production'
  | 'shipping'
  | 'shipment'
  | 'knowledge'
  | 'trace'
  | 'purchasing'
  | 'handoff'
  | 'routine';
const AREAS: Array<{ id: HankWorkArea; title: string; description: string }> = [
  { id: 'intake', title: 'Review PDFs', description: 'Extract, verify, and file documents.' },
  { id: 'readiness', title: 'Check job readiness', description: 'Find blockers and the records behind them.' },
  { id: 'receiving', title: 'Receive a delivery', description: 'Review quantities, lots, and inspection holds.' },
  { id: 'production', title: 'Report work', description: 'Report quantities and add a hold if needed.' },
  { id: 'shipping', title: 'Prepare shipping packet', description: 'Check job evidence before shipment.' },
  { id: 'knowledge', title: 'Find job knowledge', description: 'Review current documents and prior-job evidence.' },
  { id: 'purchasing', title: 'Check a purchase order', description: 'Review exceptions and draft supplier follow-up.' },
  { id: 'handoff', title: 'Hand off work', description: 'Give a named coworker the next steps.' },
  { id: 'routine', title: 'Follow an approved routine', description: 'Complete a sequence with saved evidence.' },
  { id: 'trace', title: 'Trace a lot or serial', description: 'Follow recorded material and job links.' },
];
const STATE_LABELS: Record<HankWorkState, string> = {
  working: 'Working',
  waiting_on_you: 'Waiting on you',
  waiting_on_other: 'Waiting on someone',
  finished: 'Finished',
};
const STEP_AREA: Record<HankRoutineStepKind, HankWorkArea> = {
  readiness: 'readiness',
  knowledge: 'knowledge',
  document_intake: 'intake',
  receive_delivery: 'receiving',
  report_production: 'production',
  shipping_packet: 'shipping',
  draft_shipment: 'shipment',
  purchasing_impact: 'purchasing',
  handoff: 'handoff',
  checklist: 'routine',
};

export function HankWorkWorkspace({
  context,
  initialArea = 'overview',
  initialId,
  onNavigate,
  onBusyChange,
}: {
  context: HankRecordContext;
  initialArea?: HankWorkArea;
  initialId?: number;
  onNavigate: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [area, setArea] = useState<HankWorkArea>(initialArea);
  const [detailId, setDetailId] = useState(initialId);
  const [activeContext, setActiveContext] = useState(context);
  const [scanned, setScanned] = useState<HankScannedJob | null>(null);
  const [returnRun, setReturnRun] = useState<number>();
  const [childBusy, setChildBusy] = useState(false);
  const busyRef = useRef(false);
  const [queue, setQueue] = useState<HankWorkQueue | null>(null);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState(false);
  const [queueAttempt, setQueueAttempt] = useState(0);
  const [state, setState] = useState<HankWorkState | ''>('');
  const [contextLabel, setContextLabel] = useState('');
  const { current, controller, release, changed } = useHankSessionGuard();
  const currentContext = useMemo(() => (scanned ? { workOrderId: scanned.workOrderId } : context), [context, scanned]);
  useEffect(() => {
    if (area !== 'overview') return;
    const request = controller();
    setQueueLoading(true);
    setQueueError(false);
    api
      .getHankWorkQueue(state || undefined, request.signal)
      .then(result => {
        if (current() && !request.signal.aborted) setQueue(result);
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setQueueError(true);
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setQueueLoading(false);
      });
    return () => request.abort();
  }, [area, state, queueAttempt, current, controller, release]);
  useEffect(() => {
    const request = controller();
    setContextLabel('');
    if (scanned) {
      setContextLabel(scanned.label);
      release(request);
      return;
    }
    const lookup = context.workOrderId
      ? api
          .getWorkOrder(context.workOrderId, request.signal)
          .then((job: { work_order_number: string }) => job.work_order_number)
      : context.purchaseOrderId
        ? api
            .getPurchaseOrder(context.purchaseOrderId, request.signal)
            .then((po: { po_number: string }) => po.po_number)
        : null;
    if (lookup)
      void lookup
        .then(label => {
          if (current() && !request.signal.aborted) setContextLabel(label);
        })
        .catch(() => undefined)
        .finally(() => release(request));
    else release(request);
    return () => request.abort();
  }, [context.workOrderId, context.purchaseOrderId, scanned, current, controller, release]);
  const open = (next: HankWorkArea, selected = currentContext) => {
    if (busyRef.current) return;
    setActiveContext(selected);
    setArea(next);
    setDetailId(undefined);
  };
  const busyChanged = (value: boolean) => {
    busyRef.current = value;
    setChildBusy(value);
    onBusyChange(value);
  };
  const openStep = (kind: HankRoutineStepKind, run: HankRoutineRun) => {
    if (busyRef.current) return;
    setReturnRun(run.id);
    open(STEP_AREA[kind], {
      workOrderId: run.work_order_id || undefined,
      purchaseOrderId: run.purchase_order_id || undefined,
    });
  };
  if (changed)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see work in your current company.
      </p>
    );
  const shared = { onNavigate, onBusyChange: busyChanged };
  return (
    <section aria-label="Work with Hank" className="space-y-4">
      {area !== 'overview' && (
        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            className="text-xs text-fd-blue underline"
            disabled={childBusy}
            onClick={() => {
              if (!busyRef.current) {
                setArea('overview');
                setDetailId(undefined);
              }
            }}
          >
            Back to Work
          </button>
          {returnRun && area !== 'routine' && (
            <button
              type="button"
              className="text-xs text-fd-blue underline"
              disabled={childBusy}
              onClick={() => {
                if (!busyRef.current) {
                  setArea('routine');
                  setDetailId(returnRun);
                }
              }}
            >
              Return to routine
            </button>
          )}
        </div>
      )}
      {area === 'overview' && (
        <>
          <div>
            <h3 className="text-sm font-semibold text-fd-ink">What needs doing?</h3>
            <p className="mt-1 text-xs text-fd-mute">
              Start with the records. Hank prepares the work, and you review consequential changes.
            </p>
          </div>
          {contextLabel && (
            <div className="space-y-2 border border-slate-700 p-3">
              <p className="text-xs font-semibold text-fd-ink">For {contextLabel}</p>
              <div className="flex flex-wrap gap-2">
                {currentContext.workOrderId ? (
                  <>
                    <button type="button" className="btn text-xs" onClick={() => open('readiness')}>
                      Check this job
                    </button>
                    <button type="button" className="btn text-xs" onClick={() => open('production')}>
                      Report this job
                    </button>
                    <button type="button" className="btn text-xs" onClick={() => open('shipping')}>
                      Prepare this job’s packet
                    </button>
                    <button type="button" className="btn text-xs" onClick={() => open('handoff')}>
                      Hand off this job
                    </button>
                  </>
                ) : (
                  currentContext.purchaseOrderId && (
                    <>
                      <button type="button" className="btn text-xs" onClick={() => open('purchasing')}>
                        Check this PO
                      </button>
                      <button type="button" className="btn text-xs" onClick={() => open('receiving')}>
                        Receive this PO
                      </button>
                    </>
                  )
                )}
              </div>
            </div>
          )}
          <details>
            <summary className="text-xs text-fd-blue cursor-pointer">Use a traveler barcode</summary>
            <div className="mt-2">
              <HankJobScan onSelect={setScanned} />
              {scanned && (
                <button type="button" className="mt-2 text-xs underline" onClick={() => setScanned(null)}>
                  Clear scanned job
                </button>
              )}
            </div>
          </details>
          <div className="grid grid-cols-1 min-[380px]:grid-cols-2 gap-2">
            {AREAS.map(item => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setReturnRun(undefined);
                  open(item.id);
                }}
                className="text-left border border-slate-700 p-3 hover:bg-slate-800"
              >
                <span className="block text-xs font-semibold text-fd-blue">{item.title}</span>
                <span className="block mt-1 text-[11px] text-fd-mute">{item.description}</span>
              </button>
            ))}
          </div>
          <div className="space-y-3 border-t border-slate-700 pt-4">
            <h4 className="text-xs font-semibold text-fd-ink">Your work queue</h4>
            <div className="flex items-end gap-2">
              <FormField label="Work state" className="flex-1">
                {field => (
                  <select
                    {...field}
                    value={state}
                    onChange={event => {
                      setQueue(null);
                      setState(event.target.value as HankWorkState | '');
                    }}
                    className="input w-full"
                  >
                    <option value="">All states</option>
                    {Object.entries(STATE_LABELS).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
              <button
                type="button"
                className="btn text-xs"
                disabled={queueLoading}
                onClick={() => setQueueAttempt(value => value + 1)}
              >
                Refresh queue
              </button>
            </div>
            {queueLoading && (
              <p role="status" className="text-xs text-fd-mute">
                Loading saved work…
              </p>
            )}
            {queueError && (
              <p role="alert" className="text-xs text-fd-red">
                The work queue could not be loaded. Refresh to retry.
              </p>
            )}
            {queue?.items.map(item => (
              <Link key={item.key} to={item.url} className="block border border-slate-700 p-3 space-y-1">
                <span className="block text-xs font-semibold text-fd-blue">{item.title}</span>
                <span className="block text-[11px] text-fd-mute">
                  {item.kind === 'task' && ['watching', 'snoozed'].includes(item.status)
                    ? 'Waiting for a condition'
                    : STATE_LABELS[item.state]}{' '}
                  · {item.status.replace(/_/g, ' ')} · {formatCentralDateTime(item.updated_at)}
                </span>
              </Link>
            ))}
            {queue && !queue.items.length && !queueLoading && (
              <p className="text-xs text-fd-mute">No saved work in this state.</p>
            )}
            {queue && (
              <p className="text-[11px] text-fd-mute">
                Checked {formatCentralDateTime(queue.checked_at)}.{' '}
                {queue.truncated
                  ? 'This view is limited. Open each workspace for older records.'
                  : 'Statuses reflect the last saved check.'}
              </p>
            )}
          </div>
        </>
      )}
      {area === 'intake' && (
        <HankDocumentIntake {...shared} initialId={detailId} workOrderId={activeContext.workOrderId} />
      )}
      {(['readiness', 'knowledge', 'purchasing', 'shipping', 'trace'] as HankWorkArea[]).includes(area) && (
        <>
          <HankEvidencePanel
            key={area}
            {...shared}
            kind={area as 'readiness' | 'knowledge' | 'purchasing' | 'shipping' | 'trace'}
            workOrderId={activeContext.workOrderId}
            purchaseOrderId={activeContext.purchaseOrderId}
          />
          {area === 'shipping' && (
            <button
              type="button"
              className="btn text-xs"
              disabled={childBusy}
              onClick={() => open('shipment', activeContext)}
            >
              Prepare a shipment draft
            </button>
          )}
        </>
      )}
      {(['receiving', 'production', 'shipment'] as HankWorkArea[]).includes(area) && (
        <HankOperationalTask
          key={area}
          {...shared}
          kind={
            area === 'receiving' ? 'receive_delivery' : area === 'production' ? 'report_production' : 'draft_shipment'
          }
          workOrderId={activeContext.workOrderId}
          purchaseOrderId={activeContext.purchaseOrderId}
          operationId={scanned?.workOrderId === activeContext.workOrderId ? scanned?.operationId : undefined}
        />
      )}
      {area === 'handoff' && <HankHandoffs {...shared} initialId={detailId} workOrderId={activeContext.workOrderId} />}
      {area === 'routine' && (
        <HankRoutines
          key={detailId || 'library'}
          {...shared}
          initialId={detailId}
          workOrderId={activeContext.workOrderId}
          purchaseOrderId={activeContext.purchaseOrderId}
          onOpenStep={openStep}
        />
      )}
    </section>
  );
}
