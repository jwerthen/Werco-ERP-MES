/**
 * Admin > Badge Sign-in tab.
 *
 * The company's floor-PIN policy (`require_floor_pin`, AS9100D 8.5.2). A badge
 * with a PIN always needs it; this switch decides what happens to a badge WITHOUT
 * one — off, it signs in on the badge alone as before; on, it is refused at the
 * kiosk, the crew station and Badge Only sign-in until a supervisor sets a PIN in
 * Users.
 *
 * Non-optimistic, unlike the sibling egress switches: the write answers with
 * `users_without_pin` — the active users a policy turned ON will refuse at badge sign-in —
 * and that number is the point of the screen, so the page shows only what the
 * server returned. Turning the policy ON asks first (it can stop people signing
 * in); turning it OFF is immediate.
 *
 * RBAC: AdminSettings is route-gated on `admin:settings`; the write
 * (PUT /companies/me/floor-pin-policy) is ADMIN-only and refused to API tokens,
 * so the switch is enabled for Admin only and read-only for anyone else.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ExclamationTriangleIcon, IdentificationIcon, LockClosedIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { ConfirmDialog, ErrorState, useToast } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { useAuth } from '../../context/AuthContext';
import type { Company, FloorPinPolicyResponse, UserRole } from '../../types';

const POLICY_EDITOR_ROLES: ReadonlySet<UserRole> = new Set<UserRole>(['admin', 'platform_admin']);

/** A count the server sent, or null when it sent none (an older backend, or a plain company read). */
function readUsersWithoutPin(data: Partial<FloorPinPolicyResponse> | null | undefined): number | null {
  const value = data?.users_without_pin;
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export default function FloorPinPolicyTab() {
  const { showToast } = useToast();
  const { user } = useAuth();
  const canEdit = !!user && (user.is_superuser === true || POLICY_EDITOR_ROLES.has(user.role));

  const [required, setRequired] = useState(false);
  const [usersWithoutPin, setUsersWithoutPin] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmOn, setConfirmOn] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const data = (await api.getCurrentCompany()) as Company & Partial<FloorPinPolicyResponse>;
      setRequired(!!data.require_floor_pin);
      setUsersWithoutPin(readUsersWithoutPin(data));
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const persist = async (next: boolean) => {
    setSaving(true);
    try {
      const updated = await api.updateCompanyFloorPinPolicy(next);
      const nowRequired = !!updated.require_floor_pin;
      setRequired(nowRequired);
      setUsersWithoutPin(readUsersWithoutPin(updated));
      setConfirmOn(false);
      showToast(
        'success',
        nowRequired ? 'A PIN is now required for badge sign-in' : 'A PIN is no longer required for badge sign-in'
      );
    } catch (err) {
      setConfirmOn(false);
      showToast('error', apiErrorDetail(err, 'Failed to update the badge sign-in policy'));
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = (checked: boolean) => {
    if (!canEdit || saving) return;
    if (checked) setConfirmOn(true);
    else void persist(false);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="spinner h-8 w-8" />
      </div>
    );
  }

  if (loadError) {
    return <ErrorState message="Could not load the badge sign-in policy." onRetry={() => void load()} />;
  }

  return (
    <div className="space-y-6">
      <section>
        <div className="mb-3">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-surface-700 flex items-center gap-2">
            <IdentificationIcon className="h-5 w-5 text-werco-600" aria-hidden="true" />
            Floor PIN for Badge Sign-in
          </h3>
          <p className="text-xs text-surface-500 mt-0.5">
            A floor PIN is the second factor on a badge or employee-ID sign-in (kiosk, crew station and Badge Only), so
            work recorded under a badge was recorded by the person who holds it. A badge that has a PIN always asks for
            it.
          </p>
        </div>

        {!canEdit && (
          <div className="mb-4 flex items-start gap-2 rounded border border-surface-200 bg-surface-50 px-3 py-2 text-xs text-surface-500">
            <LockClosedIcon className="h-4 w-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
            <span>Only an Administrator can change this policy. It is shown here read-only.</span>
          </div>
        )}

        <div
          className={`rounded border px-4 py-4 ${required ? 'border-werco-600/50 bg-werco-600/5' : 'border-surface-200'}`}
        >
          <label
            htmlFor="require-floor-pin-toggle"
            className={`grid grid-cols-[auto_1fr] items-start gap-x-3 ${canEdit ? 'cursor-pointer' : 'cursor-not-allowed opacity-80'}`}
          >
            <input
              id="require-floor-pin-toggle"
              type="checkbox"
              aria-label="Require a PIN for badge sign-in"
              aria-describedby="require-floor-pin-help"
              className="checkbox mt-0.5 row-span-2"
              checked={required}
              disabled={!canEdit || saving}
              onChange={e => handleToggle(e.target.checked)}
            />
            <span className="text-sm font-semibold text-surface-800">Require a PIN for badge sign-in</span>
            <span id="require-floor-pin-help" className="block text-xs text-surface-500 mt-1">
              When on, a badge with no floor PIN is refused at sign-in until a supervisor sets one in Users. When off, a
              badge with no PIN signs in on the badge alone. Password sign-in is not affected either way. Changing this
              is recorded on the audit trail.
            </span>
          </label>
          {saving && (
            <p role="status" className="mt-3 text-xs text-surface-500">
              Saving…
            </p>
          )}
        </div>

        {required && usersWithoutPin !== null && usersWithoutPin > 0 && (
          <div
            role="alert"
            className="mt-4 flex items-start gap-3 rounded border border-amber-500/40 bg-amber-500/5 px-4 py-3"
          >
            <ExclamationTriangleIcon className="h-5 w-5 text-amber-500 flex-shrink-0 mt-0.5" aria-hidden="true" />
            <div className="text-sm">
              <p className="font-semibold text-amber-400">
                {usersWithoutPin} active {usersWithoutPin === 1 ? 'user has' : 'users have'} no floor PIN
              </p>
              <p className="text-surface-500 mt-0.5">
                {usersWithoutPin === 1 ? 'That person is' : 'They are'} refused at the kiosk, the crew station and Badge
                Only sign-in until a PIN is set. Set PINs from the Floor PIN column in{' '}
                <Link to="/users" className="text-werco-600 underline">
                  Users
                </Link>
                .
              </p>
            </div>
          </div>
        )}
        {required && usersWithoutPin === 0 && (
          <p role="status" className="mt-4 text-sm text-surface-500">
            Every active user has a floor PIN.
          </p>
        )}
      </section>

      <ConfirmDialog
        open={confirmOn}
        title="Require a PIN for badge sign-in?"
        message="Every badge and employee-ID sign-in will need a floor PIN. A badge with no PIN is refused at the kiosk, the crew station and Badge Only sign-in until a supervisor sets one in Users. The change takes effect immediately and is recorded on the audit trail."
        confirmLabel="Require PIN"
        variant="warning"
        pending={saving}
        onConfirm={() => void persist(true)}
        onCancel={() => setConfirmOn(false)}
      />
    </div>
  );
}
