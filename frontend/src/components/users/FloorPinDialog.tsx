import React, { useEffect, useId, useState } from 'react';
import { KeyIcon, LockClosedIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { ConfirmDialog, FormField, LoadingButton, Modal, useToast } from '../ui';
import { apiErrorDetail } from '../../utils/apiError';
import { formatCentralDateTime, toDate } from '../../utils/centralTime';
import { FLOOR_PIN_MAX_LENGTH, FLOOR_PIN_MIN_LENGTH, isValidFloorPinFormat } from '../../services/badgeSignOn';

/** The user fields the floor-PIN UI reads (all on UserResponse). */
export interface FloorPinUser {
  id: number;
  first_name: string;
  last_name: string;
  employee_id?: string | null;
  has_floor_pin?: boolean;
  floor_pin_set_at?: string | null;
  floor_pin_locked_until?: string | null;
}

/** True while wrong PIN attempts hold the PIN locked (the server's 15-minute lock). */
export function isFloorPinLocked(user: FloorPinUser, now: number = Date.now()): boolean {
  if (!user.floor_pin_locked_until) return false;
  const until = toDate(user.floor_pin_locked_until);
  return !!until && until.getTime() > now;
}

/**
 * The Users-table cell: whether a floor PIN is set (and when), or locked, plus the
 * one action that opens `FloorPinDialog`. Admin/Manager only — the caller gates it.
 */
export function FloorPinCell({ user, onManage }: { user: FloorPinUser; onManage: (user: FloorPinUser) => void }) {
  const name = `${user.first_name} ${user.last_name}`;
  const locked = user.has_floor_pin && isFloorPinLocked(user);
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-0 text-sm">
        {user.has_floor_pin ? (
          <>
            <span className={locked ? 'font-medium text-amber-300' : 'text-slate-200'}>
              {locked ? 'Locked' : 'Set'}
            </span>
            <span className="block text-xs text-slate-400">
              {locked
                ? `until ${formatCentralDateTime(user.floor_pin_locked_until)}`
                : user.floor_pin_set_at
                  ? formatCentralDateTime(user.floor_pin_set_at)
                  : null}
            </span>
          </>
        ) : (
          <span className="text-slate-400">Not set</span>
        )}
      </div>
      {/* A word, not an icon: the row's Actions already carry a key icon for Reset Password. */}
      <button
        type="button"
        onClick={() => onManage(user)}
        className="ml-auto shrink-0 rounded-[3px] border border-slate-600 px-2 py-1 text-xs font-medium text-slate-200 hover:border-blue-400 hover:text-blue-300"
        aria-label={`Manage floor PIN for ${name}`}
      >
        Manage
      </button>
    </div>
  );
}

interface FloorPinDialogProps {
  /** The user whose PIN is being managed; null = closed. */
  user: FloorPinUser | null;
  onClose: () => void;
  /** Called after the server accepted a set / reset / clear — reload the list. */
  onChanged: () => void | Promise<void>;
}

const RANGE_TEXT = `${FLOOR_PIN_MIN_LENGTH}–${FLOOR_PIN_MAX_LENGTH} digits`;

/**
 * Users → Floor PIN: set, reset or clear one person's badge-sign-in PIN
 * (AS9100D 8.5.2). Admin/Manager (PUT / DELETE /users/{id}/floor-pin).
 *
 * Non-optimistic on purpose — the server owns the PIN rules (no 0000, no 1234, not
 * the badge's own digits) and refuses 422 with a message naming the rule, which is
 * shown here verbatim; the list reflects only what the reload returns. The PIN is
 * typed twice and never shown, logged or kept after the dialog closes.
 */
