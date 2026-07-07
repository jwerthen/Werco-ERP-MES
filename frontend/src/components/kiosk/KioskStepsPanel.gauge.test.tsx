/**
 * Process Sheets PR 4 — gauge capture on requires_gauge measurement steps.
 *
 * The gauge is MANDATORY on these steps and identified by its SCANNED code
 * (`equipment_code`) — kiosk operators cannot browse /equipment (path fence).
 * Server-validated on submit: stale gauge → 409 GAUGE_OUT_OF_CAL (no record
 * row), unknown code → 404 — both render as an inline danger strip with a
 * re-scan affordance. A success echoes the resolved gauge identity.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import KioskStepsPanel, { StepsTransport } from './KioskStepsPanel';
import type { GaugeRef, OperationStepRecord, OperationStepsView } from '../../types/processSheet';

const GAUGE: GaugeRef = { equipment_id: 5, equipment_code: 'MIC-042', name: '0-1 in micrometer' };

const STEP_GAUGED = {
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
  requires_gauge: true,
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
  steps: [STEP_GAUGED],
  steps_total: 1,
  steps_recorded: 0,
  completeness: {},
};

const RECORD_WITH_GAUGE: OperationStepRecord = {
  id: 900,
  wo_operation_step_id: 101,
  work_order_operation_id: 31,
  serial_number: null,
  value_text: null,
  value_numeric: 0.5001,
  value_bool: null,
  is_conforming: true,
  recorded_by: 3,
  recorded_by_name: 'Rosa Vega',
  recorded_at: '2026-07-06T14:30:00Z',
  source: 'kiosk',
  equipment_id: 5,
  gauge: GAUGE,
  qualification_snapshot: null,
  attachment_document_id: null,
  superseded_by_id: null,
  supersede_reason: null,
  created_at: '2026-07-06T14:30:00Z',
};

function makeTransport(overrides: Partial<StepsTransport> = {}): jest.Mocked<StepsTransport> {
  return {
    fetchView: jest.fn().mockResolvedValue(VIEW),
    createRecord: jest.fn().mockResolvedValue(RECORD_WITH_GAUGE),
    supersedeRecord: jest.fn(),
    uploadAttachment: jest.fn(),
    qualityHold: jest.fn(),
    ...overrides,
  } as jest.Mocked<StepsTransport>;
}

function renderPanel(transport: StepsTransport) {
  const showToast = jest.fn();
  render(
    <KioskStepsPanel
      operationId={31}
      jobLabel="WO-2026-0142 · Op 20 Deburr"
      transport={transport}
      blocked={false}
      online
      showToast={showToast}
      onBack={jest.fn()}
    />
  );
  return { showToast };
}

describe('KioskStepsPanel gauge capture (requires_gauge)', () => {
  it('holds the record back until a gauge code is scanned, then sends equipment_code with the value', async () => {
    const transport = makeTransport();
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    // In-tolerance value alone is NOT enough — the gauge is mandatory.
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    expect(screen.getByTestId('kiosk-record-101')).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-042' } });
    expect(screen.getByTestId('kiosk-record-101')).toBeEnabled();
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    await waitFor(() =>
      expect(transport.createRecord).toHaveBeenCalledWith(31, 101, {
        value_numeric: 0.5001,
        equipment_code: 'MIC-042',
      })
    );
  });

  it('echoes the resolved gauge name beside the field after a successful record (from the response)', async () => {
    const transport = makeTransport();
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-042' } });
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    // The refetched view still shows the editor (mock returns the same view),
    // now with the gauge code pre-filled and the resolved identity confirmed.
    const echo = await screen.findByTestId('kiosk-step-101-gauge-echo');
    expect(echo).toHaveTextContent('0-1 in micrometer (MIC-042)');
    expect(screen.getByLabelText(/gauge — scan or type/i)).toHaveValue('MIC-042');
  });

  it('renders GAUGE_OUT_OF_CAL as a danger strip naming the gauge, its status, and the calibration due date — re-scan clears it', async () => {
    const transport = makeTransport({
      createRecord: jest.fn().mockRejectedValue({
        response: {
          status: 409,
          data: {
            detail: {
              code: 'GAUGE_OUT_OF_CAL',
              detail:
                "Gauge '0-1 in micrometer' (MIC-042) is not calibration-current — use a current gauge or route this one to calibration",
              equipment_id: 5,
              status: 'out_of_calibration',
              next_calibration_date: '2026-06-01',
            },
          },
        },
      }),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-042' } });
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    const strip = await screen.findByTestId('kiosk-step-gauge-refused');
    expect(strip).toHaveTextContent('Gauge refused — not recorded');
    expect(strip).toHaveTextContent("Gauge '0-1 in micrometer' (MIC-042) is not calibration-current");
    expect(strip).toHaveTextContent('Status out of calibration');
    expect(strip).toHaveTextContent('Calibration due Jun 1, 2026');
    // NO record was written: the view must not refetch and pretend otherwise.
    expect(transport.fetchView).toHaveBeenCalledTimes(1);

    // Re-scan affordance: the field stays live; typing a new code clears the strip.
    const gaugeInput = screen.getByLabelText(/gauge — scan or type/i);
    expect(gaugeInput).toBeEnabled();
    fireEvent.change(gaugeInput, { target: { value: 'MIC-051' } });
    expect(screen.queryByTestId('kiosk-step-gauge-refused')).not.toBeInTheDocument();
  });

  it('renders an unknown gauge code (404) with the server message, ready for a re-scan', async () => {
    const transport = makeTransport({
      createRecord: jest.fn().mockRejectedValue({
        response: { status: 404, data: { detail: "No gauge with identifier 'MIC-99'" } },
      }),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-99' } });
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));

    const strip = await screen.findByTestId('kiosk-step-gauge-refused');
    expect(strip).toHaveTextContent("No gauge with identifier 'MIC-99'");
    expect(screen.getByLabelText(/gauge — scan or type/i)).toBeEnabled();
  });

  it('carries the scanned gauge code to the next serial on the same step (fresh value, no re-scan)', async () => {
    // Measuring serial after serial with the same gauge is the overwhelmingly
    // common case: after a successful record the code is seeded per-STEP, so
    // switching units pre-fills the gauge while the value input starts fresh.
    const serializedView: OperationStepsView = {
      ...VIEW,
      is_serialized: true,
      serial_numbers: ['SN-1', 'SN-2'],
    };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue(serializedView),
      createRecord: jest.fn().mockResolvedValue({ ...RECORD_WITH_GAUGE, serial_number: 'SN-1' }),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.change(screen.getByLabelText(/gauge — scan or type/i), { target: { value: 'MIC-042' } });
    fireEvent.change(screen.getByLabelText(/measured value/i), { target: { value: '0.5001' } });
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() =>
      expect(transport.createRecord).toHaveBeenCalledWith(31, 101, {
        value_numeric: 0.5001,
        equipment_code: 'MIC-042',
        serial_number: 'SN-1',
      })
    );

    fireEvent.click(screen.getByTestId('kiosk-serial-SN-2'));
    expect(screen.getByLabelText(/gauge — scan or type/i)).toHaveValue('MIC-042');
    expect(screen.getByLabelText(/measured value/i)).toHaveValue('');
  });

  it('shows the gauge identity on the locked record trail line', async () => {
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue({
        ...VIEW,
        steps: [{ ...STEP_GAUGED, records: [RECORD_WITH_GAUGE], complete: true }],
        steps_recorded: 1,
        completeness: {},
      }),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.click(screen.getByRole('button', { name: /bore diameter/i }));
    expect(await screen.findByTestId('kiosk-record-gauge-900')).toHaveTextContent(
      'Gauge 0-1 in micrometer (MIC-042)'
    );
  });
});
