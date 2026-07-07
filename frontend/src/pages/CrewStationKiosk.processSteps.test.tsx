import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CrewStationKiosk from './CrewStationKiosk';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';

// Keep KioskApiError REAL (instanceof checks in the page) but mock every call.
jest.mock('../services/kioskStationClient', () => {
  const actual = jest.requireActual('../services/kioskStationClient');
  return {
    __esModule: true,
    ...actual,
    getStationToken: jest.fn(),
    setStationToken: jest.fn(),
    clearStationToken: jest.fn(),
    getStoredStation: jest.fn(),
    stationLogin: jest.fn(),
    getQueue: jest.fn(),
    mintBadgeToken: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockIn: jest.fn(),
    clockOut: jest.fn(),
    reportProduction: jest.fn(),
    completeOperation: jest.fn(),
    holdOperation: jest.fn(),
    getOperationSteps: jest.fn(),
    recordOperationStep: jest.fn(),
    supersedeOperationStepRecord: jest.fn(),
    uploadOperationStepAttachment: jest.fn(),
  };
});

const mocked = kioskClient as jest.Mocked<typeof kioskClient>;

const STATION = {
  id: 3,
  label: 'Weld Bay Kiosk',
  work_center_id: 7,
  work_center_code: 'WELD1',
  work_center_name: 'Weld Bay 1',
};

const ITEM = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  part_number: 'PN-7731',
  part_name: 'Weldment, frame',
  operation_number: '20',
  operation_name: 'Weld',
  work_center_id: 7,
  status: 'in_progress',
  quantity_ordered: 50,
  quantity_complete: 37,
  quantity_scrapped: 2,
  priority: 5,
  due_date: null,
  roster: [],
  steps_total: 1,
  steps_recorded: 0,
};

const QUEUE_RES = {
  queue: [ITEM],
  server_time: new Date().toISOString(),
  station: STATION,
};

const ALICE = { id: 13, full_name: 'Alice W', employee_id: 'E013' };
const ALICE_MINT = { access_token: 'op-token-alice', user: ALICE };
const BOB = { id: 11, full_name: 'Bob T', employee_id: 'E011' };
const BOB_MINT = { access_token: 'op-token-bob', user: BOB };
const BOB_ROSTER_ENTRY = {
  time_entry_id: 501,
  user_id: 11,
  operator_name: 'Bob T',
  employee_id: 'E011',
  entry_type: 'run',
  clock_in: '2026-07-02T15:00:00Z',
};

// Serialized WO: SN-001 already has a conforming record; SN-002 is outstanding.
const REC_SN1 = {
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
  attachment_document_id: null,
  superseded_by_id: null,
  supersede_reason: null,
  created_at: '2026-07-02T15:30:00Z',
};

const SERIAL_STEP = {
  id: 201,
  work_order_operation_id: 31,
  source_sheet_id: 5,
  source_sheet_revision: 'B',
  sequence: 10,
  label: 'Torque check',
  instruction_text: null,
  step_type: 'measurement',
  is_required: true,
  config: { nominal: 15, lsl: 10, usl: 20, unit: 'Nm', decimals: 1 },
  requires_gauge: false,
  spc_characteristic_id: null,
  created_at: '2026-07-01T12:00:00Z',
  records: [REC_SN1],
  complete: false,
  missing_serials: ['SN-002'],
};

const SERIAL_VIEW = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_status: 'in_progress',
  is_serialized: true,
  serial_numbers: ['SN-001', 'SN-002'],
  steps: [SERIAL_STEP],
  steps_total: 1,
  steps_recorded: 0,
  // Keyed by STRINGIFIED step id — the JSON wire shape.
  completeness: { '201': { 'SN-001': true, 'SN-002': false } },
};

function renderKiosk() {
  return render(
    <MemoryRouter initialEntries={['/kiosk?kiosk=1&station=3']}>
      <CrewStationKiosk />
    </MemoryRouter>
  );
}

function unlockedStation() {
  mocked.getStationToken.mockReturnValue('station-token');
  mocked.getStoredStation.mockReturnValue(STATION);
}

/** Type a badge on the window (wedge scanner) and hit Enter. */
function scanBadge(id: string) {
  id.split('').forEach((key) => fireEvent.keyDown(window, { key }));
  fireEvent.keyDown(window, { key: 'Enter' });
}

async function openJobDetail() {
  fireEvent.click(await screen.findByRole('button', { name: /WO-2026-0142/i }));
  await screen.findByRole('region', { name: /job detail/i });
}