export default function FloorPinDialog({ user, onClose, onChanged }: FloorPinDialogProps) {
  const { showToast } = useToast();
  const titleId = useId();
  const [pin, setPin] = useState('');
  const [confirmPin, setConfirmPin] = useState('');
  const [pinError, setPinError] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);

  // A fresh, empty form every time the dialog opens (or switches person).
  useEffect(() => {
    setPin('');
    setConfirmPin('');
    setPinError(null);
    setConfirmError(null);
    setServerError(null);
    setConfirmClear(false);
  }, [user?.id]);

  if (!user) return null;

  const name = `${user.first_name} ${user.last_name}`;
  const hasPin = !!user.has_floor_pin;
  const locked = hasPin && isFloorPinLocked(user);
  const busy = saving || clearing;

  const close = () => {
    if (busy) return;
    onClose();
  };

  const digitsOnly = (value: string) => value.replace(/\D/g, '').slice(0, FLOOR_PIN_MAX_LENGTH);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    let invalid = false;
    if (!isValidFloorPinFormat(pin)) {
      setPinError(`The PIN must be ${RANGE_TEXT}.`);
      invalid = true;
    } else {
      setPinError(null);
    }
    if (confirmPin !== pin) {
      setConfirmError('The two PINs do not match.');
      invalid = true;
    } else {
      setConfirmError(null);
    }
    if (invalid) return;

    setSaving(true);
    setServerError(null);
    try {
      await api.setUserFloorPin(user.id, pin);
      setPin('');
      setConfirmPin('');
      showToast('success', hasPin ? `Floor PIN reset for ${name}` : `Floor PIN set for ${name}`);
      await onChanged();
      onClose();
    } catch (err) {
      // 422 names the rule the PIN broke; 403 is a role / credential refusal.
      setServerError(apiErrorDetail(err, 'Could not save the floor PIN.'));
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async () => {
    setClearing(true);
    setServerError(null);
    try {
      await api.clearUserFloorPin(user.id);
      showToast('success', `Floor PIN cleared for ${name}`);
      setConfirmClear(false);
      await onChanged();
      onClose();
    } catch (err) {
      setConfirmClear(false);
      setServerError(apiErrorDetail(err, 'Could not clear the floor PIN.'));
    } finally {
      setClearing(false);
    }
  };

  return (
    <>
      <Modal
        open={!confirmClear}
        onClose={close}
        size="md"
        ariaLabelledBy={titleId}
        closeOnBackdrop={!busy}
        closeOnEscape={!busy}
      >
        <h3 id={titleId} className="text-lg font-semibold mb-1 flex items-center gap-2">
          <KeyIcon className="h-5 w-5 text-slate-300" aria-hidden="true" />
          Floor PIN — {name}
        </h3>
        <p className="text-sm text-slate-400 mb-4">
          Asked for after the badge at the kiosk, the crew station and Badge Only sign-in, so work recorded under the
          badge is recorded by the person who holds it.
        </p>

        <dl className="mb-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-[4px] border border-slate-700 px-3 py-2.5 text-sm">
          {user.employee_id ? (
            <>
              <dt className="text-slate-400">Badge / ID</dt>
              <dd className="font-mono text-slate-200">{user.employee_id}</dd>
            </>
          ) : null}
          <dt className="text-slate-400">PIN</dt>
          <dd className="text-slate-200">
            {hasPin
              ? `Set${user.floor_pin_set_at ? ` ${formatCentralDateTime(user.floor_pin_set_at)}` : ''}`
              : 'Not set'}
          </dd>
          <dt className="text-slate-400">Lock</dt>
          <dd className={locked ? 'flex items-center gap-1.5 text-amber-300' : 'text-slate-200'}>
            {locked ? (
              <>
                <LockClosedIcon className="h-4 w-4" aria-hidden="true" />
                Locked after wrong PINs until {formatCentralDateTime(user.floor_pin_locked_until)}
              </>
            ) : (
              'Not locked'
            )}
          </dd>
        </dl>

        {serverError && (
          <div role="alert" className="alert alert-danger mb-4 text-sm">
            <span>{serverError}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <FormField
            label={hasPin ? 'New PIN' : 'PIN'}
            required
            error={pinError}
            help={`${RANGE_TEXT}. Not all one digit, not a straight run (1234, 4321), not the badge's own digits.`}
          >
            {field => (
              <input
                {...field}
                type="password"
                inputMode="numeric"
                autoComplete="new-password"
                maxLength={FLOOR_PIN_MAX_LENGTH}
                value={pin}
                onChange={e => setPin(digitsOnly(e.target.value))}
                className={pinError ? 'input-error font-mono tracking-[0.3em]' : 'input font-mono tracking-[0.3em]'}
                disabled={busy}
              />
            )}
          </FormField>
          <FormField label="Confirm PIN" required error={confirmError}>
            {field => (
              <input
                {...field}
                type="password"
                inputMode="numeric"
                autoComplete="new-password"
                maxLength={FLOOR_PIN_MAX_LENGTH}
                value={confirmPin}
                onChange={e => setConfirmPin(digitsOnly(e.target.value))}
                className={confirmError ? 'input-error font-mono tracking-[0.3em]' : 'input font-mono tracking-[0.3em]'}
                disabled={busy}
              />
            )}
          </FormField>
          {locked && <p className="text-xs text-slate-400">Setting a new PIN also lifts the lock.</p>}

          <div className="flex flex-wrap items-center justify-end gap-2 pt-2">
            {hasPin && (
              <button
                type="button"
                onClick={() => setConfirmClear(true)}
                disabled={busy}
                className="btn-secondary mr-auto !text-red-300"
              >
                Clear PIN
              </button>
            )}
            <button type="button" onClick={close} disabled={busy} className="btn-secondary">
              Cancel
            </button>
            <LoadingButton type="submit" loading={saving} loadingText="Saving…">
              {hasPin ? 'Reset PIN' : 'Set PIN'}
            </LoadingButton>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={confirmClear}
        title="Clear floor PIN?"
        message={`${name} will sign in with the badge alone until a new PIN is set. If the company requires a PIN for badge sign-in, the badge is refused at the kiosk instead.`}
        confirmLabel="Clear PIN"
        variant="danger"
        pending={clearing}
        onConfirm={() => void handleClear()}
        onCancel={() => setConfirmClear(false)}
      />
    </>
  );
}
