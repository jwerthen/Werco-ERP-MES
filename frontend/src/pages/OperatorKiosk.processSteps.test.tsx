import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
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
  steps_total: 3,
  steps_recorded: 1,
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

const RECORD_DONE = {
  id: 900,
  wo_operation_step_id: 102,
  work_order_operation_id: 31,
  serial_number: null,
  value_text: null,
  value_numeric: null,
  value_bool: true,
  is_conforming: true,
  recorded_by: 3,
  recorded_by_name: 'Rosa Vega',
  recorded_at: '2026-07-03T14:30:00Z',
  source: 'kiosk',
  equipment_id: null,
  gauge: null,
  qualification_snapshot: null,
  attachment_document_id: null,
  superseded_by_id: null,
  supersede_reason: null,
  created_at: '2026-07-03T14:30:00Z',
};

const STEP_BASE = {
  work_order_operation_id: 31,
  source_sheet_id: 5,
  source_sheet_revision: 'A',
  instruction_text: null,
  is_required: true,
  requires_gauge: false,
  spc_characteristic_id: null,
  created_at: '2026-07-01T12:00:00Z',
  missing_serials: [],
};

const STEP_MEASURE = {
  ...STEP_BASE,
  id: 101,
  sequence: 10,
  label: 'Bore diameter',
  instruction_text: 'Measure with bore gauge',
  step_type: 'measurement',
  config: { nominal: 0.5, lsl: 0.498, usl: 0.502, unit: 'in', decimals: 4 },
  records: [],
  complete: false,
};

const STEP_CHECK = {
  ...STEP_BASE,
  id: 102,
  sequence: 20,
  label: 'Deburr edges',
  step_type: 'checkbox',
  config: null,
  records: [RECORD_DONE],
  complete: true,
};

const STEP_PHOTO = {
  ...STEP_BASE,
  id: 103,
  sequence: 30,
  label: 'Weld seam photo',
  step_type: 'photo',
  config: { hint: 'Photo of the finished seam' },
  records: [],
  complete: false,
};

const STEPS_VIEW = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_status: 'in_progress',
  is_serialized: false,
  serial_numbers: [],
  steps: [STEP_MEASURE, STEP_CHECK, STEP_PHOTO],
  steps_total: 3,
  steps_recorded: 1,
  completeness: {},
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

async function openStepsFromActiveBanner() {
  fireEvent.click(await screen.findByTestId('kiosk-active-steps'));
  // Wait past the loading shell for the fetched view (progress header renders).
  await screen.findByTestId('kiosk-steps-progress');
}

beforeEach(() => {
  jest.clearAllMocks();
  authAs({ id: 3, first_name: 'Rosa', last_name: 'Vega', employee_id: 'EMP-4217', role: 'operator', email: 'r@x.y' });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [QUEUE_ITEM] });
  mockedApi.getOperationSteps.mockResolvedValue(STEPS_VIEW);
});

