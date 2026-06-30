/**
 * Admin > AI Privacy tab.
 *
 * Per-company console for the AI egress kill switch (``allow_ai_egress`` on the
 * company). This is a CUI / data-egress control that governs whether the
 * company's uploaded documents may be transmitted to the Anthropic AI provider
 * for extraction (PO/quote, BOM, QMS clause, routing generation, laser-nest
 * PDFs) and whether the AI copilot + natural-language search may run. When OFF,
 * no document content leaves the system boundary to the AI provider and those
 * features degrade gracefully (e.g. laser-nest extraction falls back to
 * filename-only).
 *
 * UX mirrors the sibling carrier / print egress kill switches: a status banner,
 * a labeled switch with impact helper text, and an explicit confirmation before
 * turning egress ON (turning it OFF is immediate). Flipping it is recorded on
 * the tamper-evident audit trail server-side.
 *
 * RBAC: this tab lives inside AdminSettings, which is route-gated on the
 * ``admin:settings`` permission (admin / superuser). The backend write
 * (PUT /companies/me/ai-egress) requires ADMIN — mirroring the sibling
 * carrier / print egress kill switches — so the toggle is enabled for ADMIN
 * only and rendered read-only for any other role that somehow reaches it
 * (defense in depth matching the server contract).
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  CpuChipIcon,
  LockClosedIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { LoadingButton } from '../ui/LoadingButton';
import { Modal } from '../ui/Modal';
import { useAuth } from '../../context/AuthContext';
import type { Company, UserRole } from '../../types';

const errorDetail = (err: any, fallback: string): string =>
  err?.response?.data?.detail || err?.message || fallback;

// Roles permitted to flip the kill switch (mirrors the backend ADMIN-only
// requirement on PUT /companies/me/ai-egress, matching the sibling carrier /
// print egress controls). Superusers (platform admins) also qualify via the
// admin path.
const EGRESS_EDITOR_ROLES: ReadonlySet<UserRole> = new Set<UserRole>(['admin', 'platform_admin']);

export default function AIEgressTab() {
  const { showToast } = useToast();
  const { user } = useAuth();

  const [company, setCompany] = useState<Company | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmEgress, setConfirmEgress] = useState(false);

  const canEdit = !!user && (user.is_superuser === true || EGRESS_EDITOR_ROLES.has(user.role));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = (await api.getCurrentCompany()) as Company;
      setCompany(data);
      setEnabled(!!data.allow_ai_egress);
    } catch (err) {
      showToast('error', errorDetail(err, 'Failed to load company AI settings'));
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  // Persist a new egress value. Turning it OFF is immediate; turning it ON is
  // routed through an explicit confirmation by the caller.
  const persist = useCallback(
    async (next: boolean) => {
      setSaving(true);
      try {
        const updated = await api.updateCompanyAiEgress(next);
        setCompany(updated);
        setEnabled(!!updated.allow_ai_egress);
        showToast(
          'success',
          updated.allow_ai_egress ? 'AI egress enabled' : 'AI egress disabled',
        );
      } catch (err) {
        // Roll the optimistic switch back to the server's last-known state.
        setEnabled(!!company?.allow_ai_egress);
        showToast('error', errorDetail(err, 'Failed to update AI egress'));
      } finally {
        setSaving(false);
      }
    },
    [company, showToast],
  );

  // Enabling egress is a CUI / data-egress control — require an explicit
  // confirmation before flipping it ON. Disabling is immediate.
  const handleToggle = (checked: boolean) => {
    if (!canEdit) return;
    if (checked) {
      setConfirmEgress(true);
    } else {
      persist(false);
    }
  };

  const confirmEnableEgress = () => {
    setConfirmEgress(false);
    persist(true);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Egress status banner */}
      <EgressBanner enabled={enabled} />

      <section>
        <div className="mb-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
            <CpuChipIcon className="h-5 w-5 text-werco-600" />
            AI Document Egress
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            Controls whether this company&apos;s uploaded documents may be sent to the Anthropic AI provider for
            extraction and assistance.
          </p>
        </div>

        {!canEdit && (
          <div className="mb-4 flex items-start gap-2 rounded border border-surface-200 bg-surface-50 px-3 py-2 text-xs text-surface-500">
            <LockClosedIcon className="h-4 w-4 flex-shrink-0 mt-0.5" />
            <span>
              Only an Administrator can change this control. It is shown here read-only.
            </span>
          </div>
        )}

        {/* Egress kill switch */}
        <div className={`rounded border px-4 py-4 ${enabled ? 'border-red-500/40 bg-red-500/5' : 'border-surface-200'}`}>
          <label
            htmlFor="ai-egress-toggle"
            className={`grid grid-cols-[auto_1fr] items-start gap-x-3 ${canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-80'}`}
          >
            <input
              id="ai-egress-toggle"
              type="checkbox"
              aria-label="Allow AI egress"
              className="checkbox mt-0.5 row-span-2"
              checked={enabled}
              disabled={!canEdit || saving}
              onChange={(e) => handleToggle(e.target.checked)}
            />
            <span className="flex items-center gap-2 text-sm font-semibold text-surface-800">
              <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
              Allow AI egress
            </span>
            <span className="block text-xs text-surface-500 mt-1">
              Enabling this transmits uploaded document content to the Anthropic AI provider for PO/quote, BOM,
              QMS-clause, routing-generation, and laser-nest extraction, and powers the AI copilot and
              natural-language search. This content may be CUI under some DoD contracts — obtain CUI /
              data-egress sign-off before enabling. When OFF, no document content leaves the system boundary
              and AI features degrade gracefully (e.g. laser-nest extraction falls back to filename-only).
              Off by default for new companies; toggling it is recorded on the tamper-evident audit trail.
            </span>
          </label>
        </div>
      </section>

      {/* Explicit confirmation before enabling AI egress (CUI control). */}
      <Modal open={confirmEgress} onClose={() => setConfirmEgress(false)} size="md" padded={false}>
        <div className="modal-header">
          <h3 className="text-lg font-semibold flex items-center gap-2">
            <ExclamationTriangleIcon className="h-5 w-5 text-amber-500" />
            Enable AI egress?
          </h3>
          <button onClick={() => setConfirmEgress(false)} className="p-2 rounded-lg hover:bg-surface-100">
            <span className="sr-only">Close</span>
            <XMarkIcon className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div className="p-5">
          <p className="text-sm text-surface-700">
            Enabling AI egress will transmit uploaded document content to the Anthropic AI provider for
            extraction and assistance. This content may be CUI under some DoD contracts and requires CUI /
            data-egress sign-off. The change is recorded on the tamper-evident audit trail and takes effect
            immediately company-wide.
          </p>
        </div>
        <div className="modal-footer flex justify-end gap-2 p-5 pt-0">
          <button onClick={() => setConfirmEgress(false)} className="btn-secondary">
            Cancel
          </button>
          <LoadingButton
            type="button"
            variant="danger"
            loading={saving}
            loadingText="Enabling…"
            onClick={confirmEnableEgress}
          >
            Enable egress
          </LoadingButton>
        </div>
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Egress status banner.
// ---------------------------------------------------------------------------

