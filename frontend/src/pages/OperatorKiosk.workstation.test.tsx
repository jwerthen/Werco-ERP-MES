import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { useCompany } from '../context/CompanyContext';
import { readKioskWorkstation, saveKioskWorkstation } from '../utils/kiosk';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(), getMyActiveJob: jest.fn(), getWorkCenters: jest.fn(),
    getScrapReasonCodes: jest.fn(), clockIn: jest.fn(), clockOut: jest.fn(),
    reportOperationProduction: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({ useAuth: jest.fn() }));
jest.mock('../context/CompanyContext', () => ({ useCompany: jest.fn() }));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;
const mockedUseCompany = useCompany as jest.Mock;
const OPERATOR = { id: 3, company_id: 4, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217' };
const CENTERS = [
  { id: 7, code: 'DEBUR1', name: 'Deburr bench', is_active: true },
  { id: 12, code: 'LASER1', name: 'Laser table', is_active: true },
];
const QUEUE_ITEM = {
  operation_id: 31, work_order_id: 9, work_order_number: 'WO-2026-0142', part_number: 'PN-7731',
  part_name: 'Bracket, hinge', operation_number: '20', operation_name: 'Deburr', work_center_id: 7,
  status: 'ready', quantity_ordered: 50, quantity_complete: 0, priority: 5, due_date: null,
};
const ACTIVE_JOB = {
  ...QUEUE_ITEM, time_entry_id: 501, clock_in: new Date().toISOString(), entry_type: 'run',
};

function authAs(user: object | null) {
  mockedUseAuth.mockReturnValue({
    user, isAuthenticated: Boolean(user), isLoading: false, loginWithEmployeeId: jest.fn(), logout: jest.fn(),
  });
}

function LocationReadout() {
  const location = useLocation();
  return <output data-testid="kiosk-location">{location.pathname}{location.search}</output>;
}

function kiosk(path = '/kiosk') {
  return <MemoryRouter initialEntries={[path]}><OperatorKiosk /><LocationReadout /></MemoryRouter>;
}

describe('operator kiosk workstation selection', () => {
  beforeEach(() => {
    jest.resetAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    authAs(OPERATOR);
    mockedUseCompany.mockReturnValue({ currentCompany: null });
    mockedApi.getWorkCenters.mockResolvedValue(CENTERS as any);
    mockedApi.getScrapReasonCodes.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  });

  it('requires badge authentication before loading workstation choices at /kiosk', async () => {
    authAs(null);
    const page = render(kiosk());
    expect(screen.getByText(/scan badge or enter id/i)).toBeInTheDocument();
    expect(mockedApi.getWorkCenters).not.toHaveBeenCalled();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();

    authAs(OPERATOR);
    page.rerender(kiosk());
    expect(await screen.findByRole('button', { name: /DEBUR1 Deburr bench/i })).toBeInTheDocument();
    expect(mockedApi.getWorkCenters).toHaveBeenCalledWith(true);
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
  });

  it('selects a workstation in one tap, preserves kiosk options, and remembers it after reload', async () => {
    const page = render(kiosk('/kiosk?kiosk=1&idle_logout_s=120'));
    fireEvent.click(await screen.findByRole('button', { name: /LASER1 Laser table/i }));
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(12));
    expect(readKioskWorkstation(4)).toBe(12);
    expect(screen.getByTestId('kiosk-location')).toHaveTextContent('kiosk=1&idle_logout_s=120&work_center_id=12&work_center_code=LASER1');
    page.unmount();
    mockedApi.getWorkCenterQueue.mockClear();

    render(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(12));
    expect(screen.queryByRole('heading', { name: 'Choose workstation' })).not.toBeInTheDocument();
  });

  it('keeps remembered selection through badge logout and the next operator login', async () => {
    saveKioskWorkstation(4, 12);
    const page = render(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(12));
    authAs(null);
    page.rerender(kiosk());
    expect(screen.getByText(/scan badge or enter id/i)).toBeInTheDocument();
    expect(readKioskWorkstation(4)).toBe(12);

    mockedApi.getWorkCenterQueue.mockClear();
    authAs({ ...OPERATOR, id: 5, employee_id: 'EMP-5000' });
    page.rerender(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(12));
  });

  it('does not reuse another tenant preference or a removed workstation', async () => {
    saveKioskWorkstation(4, 12);
    authAs({ ...OPERATOR, company_id: 9 });
    const page = render(kiosk());
    expect(await screen.findByRole('button', { name: /DEBUR1 Deburr bench/i })).toBeInTheDocument();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
    page.unmount();

    authAs(OPERATOR);
    saveKioskWorkstation(4, 999);
    render(kiosk());
    expect(await screen.findByRole('button', { name: /DEBUR1 Deburr bench/i })).toBeInTheDocument();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
    expect(readKioskWorkstation(4)).toBeNull();
  });

  it('honors legacy links and updates their workstation when changed without clocking out', async () => {
    saveKioskWorkstation(4, 12);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB] });
    render(kiosk('/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1'));
    fireEvent.click(await screen.findByRole('button', { name: 'Change workstation' }));
    fireEvent.click(await screen.findByRole('button', { name: /LASER1 Laser table/i }));
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenLastCalledWith(12));
    expect(screen.getByTestId('kiosk-location')).toHaveTextContent('work_center_id=12&work_center_code=LASER1');
    expect(readKioskWorkstation(4)).toBe(12);
    expect(await screen.findByRole('button', { name: /report production/i })).toBeInTheDocument();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
    expect(mockedApi.clockIn).not.toHaveBeenCalled();
  });

  it('uses the active company context when a platform admin switches tenants', async () => {
    authAs({ ...OPERATOR, role: 'platform_admin' });
    saveKioskWorkstation(4, 7);
    saveKioskWorkstation(9, 12);
    mockedUseCompany.mockReturnValue({ currentCompany: { id: 4 } });
    const page = render(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(7));
    mockedApi.getWorkCenterQueue.mockClear();

    mockedUseCompany.mockReturnValue({ currentCompany: { id: 9 } });
    // The authenticated user's home company remains 4 during this switch.
    page.rerender(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(12));
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalledWith(7);
    expect(readKioskWorkstation(4)).toBe(7);
    expect(readKioskWorkstation(9)).toBe(12);
  });

  it('waits for a platform admin active company instead of storing data under their home company', async () => {
    authAs({ ...OPERATOR, role: 'platform_admin' });
    const page = render(kiosk('/kiosk?work_center_id=12'));
    expect(screen.getByText('Starting kiosk…')).toBeInTheDocument();
    expect(mockedApi.getWorkCenters).not.toHaveBeenCalled();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
    expect(readKioskWorkstation(4)).toBeNull();

    mockedUseCompany.mockReturnValue({ currentCompany: { id: 9 } });
    page.rerender(kiosk('/kiosk?work_center_id=12'));
    await screen.findByRole('button', { name: /WO-2026-0142/i });
    expect(readKioskWorkstation(9)).toBe(12);
    expect(readKioskWorkstation(4)).toBeNull();
  });

  it('uses the authenticated operator company while the company context still contains a previous account', async () => {
    saveKioskWorkstation(4, 7);
    saveKioskWorkstation(9, 12);
    mockedUseCompany.mockReturnValue({ currentCompany: { id: 9 } });
    render(kiosk());
    await waitFor(() => expect(mockedApi.getWorkCenterQueue).toHaveBeenCalledWith(7));
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalledWith(12);
    expect(readKioskWorkstation(9)).toBe(12);
  });

  it('offers retry after workstation loading fails and excludes inactive workstations', async () => {
    mockedApi.getWorkCenters.mockRejectedValueOnce(new Error('connection lost'));
    mockedApi.getWorkCenters.mockResolvedValueOnce([...CENTERS, { id: 30, code: 'OLD', name: 'Retired', is_active: false }] as any);
    render(kiosk());
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('button', { name: /DEBUR1 Deburr bench/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retired/i })).not.toBeInTheDocument();
  });

  it('hides workstation switching while a production form is open', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB] });
    render(kiosk('/kiosk?work_center_id=7'));
    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));
    expect(screen.queryByRole('button', { name: 'Change workstation' })).not.toBeInTheDocument();
  });

  it('blocks workstation switching while a production report needs confirmation', async () => {
    sessionStorage.setItem('kiosk_production_unconfirmed_operator', JSON.stringify({
      operatorId: 3, operationId: 31,
      body: { request_id: 'original-request', quantity_complete_delta: 2, source: 'kiosk' },
    }));
    render(kiosk('/kiosk?work_center_id=7'));
    await screen.findByRole('button', { name: /WO-2026-0142/i });
    expect(screen.getByRole('button', { name: 'Change workstation' })).toBeDisabled();
  });

  it('ignores an old workstation poll that finishes after switching', async () => {
    jest.useFakeTimers();
    let finishOldPoll!: (value: any) => void;
    mockedApi.getWorkCenterQueue
      .mockResolvedValueOnce({ queue: [QUEUE_ITEM] })
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldPoll = resolve; }))
      .mockResolvedValue({ queue: [{ ...QUEUE_ITEM, operation_id: 99, work_order_number: 'WO-NEW' }] });
    const page = render(kiosk('/kiosk?work_center_id=7'));
    await screen.findByRole('button', { name: /WO-2026-0142/i });
    await act(async () => { jest.advanceTimersByTime(15_000); });
    fireEvent.click(screen.getByRole('button', { name: 'Change workstation' }));
    fireEvent.click(await screen.findByRole('button', { name: /LASER1 Laser table/i }));
    expect(await screen.findByRole('button', { name: /WO-NEW/i })).toBeInTheDocument();
    await act(async () => { finishOldPoll({ queue: [QUEUE_ITEM] }); });
    expect(screen.queryByRole('button', { name: /WO-2026-0142/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /WO-NEW/i })).toBeInTheDocument();
    page.unmount();
    jest.useRealTimers();
  });
});
