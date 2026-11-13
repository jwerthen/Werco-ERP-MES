import React, { useEffect, useMemo, useState } from 'react';
import api from '../../services/api';
import { ComboBox } from '../ui';

interface WorkOrderOption {
  id: number;
  work_order_number: string;
  part_id?: number | null;
  customer_name?: string | null;
  customer_po?: string | null;
  status?: string;
}

interface FaiWorkOrderPickerProps {
  partId: number;
  partNumber: string;
  value: string;
  onChange: (value: string) => void;
  id?: string;
  'aria-describedby'?: string;
}

/**
 * The job an FAI inspects: the OPEN work orders for the selected part. The link is what
 * puts the customer, PO and job # on the FAI log, so it is offered right where the FAI is
 * created. Looked up by the part number (the list's own search) and narrowed to rows of
 * exactly this part — a search hit on a customer or PO of another part is not offered.
 */
export default function FaiWorkOrderPicker({
  partId,
  partNumber,
  value,
  onChange,
  id,
  ...aria
}: FaiWorkOrderPickerProps) {
  const [workOrders, setWorkOrders] = useState<WorkOrderOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!partId || !partNumber) {
      setWorkOrders([]);
      setError(false);
      return;
    }
    let active = true;
    setLoading(true);
    setError(false);
    api
      .getWorkOrders({ search: partNumber, limit: 200 })
      .then((rows: WorkOrderOption[]) => {
        if (active) setWorkOrders((Array.isArray(rows) ? rows : []).filter(row => row.part_id === partId));
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
  }, [partId, partNumber, attempt]);

  const options = useMemo(
    () =>
      workOrders.map(row => ({
        value: String(row.id),
        label: row.customer_name ? `${row.work_order_number} — ${row.customer_name}` : row.work_order_number,
        hint: [row.customer_po ? `PO ${row.customer_po}` : '', row.status ? row.status.replace(/_/g, ' ') : '']
          .filter(Boolean)
          .join(' · '),
      })),
    [workOrders]
  );

  return (
    <div className="space-y-1">
      <ComboBox
        id={id}
        ariaDescribedBy={aria['aria-describedby']}
        options={options}
        value={value}
        onChange={onChange}
        disabled={!partId || loading || error}
        placeholder={
          !partId ? 'Select a part first' : loading ? 'Loading work orders…' : 'Search by work order or customer…'
        }
        emptyOptionLabel="No work order"
        noResultsLabel="No open work orders for this part"
      />
      {error && (
        <p role="alert" className="text-sm text-red-300">
          Unable to load work orders.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(n => n + 1)}>
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