describe('OperatorKiosk process steps', () => {
  it('shows the "Steps 1/3" chip on the queue card, and hides it at 0/0', async () => {
    mockedApi.getWorkCenterQueue.mockResolvedValue({
      queue: [QUEUE_ITEM, { ...QUEUE_ITEM, operation_id: 32, work_order_number: 'WO-2026-0143', steps_total: 0, steps_recorded: 0 }],
    });
    renderKiosk();

    const withSteps = await screen.findByRole('button', { name: /WO-2026-0142/i });
    expect(within(withSteps).getByTestId('kiosk-steps-chip')).toHaveTextContent('Steps 1/3');
    const withoutSteps = screen.getByRole('button', { name: /WO-2026-0143/i });
    expect(within(withoutSteps).queryByTestId('kiosk-steps-chip')).not.toBeInTheDocument();
  });

  it('opens the steps view from the active-job banner and renders typed rows with progress', async () => {
    renderKiosk();
    expect(await screen.findByTestId('kiosk-active-steps')).toHaveTextContent('Process steps · 1/3 recorded');

    await openStepsFromActiveBanner();
    expect(mockedApi.getOperationSteps).toHaveBeenCalledWith(31);
    expect(screen.getByTestId('kiosk-steps-progress')).toHaveTextContent('1/3');
    expect(screen.getByText('Bore diameter')).toBeInTheDocument();
    expect(screen.getByText('Deburr edges')).toBeInTheDocument();
    expect(screen.getByText('Weld seam photo')).toBeInTheDocument();
    // First incomplete required step auto-expands with its typed input + limits.
    expect(screen.getByLabelText(/measured value/i)).toBeInTheDocument();
    expect(screen.getAllByText(/LSL 0\.498 · NOM 0\.5 · USL 0\.502 in/).length).toBeGreaterThan(0);
  });

  it('previews tolerance live as the operator types (client-side only)', async () => {
    renderKiosk();
    await openStepsFromActiveBanner();

    const input = screen.getByLabelText(/measured value/i);
    fireEvent.change(input, { target: { value: '0.5' } });
    expect(screen.getByTestId('kiosk-step-101-tolerance-preview')).toHaveTextContent('Within limits (0.498 – 0.502)');

    fireEvent.change(input, { target: { value: '0.6' } });
    expect(screen.getByTestId('kiosk-step-101-tolerance-preview')).toHaveTextContent(
      'Outside limits (0.498 – 0.502) — the server will refuse this value'
    );
  });

  it('records a measurement non-optimistically (source:"kiosk", like clock-in) and refetches the steps view + queue', async () => {
    mockedApi.recordOperationStep.mockResolvedValue(RECORD_DONE);
    renderKiosk();
    await openStepsFromActiveBanner();
    const queueCallsBefore = mockedApi.getWorkCenterQueue.mock.calls.length;

    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    // source:"kiosk" — this kiosk runs on a normal session (no kiosk-scoped
    // credential), so the adoption-telemetry channel is client-reported,
    // exactly as clock-in reports it.
    await waitFor(() =>
      expect(mockedApi.recordOperationStep).toHaveBeenCalledWith(31, 101, {
        value_numeric: 0.5001,
        source: 'kiosk',
      })
    );
    // Refetch-after-record: no websocket for records, so the view re-reads…
    await waitFor(() => expect(mockedApi.getOperationSteps).toHaveBeenCalledTimes(2));
    // …and the host queue refreshes so the chip counts stay honest.
    await waitFor(() => expect(mockedApi.getWorkCenterQueue.mock.calls.length).toBeGreaterThan(queueCallsBefore));
    expect(await screen.findByText('Recorded — Bore diameter')).toBeInTheDocument();
  });

  it('renders the OUT_OF_TOLERANCE refusal with measured vs limits and does NOT refetch (no record was written)', async () => {
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
    renderKiosk();
    await openStepsFromActiveBanner();

    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.6' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    const strip = await screen.findByTestId('kiosk-step-oot');
    expect(strip).toHaveTextContent('Out of tolerance — not recorded');
    expect(strip).toHaveTextContent('Measured 0.6 · limits 0.498 – 0.502');
    expect(strip).toHaveTextContent(/re-measure/i);
    // The refusal wrote no record: the view must not pretend otherwise.
    expect(mockedApi.getOperationSteps).toHaveBeenCalledTimes(1);
    // Re-measure affordance: the input (with the refused value) stays active.
    expect(screen.getByLabelText(/measured value/i)).toBeEnabled();
  });

  it('PHOTO evidence is a two-step sequence: attachment upload FIRST, then the record with its document id', async () => {
    mockedApi.uploadOperationStepAttachment.mockResolvedValue({
      document_id: 77,
      document_number: 'QUA-202607-0001',
      file_name: 'seam.jpg',
      file_size: 3,
      mime_type: 'image/jpeg',
    });
    mockedApi.recordOperationStep.mockResolvedValue(RECORD_DONE);
    renderKiosk();
    await openStepsFromActiveBanner();

    // Expand the photo step and pick a file.
    fireEvent.click(screen.getByRole('button', { name: /weld seam photo/i }));
    const file = new File(['abc'], 'seam.jpg', { type: 'image/jpeg' });
    fireEvent.change(screen.getByLabelText(/take photo/i), { target: { files: [file] } });
    expect(await screen.findByText('seam.jpg')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('kiosk-record-103'));

    await waitFor(() =>
      expect(mockedApi.recordOperationStep).toHaveBeenCalledWith(31, 103, {
        attachment_document_id: 77,
        source: 'kiosk',
      })
    );
    expect(mockedApi.uploadOperationStepAttachment).toHaveBeenCalledWith(31, 103, file);
    expect(mockedApi.uploadOperationStepAttachment.mock.invocationCallOrder[0]).toBeLessThan(
      mockedApi.recordOperationStep.mock.invocationCallOrder[0]
    );
  });

  it('rejects an oversize photo client-side (10 MB cap) before any upload', async () => {
    renderKiosk();
    await openStepsFromActiveBanner();

    fireEvent.click(screen.getByRole('button', { name: /weld seam photo/i }));
    const big = new File(['x'], 'big.jpg', { type: 'image/jpeg' });
    Object.defineProperty(big, 'size', { value: 11 * 1024 * 1024 });
    fireEvent.change(screen.getByLabelText(/take photo/i), { target: { files: [big] } });

    expect(await screen.findByText(/big\.jpg is too large \(11\.0 MB\) — the limit is 10 MB/i)).toBeInTheDocument();
    expect(screen.getByTestId('kiosk-record-103')).toBeDisabled();
    expect(mockedApi.uploadOperationStepAttachment).not.toHaveBeenCalled();
  });

  it('corrections go through the supersede modal: reason required, then the append-only correction call', async () => {
    mockedApi.supersedeOperationStepRecord.mockResolvedValue({ ...RECORD_DONE, id: 901 });
    renderKiosk();
    await openStepsFromActiveBanner();

    // The completed checkbox step shows its locked record trail with Central time.
    fireEvent.click(screen.getByRole('button', { name: /deburr edges/i }));
    const trail = screen.getByRole('list', { name: /records for deburr edges/i });
    expect(within(trail).getByText('Done')).toBeInTheDocument();
    expect(within(trail).getByText(/Rosa Vega ·/)).toBeInTheDocument();

    fireEvent.click(within(trail).getByRole('button', { name: /correct/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/never erase evidence/i)).toBeInTheDocument();

    // Reason is required before the correction can be saved.
    expect(screen.getByTestId('kiosk-supersede-save')).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText(/reason for correction/i), {
      target: { value: 'Marked by mistake' },
    });
    fireEvent.click(screen.getByTestId('kiosk-supersede-save'));

    await waitFor(() =>
      expect(mockedApi.supersedeOperationStepRecord).toHaveBeenCalledWith(31, 102, 900, {
        reason: 'Marked by mistake',
        value_bool: true,
        source: 'kiosk',
      })
    );
    // Refetch after the correction lands.
    await waitFor(() => expect(mockedApi.getOperationSteps).toHaveBeenCalledTimes(2));
  });

  it('a STEPS_INCOMPLETE completion refusal renders the missing steps inline with a jump-to-step affordance', async () => {
    mockedApi.clockOut.mockResolvedValue({});
    mockedApi.completeOperation.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: {
            code: 'STEPS_INCOMPLETE',
            detail: 'Required process-sheet steps are missing conforming records for this operation',
            missing: [{ step_id: 101, label: 'Bore diameter', serials: [] }],
          },
        },
      },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    // Honest two-step message, verbatim missing labels…
    expect(
      await screen.findByText(
        'Clocked out, but completing failed: Required process steps are missing records: Bore diameter'
      )
    ).toBeInTheDocument();
    // …and the steps view opens with the inline missing banner (not just a toast).
    const banner = await screen.findByTestId('kiosk-steps-missing');
    expect(banner).toHaveTextContent('Cannot complete — required steps are missing records:');
    expect(within(banner).getByText('Bore diameter')).toBeInTheDocument();
    fireEvent.click(within(banner).getByRole('button', { name: /go to step/i }));
    expect(screen.getByLabelText(/measured value/i)).toBeInTheDocument();
  });

  it('a clock-out flagged steps_incomplete is INFO (labor recorded fine): message + steps view, no doomed complete call', async () => {
    // The clock-out CLOSED normally but the server flagged missing required
    // step records — the operation deliberately stays IN_PROGRESS. The
    // no-steps_incomplete branch (clock-out then complete fires) stays pinned
    // by the existing OperatorKiosk.test.tsx COMPLETE sequencing test.
    mockedApi.clockOut.mockResolvedValue({
      id: 501,
      steps_incomplete: {
        code: 'STEPS_INCOMPLETE',
        missing: [{ step_id: 101, label: 'Bore diameter', serials: [] }],
      },
    });
    renderKiosk();

    fireEvent.click(await screen.findByRole('button', { name: /^complete op$/i }));
    fireEvent.click(screen.getByTestId('kiosk-qty-confirm'));

    // Honest INFO state (role=status, never an error alert).
    const message = await screen.findByText(
      'Clocked out — 1 step record still needed before this operation can complete.'
    );
    expect(message.closest('[role="status"]')).not.toBeNull();
    expect(message.closest('[role="alert"]')).toBeNull();
    // The server already said complete would refuse — don't fire it.
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    // Path into the steps view, outstanding step rendered inline.
    const banner = await screen.findByTestId('kiosk-steps-missing');
    expect(within(banner).getByText('Bore diameter')).toBeInTheDocument();
  });

  describe('offline', () => {
    const POLL_MS = 15_000;

    beforeEach(() => jest.useFakeTimers());
    afterEach(() => {
      act(() => jest.runOnlyPendingTimers());
      jest.useRealTimers();
    });

    it('hard-disables recording while offline (read-only from last fetch)', async () => {
      renderKiosk();
      await openStepsFromActiveBanner();
      expect(screen.getByTestId('kiosk-record-101')).toHaveTextContent(/record/i);

      mockedApi.getWorkCenterQueue.mockRejectedValue(new Error('network down'));
      mockedApi.getMyActiveJob.mockRejectedValue(new Error('network down'));
      await act(async () => {
        jest.advanceTimersByTime(POLL_MS);
      });

      expect(await screen.findByText(/actions are disabled until the connection is restored/i)).toBeInTheDocument();
      const recordButton = screen.getByTestId('kiosk-record-101');
      expect(recordButton).toBeDisabled();
      expect(recordButton).toHaveTextContent(/offline/i);
      // The step list itself stays readable from the last fetch.
      expect(screen.getByText('Bore diameter')).toBeInTheDocument();
    });
  });
});
