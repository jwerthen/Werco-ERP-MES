/**
 * /visitor-log — authenticated admin view of the visitor sign-in/out log.
 *
 * Staff (ADMIN/MANAGER/SUPERVISOR) read and export the log; ADMIN/MANAGER can
 * staff-sign-out an on-site visitor, soft-delete a row, and manage the tablet
 * sign-in stations (create / reset-PIN / revoke). All calls go through the
 * normal `api` client (NOT the tablet's isolated signinClient).
 *
 * Built on the shared primitives: <DataTable> (client sort + CSV), <StatusBadge>
 * (signed_in→amber, signed_out→slate), <EmptyState>/<ErrorState>, useToast,
 * useDebouncedValue on the search box, and <Modal> for confirms + station mgmt.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { UserGroupIcon, ArrowRightOnRectangleIcon, TrashIcon, Cog6ToothIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { usePermissions } from '../hooks/usePermissions';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import {
  Button,
  DataTable,
  DataTableColumn,
  EmptyState,
  ErrorState,
  FormField,
  Modal,
  SelectField,
  StatusBadge,
  useToast,
} from '../components/ui';
import { purposeLabel, VISITOR_STATUS_COLORS } from '../components/visitor/visitorConstants';
import { formatCentralDateTime } from '../utils/centralTime';
import type { VisitorLogResponse, SigninStationResponse } from '../types/visitor';

// Shop-local Central date+time; em-dash for an empty timestamp (matches the
// prior local formatter's fallback rather than centralTime's default '-').
function formatDateTime(iso: string | null): string {
  return iso ? formatCentralDateTime(iso) : '—';
}

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'signed_in', label: 'On-site (signed in)' },
  { value: 'signed_out', label: 'Signed out' },
];

export default function VisitorLog() {
  const { showToast } = useToast();
  const { role, isSuperuser } = usePermissions();
  // The route is gated to ADMIN/MANAGER/SUPERVISOR for viewing. Mutations (staff
  // sign-out, soft-delete, station mgmt) are ADMIN/MANAGER only on the server;
  // mirror that in the UI so a Supervisor isn't shown buttons they'll get 403 on.
  const canManage = isSuperuser || role === 'admin' || role === 'manager';

  const [logs, setLogs] = useState<VisitorLogResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [statusFilter, setStatusFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');

  // Action state
  const [signingOutId, setSigningOutId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<VisitorLogResponse | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [stationsOpen, setStationsOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await api.getVisitorLogs({
        status: statusFilter || undefined,
        q: debouncedSearch.trim() || undefined,
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        limit: 200,
      });
      setLogs(res.items);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, debouncedSearch, dateFrom, dateTo]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleStaffSignOut = useCallback(
    async (logRow: VisitorLogResponse) => {
      setSigningOutId(logRow.id);
      try {
        await api.signOutVisitor(logRow.id);
        showToast('success', `Signed out ${logRow.visitor_name}.`);
        await load();
      } catch (err) {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        showToast('error', typeof detail === 'string' ? detail : 'Could not sign out this visitor.');
      } finally {
        setSigningOutId(null);
      }
    },
    [load, showToast]
  );

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.deleteVisitorLog(deleteTarget.id);
      showToast('success', `Removed ${deleteTarget.visitor_name} from the log.`);
      setDeleteTarget(null);
      await load();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      showToast('error', typeof detail === 'string' ? detail : 'Could not remove this entry.');
    } finally {
      setDeleting(false);
    }
  }, [deleteTarget, load, showToast]);

  const columns = useMemo<Array<DataTableColumn<VisitorLogResponse>>>(() => {
    const cols: Array<DataTableColumn<VisitorLogResponse>> = [
      {
        key: 'visitor_name',
        header: 'Visitor',
        sortable: true,
        accessor: r => r.visitor_name,
        render: r => <span className="font-medium text-fd-ink">{r.visitor_name}</span>,
        csv: r => r.visitor_name,
      },
      {
        key: 'visitor_company',
        header: 'Company',
        sortable: true,
        accessor: r => r.visitor_company ?? '',
        render: r => r.visitor_company || <span className="text-fd-mute">—</span>,
        csv: r => r.visitor_company ?? '',
      },
      {
        key: 'host_name',
        header: 'Host',
        sortable: true,
        accessor: r => r.host_name ?? '',
        render: r => r.host_name || <span className="text-fd-mute">—</span>,
        csv: r => r.host_name ?? '',
      },
      {
        key: 'purpose',
        header: 'Purpose',
        sortable: true,
        accessor: r => r.purpose,
        render: r => (
          <span>
            {purposeLabel(r.purpose)}
            {r.purpose === 'other' && r.purpose_note ? <span className="text-fd-mute"> · {r.purpose_note}</span> : null}
          </span>
        ),
        csv: r =>
          r.purpose === 'other' && r.purpose_note
            ? `${purposeLabel(r.purpose)}: ${r.purpose_note}`
            : purposeLabel(r.purpose),
      },
      {
        key: 'signed_in_at',
        header: 'Signed in',
        sortable: true,
        accessor: r => r.signed_in_at,
        render: r => <span className="tabular-nums">{formatDateTime(r.signed_in_at)}</span>,
        csv: r => r.signed_in_at,
      },
      {
        key: 'signed_out_at',
        header: 'Signed out',
        sortable: true,
        accessor: r => r.signed_out_at ?? '',
        render: r =>
          r.signed_out_at ? (
            <span className="tabular-nums">{formatDateTime(r.signed_out_at)}</span>
          ) : (
            <span className="text-fd-mute">On-site</span>
          ),
        csv: r => r.signed_out_at ?? '',
      },
      {
        key: 'station_label',
        header: 'Station',
        sortable: true,
        accessor: r => r.station_label ?? '',
        render: r => r.station_label || <span className="text-fd-mute">Staff</span>,
        csv: r => r.station_label ?? 'Staff',
      },
      {
        key: 'status',
        header: 'Status',
        sortable: true,
        accessor: r => r.status,
        render: r => <StatusBadge status={r.status} colorMap={VISITOR_STATUS_COLORS} />,
        csv: r => r.status,
      },
    ];

    if (canManage) {
      cols.push({
        key: 'actions',
        header: '',
        align: 'right',
        render: r => (
          <div className="flex items-center justify-end gap-2">
            {r.status === 'signed_in' && (
              <button
                type="button"
                onClick={() => void handleStaffSignOut(r)}
                disabled={signingOutId === r.id}
                className="inline-flex items-center gap-1 rounded-sm border border-fd-line px-2 py-1 text-xs font-mono uppercase tracking-wider text-slate-300 transition-colors hover:bg-slate-700/40 disabled:opacity-40"
                title="Sign out this visitor"
              >
                <ArrowRightOnRectangleIcon className="h-4 w-4" aria-hidden="true" />
                {signingOutId === r.id ? 'Signing out…' : 'Sign out'}
              </button>
            )}
            <button
              type="button"
              onClick={() => setDeleteTarget(r)}
              className="inline-flex items-center justify-center rounded-sm border border-fd-line p-1 text-fd-mute transition-colors hover:border-fd-red/50 hover:text-fd-red"
              aria-label={`Remove ${r.visitor_name} from the log`}
              title="Remove entry"
            >
              <TrashIcon className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        ),
      });
    }

    return cols;
  }, [canManage, signingOutId, handleStaffSignOut]);

  const onSiteCount = useMemo(() => logs.filter(l => l.status === 'signed_in').length, [logs]);

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-fd-ink">Visitor Log</h1>
          <p className="mt-1 text-sm text-fd-mute">
            {onSiteCount} visitor{onSiteCount === 1 ? '' : 's'} currently on-site
          </p>
        </div>
        {canManage && (
          <Button variant="secondary" size="sm" onClick={() => setStationsOpen(true)}>
            <Cog6ToothIcon className="mr-1.5 inline h-4 w-4" aria-hidden="true" />
            Stations
          </Button>
        )}
      </div>

      {/* Filters */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label htmlFor="visitor-search" className="label">
            Search
          </label>
          <input
            id="visitor-search"
            type="search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Name, company, host…"
            className="input w-full"
          />
        </div>
        <FormField label="Status">
          <SelectField
            value={statusFilter}
            onChange={value => setStatusFilter(value)}
            options={STATUS_OPTIONS}
            ariaLabel="Filter by status"
          />
        </FormField>
        <div>
          <label htmlFor="visitor-date-from" className="label">
            From
          </label>
          <input
            id="visitor-date-from"
            type="date"
            value={dateFrom}
            onChange={e => setDateFrom(e.target.value)}
            className="input w-full"
          />
        </div>
        <div>
          <label htmlFor="visitor-date-to" className="label">
            To
          </label>
          <input
            id="visitor-date-to"
            type="date"
            value={dateTo}
            onChange={e => setDateTo(e.target.value)}
            className="input w-full"
          />
        </div>
      </div>

      {/* Table (or error / empty) */}
      {error ? (
        <ErrorState message="Could not load the visitor log." onRetry={() => void load()} />
      ) : (
        <DataTable
          columns={columns}
          data={logs}
          rowKey={r => r.id}
          loading={loading}
          defaultSort={{ key: 'signed_in_at', dir: 'desc' }}
          pageSize={25}
          csvExport={{ filename: 'visitor-log.csv' }}
          empty={{
            icon: UserGroupIcon,
            title: 'No visitors yet',
            description: 'Sign-ins from the lobby tablet will appear here.',
          }}
          mobileCards={r => (
            <div className="rounded-sm border border-slate-700 bg-fd-panel p-4">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-fd-ink">{r.visitor_name}</span>
                <StatusBadge status={r.status} colorMap={VISITOR_STATUS_COLORS} />
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-fd-body">
                <dt className="text-fd-mute">Company</dt>
                <dd className="text-right">{r.visitor_company || '—'}</dd>
                <dt className="text-fd-mute">Host</dt>
                <dd className="text-right">{r.host_name || '—'}</dd>
                <dt className="text-fd-mute">Purpose</dt>
                <dd className="text-right">{purposeLabel(r.purpose)}</dd>
                <dt className="text-fd-mute">Signed in</dt>
                <dd className="text-right tabular-nums">{formatDateTime(r.signed_in_at)}</dd>
                <dt className="text-fd-mute">Signed out</dt>
                <dd className="text-right tabular-nums">
                  {r.signed_out_at ? formatDateTime(r.signed_out_at) : 'On-site'}
                </dd>
              </dl>
              {canManage && (
                <div className="mt-3 flex items-center justify-end gap-2">
                  {r.status === 'signed_in' && (
                    <button
                      type="button"
                      onClick={() => void handleStaffSignOut(r)}
                      disabled={signingOutId === r.id}
                      className="inline-flex items-center gap-1 rounded-sm border border-fd-line px-2 py-1 text-xs font-mono uppercase tracking-wider text-slate-300 disabled:opacity-40"
                    >
                      <ArrowRightOnRectangleIcon className="h-4 w-4" aria-hidden="true" />
                      Sign out
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setDeleteTarget(r)}
                    className="inline-flex items-center justify-center rounded-sm border border-fd-line p-1 text-fd-mute hover:text-fd-red"
                    aria-label={`Remove ${r.visitor_name} from the log`}
                  >
                    <TrashIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                </div>
              )}
            </div>
          )}
        />
      )}

      {/* Soft-delete confirm */}
      <Modal
        open={deleteTarget != null}
        onClose={() => setDeleteTarget(null)}
        size="md"
        ariaLabelledBy="visitor-delete-title"
      >
        <h2 id="visitor-delete-title" className="text-lg font-bold text-fd-ink">
          Remove visitor entry?
        </h2>
        <p className="mt-2 text-sm text-fd-body">
          This removes <span className="font-semibold text-fd-ink">{deleteTarget?.visitor_name}</span>&apos;s entry from
          the log. The record is soft-deleted (it stays in the audit trail), not permanently erased.
        </p>
        <div className="mt-5 flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => void confirmDelete()} disabled={deleting}>
            {deleting ? 'Removing…' : 'Remove'}
          </Button>
        </div>
      </Modal>

      {/* Station management */}
      {stationsOpen && canManage && <StationManagementModal onClose={() => setStationsOpen(false)} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Station management modal: list stations, create, reset PIN, revoke, show URL.
// ---------------------------------------------------------------------------

const PIN_RE = /^\d{4,8}$/;

function StationManagementModal({ onClose }: { onClose: () => void }) {
  const { showToast } = useToast();
  const [stations, setStations] = useState<SigninStationResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  // Create form
  const [newLabel, setNewLabel] = useState('');
  const [newPin, setNewPin] = useState('');
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<{ label?: string; pin?: string }>({});

  // Reset-PIN inline editor (per station id)
  const [resetForId, setResetForId] = useState<number | null>(null);
  const [resetPin, setResetPin] = useState('');
  const [resetting, setResetting] = useState(false);
  const [revokingId, setRevokingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await api.getSigninStations();
      setStations(res.stations);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const tabletUrl = useCallback((id: number) => `${window.location.origin}/visitor-signin?station=${id}`, []);

  const copyUrl = useCallback(
    async (id: number) => {
      const url = tabletUrl(id);
      try {
        await navigator.clipboard.writeText(url);
        showToast('success', 'Tablet URL copied.');
      } catch {
        showToast('info', url);
      }
    },
    [showToast, tabletUrl]
  );

  const handleCreate = useCallback(async () => {
    const errs: { label?: string; pin?: string } = {};
    if (!newLabel.trim()) errs.label = 'Give the station a label.';
    if (!PIN_RE.test(newPin)) errs.pin = 'PIN must be 4–8 digits.';
    setFormError(errs);
    if (errs.label || errs.pin) return;
    setCreating(true);
    try {
      await api.createSigninStation({ label: newLabel.trim(), pin: newPin });
      showToast('success', `Station “${newLabel.trim()}” created.`);
      setNewLabel('');
      setNewPin('');
      await load();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      showToast('error', typeof detail === 'string' ? detail : 'Could not create the station.');
    } finally {
      setCreating(false);
    }
  }, [newLabel, newPin, load, showToast]);

  const handleResetPin = useCallback(
    async (id: number) => {
      if (!PIN_RE.test(resetPin)) {
        showToast('error', 'PIN must be 4–8 digits.');
        return;
      }
      setResetting(true);
      try {
        await api.resetSigninStationPin(id, resetPin);
        showToast('success', 'PIN updated.');
        setResetForId(null);
        setResetPin('');
        await load();
      } catch (err) {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        showToast('error', typeof detail === 'string' ? detail : 'Could not reset the PIN.');
      } finally {
        setResetting(false);
      }
    },
    [resetPin, load, showToast]
  );

  const handleRevoke = useCallback(
    async (id: number) => {
      setRevokingId(id);
      try {
        await api.revokeSigninStation(id);
        showToast('success', 'Station revoked.');
        await load();
      } catch (err) {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        showToast('error', typeof detail === 'string' ? detail : 'Could not revoke the station.');
      } finally {
        setRevokingId(null);
      }
    },
    [load, showToast]
  );

  return (
    <Modal open onClose={onClose} size="2xl" ariaLabelledBy="stations-title">
      <h2 id="stations-title" className="text-lg font-bold text-fd-ink">
        Sign-in stations
      </h2>
      <p className="mt-1 text-sm text-fd-mute">
        Each station is a shared-PIN tablet at an entrance. Open its URL on the tablet and unlock once with the PIN.
      </p>

      {/* Create */}
      <div className="mt-4 rounded-sm border border-fd-line bg-fd-sunken p-4">
        <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-fd-mute">Add a station</h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <FormField label="Label" required error={formError.label} className="sm:col-span-1">
            {field => (
              <input
                {...field}
                type="text"
                value={newLabel}
                onChange={e => setNewLabel(e.target.value)}
                placeholder="Lobby Tablet"
                className={formError.label ? 'input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <FormField label="PIN (4–8 digits)" required error={formError.pin} className="sm:col-span-1">
            {field => (
              <input
                {...field}
                type="text"
                inputMode="numeric"
                value={newPin}
                onChange={e => setNewPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                placeholder="0000"
                className={formError.pin ? 'input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <div className="flex items-end sm:col-span-1">
            <Button variant="primary" onClick={() => void handleCreate()} disabled={creating} className="w-full">
              {creating ? 'Creating…' : 'Create station'}
            </Button>
          </div>
        </div>
      </div>

      {/* List */}
      <div className="mt-4">
        {error ? (
          <ErrorState message="Could not load stations." onRetry={() => void load()} />
        ) : loading ? (
          <p className="py-6 text-center text-sm text-fd-mute">Loading stations…</p>
        ) : stations.length === 0 ? (
          <EmptyState
            icon={Cog6ToothIcon}
            title="No stations yet"
            description="Create a station above to provision a tablet."
          />
        ) : (
          <ul className="space-y-2">
            {stations.map(s => (
              <li key={s.id} className="rounded-sm border border-fd-line bg-fd-panel p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-fd-ink">{s.label}</span>
                      <StatusBadge
                        status={s.revoked ? 'revoked' : 'active'}
                        colorMap={{ revoked: 'bg-red-500/20 text-red-300', active: 'bg-green-500/20 text-emerald-300' }}
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => void copyUrl(s.id)}
                      className="mt-1 max-w-full truncate font-mono text-xs text-fd-blue hover:underline"
                      title="Copy tablet URL"
                    >
                      {tabletUrl(s.id)}
                    </button>
                  </div>
                  {!s.revoked && (
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          setResetForId(resetForId === s.id ? null : s.id);
                          setResetPin('');
                        }}
                      >
                        Reset PIN
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => void handleRevoke(s.id)}
                        disabled={revokingId === s.id}
                      >
                        {revokingId === s.id ? 'Revoking…' : 'Revoke'}
                      </Button>
                    </div>
                  )}
                </div>

                {resetForId === s.id && !s.revoked && (
                  <div className="mt-3 flex items-end gap-2 border-t border-fd-line pt-3">
                    <div className="flex-1">
                      <label htmlFor={`reset-pin-${s.id}`} className="label">
                        New PIN (4–8 digits)
                      </label>
                      <input
                        id={`reset-pin-${s.id}`}
                        type="text"
                        inputMode="numeric"
                        value={resetPin}
                        onChange={e => setResetPin(e.target.value.replace(/\D/g, '').slice(0, 8))}
                        placeholder="0000"
                        className="input w-full"
                      />
                    </div>
                    <Button variant="primary" size="sm" onClick={() => void handleResetPin(s.id)} disabled={resetting}>
                      {resetting ? 'Saving…' : 'Save'}
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-5 flex justify-end">
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
      </div>
    </Modal>
  );
}
