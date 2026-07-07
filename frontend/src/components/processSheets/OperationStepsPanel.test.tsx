import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import OperationStepsPanel from './OperationStepsPanel';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getOperationSteps: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const RECORD_MEASURE = {
  id: 901,
  wo_operation_step_id: 201,
  work_order_operation_id: 31,
  serial_number: 'SN-001',
  value_text: null,
  value_numeric: 15,
  value_bool: null,
  is_conforming: true,
  recorded_by: 11,
  recorded_by_name: 'Bob T',
  recorded_at: '2026-07-02T15:30:00Z',
  source: 'kiosk',
  equipment_id: null,
  gauge: null,
  qualification_snapshot: null,
  attachment_document_id: null,
  superseded_by_id: null,
  supersede_reason: null,
  created_at: '2026-07-02T15:30:00Z',
};

// Honest "not done" from another surface: is_conforming false — render as
// unsatisfied evidence, never as a satisfying record.
const RECORD_NOT_DONE = {
  ...RECORD_MEASURE,
  id: 902,
  wo_operation_step_id: 202,
  serial_number: null,
  value_numeric: null,
  value_bool: false,
  is_conforming: false,
  recorded_by_name: 'Charlie M',
};

const STEP_BASE = {
  work_order_operation_id: 31,
  source_sheet_id: 5,
  source_sheet_revision: 'B',
  instruction_text: null,
  is_required: true,
  requires_gauge: false,
  spc_characteristic_id: null,
  created_at: '2026-07-01T12:00:00Z',
};

const VIEW = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_status: 'in_progress',
  is_serialized: true,
  serial_numbers: ['SN-001', 'SN-002'],
  steps: [
    {
      ...STEP_BASE,
      id: 201,
      sequence: 10,
      label: 'Torque check',
      step_type: 'measurement',
      config: { nominal: 15, lsl: 10, usl: 20, unit: 'Nm', decimals: 1 },
      records: [RECORD_MEASURE],
      complete: false,
      missing_serials: ['SN-002'],
    },
    {
      ...STEP_BASE,
      id: 202,
      sequence: 20,
      label: 'Deburr edges',
      step_type: 'checkbox',
      config: null,
      records: [RECORD_NOT_DONE],
      complete: false,
      missing_serials: ['SN-001', 'SN-002'],
    },
  ],
  steps_total: 2,
  steps_recorded: 0,
  completeness: { '201': { 'SN-001': true, 'SN-002': false }, '202': { 'SN-001': false, 'SN-002': false } },
};

describe('OperationStepsPanel (desktop read-only records panel)', () => {
  beforeEach(() => jest.clearAllMocks());

  it('renders the record trail read-only: values, serials, recorder, Central time — no capture affordances', async () => {
    mockedApi.getOperationSteps.mockResolvedValue(VIEW);
    render(<OperationStepsPanel operationId={31} />);

    expect(await screen.findByTestId('operation-steps-panel')).toBeInTheDocument();
    expect(mockedApi.getOperationSteps).toHaveBeenCalledWith(31);
    expect(screen.getByText('0/2 required recorded · 2 serials')).toBeInTheDocument();
    expect(screen.getByText(/Rev B/)).toBeInTheDocument();

    const measureTrail = screen.getByRole('list', { name: /records for torque check/i });
    expect(within(measureTrail).getByText('15 Nm')).toBeInTheDocument();
    expect(within(measureTrail).getByText('SN SN-001')).toBeInTheDocument();
    expect(within(measureTrail).getByText(/Bob T ·/)).toBeInTheDocument();
    // Outstanding serials are named per step.
    expect(screen.getByText('Missing: SN-002')).toBeInTheDocument();

    // The checkbox-false record renders as visible-but-unsatisfying evidence.
    const checkTrail = screen.getByRole('list', { name: /records for deburr edges/i });
    expect(within(checkTrail).getByText('Not done')).toBeInTheDocument();
    expect(within(checkTrail).getByText('Not satisfied')).toBeInTheDocument();

    // Read-only: office staff see evidence, they don't capture or correct it.
    expect(screen.queryByRole('button', { name: /record/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /correct/i })).not.toBeInTheDocument();
  });

  it('shows the gauge identity and a warn-and-record qualification marker on trail lines (PR 4)', async () => {
    const flaggedRecord = {
      ...RECORD_MEASURE,
      id: 903,
      gauge: { equipment_id: 5, equipment_code: 'MIC-042', name: '0-1 in micrometer' },
      qualification_snapshot: {
        evaluated_at: '2026-07-02T15:30:00Z',
        user_id: 11,
        work_center_id: 7,
        qualified: false,
        exceptions: [{ message: 'Missing certification: Torque tools L2' }],
      },
    };
    mockedApi.getOperationSteps.mockResolvedValue({
      ...VIEW,
      steps: [{ ...VIEW.steps[0], records: [flaggedRecord] }],
    });
    render(<OperationStepsPanel operationId={31} />);
    await screen.findByTestId('operation-steps-panel');

    // Gauge identity straight off the record — no extra fetch.
    expect(screen.getByTestId('record-gauge-903')).toHaveTextContent('Gauge 0-1 in micrometer (MIC-042)');

    // qualified === false → small amber marker, exception messages on the tooltip.
    const marker = screen.getByTestId('record-qualification-warning-903');
    expect(marker).toHaveTextContent('Qual');
    expect(marker).toHaveAttribute('title', 'Missing certification: Torque tools L2');

    // A qualified (or unevaluable) record shows NO marker: the base fixture
    // renders in the second step's trail without one.
    expect(screen.queryByTestId('record-qualification-warning-902')).not.toBeInTheDocument();
  });

  it('shows an empty state when the operation has no snapshot steps', async () => {
    mockedApi.getOperationSteps.mockResolvedValue({ ...VIEW, steps: [], steps_total: 0, steps_recorded: 0 });
    render(<OperationStepsPanel operationId={31} />);

    expect(await screen.findByText('No process steps')).toBeInTheDocument();
  });

  it('renders ErrorState with a working Retry on load failure', async () => {
    mockedApi.getOperationSteps
      .mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
      .mockResolvedValueOnce(VIEW);
    render(<OperationStepsPanel operationId={31} />);

    const errorState = await screen.findByTestId('error-state');
    expect(errorState).toHaveTextContent('boom');
    fireEvent.click(within(errorState).getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(screen.getByTestId('operation-steps-panel')).toBeInTheDocument());
    expect(mockedApi.getOperationSteps).toHaveBeenCalledTimes(2);
  });
});
