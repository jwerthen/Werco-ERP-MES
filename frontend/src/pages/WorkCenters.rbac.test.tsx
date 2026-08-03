/**
 * WorkCenters — RBAC gating of the inline status select.
 *
 * `POST /work-centers/{id}/status` used to accept ANY authenticated user; it is now
 * Admin / Manager (platform_admin and superuser escalate, matching `require_role`). Without
 * gating the control, the failure mode is the bad one: the `<select>` renders, accepts a
 * change, fires the request, and the user only learns they aren't allowed when a 403 toast
 * appears afterwards.
 *
 * DEFENSE IN DEPTH, not the primary gate — `/work-centers` IS route-guarded, on
 * `admin:settings` (App.tsx `routeAccessRequirements`), a NARROWER set (platform_admin +
 * admin) than these endpoints allow. So no role that can currently reach the page is
 * refused by this gate. It becomes load-bearing if that route tier is widened to Manager to
 * match the endpoints, and it keeps the control tied to its own verb's role set meanwhile.
 * The tests below therefore exercise the component directly, which is the only level at
 * which the un-guarded case is reachable at all.
 *
 * This locks both halves:
 *   1. An authorized role (manager) gets the interactive select, wired to
 *      `updateWorkCenterStatus`.
 *   2. An unauthorized role (operator) gets a read-only StatusBadge and NO select at all,
 *      so there is nothing to click and no request to refuse.
 *
 * The desktop table and the responsive mobile cards both mount in JSDOM, so the
 * select-count assertions are scoped by the per-row aria-label rather than by role alone.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import WorkCenters from './WorkCenters';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenters: jest.fn(),
    getWorkCenterTypes: jest.fn(),
    updateWorkCenterStatus: jest.fn(),
    createWorkCenter: jest.fn(),
    updateWorkCenter: jest.fn(),
  },
}));

// Mutable mock user so each test can pick a role before rendering.
let mockUser: { id: number; role: string; is_superuser?: boolean } = {
  id: 1,
  role: 'manager',
  is_superuser: false,
};
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const wc = {
  version: 0,
  description: '',
  availability_rate: 1,
  building: '',
  area: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  hourly_rate: 100,
  capacity_hours_per_day: 8,
  efficiency_factor: 1,
  current_status: 'available',
  id: 1,
  code: 'LASER-01',
  name: 'Trumpf Fiber',
  work_center_type: 'laser',
  is_active: true,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <WorkCenters />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('WorkCenters inline status select — RBAC', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getWorkCenters.mockResolvedValue([wc] as any);
    mockedApi.getWorkCenterTypes.mockResolvedValue({ types: ['laser'] } as any);
  });

  it('gives a manager an interactive select wired to the status endpoint', async () => {
    mockUser = { id: 1, role: 'manager', is_superuser: false };
    mockedApi.updateWorkCenterStatus.mockResolvedValue({ ...wc, current_status: 'offline' } as any);
    renderPage();

    const selects = await screen.findAllByLabelText('Status for LASER-01');
    expect(selects.length).toBeGreaterThan(0);

    fireEvent.change(selects[0], { target: { value: 'offline' } });

    await waitFor(() => expect(mockedApi.updateWorkCenterStatus).toHaveBeenCalledWith(1, 'offline'));
  });

  it('gives an operator a read-only badge and no select to change', async () => {
    mockUser = { id: 2, role: 'operator', is_superuser: false };
    renderPage();

    // The row still renders (the page is readable) ...
    expect(await screen.findAllByText('LASER-01')).not.toHaveLength(0);
    // ... but the control the server would refuse is simply not offered.
    expect(screen.queryByLabelText('Status for LASER-01')).not.toBeInTheDocument();
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
    expect(mockedApi.updateWorkCenterStatus).not.toHaveBeenCalled();
    // The status is still shown, just not editable.
    expect(screen.getAllByText(/available/i).length).toBeGreaterThan(0);
  });

  it('escalates for a superuser whose role would otherwise be refused', async () => {
    mockUser = { id: 3, role: 'operator', is_superuser: true };
    renderPage();

    expect((await screen.findAllByLabelText('Status for LASER-01')).length).toBeGreaterThan(0);
  });
});
