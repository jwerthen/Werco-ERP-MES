/**
 * Admin > Wallboard Displays tab (A0.5).
 *
 * Manage scoped display tokens for unattended shop-floor TVs:
 *  - create (label + expiry) — the JWT and ready-made /wallboard URL are
 *    shown exactly ONCE with copy buttons (the server never returns the
 *    token again);
 *  - list (label / created / expiry / status);
 *  - revoke (the TV loses access on its next 30s poll).
 *
 * Rendered inside AdminSettings (AdminRoute-gated); the backend additionally
 * enforces ADMIN/MANAGER on every display-token endpoint.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowPathIcon,
  ClipboardDocumentIcon,
  ExclamationTriangleIcon,
  NoSymbolIcon,
  PlusIcon,
  TvIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import type { DisplayToken, DisplayTokenIssued } from '../../types/wallboard';
import { formatCentralDateTime, toDate } from '../../utils/centralTime';

export default function DisplayTokensTab() {
  const [tokens, setTokens] = useState<DisplayToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [label, setLabel] = useState('');
  const [expiresDays, setExpiresDays] = useState(90);
  const [issued, setIssued] = useState<DisplayTokenIssued | null>(null);
  const [copied, setCopied] = useState<'token' | 'url' | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTokens(await api.listDisplayTokens());
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load display tokens');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!label.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const result = await api.createDisplayToken({ label: label.trim(), expires_days: expiresDays });
      setIssued(result);
      setLabel('');
      await load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to create display token');
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (token: DisplayToken) => {
    if (!window.confirm(`Revoke "${token.label}"? The TV loses access within ~30 seconds.`)) return;
    try {
      await api.revokeDisplayToken(token.id);
      await load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to revoke display token');
    }
  };

  // Token goes in the URL FRAGMENT, never the query string: fragments stay in
  // the browser, so the long-lived display credential can't land in server
  // access logs. wallboardClient still accepts legacy ?token= URLs.
  const wallboardUrl = issued ? `${window.location.origin}/wallboard#token=${issued.token}` : '';

  const copy = async (text: string, which: 'token' | 'url') => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(which);
      setTimeout(() => setCopied(null), 2000);
    } catch {
      // Clipboard unavailable — the value is selectable in the input below.
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
          <TvIcon className="h-5 w-5 text-werco-600" />
          Wallboard Displays
        </h3>
        <p className="text-xs text-surface-500 mt-0.5">
          Scoped, revocable tokens for unattended shop TVs. A display token can ONLY read the wallboard —
          it cannot access any other data or make changes. Issuance and revocation are audit-logged.
        </p>
      </div>

      {error && (
        <div className="flex items-start gap-3 border border-red-500/40 bg-red-500/5 px-4 py-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" />
          <p className="text-sm text-red-600">{error}</p>
        </div>
      )}

      {/* Create form */}
      <form onSubmit={handleCreate} className="border border-surface-200 px-4 py-4 space-y-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-surface-600">New display</p>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-xs text-surface-500 gap-1">
            Label
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="North wall TV"
              maxLength={100}
              required
              className="input input-bordered w-64"
              data-testid="display-token-label"
            />
          </label>
          <label className="flex flex-col text-xs text-surface-500 gap-1">
            Expires (days)
            <input
              type="number"
              min={1}
              max={365}
              value={expiresDays}
              onChange={(e) => setExpiresDays(Number(e.target.value))}
              className="input input-bordered w-28"
              data-testid="display-token-days"
            />
          </label>
          <button type="submit" disabled={creating || !label.trim()} className="btn-primary btn-sm">
            <PlusIcon className="h-4 w-4 mr-1" />
            {creating ? 'Creating…' : 'Create token'}
          </button>
        </div>
      </form>

      {/* One-time token reveal */}
      {issued && (
        <div className="border border-amber-500/50 bg-amber-500/5 px-4 py-4 space-y-3" data-testid="issued-panel">
          <p className="text-sm font-semibold text-amber-600">
            Token for “{issued.label}” — copy it now. It will not be shown again.
          </p>
          <div className="flex items-center gap-2">
            <input
              readOnly
              value={wallboardUrl}
              className="input input-bordered flex-1 font-mono text-xs"
              onFocus={(e) => e.target.select()}
              data-testid="issued-url"
            />
            <button type="button" className="btn-secondary btn-sm" onClick={() => copy(wallboardUrl, 'url')}>
              <ClipboardDocumentIcon className="h-4 w-4 mr-1" />
              {copied === 'url' ? 'Copied!' : 'Copy URL'}
            </button>
          </div>
          <div className="flex items-center gap-2">
            <input
              readOnly
              value={issued.token}
              className="input input-bordered flex-1 font-mono text-xs"
              onFocus={(e) => e.target.select()}
              data-testid="issued-token"
            />
            <button type="button" className="btn-secondary btn-sm" onClick={() => copy(issued.token, 'token')}>
              <ClipboardDocumentIcon className="h-4 w-4 mr-1" />
              {copied === 'token' ? 'Copied!' : 'Copy token'}
            </button>
          </div>
          <p className="text-xs text-surface-500">
            Open the URL on the TV's browser — the token is stored on the device and stripped from the
            address bar. Add <span className="font-mono">?dept=&lt;work center type&gt;</span> before the{' '}
            <span className="font-mono">#token</span> part to show one department only.
          </p>
          <button type="button" className="btn-secondary btn-sm" onClick={() => setIssued(null)}>
            Done — I copied it
          </button>
        </div>
      )}

      {/* Token list */}
      {loading ? (
        <div className="flex items-center justify-center py-12" data-testid="display-tokens-loading">
          <div className="spinner h-8 w-8" />
        </div>
      ) : tokens.length === 0 ? (
        <div className="text-center py-12 border border-surface-200">
          <TvIcon className="h-10 w-10 text-surface-300 mx-auto mb-2" />
          <p className="text-surface-500 text-sm">No display tokens yet</p>
          <p className="text-surface-400 text-xs mt-1">Create one to put the wallboard on a shop TV.</p>
        </div>
      ) : (
        <div className="table-container border border-surface-200">
          <table className="table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Status</th>
                <th className="text-right">
                  <button type="button" onClick={load} className="btn-ghost btn-xs" title="Refresh">
                    <ArrowPathIcon className="h-4 w-4" />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => {
                // toDate treats the backend's zone-less naive-UTC string as
                // UTC — native new Date(...) would parse it as LOCAL time and
                // disagree with the server by the UTC offset.
                const expiresAt = toDate(token.expires_at);
                const expired = expiresAt !== null && expiresAt.getTime() <= Date.now();
                return (
                  <tr key={token.id}>
                    <td className="font-medium">{token.label}</td>
                    <td className="font-mono text-sm">{formatCentralDateTime(token.created_at)}</td>
                    <td className="font-mono text-sm">{formatCentralDateTime(token.expires_at)}</td>
                    <td>
                      {token.revoked ? (
                        <span className="text-red-600 font-semibold text-sm uppercase">Revoked</span>
                      ) : expired ? (
                        <span className="text-amber-600 font-semibold text-sm uppercase">Expired</span>
                      ) : (
                        <span className="text-green-600 font-semibold text-sm uppercase">Active</span>
                      )}
                    </td>
                    <td className="text-right">
                      {!token.revoked && (
                        <button
                          type="button"
                          onClick={() => handleRevoke(token)}
                          className="btn-ghost btn-xs text-red-600"
                          data-testid={`revoke-${token.id}`}
                        >
                          <NoSymbolIcon className="h-4 w-4 mr-1" />
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
