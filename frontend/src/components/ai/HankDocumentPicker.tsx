import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { FormField } from '../ui/FormField';
import { useHankSessionGuard } from './useHankSessionGuard';

export interface HankDocumentChoice {
  id: number;
  document_number: string;
  title: string;
  revision: string;
  status: string;
  mime_type?: string;
  file_name?: string;
}
export function HankDocumentPicker({
  value,
  onChange,
  disabled,
  label = 'Document',
  pdfOnly = false,
}: {
  value: string;
  onChange: (value: string, record?: HankDocumentChoice) => void;
  disabled?: boolean;
  label?: string;
  pdfOnly?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [rows, setRows] = useState<HankDocumentChoice[]>([]);
  const [selected, setSelected] = useState<HankDocumentChoice>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { current, controller, release } = useHankSessionGuard();
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setError(false);
    const timer = window.setTimeout(() => {
      api
        .getDocuments({ search: query || undefined, limit: 25 }, request.signal)
        .then((items: HankDocumentChoice[]) => {
          if (current() && !request.signal.aborted)
            setRows(
              items.filter(
                item => !pdfOnly || item.mime_type === 'application/pdf' || /\.pdf$/i.test(item.file_name || '')
              )
            );
        })
        .catch(() => {
          if (current() && !request.signal.aborted) setError(true);
        })
        .finally(() => {
          release(request);
          if (current() && !request.signal.aborted) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      request.abort();
      release(request);
    };
  }, [query, attempt, pdfOnly, current, controller, release]);
  const options = selected && !rows.some(row => row.id === selected.id) ? [selected, ...rows] : rows;
  return (
    <div className="space-y-2">
      <FormField label={`Find ${label.toLowerCase()}`} help="Search document number or title. Up to 25 matches.">
        {field => (
          <input
            {...field}
            className="input w-full"
            value={query}
            disabled={disabled}
            maxLength={100}
            onChange={event => setQuery(event.target.value)}
          />
        )}
      </FormField>
      <FormField label={label}>
        {field => (
          <select
            {...field}
            value={value}
            disabled={disabled || loading || error}
            className="input w-full"
            onChange={event => {
              const row = options.find(item => String(item.id) === event.target.value);
              setSelected(row);
              onChange(event.target.value, row);
            }}
          >
            <option value="">{loading ? 'Loading documents…' : 'Select document…'}</option>
            {options.map(row => (
              <option key={row.id} value={row.id}>
                {row.document_number} · Rev {row.revision} · {row.title} · {row.status}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          Documents could not be loaded.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
            Retry document choices
          </button>
        </p>
      )}
    </div>
  );
}
