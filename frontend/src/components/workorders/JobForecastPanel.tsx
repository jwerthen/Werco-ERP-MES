import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { DeliveryPrediction } from '../../types/jobPlanning';
import { Button, Spinner } from '../ui';
import { formatCentralDate } from '../../utils/centralTime';
import MaterialReadinessSummary from './MaterialReadinessSummary';

export default function JobForecastPanel({ workOrderId }: { workOrderId: number }) {
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [data, setData] = useState<DeliveryPrediction | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    setData(null);
  }, [workOrderId]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setError(false);
    api
      .predictDelivery(workOrderId)
      .then(result => {
        if (active) setData(result);
      })
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [workOrderId, open, reload]);
  return (
    <section className="card p-4">
      <Button variant="secondary" aria-expanded={open} onClick={() => setOpen(!open)}>
        Completion forecast
      </Button>
      {open && (
        <div className="mt-3 space-y-3">
          {loading && (
            <div className="flex gap-2 items-center">
              <Spinner size="sm" /> Updating forecast…
            </div>
          )}
          {error && (
            <p role="alert" className="text-amber-200">
              Forecast could not be refreshed. {data ? 'The last result is stale.' : 'Completion is unknown.'}
            </p>
          )}
          {data && (
            <>
              <p className="text-lg">
                Estimated finish:{' '}
                <strong>
                  {data.predicted_completion ? formatCentralDate(data.predicted_completion.slice(0, 10)) : 'Unknown'}
                </strong>
              </p>
              <p className="text-sm text-slate-400">{data.basis}</p>
              {data.warnings.map((warning, index) => (
                <p key={index} className="text-sm text-amber-200">
                  {warning}
                </p>
              ))}
              {data.materials && <MaterialReadinessSummary materials={data.materials} />}
              <ol className="space-y-2 text-sm">
                {data.operations.map(op => (
                  <li key={op.operation_id}>
                    {op.operation_name} · {op.work_center_name}:{' '}
                    {op.predicted_start ? formatCentralDate(op.predicted_start.slice(0, 10)) : 'Unknown'} →{' '}
                    {op.predicted_end ? formatCentralDate(op.predicted_end.slice(0, 10)) : 'Unknown'}
                  </li>
                ))}
              </ol>
            </>
          )}
          <Button variant="secondary" disabled={loading} onClick={() => setReload(value => value + 1)}>
            {error ? 'Retry forecast' : 'Refresh forecast'}
          </Button>
        </div>
      )}
    </section>
  );
}
