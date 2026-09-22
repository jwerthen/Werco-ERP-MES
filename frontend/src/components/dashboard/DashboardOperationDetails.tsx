import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { ActiveAssignment, LaserNestInfo } from '../../types';
import { formatOperationLabel } from '../../utils/operationLabel';
import { ErrorState, Modal } from '../ui';
import LaserNestOperatorPanel from '../laser/LaserNestOperatorPanel';
import LaserNestPdfPreview from '../laser/LaserNestPdfPreview';

interface OperationDetails {
  operation: {
    id: number;
    operation_number?: string | null;
    name: string;
    description?: string | null;
    status: string;
    quantity_ordered: number;
    quantity_complete: number;
    quantity_scrapped: number;
    setup_instructions?: string | null;
    run_instructions?: string | null;
    laser_nest?: LaserNestInfo | null;
  };
  work_order: { id: number; work_order_number: string; customer_name?: string | null };
  work_center: { name?: string | null } | null;
}

export default function DashboardOperationDetails({
  assignment,
  onClose,
}: {
  assignment: ActiveAssignment;
  onClose: () => void;
}) {
  const [details, setDetails] = useState<OperationDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [showPdf, setShowPdf] = useState(false);
  const operationId = assignment.operation.id;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setDetails(null);
    setError('');
    setShowPdf(false);
    if (operationId == null) return;

    api.getOperationDetails(operationId).then(
      response => {
        if (!cancelled) {
          setDetails(response);
          setLoading(false);
        }
      },
      () => {
        if (!cancelled) {
          setError('Could not load this operation. Please try again.');
          setLoading(false);
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [operationId, attempt]);

  const operation = details?.operation;
  const nest = operation?.laser_nest;

  return (
    <Modal open onClose={onClose} size="4xl" ariaLabel="Operation details">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-fd-ink">Operation details</h2>
          <p className="text-sm text-fd-mute">
            {assignment.user.name || assignment.user.display_name} · {assignment.work_order.work_order_number}
          </p>
        </div>
        <button type="button" onClick={onClose} className="btn-ghost btn-sm" aria-label="Close operation details">
          <XMarkIcon className="h-5 w-5" aria-hidden="true" />
        </button>
      </div>

      {loading && (
        <p role="status" className="py-8 text-center text-fd-mute">
          Loading operation details…
        </p>
      )}
      {error && <ErrorState message={error} onRetry={() => setAttempt(value => value + 1)} />}
      {details && operation && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="font-semibold text-fd-ink">
                {operation.operation_number && `${formatOperationLabel(operation.operation_number)} · `}
                {operation.name}
              </h3>
              {details.work_order.customer_name && (
                <p className="text-sm text-fd-mute">{details.work_order.customer_name}</p>
              )}
            </div>
            <Link to={`/work-orders/${details.work_order.id}`} className="btn-secondary btn-sm">
              Open work order
            </Link>
          </div>
          <dl className="grid grid-cols-2 gap-3 rounded border border-fd-line p-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-fd-mute">Work center</dt>
              <dd>{details.work_center?.name || 'Unassigned'}</dd>
            </div>
            <div>
              <dt className="text-fd-mute">Status</dt>
              <dd className="capitalize">{operation.status.replace(/_/g, ' ')}</dd>
            </div>
            <div>
              <dt className="text-fd-mute">{nest ? 'Runs complete' : 'Quantity complete'}</dt>
              <dd>
                {operation.quantity_complete} / {operation.quantity_ordered}
              </dd>
            </div>
            <div>
              <dt className="text-fd-mute">Scrapped</dt>
              <dd>{operation.quantity_scrapped ?? 0}</dd>
            </div>
          </dl>

          {nest && (
            <section aria-label="CNC nest" className="space-y-3">
              <LaserNestOperatorPanel nest={nest} allowPreview={false} />
              {nest.cnc_file_name && <p className="break-all text-sm text-fd-mute">CNC file: {nest.cnc_file_name}</p>}
              {nest.has_document ? (
                <>
                  <button
                    type="button"
                    className="btn-primary"
                    aria-expanded={showPdf}
                    onClick={() => setShowPdf(value => !value)}
                  >
                    {showPdf ? 'Hide nest PDF' : 'View nest PDF'}
                  </button>
                  {showPdf && (
                    <LaserNestPdfPreview
                      key={nest.id}
                      laserNestId={nest.id}
                      fileName={nest.document_file_name}
                      heightClassName="h-[60vh]"
                    />
                  )}
                </>
              ) : (
                <p className="text-sm text-fd-mute">No PDF is attached to this nest.</p>
              )}
            </section>
          )}

          {[
            ['Description', operation.description],
            ['Setup instructions', operation.setup_instructions],
            ['Run instructions', operation.run_instructions],
          ].map(([label, value]) =>
            value ? (
              <section key={label}>
                <h3 className="font-semibold text-fd-ink">{label}</h3>
                <p className="whitespace-pre-wrap text-sm text-fd-mute">{value}</p>
              </section>
            ) : null
          )}
        </div>
      )}
    </Modal>
  );
}
