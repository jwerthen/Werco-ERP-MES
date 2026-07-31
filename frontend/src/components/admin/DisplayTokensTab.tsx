/**
 * Admin > Wallboard Displays tab (A0.5).
 *
 * Manage scoped display tokens for unattended shop-floor TVs:
 *  - create (label + expiry + optional department + optional "show customer
 *    names" opt-in) — the 8-char TV SETUP CODE is the primary hand-off (enter
 *    it at /tv on the TV; 15 minutes, single use), with the raw JWT + ready-made
 *    /wallboard URL as the fallback. All of it is shown exactly ONCE (the server
 *    never returns them again). "Show customer names" defaults OFF (public-safe)
 *    and should be ON only for a trusted executive-office screen;
 *  - list (label / dept / customers / created / expiry / status);
 *  - "New setup code" per row — re-issues a fresh one-time pairing code for
 *    an existing display (disabled for revoked/expired rows);
 *  - revoke (the TV loses access on its next 30s poll).
 *
 * Rendered inside AdminSettings (AdminRoute-gated); the backend additionally
 * enforces ADMIN/MANAGER on every display-token endpoint (the TV-side claim
 * of a setup code is the one PUBLIC endpoint, and it lives in
 * services/wallboardClient, not here).
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowPathIcon,
  ClipboardDocumentIcon,
  ExclamationTriangleIcon,
  NoSymbolIcon,
  PlusIcon,
  QrCodeIcon,
  TvIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import type { DisplayToken, SetupCodeResponse } from '../../types/wallboard';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { useToast } from '../ui/Toast';
import { formatCentralDateTime, toDate } from '../../utils/centralTime';

/** Group the 8-char code as XXXX-XXXX — how it's read out and typed on the TV. */
function formatSetupCode(code: string): string {
  return code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
}

/**
 * The one-time reveal panel's payload — from create (token + code) or from a
 * per-row "New setup code" re-issue (code only). `setupCode` stays defensive
 * against an older backend that doesn't return codes yet.
 */
interface OneTimeReveal {
  label: string;
  setupCode: string | null;
  dept: string | null;
  /** Present only on create — the raw JWT for the legacy #token= URL hand-off. */
  token: string | null;
}

