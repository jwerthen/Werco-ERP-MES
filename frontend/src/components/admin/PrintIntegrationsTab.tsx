/**
 * Admin > Label Printing tab.
 *
 * Per-company admin console for the thermal receiving-label feature (ProxyBox /
 * WHTP203e bridge):
 *   - configure the ProxyBox connection (base URL + device target) and the
 *     WRITE-ONLY API key — the read shape only ever exposes ``api_key_last4``,
 *     never a full key;
 *   - set the print defaults (copies, paper size) and the
 *     ``auto_print_on_receipt`` toggle;
 *   - flip the ``allow_print_egress`` kill switch, which gates ALL outbound
 *     calls to the ProxyBox tunnel and defaults OFF until a human signs off on
 *     CUI / data-egress. Enabling it requires an explicit confirmation.
 *
 * This tab is rendered inside AdminSettings, which is already route-gated on the
 * ``admin:settings`` permission, so all routes here are admin-only.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PrinterIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { LoadingButton } from '../ui/LoadingButton';
import type { PrintProfile, PrintProfileUpdate } from '../../types/print';

const errorDetail = (err: any, fallback: string): string =>
  err?.response?.data?.detail || err?.message || fallback;

const PAPER_SIZE_OPTIONS = ['4x6', '4x3', '2x1', '3x2'];

interface ProfileFormState {
  proxybox_base_url: string;
  proxybox_target: string;
  api_key: string;
  default_copies: number;
  default_paper_size: string;
  auto_print_on_receipt: boolean;
  allow_print_egress: boolean;
}

const buildForm = (p: PrintProfile | null): ProfileFormState => ({
  proxybox_base_url: p?.proxybox_base_url ?? '',
  proxybox_target: p?.proxybox_target ?? '',
  // The stored key is never returned; start blank and only send a new value
  // when the admin types one (otherwise the stored key is preserved).
  api_key: '',
  default_copies: p?.default_copies ?? 1,
  default_paper_size: p?.default_paper_size ?? '4x6',
  auto_print_on_receipt: p?.auto_print_on_receipt ?? false,
  allow_print_egress: p?.allow_print_egress ?? false,
});

export default function PrintIntegrationsTab() {
  const { showToast } = useToast();

  const [profile, setProfile] = useState<PrintProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<ProfileFormState>(() => buildForm(null));
  const [saving, setSaving] = useState(false);
  const [confirmEgress, setConfirmEgress] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    // The print profile 404s until first configured -- treat that as "none yet".
    try {
      const prof = await api.getPrintProfile();
      setProfile(prof);
      setForm(buildForm(prof));
    } catch (err: any) {
      if (err?.response?.status === 404) {
        setProfile(null);
        setForm(buildForm(null));
      } else {
        showToast('error', errorDetail(err, 'Failed to load print profile'));
      }
    }
    setLoading(false);
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  const update = <K extends keyof ProfileFormState>(key: K, value: ProfileFormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const persist = async (overrides?: Partial<ProfileFormState>) => {
    const next = { ...form, ...overrides };
    setSaving(true);
    try {
      const payload: PrintProfileUpdate = {
        proxybox_base_url: next.proxybox_base_url.trim() || null,
        proxybox_target: next.proxybox_target.trim() || null,
        default_copies: next.default_copies,
        default_paper_size: next.default_paper_size.trim() || null,
        auto_print_on_receipt: next.auto_print_on_receipt,
        allow_print_egress: next.allow_print_egress,
      };
      // The API key is write-only: only send it when the admin actually typed a
      // new value, so a blank field never wipes/rotates the stored key.
      if (next.api_key.trim()) {
        payload.api_key = next.api_key.trim();
      }
      const saved = await api.updatePrintProfile(payload);
      setProfile(saved);
      setForm(buildForm(saved));
      showToast('success', 'Print settings saved');
    } catch (err) {
      showToast('error', errorDetail(err, 'Failed to save print settings'));
    } finally {
      setSaving(false);
    }
  };

  // Toggling egress ON is a CUI / data-egress control — require an explicit
  // confirmation before flipping it. Turning it OFF is immediate.
  const handleEgressToggle = (checked: boolean) => {
    if (checked) {
      setConfirmEgress(true);
    } else {
      update('allow_print_egress', false);
    }
  };

  const confirmEnableEgress = () => {
    setConfirmEgress(false);
    update('allow_print_egress', true);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    persist();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  const egressOn = form.allow_print_egress;

  return (
    <div className="space-y-8">
      {/* Egress status banner */}
      <EgressBanner enabled={egressOn} configured={!!profile} />

      <section>
        <div className="mb-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
            <PrinterIcon className="h-5 w-5 text-werco-600" />
            Thermal Label Printer (ProxyBox)
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            Connection for the 4×6 receiving-label printer bridge. The API key is encrypted at rest and never
            displayed in full.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="label">ProxyBox base URL</label>
              <input
                className="input font-mono"
                value={form.proxybox_base_url}
                onChange={(e) => update('proxybox_base_url', e.target.value)}
                placeholder="https://pbx-xxxx.pbxz.cloud/api/v1"
              />
            </div>
            <div>
              <label className="label">Device target</label>
              <input
                className="input font-mono"
                value={form.proxybox_target}
                onChange={(e) => update('proxybox_target', e.target.value)}
                placeholder="Target printer id on the ProxyBox device"
              />
            </div>
            <div>
              <label className="label">
                API Key{' '}
                {profile?.has_api_key && (
                  <span className="text-surface-500 font-normal">(leave blank to keep)</span>
                )}
              </label>
              <input
                type="password"
                autoComplete="new-password"
                className="input font-mono"
                value={form.api_key}
                onChange={(e) => update('api_key', e.target.value)}
                placeholder={
                  profile?.has_api_key && profile?.api_key_last4
                    ? `••••${profile.api_key_last4}`
                    : 'Write-only; encrypted at rest'
                }
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="label">Default copies</label>
                <input
                  type="number"
                  min={1}
                  max={20}
                  step={1}
                  className="input"
                  value={form.default_copies}
                  onChange={(e) => update('default_copies', parseInt(e.target.value, 10) || 1)}
                />
              </div>
              <div>
                <label className="label">Paper size</label>
                <input
                  className="input"
                  list="print-paper-sizes"
                  value={form.default_paper_size}
                  onChange={(e) => update('default_paper_size', e.target.value)}
                  placeholder="4x6"
                />
                <datalist id="print-paper-sizes">
                  {PAPER_SIZE_OPTIONS.map((size) => (
                    <option key={size} value={size} />
                  ))}
                </datalist>
              </div>
            </div>
          </div>

          {/* Auto-print toggle */}
          <div className="rounded border border-surface-200 px-4 py-4">
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="checkbox mt-0.5"
                checked={form.auto_print_on_receipt}
                onChange={(e) => update('auto_print_on_receipt', e.target.checked)}
              />
              <span>
                <span className="text-sm font-semibold text-surface-800">Auto-print on receipt</span>
                <span className="block text-xs text-surface-500 mt-1">
                  Automatically queue a 4×6 label whenever material is received. Requires egress to be enabled
                  below — both must be on for an auto-print to occur.
                </span>
              </span>
            </label>
          </div>

          {/* Egress kill switch */}
          <div className={`rounded border px-4 py-4 ${egressOn ? 'border-red-500/40 bg-red-500/5' : 'border-surface-200'}`}>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="checkbox mt-0.5"
                checked={egressOn}
                onChange={(e) => handleEgressToggle(e.target.checked)}
              />
              <span>
                <span className="flex items-center gap-2 text-sm font-semibold text-surface-800">
                  <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
                  Allow print egress
                </span>
                <span className="block text-xs text-surface-500 mt-1">
                  Enabling this transmits rendered label data to a third-party ProxyBox bridge over the
                  internet. This data may be CUI under some DoD contracts — obtain CUI / data-egress sign-off
                  before enabling. Off by default; toggling it is recorded on the tamper-evident audit trail.
                </span>
              </span>
            </label>
          </div>

          <div className="flex justify-end">
            <LoadingButton type="submit" loading={saving} loadingText="Saving…">
              Save Print Settings
            </LoadingButton>
          </div>
        </form>
      </section>

      {/* Explicit confirmation before enabling print egress (CUI control). */}
      {confirmEgress && (
        <div className="modal-overlay" onClick={() => setConfirmEgress(false)}>
          <div className="modal max-w-md" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold flex items-center gap-2">
                <ExclamationTriangleIcon className="h-5 w-5 text-amber-500" />
                Enable print egress?
              </h3>
              <button onClick={() => setConfirmEgress(false)} className="p-2 rounded-lg hover:bg-surface-100">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <div className="p-5">
              <p className="text-sm text-surface-700">
                Enabling print egress will transmit rendered label data to the third-party ProxyBox bridge over
                the internet. This may be CUI under some DoD contracts and requires CUI / data-egress sign-off.
                The change is recorded on the tamper-evident audit trail.
              </p>
              <p className="text-sm text-surface-700 mt-3">
                You still need to <span className="font-semibold">Save Print Settings</span> for this to take
                effect.
              </p>
            </div>
            <div className="modal-footer flex justify-end gap-2 p-5 pt-0">
              <button onClick={() => setConfirmEgress(false)} className="btn-secondary">Cancel</button>
              <button
                onClick={confirmEnableEgress}
                className="btn bg-red-600 text-white hover:bg-red-700 px-4 py-2"
              >
                Enable egress
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Egress status banner.
// ---------------------------------------------------------------------------

function EgressBanner({ enabled, configured }: { enabled: boolean; configured: boolean }) {
  if (enabled) {
    return (
      <div className="flex items-start gap-3 rounded border border-green-500/40 bg-green-500/5 px-4 py-3">
        <CheckCircleIcon className="h-5 w-5 text-green-500 flex-shrink-0 mt-0.5" />
        <div className="text-sm">
          <p className="font-semibold text-green-400">Print egress is ENABLED</p>
          <p className="text-surface-500 mt-0.5">
            Receiving labels can be transmitted to the configured ProxyBox bridge. Disable below if egress is no
            longer authorized.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-3 rounded border border-amber-500/40 bg-amber-500/5 px-4 py-3">
      <ExclamationTriangleIcon className="h-5 w-5 text-amber-500 flex-shrink-0 mt-0.5" />
      <div className="text-sm">
        <p className="font-semibold text-amber-400">Print egress is DISABLED</p>
        <p className="text-surface-500 mt-0.5">
          {configured
            ? 'Label printing will be rejected until egress is enabled below.'
            : 'No print profile is configured yet. Label printing is disabled until you save a profile and enable egress below.'}{' '}
          Enabling egress transmits label data to a third-party ProxyBox bridge and requires CUI / data-egress
          sign-off first.
        </p>
      </div>
    </div>
  );
}