function EgressBanner({ enabled }: { enabled: boolean }) {
  if (enabled) {
    return (
      <div className="flex items-start gap-3 rounded border border-green-500/40 bg-green-500/5 px-4 py-3">
        <CheckCircleIcon className="h-5 w-5 text-green-500 flex-shrink-0 mt-0.5" />
        <div className="text-sm">
          <p className="font-semibold text-green-400">AI egress is ENABLED</p>
          <p className="text-surface-500 mt-0.5">
            Uploaded document content can be transmitted to the Anthropic AI provider for extraction, the AI
            copilot, and natural-language search. Disable below if egress is no longer authorized.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-3 rounded border border-amber-500/40 bg-amber-500/5 px-4 py-3">
      <ExclamationTriangleIcon className="h-5 w-5 text-amber-500 flex-shrink-0 mt-0.5" />
      <div className="text-sm">
        <p className="font-semibold text-amber-400">AI egress is DISABLED</p>
        <p className="text-surface-500 mt-0.5">
          No document content leaves the system boundary to the AI provider. AI-backed extraction, the copilot,
          and natural-language search are degraded (e.g. laser-nest extraction falls back to filename-only).
          Enabling egress transmits document content to a third-party AI provider and requires CUI /
          data-egress sign-off first.
        </p>
      </div>
    </div>
  );
}
