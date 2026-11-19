/**
 * FloorPinDialog / FloorPinCell — Users → Floor PIN (AS9100D 8.5.2): set, reset, clear or
 * unlock one person's badge-sign-in PIN (PUT / DELETE /users/{id}/floor-pin, POST
 * .../floor-pin/unlock): an Admin for anyone in the company, a Manager only for Operator,
 * Supervisor, Shipping and Viewer accounts, a platform account only a platform principal.
 *
 * Non-optimistic by design: the server owns the strength rules and answers 422 naming the
 * broken rule, which the dialog shows verbatim and stays open on; the list reloads only after
 * the server said yes. Client-side the dialog checks only shape (4–6 digits) and that the two
 * entries match, so nothing malformed is ever sent. The PIN is masked, never displayed, and
 * cleared on reopen. Clear goes through a ConfirmDialog.
 *
 * `services/api` is mocked at the module boundary.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import FloorPinDialog, {
  FloorPinCell,
  FloorPinUser,
  MANAGER_FLOOR_PIN_TARGET_ROLES,
  floorPinManageRefusal,
  floorPinStatus,
  isFloorPinLocked,
} from './FloorPinDialog';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    setUserFloorPin: jest.fn(),
    clearUserFloorPin: jest.fn(),
    unlockUserFloorPin: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const ROSA: FloorPinUser = {
  id: 2,
  first_name: 'Rosa',
  last_name: 'Vega',
  employee_id: '4217',
  has_floor_pin: false,
  floor_pin_set_at: null,
  floor_pin_locked_until: null,
};
const inMinutes = (m: number) => new Date(Date.now() + m * 60_000).toISOString();
const WITH_PIN: FloorPinUser = { ...ROSA, has_floor_pin: true, floor_pin_set_at: '2026-11-16T15:00:00Z' };
const LOCKED: FloorPinUser = { ...WITH_PIN, floor_pin_locked_until: inMinutes(12) };
// 15 cumulative wrong PINs: no time lifts it (whatever floor_pin_locked_until says).
const ADMIN_LOCKED: FloorPinUser = { ...WITH_PIN, floor_pin_admin_locked: true, floor_pin_locked_until: null };
// A promotion into Admin / Manager / Quality cleared the PIN.
const RESET_REQUIRED: FloorPinUser = { ...ROSA, role: 'quality', has_floor_pin: false, floor_pin_reset_required: true };

const httpError = (status: number, detail: unknown) => {
  const err = new Error(`Request failed with status code ${status}`) as Error & {
    response: { status: number; data: { detail: unknown } };
  };
  err.response = { status, data: { detail } };
  return err;
};

function renderDialog(user: FloorPinUser | null = ROSA) {
  const onClose = jest.fn();
  const onChanged = jest.fn().mockResolvedValue(undefined);
  const utils = render(
    <ToastProvider>
      <FloorPinDialog user={user} onClose={onClose} onChanged={onChanged} />
    </ToastProvider>
  );
  return { ...utils, onClose, onChanged };
}

const pinField = () => screen.getByLabelText(/^(New )?PIN/) as HTMLInputElement;
const confirmField = () => screen.getByLabelText(/^Confirm PIN/) as HTMLInputElement;

function fill(pin: string, confirm = pin) {
  fireEvent.change(pinField(), { target: { value: pin } });
  fireEvent.change(confirmField(), { target: { value: confirm } });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.setUserFloorPin.mockResolvedValue({} as never);
  mockApi.clearUserFloorPin.mockResolvedValue(undefined);
  mockApi.unlockUserFloorPin.mockResolvedValue(undefined);
});

describe('FloorPinDialog', () => {
  it('renders nothing when closed', () => {
    renderDialog(null);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows whose PIN, its state and two masked, labelled fields', () => {
    renderDialog();

    expect(screen.getByRole('dialog', { name: /Floor PIN — Rosa Vega/ })).toBeInTheDocument();
    expect(screen.getByText('4217')).toBeInTheDocument();
    expect(screen.getByText('Not set')).toBeInTheDocument();
    expect(screen.getByText('Not locked')).toBeInTheDocument();
    for (const field of [pinField(), confirmField()]) {
      expect(field).toHaveAttribute('type', 'password');
      expect(field).toHaveAttribute('aria-required', 'true');
      expect(field).toHaveAttribute('inputMode', 'numeric');
    }
    expect(screen.getByRole('button', { name: 'Set PIN' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear PIN' })).not.toBeInTheDocument();
  });

  it('keeps digits only, at most six', () => {
    renderDialog();
    fireEvent.change(pinField(), { target: { value: '0a9 1-2x34567' } });
    expect(pinField().value).toBe('091234');
  });

  it('sets the PIN (leading zero kept), toasts, reloads the list and then closes', async () => {
    const { onClose, onChanged } = renderDialog();

    fill('0912');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(mockApi.setUserFloorPin).toHaveBeenCalledWith(2, '0912');
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(onChanged.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0]);
    expect(await screen.findByText('Floor PIN set for Rosa Vega')).toBeInTheDocument();
  });

  it('refuses a short PIN and a mismatch without calling the server', () => {
    const { onClose } = renderDialog();

    fill('091', '091');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));
    expect(screen.getByText('The PIN must be 4–6 digits.')).toBeInTheDocument();
    expect(pinField()).toHaveAttribute('aria-invalid', 'true');

    fill('0912', '0913');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));
    expect(screen.getByText('The two PINs do not match.')).toBeInTheDocument();
    expect(confirmField()).toHaveAttribute('aria-invalid', 'true');
    expect(screen.queryByText('The PIN must be 4–6 digits.')).not.toBeInTheDocument();

    expect(mockApi.setUserFloorPin).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("shows the server's rule refusal verbatim and stays open", async () => {
    mockApi.setUserFloorPin.mockRejectedValueOnce(
      httpError(422, 'A floor PIN cannot be a straight run of digits counting up or down.')
    );
    const { onClose, onChanged } = renderDialog();

    fill('2468');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'A floor PIN cannot be a straight run of digits counting up or down.'
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(screen.queryByText(/Floor PIN set/)).not.toBeInTheDocument();
  });

  it("shows a Manager's 403 on an Admin's PIN verbatim", async () => {
    mockApi.setUserFloorPin.mockRejectedValueOnce(
      httpError(403, "Only an Admin can set or clear an Admin's floor PIN")
    );
    renderDialog();

    fill('0912');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));

    expect(await screen.findByText("Only an Admin can set or clear an Admin's floor PIN")).toBeInTheDocument();
  });

  it('holds the dialog while saving: buttons disabled, no double submit', async () => {
    let resolve!: () => void;
    mockApi.setUserFloorPin.mockReturnValueOnce(
      new Promise(r => {
        resolve = () => r({} as never);
      }) as never
    );
    const { onClose } = renderDialog();

    fill('0912');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled());
    expect(pinField()).toBeDisabled();
    fireEvent.submit(pinField().closest('form') as HTMLFormElement);
    expect(mockApi.setUserFloorPin).toHaveBeenCalledTimes(1);

    resolve();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('with a PIN set: Reset wording, the set date, and a Clear PIN action', async () => {
    const { onClose, onChanged } = renderDialog(WITH_PIN);

    expect(screen.getByLabelText(/^New PIN/)).toBeInTheDocument();
    expect(screen.getByText(/^Set /)).toBeInTheDocument();
    fill('0912');
    fireEvent.click(screen.getByRole('button', { name: 'Reset PIN' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByText('Floor PIN reset for Rosa Vega')).toBeInTheDocument();
  });

  it('Clear asks first, then clears and reloads', async () => {
    const { onClose, onChanged } = renderDialog(WITH_PIN);

    fireEvent.click(screen.getByRole('button', { name: 'Clear PIN' }));
    const confirm = await screen.findByRole('dialog', { name: /Clear floor PIN\?/ });
    expect(mockApi.clearUserFloorPin).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Clear PIN' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mockApi.clearUserFloorPin).toHaveBeenCalledWith(2);
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByText('Floor PIN cleared for Rosa Vega')).toBeInTheDocument();
  });

  it('cancelling the Clear confirmation clears nothing', async () => {
    renderDialog(WITH_PIN);

    fireEvent.click(screen.getByRole('button', { name: 'Clear PIN' }));
    const confirm = await screen.findByRole('dialog', { name: /Clear floor PIN\?/ });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.getByRole('dialog', { name: /Floor PIN — Rosa Vega/ })).toBeInTheDocument());
    expect(mockApi.clearUserFloorPin).not.toHaveBeenCalled();
  });

  it('a refused clear comes back to the dialog with the reason', async () => {
    mockApi.clearUserFloorPin.mockRejectedValueOnce(httpError(403, 'Insufficient permissions'));
    const { onClose } = renderDialog(WITH_PIN);

    fireEvent.click(screen.getByRole('button', { name: 'Clear PIN' }));
    const confirm = await screen.findByRole('dialog', { name: /Clear floor PIN\?/ });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Clear PIN' }));

    expect(await screen.findByText('Insufficient permissions')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('shows a lock and says a new PIN lifts it', () => {
    renderDialog(LOCKED);
    expect(screen.getByText(/Locked after wrong PINs until/)).toBeInTheDocument();
    expect(screen.getByText('Setting a new PIN also lifts the lock.')).toBeInTheDocument();
  });

  it('Unlock lifts the lock without touching the PIN, reloads, and keeps the dialog open', async () => {
    const { onClose, onChanged } = renderDialog(LOCKED);

    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(mockApi.unlockUserFloorPin).toHaveBeenCalledWith(2);
    expect(mockApi.setUserFloorPin).not.toHaveBeenCalled();
    expect(mockApi.clearUserFloorPin).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(await screen.findByText('Floor PIN unlocked for Rosa Vega')).toBeInTheDocument();
  });

  it('a refused unlock shows the reason and stays open', async () => {
    mockApi.unlockUserFloorPin.mockRejectedValueOnce(
      httpError(
        403,
        "A Manager can manage floor PINs only for Operator, Supervisor, Shipping and Viewer accounts. Ask an Admin to manage this account's PIN."
      )
    );
    const { onClose, onChanged } = renderDialog(LOCKED);

    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));

    expect(await screen.findByText(/A Manager can manage floor PINs only for/)).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('offers no Unlock when nothing is locked, or the lock has run out', () => {
    const { rerender } = renderDialog(WITH_PIN);
    expect(screen.queryByRole('button', { name: 'Unlock' })).not.toBeInTheDocument();

    rerender(
      <ToastProvider>
        <FloorPinDialog
          user={{ ...WITH_PIN, floor_pin_locked_until: inMinutes(-1) }}
          onClose={jest.fn()}
          onChanged={jest.fn()}
        />
      </ToastProvider>
    );
    expect(screen.queryByRole('button', { name: 'Unlock' })).not.toBeInTheDocument();
    expect(screen.getByText('Not locked')).toBeInTheDocument();
  });

  it('a fresh, empty form when it opens for another person', () => {
    const onClose = jest.fn();
    const onChanged = jest.fn();
    const { rerender } = render(
      <ToastProvider>
        <FloorPinDialog user={ROSA} onClose={onClose} onChanged={onChanged} />
      </ToastProvider>
    );
    fill('0912', '0913');
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }));
    expect(screen.getByText('The two PINs do not match.')).toBeInTheDocument();

    rerender(
      <ToastProvider>
        <FloorPinDialog user={{ ...ROSA, id: 3, first_name: 'Li' }} onClose={onClose} onChanged={onChanged} />
      </ToastProvider>
    );

    expect(pinField().value).toBe('');
    expect(confirmField().value).toBe('');
    expect(screen.queryByText('The two PINs do not match.')).not.toBeInTheDocument();
  });
});

describe('FloorPinCell', () => {
  it('names the person on its action and opens the dialog for them', () => {
    const onManage = jest.fn();
    render(<FloorPinCell user={ROSA} onManage={onManage} />);

    expect(screen.getByText('Not set')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Manage floor PIN for Rosa Vega' }));
    expect(onManage).toHaveBeenCalledWith(ROSA);
  });

  it('replaces the action with the reason when this caller may not manage the person', () => {
    const onManage = jest.fn();
    render(<FloorPinCell user={WITH_PIN} onManage={onManage} manageRefusal="Admin only" />);

    expect(screen.queryByRole('button', { name: /Manage floor PIN/ })).not.toBeInTheDocument();
    expect(screen.getByText('Admin only')).toBeInTheDocument();
    expect(screen.getByText('Set')).toBeInTheDocument();
  });

  it('shows Set, or Locked while the lock runs', () => {
    const { rerender } = render(<FloorPinCell user={WITH_PIN} onManage={jest.fn()} />);
    expect(screen.getByText('Set')).toBeInTheDocument();

    rerender(<FloorPinCell user={LOCKED} onManage={jest.fn()} />);
    expect(screen.getByText('Locked')).toBeInTheDocument();
  });
});

describe('isFloorPinLocked', () => {
  it('only a lock in the future counts', () => {
    const now = Date.parse('2026-11-19T12:00:00Z');
    expect(isFloorPinLocked({ ...ROSA, floor_pin_locked_until: '2026-11-19T12:05:00Z' }, now)).toBe(true);
    expect(isFloorPinLocked({ ...ROSA, floor_pin_locked_until: '2026-11-19T11:55:00Z' }, now)).toBe(false);
    expect(isFloorPinLocked({ ...ROSA, floor_pin_locked_until: null }, now)).toBe(false);
    // A zone-less legacy value is read as UTC, never local time.
    expect(isFloorPinLocked({ ...ROSA, floor_pin_locked_until: '2026-11-19T12:05:00' }, now)).toBe(true);
  });
});

describe('floorPinManageRefusal (the client twin of the server rule)', () => {
  const ALL_ROLES = ['platform_admin', 'admin', 'manager', 'supervisor', 'operator', 'quality', 'shipping', 'viewer'];

  it('lets a Manager manage exactly Operator, Supervisor, Shipping and Viewer', () => {
    expect(Array.from(MANAGER_FLOOR_PIN_TARGET_ROLES).sort()).toEqual(['operator', 'shipping', 'supervisor', 'viewer']);
    for (const role of ALL_ROLES) {
      const expected = MANAGER_FLOOR_PIN_TARGET_ROLES.has(role)
        ? null
        : role === 'platform_admin'
          ? 'Platform admin only'
          : 'Admin only';
      expect(floorPinManageRefusal({ role: 'manager' }, { role })).toBe(expected);
    }
  });

  it('lets an Admin manage every tenant role but not a platform account', () => {
    for (const role of ALL_ROLES.filter(r => r !== 'platform_admin')) {
      expect(floorPinManageRefusal({ role: 'admin' }, { role })).toBeNull();
    }
    expect(floorPinManageRefusal({ role: 'admin' }, { role: 'platform_admin' })).toBe('Platform admin only');
    expect(floorPinManageRefusal({ role: 'admin' }, { role: 'operator', is_superuser: true })).toBe(
      'Platform admin only'
    );
  });

  it('a superuser target is a platform account even when its role is a floor role', () => {
    expect(floorPinManageRefusal({ role: 'manager' }, { role: 'operator', is_superuser: true })).toBe(
      'Platform admin only'
    );
  });

  it('a platform principal manages anyone', () => {
    for (const role of ALL_ROLES) {
      expect(floorPinManageRefusal({ role: 'platform_admin' }, { role })).toBeNull();
      expect(floorPinManageRefusal({ role: 'operator', is_superuser: true }, { role })).toBeNull();
    }
  });

  it('every other caller, and no caller, is refused', () => {
    for (const role of ['supervisor', 'operator', 'quality', 'shipping', 'viewer']) {
      expect(floorPinManageRefusal({ role }, { role: 'operator' })).toBe('Admin only');
    }
    expect(floorPinManageRefusal(null, { role: 'operator' })).toBe('Admin only');
    expect(floorPinManageRefusal({ role: 'manager' }, { role: null })).toBe('Admin only');
  });
});

describe('the admin lock and the role-change reset (review fixes)', () => {
  it('an admin-locked PIN reads "Locked: needs unlock" and offers Unlock, which keeps the dialog open', async () => {
    const { onChanged, onClose } = renderDialog(ADMIN_LOCKED);

    expect(screen.getByText(/Locked: needs unlock/)).toBeInTheDocument();
    expect(screen.getByText('Setting a new PIN also lifts the lock.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(mockApi.unlockUserFloorPin).toHaveBeenCalledWith(2);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('a role-change reset reads "New PIN needed (role changed)" and offers Set PIN, not Clear', () => {
    renderDialog(RESET_REQUIRED);

    expect(screen.getByText('New PIN needed (role changed)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Set PIN' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear PIN' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unlock' })).not.toBeInTheDocument();
  });

  it('the cell shows both states as needing attention', () => {
    const { rerender } = render(<FloorPinCell user={ADMIN_LOCKED} onManage={jest.fn()} />);
    expect(screen.getByText('Locked: needs unlock')).toBeInTheDocument();

    rerender(<FloorPinCell user={RESET_REQUIRED} onManage={jest.fn()} />);
    expect(screen.getByText('New PIN needed (role changed)')).toBeInTheDocument();
  });

  it('floorPinStatus ranks the reset, then the admin lock, then a timed lock, then set / not set', () => {
    const now = Date.parse('2026-11-19T12:00:00Z');
    const timed = { ...WITH_PIN, floor_pin_locked_until: '2026-11-19T12:05:00Z' };
    expect(floorPinStatus({ ...timed, floor_pin_admin_locked: true, floor_pin_reset_required: true }, now).kind).toBe(
      'reset_required'
    );
    expect(floorPinStatus({ ...timed, floor_pin_admin_locked: true }, now).kind).toBe('admin_locked');
    expect(floorPinStatus(timed, now)).toMatchObject({ kind: 'locked', label: 'Locked', attention: true });
    expect(floorPinStatus(WITH_PIN, now)).toMatchObject({ kind: 'set', label: 'Set', attention: false });
    expect(floorPinStatus(ROSA, now)).toMatchObject({ kind: 'not_set', label: 'Not set', attention: false });
  });

  it('isFloorPinLocked counts the admin lock with no end time', () => {
    expect(isFloorPinLocked(ADMIN_LOCKED)).toBe(true);
    expect(isFloorPinLocked({ ...ADMIN_LOCKED, floor_pin_admin_locked: false })).toBe(false);
  });

  it("a Manager may unlock an in-scope admin lock; a role-change reset (Admin / Manager / Quality) is an Admin's", () => {
    const lockedOperator: FloorPinUser = { ...ADMIN_LOCKED, role: 'operator' };
    expect(floorPinManageRefusal({ role: 'manager' }, lockedOperator)).toBeNull();
    expect(floorPinManageRefusal({ role: 'manager' }, RESET_REQUIRED)).toBe('Admin only');
    expect(floorPinManageRefusal({ role: 'admin' }, RESET_REQUIRED)).toBeNull();
  });
});