/** Job detail → Steps verb → badge scan → loaded steps view. */
async function openStepsAsAlice() {
  await openJobDetail();
  fireEvent.click(screen.getByTestId('crew-steps-verb'));
  await screen.findByText(/scan badge to open steps/i);
  scanBadge('E013');
  await screen.findByTestId('kiosk-steps-progress');
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getStationToken.mockReturnValue(null);
  mocked.getStoredStation.mockReturnValue(null);
  mocked.getQueue.mockResolvedValue(QUEUE_RES);
  mocked.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
  mocked.mintBadgeToken.mockResolvedValue(ALICE_MINT);
  mocked.getOperationSteps.mockResolvedValue(SERIAL_VIEW);
});

describe('CrewStationKiosk process steps', () => {
  it('shows the steps chip on the crew job card and hides it at 0/0', async () => {
    unlockedStation();
    mocked.getQueue.mockResolvedValue({
      ...QUEUE_RES,
      queue: [ITEM, { ...ITEM, operation_id: 32, work_order_number: 'WO-2026-0143', steps_total: 0, steps_recorded: 0 }],
    });
    renderKiosk();

    const withSteps = await screen.findByRole('button', { name: /WO-2026-0142/i });
    expect(within(withSteps).getByTestId('kiosk-steps-chip')).toHaveTextContent('Steps 0/1');
    const withoutSteps = screen.getByRole('button', { name: /WO-2026-0143/i });
    expect(within(withoutSteps).queryByTestId('kiosk-steps-chip')).not.toBeInTheDocument();
  });

  it('hides the Steps verb on the job screen when the operation has no steps', async () => {
    unlockedStation();
    mocked.getQueue.mockResolvedValue({
      ...QUEUE_RES,
      queue: [{ ...ITEM, steps_total: 0, steps_recorded: 0 }],
    });
    renderKiosk();

    await openJobDetail();
    expect(screen.queryByTestId('crew-steps-verb')).not.toBeInTheDocument();
  });

  it('gates the steps view behind a badge scan and reads it with the minted OPERATOR token', async () => {
    unlockedStation();
    renderKiosk();

    await openJobDetail();
    expect(screen.getByTestId('crew-steps-verb')).toHaveTextContent('Steps 0/1');
    fireEvent.click(screen.getByTestId('crew-steps-verb'));

    // No steps read before the badge establishes the recording identity.
    expect(await screen.findByText(/scan badge to open steps/i)).toBeInTheDocument();
    expect(mocked.getOperationSteps).not.toHaveBeenCalled();

    scanBadge('E013');
    await screen.findByTestId('kiosk-steps-progress');
    expect(mocked.mintBadgeToken).toHaveBeenCalledWith('E013');
    expect(mocked.getOperationSteps).toHaveBeenCalledWith('op-token-alice', 31);
    expect(screen.getByText('Recording as Alice W')).toBeInTheDocument();
  });

  it('renders per-serial state: switching the serial selector re-renders that serial\'s records and inputs', async () => {
    unlockedStation();
    renderKiosk();
    await openStepsAsAlice();

    // Default serial is the WO's first (SN-001) — its slot is satisfied, so the
    // locked record trail shows and no fresh-record input renders.
    fireEvent.click(screen.getByTestId('kiosk-serial-SN-001'));
    const trail = await screen.findByRole('list', { name: /records for torque check/i });
    expect(within(trail).getByText('15 Nm')).toBeInTheDocument();
    expect(within(trail).getByText(/Bob T ·/)).toBeInTheDocument();
    // Satisfied slot: no fresh-record input.
    expect(screen.queryByTestId('kiosk-record-201')).not.toBeInTheDocument();

    // Switch to the outstanding serial: the trail disappears, the input appears.
    fireEvent.click(screen.getByTestId('kiosk-serial-SN-002'));
    expect(screen.queryByRole('list', { name: /records for torque check/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/measured value/i)).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-record-201')).toBeInTheDocument();
  });

  it('records against the selected serial with the operator token and refetches steps + queue', async () => {
    unlockedStation();
    mocked.recordOperationStep.mockResolvedValue({ ...REC_SN1, id: 902, serial_number: 'SN-002' });
    renderKiosk();
    await openStepsAsAlice();
    const queueCallsBefore = mocked.getQueue.mock.calls.length;

    fireEvent.click(screen.getByTestId('kiosk-serial-SN-002'));
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '12' } });
    fireEvent.click(screen.getByTestId('kiosk-record-201'));

    await waitFor(() =>
      expect(mocked.recordOperationStep).toHaveBeenCalledWith('op-token-alice', 31, 201, {
        value_numeric: 12,
        serial_number: 'SN-002',
      })
    );
    // Refetch-after-record (no websocket) + the host queue re-read for the chip.
    await waitFor(() => expect(mocked.getOperationSteps).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mocked.getQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));
  });

  it('a 401 mid-record (expired 5-minute badge token) returns to the scan screen, never locks the station', async () => {
    unlockedStation();
    mocked.recordOperationStep.mockRejectedValue(new KioskApiError(401, 'Token expired', 'Token expired'));
    renderKiosk();
    await openStepsAsAlice();

    fireEvent.click(screen.getByTestId('kiosk-serial-SN-002'));
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '12' } });
    fireEvent.click(screen.getByTestId('kiosk-record-201'));

    expect(await screen.findByText(/scan badge to open steps/i)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/badge session expired/i);
    // Flow reset, not a station lock: no PIN screen, token untouched.
    expect(screen.queryByText(/enter station pin/i)).not.toBeInTheDocument();
    expect(mocked.clearStationToken).not.toHaveBeenCalled();
  });

  it('a STEPS_INCOMPLETE completion refusal jumps into the steps view with the missing serials inline', async () => {
    unlockedStation();
    mocked.completeOperation.mockRejectedValue(
      new KioskApiError(
        409,
        {
          code: 'STEPS_INCOMPLETE',
          detail: 'Required process-sheet steps are missing conforming records for this operation',
          missing: [{ step_id: 201, label: 'Torque check', serials: ['SN-002'] }],
        },
        'Required process-sheet steps are missing conforming records for this operation'
      )
    );
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /^complete$/i }));
    // Clear the prefilled GOOD so no production report happens (pure complete).
    fireEvent.click(await screen.findByTestId('kiosk-key-clear'));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    await screen.findByRole('dialog');
    scanBadge('E013');

    // The badge that signed the completion attempt carries into the steps view.
    const banner = await screen.findByTestId('kiosk-steps-missing');
    expect(banner).toHaveTextContent('Torque check');
    expect(banner).toHaveTextContent('SN-002');
    expect(screen.getByText('Recording as Alice W')).toBeInTheDocument();
    expect(mocked.getOperationSteps).toHaveBeenCalledWith('op-token-alice', 31);
    // The refusal's outstanding serial is pre-selected for recording.
    expect(screen.getByTestId('kiosk-serial-SN-002')).toHaveAttribute('aria-pressed', 'true');
  });

  it('a LEAVE clock-out flagged steps_incomplete is INFO (labor recorded fine): message + straight into steps as the leaver', async () => {
    // The clock-out CLOSED normally; the operation deliberately stays
    // IN_PROGRESS until the flagged records land. The no-steps_incomplete
    // branch (success toast, back to the job screen) stays pinned by the
    // existing CrewStationKiosk.test.tsx job-first LEAVE test.
    unlockedStation();
    mocked.getQueue.mockResolvedValue({ ...QUEUE_RES, queue: [{ ...ITEM, roster: [BOB_ROSTER_ENTRY] }] });
    mocked.mintBadgeToken.mockResolvedValue(BOB_MINT);
    mocked.clockOut.mockResolvedValue({
      steps_incomplete: {
        code: 'STEPS_INCOMPLETE',
        missing: [{ step_id: 201, label: 'Torque check', serials: ['SN-002'] }],
      },
    });
    renderKiosk();

    await openJobDetail();
    fireEvent.click(screen.getByRole('button', { name: /join \/ leave/i }));
    scanBadge('E011'); // rostered → LEAVE
    await screen.findByText(/clock out — bob t/i);
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm')); // 0/0 allowed

    // Honest INFO state (role=status, never an error alert).
    const message = await screen.findByText(
      'Clocked out — 1 step record still needed before this operation can complete.'
    );
    expect(message.closest('[role="status"]')).not.toBeNull();
    expect(message.closest('[role="alert"]')).toBeNull();
    // Straight into the steps view attributed to Bob's fresh badge token…
    await screen.findByTestId('kiosk-steps-progress');
    expect(screen.getByText('Recording as Bob T')).toBeInTheDocument();
    expect(mocked.getOperationSteps).toHaveBeenCalledWith('op-token-bob', 31);
    // …with the outstanding step/serial inline and pre-selected.
    const banner = screen.getByTestId('kiosk-steps-missing');
    expect(banner).toHaveTextContent('Torque check');
    expect(banner).toHaveTextContent('SN-002');
    expect(screen.getByTestId('kiosk-serial-SN-002')).toHaveAttribute('aria-pressed', 'true');
  });
});
