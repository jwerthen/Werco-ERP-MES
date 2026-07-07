/**
 * Process Sheets PR 4 — the one-tap OOT → NCR + quality hold flow inside the
 * shared KioskStepsPanel OOT refusal strip.
 *
 * Server-gated and NON-optimistic: the confirm sub-state fires ONE call that
 * atomically files the NCR + QUALITY_HOLD blocker, flips the op ON_HOLD, and
 * closes open time entries. Success hands the result to the host (onQualityHeld)
 * so the NCR number lands on a queue-independent view; refusals render the
 * server's detail VERBATIM inside the strip and keep the confirm open.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import KioskStepsPanel, { StepsTransport } from './KioskStepsPanel';
import type { OperationStepsView, QualityHoldResult } from '../../types/processSheet';

const STEP_MEASURE = {
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
};

const VIEW: OperationStepsView = {
  operation_id: 31,
  work_order_id: 9,
  work_order_number: 'WO-2026-0142',
  operation_status: 'in_progress',
  is_serialized: false,
  serial_numbers: [],
  steps: [STEP_MEASURE],
  steps_total: 1,
  steps_recorded: 0,
  completeness: {},
};

const OOT_ERROR = {
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
};

const HOLD_RESULT: QualityHoldResult = {
  message: 'Quality hold raised',
  ncr_id: 55,
  ncr_number: 'NCR-000123',
  blocker_id: 77,
  operation_id: 31,
  operation_status: 'on_hold',
  closed_time_entry_ids: [501],
};

function makeTransport(overrides: Partial<StepsTransport> = {}): jest.Mocked<StepsTransport> {
  return {
    fetchView: jest.fn().mockResolvedValue(VIEW),
    createRecord: jest.fn().mockRejectedValue(OOT_ERROR),
    supersedeRecord: jest.fn(),
    uploadAttachment: jest.fn(),
    qualityHold: jest.fn().mockResolvedValue(HOLD_RESULT),
    ...overrides,
  } as jest.Mocked<StepsTransport>;
}

function renderPanel(transport: StepsTransport, props: Partial<React.ComponentProps<typeof KioskStepsPanel>> = {}) {
  const showToast = jest.fn();
  const onBack = jest.fn();
  const onQualityHeld = jest.fn();
  render(
    <KioskStepsPanel
      operationId={31}
      jobLabel="WO-2026-0142 · Op 20 Deburr"
      transport={transport}
      blocked={false}
      online
      showToast={showToast}
      onBack={onBack}
      onQualityHeld={onQualityHeld}
      {...props}
    />
  );
  return { showToast, onBack, onQualityHeld };
}

/** Drive the panel into the OOT refusal strip (measurement 0.6 vs 0.498–0.502). */
async function refuseOutOfTolerance() {
  await screen.findByTestId('kiosk-steps-progress');
  fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.6' } });
  fireEvent.click(screen.getByTestId('kiosk-record-101'));
  await screen.findByTestId('kiosk-step-oot');
}

