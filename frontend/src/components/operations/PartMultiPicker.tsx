import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import { ComboBox } from '../ui/ComboBox';
export default function PartMultiPicker({
  value,
  onChange,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  id?: string;
}) {
  const [parts, setParts] = useState<any[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(false);
    api
      .getParts({ active_only: true, item_group: 'all' })
      .then(rows => {
        if (active) setParts(rows);
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
  }, [attempt]);
  const selected = value.split(',').filter(Boolean);
  return (
    <div className="space-y-2">
      <ComboBox
        id={id}
        value=""
        options={parts
          .filter(part => !selected.includes(String(part.id)))
          .map(part => ({ value: String(part.id), label: `${part.part_number} — ${part.name}` }))}
        onChange={part => {
          if (part) onChange([...selected, part].join(','));
        }}
        placeholder={loading ? 'Loading parts…' : 'Search and add affected parts…'}
        disabled={loading || error}
      />
      {error && (
        <p role="alert">
          Unable to load parts.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(n => n + 1)}>
            Retry
          </button>
        </p>
      )}
      <ul className="flex flex-wrap gap-2">
        {selected.map(part => (
          <li className="border border-fd-line p-2 rounded" key={part}>
            {parts.find(row => String(row.id) === part)?.part_number || 'Selected part'}{' '}
            <button
              type="button"
              aria-label={`Remove ${parts.find(row => String(row.id) === part)?.part_number || 'part'}`}
              className="ml-2 underline"
              onClick={() => onChange(selected.filter(item => item !== part).join(','))}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
