import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { ComboBox } from '../ui/ComboBox';

type Kind = 'part' | 'vendor' | 'workCenter' | 'workOrder' | 'user';
interface Props {
  kind: Kind;
  value: string | number;
  onChange: (value: string) => void;
  id?: string;
  'aria-describedby'?: string;
  'aria-label'?: string;
  disabled?: boolean;
  optional?: boolean;
  valueMode?: 'id' | 'label';
}
/** Tenant-scoped readable lookups. Never asks the operator to know a database ID. */
export default function EntityPicker({
  kind,
  value,
  onChange,
  id,
  disabled,
  optional,
  valueMode = 'id',
  ...aria
}: Props) {
  const [options, setOptions] = useState<{ value: string; label: string; hint?: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(false);
    const request =
      kind === 'part'
        ? api.getParts({ active_only: true, item_group: 'all' })
        : kind === 'vendor'
          ? api.getVendors({ active_only: true })
          : kind === 'workCenter'
            ? api.getWorkCenters()
            : kind === 'workOrder'
              ? api.getWorkOrders()
              : api.getAssignmentPeople();
    request
      .then(rows => {
        if (active)
          setOptions(
            rows.map((row: any) => ({
              value:
                valueMode === 'label'
                  ? row.full_name || `${row.first_name || ''} ${row.last_name || ''}`.trim() || row.email
                  : String(row.id),
              label:
                kind === 'part'
                  ? `${row.part_number} — ${row.name}`
                  : kind === 'vendor'
                    ? `${row.code || row.vendor_code || ''} ${row.name}`.trim()
                    : kind === 'workOrder'
                      ? `${row.work_order_number} — ${row.part?.part_number || row.part_number || row.customer_name || ''}`
                      : kind === 'user'
                        ? row.full_name || `${row.first_name || ''} ${row.last_name || ''}`.trim() || row.email
                        : `${row.code || ''} ${row.name}`.trim(),
              hint: row.email || row.customer_name,
            }))
          );
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
  }, [kind, attempt, valueMode]);
  return (
    <div className="space-y-1">
      <ComboBox
        id={id}
        options={options}
        value={value ? String(value) : ''}
        onChange={onChange}
        disabled={disabled || loading || error}
        placeholder={loading ? 'Loading choices…' : 'Search by name or number…'}
        emptyOptionLabel={optional ? 'None' : 'Select…'}
        noResultsLabel="No matching records"
        ariaLabel={aria['aria-label']}
        ariaDescribedBy={aria['aria-describedby']}
      />
      {error && (
        <p role="alert" className="text-sm text-red-300">
          Unable to load choices.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(n => n + 1)}>
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
