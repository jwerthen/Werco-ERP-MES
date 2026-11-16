/**
 * Admin → API Tokens (`/admin/api-tokens`).
 *
 * Long-lived, revocable, per-user tokens for non-interactive clients — the Werco
 * Assistant bot, an MCP client, the Claude custom connector. Each token acts AS
 * one real user and is never more powerful than that user's role:
 *   - scope `read_only` refuses every non-GET route and every route under a
 *     denied tag (users, documents, audit, settings, …);
 *   - `ai_external` marks a read-only token whose answers leave for an outside AI
 *     service: it is refused while the company's AI-egress switch is off, it
 *     reaches only an allowlist of reads, and records of export-controlled or
 *     unlinkable customers are redacted (Customers → Export control).
 *
 * RBAC: the backend gates every `/api-tokens` verb `require_role([ADMIN])` (which
 * waves platform admins / superusers through) and refuses an API-token
 * credential outright, so the route sits behind `AdminRoute` and the nav entry
 * is `adminOnly` — the same role set. Issue and revoke are server-gated and so
 * non-optimistic: the list reflects only what the server returns after each
 * write. The issued token lives in memory for the one-time reveal only; nothing
 * on this page logs, stores or URL-encodes it.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ExclamationTriangleIcon, KeyIcon, NoSymbolIcon, PlusIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { Button, DataTable, InputDialog, useToast } from '../components/ui';
import type { ComboBoxOption, DataTableColumn } from '../components/ui';
import ApiTokenIssueDialog from '../components/admin/ApiTokenIssueDialog';
import ApiTokenRevealDialog from '../components/admin/ApiTokenRevealDialog';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import type { ApiToken, ApiTokenIssued } from '../types/apiToken';
import type { User } from '../types';
import { formatCentralDateTime, toDate } from '../utils/centralTime';

const MIN_REVOKE_REASON = 3;

type TokenState = 'active' | 'expired' | 'revoked';

function tokenState(token: ApiToken, now: number): TokenState {
  if (token.revoked) return 'revoked';
  const expiresAt = toDate(token.expires_at);
  return expiresAt !== null && expiresAt.getTime() <= now ? 'expired' : 'active';
}

const STATE_CLASS: Record<TokenState, string> = {
  active: 'border-fd-green/40 bg-fd-green/10 text-fd-green',
  expired: 'border-fd-amber/40 bg-fd-amber/10 text-fd-amber',
  revoked: 'border-fd-red/40 bg-fd-red/10 text-fd-red',
};

const CHIP = 'inline-block rounded-sm border px-1.5 py-px font-mono text-[10px] font-semibold uppercase tracking-wide';

function userDisplayName(user: Pick<User, 'first_name' | 'last_name' | 'email'>): string {
  const name = [user.first_name, user.last_name].filter(Boolean).join(' ').trim();
  return name || user.email;
}

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}

export default function ApiTokens() {
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const includeRevoked = searchParams.get('revoked') === '1';

  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [users, setUsers] = useState<User[]>([]);
  const [usersError, setUsersError] = useState(false);
  const [aiEgressAllowed, setAiEgressAllowed] = useState<boolean | null>(null);

  const [issueOpen, setIssueOpen] = useState(false);
  const [issued, setIssued] = useState<ApiTokenIssued | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<ApiToken | null>(null);
  const [revokePending, setRevokePending] = useState(false);

  // Leaving while the one-time token is on screen loses it for good.
  useUnsavedChanges(issued !== null, 'The API token has not been dismissed. It will not be shown again. Leave?');

  const loadTokens = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      setTokens(await api.listApiTokens({ includeRevoked }));
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [includeRevoked]);

  const loadUsers = useCallback(async () => {
    setUsersError(false);
    try {
      setUsers((await api.getUsers(true)) as User[]);
    } catch {
      setUsersError(true);
    }
  }, []);

  useEffect(() => {
    loadTokens();
  }, [loadTokens]);

  useEffect(() => {
    loadUsers();
    let cancelled = false;
    // Advisory only: the server enforces egress on every AI-external request.
    api
      .getCurrentCompany()
      .then(company => {
        if (!cancelled) setAiEgressAllowed(Boolean(company?.allow_ai_egress));
      })
      .catch(() => {
        if (!cancelled) setAiEgressAllowed(null);
      });
    return () => {
      cancelled = true;
    };
  }, [loadUsers]);

  const usersById = useMemo(() => new Map(users.map(user => [user.id, user])), [users]);
  const holderName = useCallback(
    (userId: number) => {
      const user = usersById.get(userId);
      return user ? userDisplayName(user) : `User #${userId}`;
    },
    [usersById]
  );

  // The server refuses (409) an inactive user and any platform principal, so
  // neither is offered.
  const userOptions = useMemo<ComboBoxOption[]>(
    () =>
      users
        .filter(user => user.is_active && user.role !== 'platform_admin' && !user.is_superuser)
        .map(user => ({
          value: String(user.id),
          label: userDisplayName(user),
          hint: `${user.role} · ${user.email}`,
        }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [users]
  );

  const setIncludeRevoked = (next: boolean) => {
    const params = new URLSearchParams(searchParams);
    if (next) params.set('revoked', '1');
    else params.delete('revoked');
    setSearchParams(params, { replace: true });
  };

  const handleIssued = (token: ApiTokenIssued) => {
    setIssueOpen(false);
    setIssued(token);
    loadTokens();
  };

  const handleRevoke = async (reason: string) => {
    if (!revokeTarget || revokePending) return;
    if (reason.length < MIN_REVOKE_REASON) {
      showToast('error', `Give a reason of at least ${MIN_REVOKE_REASON} characters.`);
      return;
    }
    setRevokePending(true);
    try {
      const revoked = await api.revokeApiToken(revokeTarget.id, reason);
      showToast('success', `Revoked “${revoked.label}”. Its holder is refused on its next request.`);
      setRevokeTarget(null);
      await loadTokens();
    } catch (err) {
      showToast('error', errorDetail(err, 'Could not revoke the API token.'));
    } finally {
      setRevokePending(false);
    }
  };

  const columns = useMemo<Array<DataTableColumn<ApiToken>>>(() => {
    const now = Date.now();
    return [
      {
        key: 'label',
        header: 'Label',
        sortable: true,
        accessor: row => row.label,
        render: row => <span className="font-medium text-fd-ink">{row.label}</span>,
      },
      {
        key: 'holder',
        header: 'Acts as',
        sortable: true,
        accessor: row => holderName(row.user_id),
      },
      {
        key: 'scope',
        header: 'Scope',
        sortable: true,
        accessor: row => row.scope,
        render: row => (
          <span
            className={`${CHIP} ${
              row.scope === 'read_only' ? 'border-fd-blue/40 bg-fd-blue/10 text-fd-blue' : 'border-fd-line text-fd-body'
            }`}
          >
            {row.scope === 'read_only' ? 'Read-only' : 'Full'}
          </span>
        ),
      },
      {
        key: 'ai_external',
        header: 'AI-external',
        accessor: row => (row.ai_external ? 'yes' : 'no'),
        render: row =>
          row.ai_external ? (
            <span className={`${CHIP} border-fd-amber/40 bg-fd-amber/10 text-fd-amber`}>AI-external</span>
          ) : (
            <span className="text-fd-faint">—</span>
          ),
      },
      {
        key: 'jti_prefix',
        header: 'JTI',
        accessor: row => row.jti_prefix,
        render: row => <span className="font-mono text-xs text-fd-body">{row.jti_prefix}</span>,
      },
      {
        key: 'last_used_at',
        header: 'Last used',
        sortable: true,
        accessor: row => toDate(row.last_used_at)?.getTime() ?? 0,
        csv: row => (row.last_used_at ? formatCentralDateTime(row.last_used_at) : 'Never'),
        render: row => (
          <span className="font-mono text-xs">
            {row.last_used_at ? formatCentralDateTime(row.last_used_at) : 'Never'}
          </span>
        ),
      },
      {
        key: 'expires_at',
        header: 'Expires',
        accessor: row => toDate(row.expires_at)?.getTime() ?? Number.MAX_SAFE_INTEGER,
        csv: row => (row.expires_at ? formatCentralDateTime(row.expires_at) : 'Never'),
        render: row => (
          <span className="font-mono text-xs">{row.expires_at ? formatCentralDateTime(row.expires_at) : 'Never'}</span>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        accessor: row => tokenState(row, now),
        render: row => {
          const state = tokenState(row, now);
          return (
            <div className="min-w-0">
              <span className={`${CHIP} ${STATE_CLASS[state]}`}>{state}</span>
              {row.revoked && (
                <p className="mt-0.5 max-w-[16rem] text-[11px] text-fd-mute [overflow-wrap:anywhere]">
                  {row.revoked_at ? formatCentralDateTime(row.revoked_at) : ''}
                  {row.revoke_reason ? ` — ${row.revoke_reason}` : ''}
                </p>
              )}
            </div>
          );
        },
      },
      {
        key: 'actions',
        header: <span className="sr-only">Actions</span>,
        align: 'right',
        csv: () => '',
        render: row =>
          row.revoked ? null : (
            <Button
              variant="ghost"
              size="sm"
              className="text-fd-red"
              aria-label={`Revoke ${row.label}`}
              onClick={() => setRevokeTarget(row)}
            >
              <NoSymbolIcon className="mr-1 h-4 w-4" aria-hidden="true" />
              Revoke
            </Button>
          ),
      },
    ];
  }, [holderName]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <KeyIcon className="h-7 w-7 flex-shrink-0 text-fd-blue" aria-hidden="true" />
          <div>
            <h1 className="text-2xl font-bold text-fd-ink">API Tokens</h1>
            <p className="max-w-3xl text-sm text-fd-mute">
              Long-lived credentials for bots and AI connectors. A token acts as one user and can never do more than
              that user&apos;s role allows; a read-only scope narrows it further. Issuance and revocation are audited,
              and a revoked token is refused on its next request.
            </p>
          </div>
        </div>
        <Button onClick={() => setIssueOpen(true)} disabled={usersError}>
          <PlusIcon className="mr-1 h-4 w-4" aria-hidden="true" />
          Issue token
        </Button>
      </div>

      {aiEgressAllowed === false && (
        <div
          className="flex items-start gap-2.5 rounded-sm border border-fd-amber/50 bg-fd-amber/10 p-3 text-sm text-fd-body"
          data-testid="api-tokens-egress-off"
        >
          <ExclamationTriangleIcon className="mt-0.5 h-5 w-5 flex-shrink-0 text-fd-amber" aria-hidden="true" />
          <p>
            <span className="font-semibold text-fd-amber">AI egress is off for this company.</span> Every AI-external
            token is refused (403) until an Admin enables it in{' '}
            <Link to="/admin/settings?tab=aiprivacy" className="text-fd-blue underline">
              Admin Settings → AI Privacy
            </Link>
            . Turn it on only after the export-control review is cleared.
          </p>
        </div>
      )}

      {usersError && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-sm border border-fd-red/40 bg-fd-red/5 p-3 text-sm text-fd-body">
          <span>Could not load users, so tokens cannot be issued and holders show by id.</span>
          <Button variant="secondary" size="sm" onClick={loadUsers}>
            Retry
          </Button>
        </div>
      )}

      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex items-center gap-2">
          <input
            id="api-tokens-include-revoked"
            type="checkbox"
            className="checkbox checkbox-sm"
            checked={includeRevoked}
            onChange={event => setIncludeRevoked(event.target.checked)}
          />
          <label htmlFor="api-tokens-include-revoked" className="text-sm text-fd-body">
            Show revoked tokens
          </label>
        </div>
        <p className="mt-1 text-[11px] text-fd-mute">The record of who held access. A revoke is one-way.</p>
      </div>

      <DataTable<ApiToken>
        columns={columns}
        data={tokens}
        rowKey={row => row.id}
        loading={loading}
        error={loadError ? "Couldn't load API tokens." : false}
        onRetry={loadTokens}
        rowClassName={row => (row.revoked ? 'opacity-70' : '')}
        empty={{
          icon: KeyIcon,
          title: includeRevoked ? 'No API tokens yet' : 'No active API tokens',
          description: 'Issue one to connect a bot or an AI connector.',
        }}
        dense
      />

      <ApiTokenIssueDialog
        open={issueOpen}
        userOptions={userOptions}
        aiEgressAllowed={aiEgressAllowed}
        onCancel={() => setIssueOpen(false)}
        onIssued={handleIssued}
      />

      <ApiTokenRevealDialog
        issued={issued}
        holderName={issued ? holderName(issued.user_id) : ''}
        aiEgressAllowed={aiEgressAllowed}
        onDone={() => setIssued(null)}
      />

      <InputDialog
        open={revokeTarget !== null}
        title="Revoke API token"
        message={
          revokeTarget
            ? `Revoke “${revokeTarget.label}” (acts as ${holderName(revokeTarget.user_id)})? Its holder is refused on its very next request. This cannot be undone.`
            : undefined
        }
        label="Reason"
        placeholder="Why is this token being revoked?"
        submitLabel="Revoke"
        pending={revokePending}
        onSubmit={handleRevoke}
        onCancel={() => {
          if (!revokePending) setRevokeTarget(null);
        }}
      />
    </div>
  );
}