describe('KioskStepsPanel one-tap OOT quality hold', () => {
  it('confirm sub-state → POST quality-hold with the refused value + notes → result handed to the host', async () => {
    const transport = makeTransport();
    const { onQualityHeld } = renderPanel(transport);
    await refuseOutOfTolerance();

    // The strip offers the one-tap; nothing has been sent yet.
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    const confirm = await screen.findByTestId('kiosk-oot-hold-confirm');
    expect(confirm).toHaveTextContent(/file an ncr for this measurement/i);
    // Measured vs limits stay visible on the strip through the confirm.
    expect(screen.getByTestId('kiosk-step-oot')).toHaveTextContent('Measured 0.6 · limits 0.498 – 0.502');
    expect(transport.qualityHold).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/notes for quality/i), { target: { value: 'Chipped insert' } });
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(transport.qualityHold).toHaveBeenCalledWith(31, 101, {
        measured_value: 0.6,
        notes: 'Chipped insert',
      })
    );
    // Success is the HOST's to show (queue-independent NCR screen).
    await waitFor(() => expect(onQualityHeld).toHaveBeenCalledWith(HOLD_RESULT));
    // The refused value was never recorded — no refetch happened.
    expect(transport.fetchView).toHaveBeenCalledTimes(1);
    expect(transport.createRecord).toHaveBeenCalledTimes(1);
  });

  it("sends the refused attempt's gauge as equipment_code — notes stay PURE operator notes (server writes the gauge identity)", async () => {
    const gaugeStep = { ...STEP_MEASURE, requires_gauge: true };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue({ ...VIEW, steps: [gaugeStep] }),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-042' } });
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.6' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await screen.findByTestId('kiosk-step-oot');

    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.change(await screen.findByLabelText(/notes for quality/i), { target: { value: 'Chipped insert' } });
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(transport.qualityHold).toHaveBeenCalledWith(31, 101, {
        measured_value: 0.6,
        equipment_code: 'MIC-042',
        notes: 'Chipped insert',
      })
    );
  });

  it('a stale VALUE_IN_TOLERANCE retry surfaces the verbatim detail, clears the dead OOT strip, and refreshes the view', async () => {
    const transport = makeTransport({
      qualityHold: jest.fn().mockRejectedValue({
        response: {
          status: 409,
          data: {
            detail: {
              code: 'VALUE_IN_TOLERANCE',
              detail:
                'Measured 0.6 is within tolerance (0.4 to 0.7) — record it as a step record instead of raising a quality hold',
              measured: 0.6,
              lsl: 0.4,
              usl: 0.7,
            },
          },
        },
      }),
    });
    const { showToast, onQualityHeld } = renderPanel(transport);
    await refuseOutOfTolerance();

    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        'error',
        'Measured 0.6 is within tolerance (0.4 to 0.7) — record it as a step record instead of raising a quality hold'
      )
    );
    // The strip's premise is dead (the server says the value is IN band): the
    // refusal UI clears and the view refetches to current reality.
    expect(screen.queryByTestId('kiosk-step-oot')).not.toBeInTheDocument();
    await waitFor(() => expect(transport.fetchView).toHaveBeenCalledTimes(2));
    expect(onQualityHeld).not.toHaveBeenCalled();
  });

  it('a 400 refusal (e.g. limit-less snapshot config) surfaces the verbatim detail and refreshes the view', async () => {
    const transport = makeTransport({
      qualityHold: jest.fn().mockRejectedValue({
        response: {
          status: 400,
          data: {
            detail:
              'This measurement step has no numeric tolerance limits — an unbounded measurement '
              + 'cannot be out of tolerance, so it takes no quality hold',
          },
        },
      }),
    });
    const { showToast } = renderPanel(transport);
    await refuseOutOfTolerance();

    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('error', expect.stringContaining('no numeric tolerance limits'))
    );
    expect(screen.queryByTestId('kiosk-step-oot')).not.toBeInTheDocument();
    await waitFor(() => expect(transport.fetchView).toHaveBeenCalledTimes(2));
  });

  it('a refused hold renders the server detail VERBATIM inside the confirm and stays open', async () => {
    const transport = makeTransport({
      qualityHold: jest
        .fn()
        .mockRejectedValue({ response: { status: 409, data: { detail: 'Operation already on hold' } } }),
    });
    const { onQualityHeld } = renderPanel(transport);
    await refuseOutOfTolerance();

    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    const error = await screen.findByTestId('kiosk-oot-hold-error');
    expect(error).toHaveTextContent('Operation already on hold');
    // Still in the confirm sub-state — the operator can retry or cancel.
    expect(screen.getByTestId('kiosk-oot-hold-confirm')).toBeInTheDocument();
    expect(onQualityHeld).not.toHaveBeenCalled();
  });

  it('without a host handler, falls back to an info toast with the NCR number + onBack', async () => {
    const transport = makeTransport();
    const { showToast, onBack } = renderPanel(transport, { onQualityHeld: undefined });
    await refuseOutOfTolerance();

    fireEvent.click(screen.getByTestId('kiosk-oot-hold-ncr'));
    fireEvent.click(screen.getByTestId('kiosk-oot-hold-submit'));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('info', 'NCR-000123 filed — operation on hold')
    );
    expect(onBack).toHaveBeenCalled();
  });
});
