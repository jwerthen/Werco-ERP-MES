import React, { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { useCompany } from '../context/CompanyContext';
import { formatCentralDateTime } from '../utils/centralTime';

export interface BackgroundEmailLog {
  id: number;
  user_id: number;
  subject: string | null;
  sent: boolean;
  error: string | null;
  provider_status: string | null;
  sent_at: string | null;
}
export function backgroundEmailStatus(row: BackgroundEmailLog): string {
  if (row.provider_status === 'sending' && row.sent_at) {
    const timestamp = /Z$|[+-]\d{2}:\d{2}$/.test(row.sent_at) ? row.sent_at : `${row.sent_at}Z`;
    if (Date.now() - new Date(timestamp).getTime() > 10 * 60 * 1000) return 'Outcome unknown — worker did not finish';
  }
  const labels: Record<string, string> = {
    queued: 'Queued',
    sending: 'Sending',
    retrying: 'Retry pending — not submitted',
    accepted: 'Accepted by mail server',
    failed: 'Failed — not sent',
    skipped: 'Skipped — not sent',
    unknown: 'Outcome unknown — check mail server',
  };
  return (
    labels[row.provider_status || ''] || (row.sent ? 'Legacy queue record — delivery unverified' : 'Failed — not sent')
  );
}
export default function BackgroundEmailActivity() {
  const { user } = useAuth();
  const { currentCompany } = useCompany();
  const [params, setParams] = useSearchParams();
  const selectedId = Number(params.get('delivery')) || undefined;
  const canViewCompany = ['admin', 'manager', 'supervisor'].includes(user?.role || '');
  const [mineOnly, setMineOnly] = useState(true);
  const [onlyFailures, setOnlyFailures] = useState(true);
  const [rows, setRows] = useState<BackgroundEmailLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const request = useRef(0);
  useEffect(() => {
    const seq = ++request.current;
    setLoading(true);
    setRows([]);
    setError('');
    api
      .getNotificationLogs({
        channel: 'email',
        limit: 50,
        mine_only: !canViewCompany || mineOnly,
        ...(selectedId ? { delivery_id: selectedId } : onlyFailures ? { status: 'failed' } : {}),
      })
      .then(items => {
        if (request.current === seq) setRows(items);
      })
      .catch(() => {
        if (request.current === seq)
          setError('Email activity could not be loaded. Refresh to check recorded outcomes.');
      })
      .finally(() => {
        if (request.current === seq) setLoading(false);
      });
    return () => {
      request.current += 1;
    };
  }, [canViewCompany, mineOnly, onlyFailures, selectedId, revision, user?.id, currentCompany?.id]);
  return (
    <section aria-label="Background email activity" className="mt-5 rounded border border-fd-line bg-fd-surface p-4">
      <h2 className="text-lg font-semibold">Background email activity</h2>
      <p className="mt-1 text-sm text-fd-mute">
        Notification emails only. Showing up to 50 recent records. Mail server acceptance does not confirm inbox
        delivery.
      </p>
      <div className="my-3 flex flex-wrap items-end gap-3">
        <label className="text-sm">
          Email status
          <select
            className="input mt-1 block"
            value={onlyFailures ? 'failed' : 'all'}
            disabled={!!selectedId}
            onChange={event => setOnlyFailures(event.target.value === 'failed')}
          >
            <option value="failed">Failures and unknown outcomes</option>
            <option value="all">All email activity</option>
          </select>
        </label>
        {canViewCompany && (
          <label className="text-sm">
            Email activity scope
            <select
              className="input mt-1 block"
              value={mineOnly ? 'mine' : 'company'}
              onChange={event => setMineOnly(event.target.value === 'mine')}
            >
              <option value="mine">My emails</option>
              <option value="company">Company emails</option>
            </select>
          </label>
        )}
        <button className="btn-secondary" disabled={loading} onClick={() => setRevision(value => value + 1)}>
          Refresh email activity
        </button>
        {selectedId && (
          <button
            className="btn-secondary"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete('delivery');
              setParams(next);
            }}
          >
            Clear email selection
          </button>
        )}
      </div>
      {loading ? (
        <p role="status">Loading email activity…</p>
      ) : error ? (
        <p role="alert" className="text-red-300">
          {error}
        </p>
      ) : rows.length ? (
        <ul className="space-y-3">
          {rows.map(row => (
            <li key={row.id} className="rounded border border-fd-line p-3">
              <p className="font-medium">{row.subject || 'Background notification email'}</p>
              <p className="text-sm">{backgroundEmailStatus(row)}</p>
              {row.error && <p className="mt-1 text-sm text-amber-300">{row.error}</p>}
              {['sending', 'unknown'].includes(row.provider_status || '') && (
                <p className="text-sm text-fd-mute">
                  Check the mail server before resending. This page does not retry email.
                </p>
              )}
              <p className="mt-1 text-xs text-fd-mute">
                Record #{row.id} · User #{row.user_id}
                {row.sent_at ? ` · ${formatCentralDateTime(row.sent_at)}` : ''}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-fd-mute">
          {selectedId
            ? 'This email record is unavailable in the selected scope.'
            : onlyFailures
              ? 'No recorded email failures in this scope.'
              : 'No background email activity in this scope.'}
        </p>
      )}
    </section>
  );
}
