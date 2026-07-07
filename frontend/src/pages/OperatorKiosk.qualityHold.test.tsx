/**
 * Process Sheets PR 4 — the one-tap OOT → NCR flow through the single-operator
 * kiosk host: the quality-hold call carries source:"kiosk" (this kiosk runs on
 * a normal session — the client reports the channel exactly like clock-in),
 * success lands on the queue-independent NCR screen with the number readable,
 * and Done follows the existing HOLD exit (back to the queue, queue refreshed).
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
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
    getOperationSteps: jest.fn(),
    recordOperationStep: jest.fn(),
    supersedeOperationStepRecord: jest.fn(),
    uploadOperationStepAttachment: jest.fn(),
    raiseStepQualityHold: jest.fn(),
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
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 5,
  priority: 5,
  due_date: null,
  steps_total: 1,
  steps_recorded: 0,
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

const STEPS_VIEW = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_status: 'in_progress',
  is_serialized: false,
  serial_numbers: [],
  steps: [
    {
      id: 101,
      work_order_operation_id: 31,
      source_sheet_id: 5,
      source_sheet_revision: 'A',
      sequence: 10,
      label: 'Bore diameter',
      instruction_text: null,
      step_type: 'measurement',
      is_required: true,
      config: { nominal: 0.5, lsl: 0.498, usl: 0.502, unit: 'in', decimals: 4 },
      requires_gauge: false,
      spc_characteristic_id: null,
      created_at: '2026-07-01T12:00:00Z',
      records: [],
      complete: false,
      missing_serials: [],
    },
  ],
  steps_total: 1,
  steps_recorded: 0,
  completeness: {},
};

const HOLD_RESULT = {
  message: 'Quality hold raised',
  ncr_id: 55,
  ncr_number: 'NCR-000123',
  blocker_id: 77,
  operation_id: 31,
  operation_status: 'on_hold',
  closed_time_entry_ids: [501],
};

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
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  mockedApi.getOperationSteps.mockResolvedValue(STEPS_VIEW);
  mockedApi.recordOperationStep.mockRejectedValue({
    response: {
      status: 409,
      data: {
        detail: {
          code: 'OUT_OF_TOLERANCE',
          detail: 'Measured 0.6 is outside tolerance (0.498 to 0.502)',
          measured: 0.6,
          lsl: 0.498,
          usl: 0.502,
        },
      },
    },
  });
  mockedApi.raiseStepQualityHold.mockResolvedValue(HOLD_RESULT);
});

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&work_center_id=7&work_center_code=DEBUR1']}>
      <OperatorKiosk />
    </MemoryRouter>
  );
}

describe('OperatorKiosk one-tap OOT quality hold', () => {
  it('confirm → NCR filed with source:"kiosk" → NCR number shown prominently → Done returns to the refreshed queue', async () => {
    renderKiosk();

    // Into the steps view, refuse an OOT measurement.
    fireEvent.click(await screen.findByTestId('kiosk-active-steps'));
    await screen.findByTestId('kiosk-steps-progress');
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.6' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await screen.findByTestId('kiosk-step-oot');
    const queueCallsBefore = mockedApi.getWorkCenterQueue.mock.calls.length;

    // One-tap: confirm sub-state, optional note, file it.
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.change(await screen.findByLabelText(/notes for quality/i), { target: { value: 'Chipped insert' } });
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(mockedApi.raiseStepQualityHold).toHaveBeenCalledWith(31, 101, {
        measured_value: 0.6,
        notes: 'Chipped insert',
        source: 'kiosk',
      })
    );

    // The NCR number is PROMINENT on a dedicated screen (not a 3s toast) and
    // the hold consequences are honest (op held, labor entry closed).
    const ncrNumber = await screen.findByTestId('kiosk-ncr-number');
    expect(ncrNumber).toHaveTextContent('NCR-000123');
    expect(screen.getByText('NCR-000123 filed — this operation is on hold for quality review.')).toBeInTheDocument();
    expect(screen.getByText(/open labor entry was clocked out automatically/i)).toBeInTheDocument();
    // The queue refreshed underneath (hold behavior parity).
    await waitFor(() => expect(mockedApi.getWorkCenterQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));

    // Done follows the existing HOLD exit: back to the queue.
    fireEvent.click(screen.getByTestId('kiosk-ncr-done'));
    expect(await screen.findByRole('heading', { name: /my queue/i })).toBeInTheDocument();
  });
});
