/**
 * Admin > SMS Privacy tab.
 *
 * Per-company console for the SMS egress kill switch (``allow_sms_egress`` on the
 * company). This is a CUI / data-egress control that governs whether notification
 * SMS bodies may be transmitted to the commercial SMS carrier (Twilio). When OFF,
 * no message text leaves the system boundary to the carrier and every SMS
 * notification is suppressed server-side (fail-closed) — the in-app inbox and
 * email channels are unaffected.
 *
 * UX mirrors the sibling AI / carrier / print egress kill switches: a status
 * banner, a labeled switch with impact helper text, and an explicit confirmation
 * before turning egress ON (turning it OFF is immediate). Flipping it is recorded
 * on the tamper-evident audit trail server-side.
 *
 * NOTE: no Twilio credential is ever read, written, or displayed here — the
 * account SID / auth token / from-number live only in server-side settings. This
 * console toggles one boolean.
 *
 * RBAC: this tab lives inside AdminSettings, which is route-gated on the
 * ``admin:settings`` permission (admin / superuser). The backend write
 * (PUT /companies/me/sms-egress) requires ADMIN — mirroring the sibling egress
 * kill switches — so the toggle is enabled for ADMIN only and rendered read-only
 * for any other role that somehow reaches it (defense in depth matching the
 * server contract).
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  CheckCircleIcon,
  ChatBubbleLeftRightIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';
import { useToast } from '../ui/Toast';
import { LoadingButton } from '../ui/LoadingButton';
import { Modal } from '../ui/Modal';
import { useOptimisticMutation } from '../../hooks/useOptimisticMutation';
import { useAuth } from '../../context/AuthContext';
import type { Company, UserRole } from '../../types';

const errorDetail = (err: any, fallback: string): string =>
  err?.response?.data?.detail || err?.message || fallback;

// Roles permitted to flip the kill switch (mirrors the backend ADMIN-only
// requirement on PUT /companies/me/sms-egress, matching the sibling AI /
// carrier / print egress controls). Superusers (platform admins) also qualify.
const EGRESS_EDITOR_ROLES: ReadonlySet<UserRole> = new Set<UserRole>(['admin', 'platform_admin']);

export default function SmsEgressTab() {
  const { showToast } = useToast();
  const { user } = useAuth();

  // Last state the SERVER confirmed — the rollback target for the optimistic flip.
  const [serverEnabled, setServerEnabled] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [confirmEgress, setConfirmEgress] = useState(false);

  const canEdit = !!user && (user.is_superuser === true || EGRESS_EDITOR_ROLES.has(user.role));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = (await api.getCurrentCompany()) as Company;
      setServerEnabled(!!data.allow_sms_egress);
      setEnabled(!!data.allow_sms_egress);
    } catch (err) {
      showToast('error', errorDetail(err, 'Failed to load company SMS settings'));
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  // The egress toggle is a rarely-rejected write, so it follows the repo's
  // optimistic-UI convention: flip synchronously, roll back + surface the
  // server's verbatim `detail` on failure (never a success toast for a failure).
  const { run: persist, pending: saving } = useOptimisticMutation<Company, boolean>({
    applyOptimistic: (next) => setEnabled(next),
    rollback: () => setEnabled(serverEnabled),
    mutate: (next) => api.updateCompanySmsEgress(next),
    reconcile: (updated) => {
      setServerEnabled(!!updated.allow_sms_egress);
      setEnabled(!!updated.allow_sms_egress);
      showToast('success', updated.allow_sms_egress ? 'SMS egress enabled' : 'SMS egress disabled');
    },
    errorFallback: 'Failed to update SMS egress',
  });

  // Enabling egress is a CUI / data-egress control — require an explicit
  // confirmation before flipping it ON. Disabling is immediate.
  const handleToggle = (checked: boolean) => {
    if (!canEdit) return;
    if (checked) {
      setConfirmEgress(true);
    } else {
      void persist(false);
    }
  };

  const confirmEnableEgress = () => {
    setConfirmEgress(false);
    void persist(true);
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
            <ChatBubbleLeftRightIcon className="h-5 w-5 text-werco-600" />
            SMS Notification Egress
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            Controls whether notification text messages may be sent to the commercial SMS carrier for
            delivery to employees&apos; mobile phones.
          </p>
        </div>

        {!canEdit && (
          <div className="mb-4 flex items-start gap-2 rounded border border-surface-200 bg-surface-50 px-3 py-2 text-xs text-surface-500">
            <LockClosedIcon className="h-4 w-4 flex-shrink-0 mt-0.5" />
            <span>Only an Administrator can change this control. It is shown here read-only.</span>
          </div>
        )}

        {/* Egress kill switch */}
        <div className={`rounded border px-4 py-4 ${enabled ? 'border-red-500/40 bg-red-500/5' : 'border-surface-200'}`}>
          <label
            htmlFor="sms-egress-toggle"
            className={`grid grid-cols-[auto_1fr] items-start gap-x-3 ${canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-80'}`}
          >
            <input
              id="sms-egress-toggle"
              type="checkbox"
              aria-label="Allow SMS egress"
              className="checkbox mt-0.5 row-span-2"
              checked={enabled}
              disabled={!canEdit || saving}
              onChange={(e) => handleToggle(e.target.checked)}
            />
            <span className="flex items-center gap-2 text-sm font-semibold text-surface-800">
              <ExclamationTriangleIcon className="h-4 w-4 text-amber-500" />
              Allow SMS egress
            </span>
            <span className="block text-xs text-surface-500 mt-1">
              Enabling this transmits notification message bodies and employee mobile numbers to a commercial
              SMS carrier outside the system boundary. Message bodies are deliberately terse — record type,
              number, and event only, never customer names, part descriptions, or quantities — but they still
              leave the boundary and may be CUI-adjacent under some DoD contracts. Obtain CUI / data-egress
              sign-off before enabling. When OFF, no SMS is sent and the in-app inbox and email channels are
              unaffected. Off by default for new companies; toggling it is recorded on the tamper-evident audit
              trail.
            </span>
          </label>
        </div>

        <p className="mt-3 text-xs text-surface-500">
          Individual employees still choose their own SMS opt-ins (default off) and must save a mobile number in
          My Settings before any message is sent to them.
        </p>
      </section>

      {/* Explicit confirmation before enabling SMS egress (CUI control). */}
      <Modal open={confirmEgress} onClose={() => setConfirmEgress(false)} size="md" padded={false}>
        <div className="modal-header">
          <h3 className="text-lg font-semibold flex items-center gap-2">
            <ExclamationTriangleIcon className="h-5 w-5 text-amber-500" />
            Enable SMS egress?
          </h3>
          <button onClick={() => setConfirmEgress(false)} className="p-2 rounded-lg hover:bg-surface-100">
            <span className="sr-only">Close</span>
            <XMarkIcon className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div className="p-5">
          <p className="text-sm text-surface-700">
            Enabling SMS egress will transmit notification message bodies and employee mobile numbers to a
            commercial SMS carrier outside the system boundary. This content may be CUI-adjacent under some DoD
            contracts and requires CUI / data-egress sign-off. The change is recorded on the tamper-evident
            audit trail and takes effect immediately company-wide.
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
          <p className="font-semibold text-green-400">SMS egress is ENABLED</p>
          <p className="text-surface-500 mt-0.5">
            Notification text messages can be transmitted to the commercial SMS carrier for employees who have
            opted in and saved a mobile number. Disable below if egress is no longer authorized.
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-3 rounded border border-amber-500/40 bg-amber-500/5 px-4 py-3">
      <ExclamationTriangleIcon className="h-5 w-5 text-amber-500 flex-shrink-0 mt-0.5" />
      <div className="text-sm">
        <p className="font-semibold text-amber-400">SMS egress is DISABLED</p>
        <p className="text-surface-500 mt-0.5">
          No message text or mobile number leaves the system boundary to the SMS carrier, and every SMS
          notification is suppressed. The in-app inbox and email channels are unaffected. Enabling egress sends
          message bodies to a third-party carrier and requires CUI / data-egress sign-off first.
        </p>
      </div>
    </div>
  );
}