export default function DisplayTokensTab() {
  const { showToast } = useToast();
  const [tokens, setTokens] = useState<DisplayToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [label, setLabel] = useState('');
  const [dept, setDept] = useState('');
  const [expiresDays, setExpiresDays] = useState(90);
  const [showCustomerNames, setShowCustomerNames] = useState(false);
  const [reveal, setReveal] = useState<OneTimeReveal | null>(null);
  const [reissuingId, setReissuingId] = useState<number | null>(null);
  const [copied, setCopied] = useState<'token' | 'url' | 'code' | null>(null);

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
      const result = await api.createDisplayToken({
        label: label.trim(),
        expires_days: expiresDays,
        show_customer_names: showCustomerNames,
        ...(dept.trim() ? { dept: dept.trim() } : {}),
      });
      setReveal({
        label: result.label,
        setupCode: result.setup_code ?? null,
        dept: result.dept ?? null,
        token: result.token,
      });
      setLabel('');
      setDept('');
      setShowCustomerNames(false);
      await load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to create display token');
    } finally {
      setCreating(false);
    }
  };

  const handleNewSetupCode = async (token: DisplayToken) => {
    setReissuingId(token.id);
    try {
      const result: SetupCodeResponse = await api.issueDisplaySetupCode(token.id);
      setReveal({
        label: result.label,
        setupCode: result.setup_code,
        dept: result.dept,
        token: null,
      });
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || err?.message || 'Failed to issue setup code');
    } finally {
      setReissuingId(null);
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
  const wallboardUrl = reveal?.token ? `${window.location.origin}/wallboard#token=${reveal.token}` : '';
  const tvUrl = `${window.location.origin}/tv`;

  const copy = async (text: string, which: 'token' | 'url' | 'code') => {
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
              aria-label="Display label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="North wall TV"
              maxLength={100}
              required
              className="input input-bordered w-64"
              data-testid="display-token-label"
            />
          </label>
          <FormField label="Department (optional)" labelClassName="text-xs text-surface-500 font-normal">
            {(field) => (
              <input
                {...field}
                type="text"
                value={dept}
                onChange={(e) => setDept(e.target.value)}
                placeholder="e.g. weld"
                maxLength={50}
                className="input input-bordered w-44"
                data-testid="display-token-dept"
              />
            )}
          </FormField>
          <label className="flex flex-col text-xs text-surface-500 gap-1">
            Expires (days)
            <input
              type="number"
              aria-label="Expires (days)"
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
        <label className="flex items-start gap-2 text-xs text-surface-600 max-w-xl">
          <input
            type="checkbox"
            className="checkbox checkbox-sm mt-0.5"
            checked={showCustomerNames}
            onChange={(e) => setShowCustomerNames(e.target.checked)}
            data-testid="display-token-show-customers"
          />
          <span>
            <span className="font-semibold text-surface-700">Show customer names on this display</span> — reveals the
            work-order customer on each tile. Leave OFF for public shop-floor TVs; turn ON only for a trusted
            executive-office screen.
          </span>
        </label>
      </form>

      {/* One-time reveal: setup code (primary) + raw token/URL fallback on create */}
      {reveal && (
        <div className="border border-amber-500/50 bg-amber-500/5 px-4 py-4 space-y-4" data-testid="issued-panel">
          <p className="text-sm font-semibold text-amber-600">
            {reveal.token ? 'Token' : 'Setup code'} for “{reveal.label}” — copy it now. It will not be shown again.
          </p>

          {reveal.setupCode && (
            <div className="space-y-2 border border-surface-200 bg-fd-panel/40 px-4 py-3">
              <p className="text-sm font-semibold text-surface-700">
                On the TV: go to <span className="font-mono">{tvUrl}</span> and enter code
              </p>
              <div className="flex items-center gap-3">
                <span
                  className="font-mono text-3xl font-bold tracking-[0.25em] tabular-nums"
                  data-testid="issued-setup-code"
                >
                  {formatSetupCode(reveal.setupCode)}
                </span>
                <button
                  type="button"
                  className="btn-secondary btn-sm"
                  onClick={() => copy(formatSetupCode(reveal.setupCode ?? ''), 'code')}
                >
                  <ClipboardDocumentIcon className="h-4 w-4 mr-1" />
                  {copied === 'code' ? 'Copied!' : 'Copy code'}
                </button>
              </div>
              <p className="text-xs text-surface-500">
                Valid 15 minutes, single use.
                {reveal.dept ? (
                  <>
                    {' '}
                    Pinned to department <span className="font-mono">{reveal.dept}</span>.
                  </>
                ) : null}
              </p>
            </div>
          )}

          {reveal.token && (
            <div className="space-y-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-surface-500">
                Fallback: one-time direct link
              </p>
              <div className="flex items-center gap-2">
                <input
                  readOnly
                  aria-label="Wallboard URL"
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
                  aria-label="Display token"
                  value={reveal.token}
                  className="input input-bordered flex-1 font-mono text-xs"
                  onFocus={(e) => e.target.select()}
                  data-testid="issued-token"
                />
                <button type="button" className="btn-secondary btn-sm" onClick={() => copy(reveal.token ?? '', 'token')}>
                  <ClipboardDocumentIcon className="h-4 w-4 mr-1" />
                  {copied === 'token' ? 'Copied!' : 'Copy token'}
                </button>
              </div>
              <p className="text-xs text-surface-500">
                Opening the URL on the TV's browser stores the token on the device and strips it from the
                address bar. Add <span className="font-mono">?dept=&lt;work center type&gt;</span> before the{' '}
                <span className="font-mono">#token</span> part to show one department only.
              </p>
            </div>
          )}

          <button type="button" className="btn-secondary btn-sm" onClick={() => setReveal(null)}>
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
                <th>Dept</th>
                <th>Customers</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Status</th>
                <th className="text-right">
                  <button type="button" onClick={load} className="btn-ghost btn-xs" title="Refresh" aria-label="Refresh">
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
                const unusable = token.revoked || expired;
                return (
                  <tr key={token.id}>
                    <td className="font-medium">{token.label}</td>
                    <td className="font-mono text-sm">{token.dept || '—'}</td>
                    <td className="text-sm">
                      {token.show_customer_names ? (
                        <span className="text-amber-600 font-semibold uppercase text-xs">Shown</span>
                      ) : (
                        <span className="text-surface-500 uppercase text-xs">Hidden</span>
                      )}
                    </td>
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
                      <LoadingButton
                        variant="ghost"
                        size="sm"
                        className="btn-xs"
                        loading={reissuingId === token.id}
                        disabled={unusable || reissuingId !== null}
                        title={unusable ? 'Revoked/expired displays cannot be paired' : 'Issue a fresh TV pairing code'}
                        onClick={() => handleNewSetupCode(token)}
                        data-testid={`new-code-${token.id}`}
                      >
                        <QrCodeIcon className="h-4 w-4 mr-1" />
                        New setup code
                      </LoadingButton>
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
