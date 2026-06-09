/**
 * Inline tracking display for a shipment.
 *
 * Lazily fetches ``getTracking`` (read-only, NOT egress-gated — it serves data
 * already flowed back from inbound carrier webhooks) and renders the current
 * tracking status + the latest events. Replaces the old manual ``prompt()``
 * tracking-number capture entirely.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { ArrowPathIcon, MapPinIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { formatCentralDateTime } from '../../utils/centralTime';
import type { ShipmentTracking } from '../../types/shipping';

const STATUS_STYLES: Record<string, string> = {
  delivered: 'bg-emerald-500/20 text-emerald-300',
  out_for_delivery: 'bg-blue-500/20 text-blue-300',
  in_transit: 'bg-blue-500/20 text-blue-300',
  pre_transit: 'bg-yellow-500/20 text-yellow-300',
  available_for_pickup: 'bg-yellow-500/20 text-yellow-300',
  return_to_sender: 'bg-red-500/20 text-red-300',
  failure: 'bg-red-500/20 text-red-300',
  cancelled: 'bg-red-500/20 text-red-300',
  unknown: 'bg-slate-500/20 text-slate-300',
};

const statusClass = (status?: string | null): string =>
  STATUS_STYLES[(status || 'unknown').toLowerCase()] || STATUS_STYLES.unknown;

const prettyStatus = (status?: string | null): string =>
  (status || 'unknown').replace(/_/g, ' ');

export default function ShipmentTrackingPanel({ shipmentId }: { shipmentId: number }) {
  const { showToast } = useToast();
  const [tracking, setTracking] = useState<ShipmentTracking | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getTracking(shipmentId);
      setTracking(data);
    } catch (err) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      showToast('error', detail || 'Failed to load tracking');
    } finally {
      setLoading(false);
    }
  }, [shipmentId, showToast]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return <div className="px-4 py-3 text-sm text-slate-400">Loading tracking…</div>;
  }

  if (!tracking) {
    return <div className="px-4 py-3 text-sm text-slate-400">No tracking available.</div>;
  }

  return (
    <div className="px-4 py-3 space-y-3 bg-slate-900/40 border-t border-slate-700">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`px-2 py-1 text-xs font-medium ${statusClass(tracking.tracking_status)}`}>
          {prettyStatus(tracking.tracking_status)}
        </span>
        {tracking.tracking_number && (
          <span className="font-mono text-sm text-slate-300">{tracking.tracking_number}</span>
        )}
        {tracking.tracking_status_detail && (
          <span className="text-xs text-slate-400">{tracking.tracking_status_detail}</span>
        )}
        {tracking.actual_delivery && (
          <span className="text-xs text-emerald-300">
            Delivered {formatCentralDateTime(tracking.actual_delivery)}
          </span>
        )}
        <button
          onClick={load}
          className="ml-auto p-1.5 text-slate-500 hover:text-werco-primary"
          title="Refresh tracking"
        >
          <ArrowPathIcon className="h-4 w-4" />
        </button>
      </div>

      {tracking.events.length === 0 ? (
        <p className="text-xs text-slate-500">No tracking events yet.</p>
      ) : (
        <ol className="space-y-2">
          {tracking.events.slice(0, 8).map((e, i) => (
            <li key={e.id ?? i} className="flex items-start gap-2 text-xs">
              <MapPinIcon className="h-3.5 w-3.5 text-slate-500 mt-0.5 flex-shrink-0" />
              <div>
                <span className="text-slate-200 font-medium">{prettyStatus(e.status)}</span>
                {e.message && <span className="text-slate-400"> — {e.message}</span>}
                <div className="text-slate-500">
                  {e.location ? `${e.location} · ` : ''}
                  {e.occurred_at ? formatCentralDateTime(e.occurred_at) : ''}
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      {tracking.last_tracking_sync_at && (
        <p className="text-[11px] text-slate-600">
          Last synced {formatCentralDateTime(tracking.last_tracking_sync_at)}
        </p>
      )}
    </div>
  );
}
