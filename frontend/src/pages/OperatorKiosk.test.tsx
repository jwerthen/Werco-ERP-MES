import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperatorKiosk from './OperatorKiosk';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkCenterQueue: jest.fn(),
    getMyActiveJob: jest.fn(),
    getWorkCenters: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../context/AuthContext', () => ({
  useAuth: jest.fn(),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const mockedUseAuth = useAuth as jest.Mock;

const QUEUE_ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_number: '20',
  operation_name: 'Deburr',
  work_center_id: 7,
  status: 'ready',
  quantity_ordered: 50,
  quantity_complete: 0,
  priority: 5,
  due_date: null,
};

const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 9,
  operation_id: 31,
  work_center_id: 7,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Bracket, hinge',
  operation_name: 'Deburr',
  operation_number: '20',
  quantity_ordered: 50,
  quantity_complete: 5,
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

function authAs(user: object | null) {
  mockedUseAuth.mockReturnValue({
    user,
    isAuthenticated: !!user,
    isLoading: false,
    loginWithEmployeeId: jest.fn(),
    logout: jest.fn(),
  });
}

describe('OperatorKiosk', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    authAs({ id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [], active_job: null });
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  });

  it('clock-in is two taps and sends source:"kiosk"', async () => {
    mockedApi.clockIn.mockResolvedValue({ id: 501 });
    renderKiosk();

    // Tap 1: the queue card.
    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
    // Tap 2: confirm CLOCK IN.
    fireEvent.click(screen.getByRole('button', { name: /^clock in$/i }));

    await waitFor(() =>
      expect(mockedApi.clockIn).toHaveBeenCalledWith({
        work_order_id: 9,
        operation_id: 31,
        work_center_id: 7,
        entry_type: 'run',
        source: 'kiosk',
      })
    );
  });

  it('surfaces backend gating errors verbatim instead of suppressing them', async () => {
    mockedApi.clockIn.mockRejectedValue({
      response: { data: { detail: 'Previous operations must be completed first' } },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
    fireEvent.click(screen.getByRole('button', { name: /^clock in$/i }));

    expect(await screen.findByText('Previous operations must be completed first')).toBeInTheDocument();
  });

  it('requires a scrap reason before production can be saved, then reports with source:"kiosk"', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.reportOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));

    // Enter 3 good, 2 scrap on the keypad.
    fireEvent.click(screen.getByTestId('kiosk-key-3'));
    fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
    fireEvent.click(screen.getByTestId('kiosk-key-2'));

    // Scrap entered but no reason chosen → save must be blocked.
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(screen.getByText(/scrap reason — required/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeEnabled();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 2,
        notes: 'Scrap reason: Material defect',
        source: 'kiosk',
      })
    );
  });

  it('COMPLETE clocks out first (with quantities + source) then completes at the target qty', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete$/i }));
    // GOOD prefills with the remaining quantity (50 - 5 = 45); confirm as-is.
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalled());
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 45,
      quantity_scrapped: 0,
      scrap_reason: undefined,
      source: 'kiosk',
    });
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(31, { quantity_complete: 50, source: 'kiosk' });
    const clockOutOrder = mockedApi.clockOut.mock.invocationCallOrder[0];
    const completeOrder = mockedApi.completeOperation.mock.invocationCallOrder[0];
    expect(clockOutOrder).toBeLessThan(completeOrder);
  });

  it('hold requires choosing a reason and files the matching blocker category', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.holdOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^hold$/i }));
    expect(screen.getByRole('button', { name: /^hold job$/i })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Machine down' }));
    fireEvent.click(screen.getByRole('button', { name: /^hold job$/i }));

    await waitFor(() =>
      expect(mockedApi.holdOperation).toHaveBeenCalledWith(31, { category: 'machine_down', severity: 'medium' })
    );
  });

  it('shows the badge login screen when unauthenticated', async () => {
    authAs(null);
    renderKiosk();
    expect(await screen.findByText(/scan your badge/i)).toBeInTheDocument();
    expect(mockedApi.getWorkCenterQueue).not.toHaveBeenCalled();
  });
});
