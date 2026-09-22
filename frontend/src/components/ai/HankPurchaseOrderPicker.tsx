import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { ComboBox } from '../ui/ComboBox';
import { useHankSessionGuard } from './useHankSessionGuard';

export function HankPurchaseOrderPicker({
  value,
  onChange,
  disabled,
  id,
  'aria-describedby': describedBy,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  id?: string;
  'aria-describedby'?: string;
}) {
  const [options, setOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { current, controller, release } = useHankSessionGuard();
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setFailed(false);
    api
      .getPurchaseOrders(undefined, request.signal)
      .then((rows: Array<{ id: number; po_number: string; vendor?: { name: string }; vendor_name?: string }>) => {
        if (current() && !request.signal.aborted)
          setOptions(
            rows.map(row => ({
              value: String(row.id),
              label: `${row.po_number} · ${row.vendor?.name || row.vendor_name || 'Purchase order'}`,
            }))
          );
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setFailed(true);
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [attempt, current, controller, release]);
  return (
    <div className="space-y-1">
      <ComboBox
        id={id}
        options={options}
        value={value}
        onChange={onChange}
        disabled={disabled || loading || failed}
        placeholder={loading ? 'Loading purchase orders…' : 'Find PO number or supplier…'}
        ariaDescribedBy={describedBy}
      />
      {failed && (
        <p role="alert" className="text-xs text-fd-red">
          Purchase orders could not be loaded.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
            Retry PO choices
          </button>
        </p>
      )}
    </div>
  );
}
