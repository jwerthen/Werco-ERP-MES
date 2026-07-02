import React, { useCallback, useEffect, useState } from 'react';
import { ComputerDesktopIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { Button, EmptyState, ErrorState, FormField, StatusBadge, useToast } from '../ui';
import type { WorkCenter } from '../../types';
import type { KioskStationResponse } from '../../types/kioskStation';

const PIN_RE = /^\d{4,8}$/;

interface KioskStationsAdminModalProps {
  /** Work centers to bind a new station to (the page already has them loaded). */
  workCenters: WorkCenter[];
  onClose: () => void;
}

/**
 * Crew-station kiosk management: list stations, create (work-center-bound),
 * reset PIN, revoke, and copy the terminal URL (/kiosk?kiosk=1&station=<id>).
 * A VisitorLog StationManagementModal twin — stations are revoked (flag flip),
 * never deleted, so a lost tablet dies on its next poll.
 */
export default function KioskStationsAdminModal({ workCenters, onClose }: KioskStationsAdminModalProps) {
  const { showToast } = useToast();
  const [stations, setStations] = useState<KioskStationResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  // Create form
  const [newLabel, setNewLabel] = useState('');
  const [newPin, setNewPin] = useState('');
  const [newWorkCenterId, setNewWorkCenterId] = useState<string>('');
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<{ label?: string; pin?: string; workCenter?: string }>({});

  // Reset-PIN inline editor (per station id)
  const [resetForId, setResetForId] = useState<number | null>(null);
  const [resetPin, setResetPin] = useState('');
  const [resetting, setResetting] = useState(false);
  const [revokingId, setRevokingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(false);
    try {
      const res = await api.getKioskStations();
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

  const stationUrl = useCallback((id: number) => `${window.location.origin}/kiosk?kiosk=1&station=${id}`, []);

  const copyUrl = useCallback(
    async (id: number) => {
      const url = stationUrl(id);
      try {
        await navigator.clipboard.writeText(url);
        showToast('success', 'Station URL copied.');
      } catch {
        showToast('info', url);
      }
    },
    [showToast, stationUrl]
  );

  const workCenterLabel = useCallback(
    (station: KioskStationResponse) => {
      if (station.work_center_code || station.work_center_name) {
        return [station.work_center_code, station.work_center_name].filter(Boolean).join(' · ');
      }
      const match = workCenters.find((wc) => wc.id === station.work_center_id);
      return match ? `${match.code} · ${match.name}` : `Work center #${station.work_center_id}`;
    },
    [workCenters]
  );

  const handleCreate = useCallback(async () => {
    const errs: { label?: string; pin?: string; workCenter?: string } = {};
    if (!newLabel.trim()) errs.label = 'Give the station a label.';
    if (!PIN_RE.test(newPin)) errs.pin = 'PIN must be 4–8 digits.';
    if (!newWorkCenterId) errs.workCenter = 'Pick the work center this terminal serves.';
    setFormError(errs);
    if (errs.label || errs.pin || errs.workCenter) return;
    setCreating(true);
    try {
      await api.createKioskStation({
        label: newLabel.trim(),
        pin: newPin,
        work_center_id: Number(newWorkCenterId),
      });
      showToast('success', `Station “${newLabel.trim()}” created.`);
      setNewLabel('');
      setNewPin('');
      setNewWorkCenterId('');
      await load();
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      showToast('error', typeof detail === 'string' ? detail : 'Could not create the station.');
    } finally {
      setCreating(false);
    }
  }, [newLabel, newPin, newWorkCenterId, load, showToast]);

  const handleResetPin = useCallback(
    async (id: number) => {
      if (!PIN_RE.test(resetPin)) {
        showToast('error', 'PIN must be 4–8 digits.');
        return;
      }
      setResetting(true);
      try {
        await api.resetKioskStationPin(id, resetPin);
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
        await api.revokeKioskStation(id);
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
    <Modal open onClose={onClose} size="2xl" ariaLabelledBy="kiosk-stations-title">
      <h2 id="kiosk-stations-title" className="text-lg font-bold text-fd-ink">
        Kiosk stations
      </h2>
      <p className="mt-1 text-sm text-fd-mute">
        Each station is a shared crew terminal bound to one work center. Open its URL on the shop tablet and unlock
        once with the PIN — operators then join and leave jobs by badge scan.
      </p>

      {/* Create */}
      <div className="mt-4 rounded-sm border border-fd-line bg-fd-sunken p-4">
        <h3 className="mb-3 font-mono text-xs uppercase tracking-wider text-fd-mute">Add a station</h3>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <FormField label="Label" required error={formError.label} className="sm:col-span-1">
            {field => (
              <input
                {...field}
                type="text"
                value={newLabel}
                onChange={e => setNewLabel(e.target.value)}
                placeholder="Weld Bay Kiosk"
                className={formError.label ? 'input-error w-full' : 'input w-full'}
              />
            )}
          </FormField>
          <FormField label="Work center" required error={formError.workCenter} className="sm:col-span-1">
            {field => (
              <select
                {...field}
                value={newWorkCenterId}
                onChange={e => setNewWorkCenterId(e.target.value)}
                className={formError.workCenter ? 'input-error w-full' : 'input w-full'}
              >
                <option value="">Select…</option>
                {workCenters.map(wc => (
                  <option key={wc.id} value={wc.id}>
                    {wc.code} · {wc.name}
                  </option>
                ))}
              </select>
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
          <ErrorState message="Could not load kiosk stations." onRetry={() => void load()} />
        ) : loading ? (
          <p className="py-6 text-center text-sm text-fd-mute">Loading stations…</p>
        ) : stations.length === 0 ? (
          <EmptyState
            icon={ComputerDesktopIcon}
            title="No kiosk stations yet"
            description="Create a station above to provision a crew terminal."
          />
        ) : (
          <ul className="space-y-2">
            {stations.map(s => (
              <li key={s.id} className="rounded-sm border border-fd-line bg-fd-panel p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-fd-ink">{s.label}</span>
                      <span className="text-xs text-fd-mute">{workCenterLabel(s)}</span>
                      <StatusBadge
                        status={s.revoked ? 'revoked' : 'active'}
                        colorMap={{ revoked: 'bg-red-500/20 text-red-300', active: 'bg-green-500/20 text-emerald-300' }}
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => void copyUrl(s.id)}
                      className="mt-1 max-w-full truncate font-mono text-xs text-fd-blue hover:underline"
                      title="Copy station URL"
                    >
                      {stationUrl(s.id)}
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
                      <label htmlFor={`kiosk-reset-pin-${s.id}`} className="label">
                        New PIN (4–8 digits)
                      </label>
                      <input
                        id={`kiosk-reset-pin-${s.id}`}
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
