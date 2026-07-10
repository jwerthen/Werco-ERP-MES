/**
 * OperatorKiosk — company scrap-code payloads (Lean Phase 1 / issue #88).
 *
 * When GET /quality/scrap-reason-codes resolves with active codes, the kiosk
 * quantity screens run in CODES mode: the scrap picker is built from
 * "CODE — Name" tiles and the mutations carry the structured
 * `scrap_reason_code_id` (plus the optional typed detail as `scrap_reason`):
 *  - REPORT PRODUCTION posts the code id on the production report,
 *  - COMPLETE posts the code id on the clock-out leg,
 *  - a codes fetch failure fails soft to the legacy SCRAP_REASONS vocabulary
 *    (a codes outage must never brick shop-floor scrap entry).
 *
 * The pre-codes payload contract (text reason, no code id) stays locked in
 * OperatorKiosk.test.tsx, which does not mock getScrapReasonCodes.
 */
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
    getScrapReasonCodes: jest.fn(),
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

const CODES = [
  { id: 7, code: 'OT', name: 'Out of tolerance', category: 'operator', description: null, is_active: true, display_order: 1 },
  { id: 9, code: 'MAT', name: 'Material defect', category: 'material', description: null, is_active: true, display_order: 2 },
];

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

/** Enter 3 good then 2 scrap through the shared keypad. */
function enterThreeGoodTwoScrap() {
  fireEvent.click(screen.getByTestId('kiosk-key-3'));
  fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
  fireEvent.click(screen.getByTestId('kiosk-key-2'));
}

describe('OperatorKiosk scrap-code payloads', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedUseAuth.mockReturnValue({
      user: { id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' },
      isAuthenticated: true,
      isLoading: false,
      loginWithEmployeeId: jest.fn(),
      logout: jest.fn(),
    });
    mockedApi.getWorkCenters.mockResolvedValue([]);
    mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
    mockedApi.getScrapReasonCodes.mockResolvedValue(CODES as any);
  });

  it('REPORT PRODUCTION sends scrap_reason_code_id (no detail -> no scrap_reason) with source:"kiosk"', async () => {
    mockedApi.reportOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));
    // Codes mode: the picker is the company vocabulary, not the legacy tiles.
    enterThreeGoodTwoScrap();
    expect(screen.getByTestId('kiosk-qty-confirm')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Out of tolerance' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'OT — Out of tolerance' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 2,
        scrap_reason: undefined,
        scrap_reason_code_id: 7,
        source: 'kiosk',
      })
    );
  });

  it('REPORT PRODUCTION sends the optional typed detail as scrap_reason alongside the code id', async () => {
    mockedApi.reportOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));
    enterThreeGoodTwoScrap();
    fireEvent.click(screen.getByRole('button', { name: 'MAT — Material defect' }));
    fireEvent.change(screen.getByTestId('kiosk-scrap-detail'), { target: { value: 'porosity on face' } });
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 2,
        scrap_reason: 'porosity on face',
        scrap_reason_code_id: 9,
        source: 'kiosk',
      })
    );
  });

  it('COMPLETE carries the code id on the clock-out leg, then completes at target', async () => {
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete$/i }));
    // GOOD prefills with the remaining quantity (50 - 5 = 45); add 2 scrap.
    fireEvent.click(screen.getByTestId('kiosk-qty-scrap'));
    fireEvent.click(screen.getByTestId('kiosk-key-2'));
    fireEvent.click(screen.getByRole('button', { name: 'OT — Out of tolerance' }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalled());
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 45,
      quantity_scrapped: 2,
      scrap_reason: undefined,
      scrap_reason_code_id: 7,
      source: 'kiosk',
    });
    expect(mockedApi.completeOperation).toHaveBeenCalledWith(31, { quantity_complete: 50, source: 'kiosk' });
  });

  it('fails soft to the legacy reason tiles (text payload, no code id) when the codes fetch rejects', async () => {
    mockedApi.getScrapReasonCodes.mockRejectedValue(new Error('boom'));
    mockedApi.reportOperationProduction.mockResolvedValue({});
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /report production/i }));
    enterThreeGoodTwoScrap();

    // Legacy vocabulary renders; no codes-mode detail input.
    fireEvent.click(screen.getByRole('button', { name: 'Material defect' }));
    expect(screen.queryByTestId('kiosk-scrap-detail')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await waitFor(() =>
      expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(31, {
        quantity_complete_delta: 3,
        quantity_scrapped_delta: 2,
        scrap_reason: 'Material defect',
        scrap_reason_code_id: undefined,
        source: 'kiosk',
      })
    );
  });
});
