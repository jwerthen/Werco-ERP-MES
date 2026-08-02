/**
 * Evidence-retry dedupe on PHOTO/FILE steps (deferred item closed post-PR-4).
 *
 * The capture flow is two-step: uploadAttachment mints a QUALITY_RECORD
 * Document, then createRecord/supersedeRecord links it. A failed link retry
 * must REUSE the already-minted document — re-uploading on every attempt
 * orphans duplicate Documents on the work order's AS9100 evidence trail.
 * The cache is per slot (+ corrected record id on the supersede path) and
 * File object identity: a re-picked file, a new slot, or a success always
 * uploads fresh.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import KioskStepsPanel, { StepsTransport } from './KioskStepsPanel';
import type { OperationStepRecord, OperationStepsView, StepAttachmentResult } from '../../types/processSheet';

const STEP_PHOTO = {
  id: 101,
  work_order_operation_id: 31,
  source_sheet_id: 5,
  source_sheet_revision: 'A',
  sequence: 10,
  label: 'Weld seam photo',
  instruction_text: null,
  step_type: 'photo',
  is_required: true,
  config: null,
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
  steps: [STEP_PHOTO],
  steps_total: 1,
  steps_recorded: 0,
  completeness: {},
};

const PHOTO_RECORD: OperationStepRecord = {
  id: 900,
  wo_operation_step_id: 101,
  work_order_operation_id: 31,
  serial_number: null,
  value_text: null,
  value_numeric: null,
  value_bool: null,
  is_conforming: true,
  recorded_by: 3,
  recorded_by_name: 'Rosa Vega',
  recorded_at: '2026-07-06T14:30:00Z',
  source: 'kiosk',
  equipment_id: null,
  gauge: null,
  qualification_snapshot: null,
  attachment_document_id: 501,
  superseded_by_id: null,
  supersede_reason: null,
  created_at: '2026-07-06T14:30:00Z',
};

function uploadResult(documentId: number): StepAttachmentResult {
  return {
    document_id: documentId,
    document_number: `DOC-${documentId}`,
    file_name: 'evidence.jpg',
    file_size: 4,
    mime_type: 'image/jpeg',
  };
}

/** A refusal with no structured code — surfaces as an error toast, draft intact. */
const CONFLICT_ERROR = {
  response: { status: 409, data: { detail: 'A record for this step landed from another station' } },
};

function makeTransport(overrides: Partial<StepsTransport> = {}): jest.Mocked<StepsTransport> {
  return {
    fetchView: jest.fn().mockResolvedValue(VIEW),
    createRecord: jest.fn().mockResolvedValue(PHOTO_RECORD),
    supersedeRecord: jest.fn(),
    uploadAttachment: jest.fn().mockResolvedValue(uploadResult(501)),
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

function pickFile(file: File) {
  fireEvent.change(screen.getByLabelText(/take photo/i), { target: { files: [file] } });
}

describe('KioskStepsPanel evidence-retry dedupe', () => {
  it('a retry after a failed createRecord reuses the uploaded document instead of re-uploading', async () => {
    const transport = makeTransport({
      createRecord: jest.fn().mockRejectedValueOnce(CONFLICT_ERROR).mockResolvedValue(PHOTO_RECORD),
    });
    const { showToast } = renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    const file = new File(['abcd'], 'evidence.jpg', { type: 'image/jpeg' });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('error', expect.stringContaining('another station')));
    expect(transport.createRecord).toHaveBeenCalledTimes(1);

    // The failed submit keeps the picked file — the operator just taps again.
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(2));

    // ONE upload, and both create attempts link the SAME document.
    expect(transport.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(transport.uploadAttachment).toHaveBeenCalledWith(31, 101, file);
    expect(transport.createRecord.mock.calls[0][2]).toEqual({ attachment_document_id: 501 });
    expect(transport.createRecord.mock.calls[1][2]).toEqual({ attachment_document_id: 501 });
  });

  it('picking a different file after the failure uploads the new file (identity keyed)', async () => {
    const transport = makeTransport({
      createRecord: jest.fn().mockRejectedValueOnce(CONFLICT_ERROR).mockResolvedValue(PHOTO_RECORD),
      uploadAttachment: jest
        .fn()
        .mockResolvedValueOnce(uploadResult(501))
        .mockResolvedValueOnce(uploadResult(502)),
    });
    const { showToast } = renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    const first = new File(['abcd'], 'blurry.jpg', { type: 'image/jpeg' });
    pickFile(first);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('error', expect.any(String)));

    // Retake → a different File object must NOT reuse the cached document.
    fireEvent.click(screen.getByRole('button', { name: /retake/i }));
    const second = new File(['efgh'], 'sharp.jpg', { type: 'image/jpeg' });
    pickFile(second);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(2));

    expect(transport.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(transport.uploadAttachment).toHaveBeenLastCalledWith(31, 101, second);
    expect(transport.createRecord.mock.calls[1][2]).toEqual({ attachment_document_id: 502 });
  });

  it('after a success, the next record on the same step uploads fresh (cache cleared)', async () => {
    const transport = makeTransport({
      uploadAttachment: jest
        .fn()
        .mockResolvedValueOnce(uploadResult(501))
        .mockResolvedValueOnce(uploadResult(502)),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    const file = new File(['abcd'], 'evidence.jpg', { type: 'image/jpeg' });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(1));

    // The refetched view (mock: still no records) re-opens the picker. Picking
    // the SAME File object again must upload again — success cleared the cache.
    await waitFor(() => expect(screen.getByLabelText(/take photo/i)).toBeInTheDocument());
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(2));

    expect(transport.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(transport.createRecord.mock.calls[1][2]).toEqual({ attachment_document_id: 502 });
  });

  it('a supersede retry (modal stays open on refusal) also reuses the uploaded document', async () => {
    const completedView: OperationStepsView = {
      ...VIEW,
      steps: [{ ...STEP_PHOTO, records: [PHOTO_RECORD], complete: true }],
      steps_recorded: 1,
    };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue(completedView),
      supersedeRecord: jest
        .fn()
        .mockRejectedValueOnce(CONFLICT_ERROR)
        .mockResolvedValue({ ...PHOTO_RECORD, id: 901, attachment_document_id: 601 }),
      uploadAttachment: jest.fn().mockResolvedValue(uploadResult(601)),
    });
    renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    fireEvent.click(screen.getByRole('button', { name: /weld seam photo/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^correct$/i }));

    fireEvent.change(screen.getByLabelText(/reason for correction/i), { target: { value: 'Wrong seam' } });
    const replacement = new File(['efgh'], 'correct-seam.jpg', { type: 'image/jpeg' });
    pickFile(replacement);
    fireEvent.click(screen.getByTestId('kiosk-supersede-save'));

    // Refused: the modal renders the message and stays open with the same File.
    await screen.findByText(/another station/i);
    expect(transport.supersedeRecord).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('kiosk-supersede-save'));
    await waitFor(() => expect(transport.supersedeRecord).toHaveBeenCalledTimes(2));

    expect(transport.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(transport.uploadAttachment).toHaveBeenCalledWith(31, 101, replacement);
    expect(transport.supersedeRecord.mock.calls[0][3]).toEqual({
      reason: 'Wrong seam',
      attachment_document_id: 601,
    });
    expect(transport.supersedeRecord.mock.calls[1][3]).toEqual({
      reason: 'Wrong seam',
      attachment_document_id: 601,
    });
  });

  it('serialized: the retry reuses the upload for the SAME serial; switching serials uploads fresh', async () => {
    const serializedView: OperationStepsView = {
      ...VIEW,
      is_serialized: true,
      serial_numbers: ['SN-1', 'SN-2'],
    };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue(serializedView),
      // BOTH attempts on SN-1 fail, so the cache stays primed with SN-1's
      // document when the operator moves on — SN-2 must not inherit it.
      createRecord: jest
        .fn()
        .mockRejectedValueOnce(CONFLICT_ERROR)
        .mockRejectedValueOnce(CONFLICT_ERROR)
        .mockResolvedValue({ ...PHOTO_RECORD, serial_number: 'SN-2' }),
      uploadAttachment: jest
        .fn()
        .mockResolvedValueOnce(uploadResult(501))
        .mockResolvedValueOnce(uploadResult(502)),
    });
    const { showToast } = renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    // Default slot is SN-1. Fail, then retry: the upload is reused.
    const file = new File(['abcd'], 'evidence.jpg', { type: 'image/jpeg' });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(showToast).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(2));
    expect(transport.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(transport.createRecord.mock.calls[0][2]).toEqual({ serial_number: 'SN-1', attachment_document_id: 501 });
    expect(transport.createRecord.mock.calls[1][2]).toEqual({ serial_number: 'SN-1', attachment_document_id: 501 });

    // Switch serial: the slot reset clears the cache, so even the SAME File
    // object uploads fresh and the body carries the new serial + document.
    fireEvent.click(screen.getByTestId('kiosk-serial-SN-2'));
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(3));
    expect(transport.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(transport.uploadAttachment).toHaveBeenLastCalledWith(31, 101, file);
    expect(transport.createRecord.mock.calls[2][2]).toEqual({ serial_number: 'SN-2', attachment_document_id: 502 });
  });

  it('navigating to another step after a failure never hands it the first step\'s cached document', async () => {
    const STEP_B = { ...STEP_PHOTO, id: 102, sequence: 20, label: 'Fixture photo' };
    const twoStepView: OperationStepsView = {
      ...VIEW,
      steps: [STEP_PHOTO, STEP_B],
      steps_total: 2,
    };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue(twoStepView),
      createRecord: jest.fn().mockRejectedValueOnce(CONFLICT_ERROR).mockResolvedValue(PHOTO_RECORD),
      uploadAttachment: jest
        .fn()
        .mockResolvedValueOnce(uploadResult(501))
        .mockResolvedValueOnce(uploadResult(502)),
    });
    const { showToast } = renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    // Fail on step A — its upload (501) stays cached.
    const file = new File(['abcd'], 'evidence.jpg', { type: 'image/jpeg' });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('error', expect.any(String)));

    // Expand step B and record the SAME File object there: step B must get its
    // OWN upload, never step A's cached document id.
    fireEvent.click(screen.getByRole('button', { name: /fixture photo/i }));
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-102'));
    await waitFor(() => expect(transport.createRecord).toHaveBeenCalledTimes(2));

    expect(transport.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(transport.uploadAttachment).toHaveBeenLastCalledWith(31, 102, file);
    expect(transport.createRecord.mock.calls[1][0]).toBe(31);
    expect(transport.createRecord.mock.calls[1][1]).toBe(102);
    expect(transport.createRecord.mock.calls[1][2]).toEqual({ attachment_document_id: 502 });
  });

  it('a create-path cache primed by a failure is never reused by the supersede path', async () => {
    const SN1_RECORD: OperationStepRecord = { ...PHOTO_RECORD, serial_number: 'SN-1', attachment_document_id: 400 };
    const mixedView: OperationStepsView = {
      ...VIEW,
      is_serialized: true,
      serial_numbers: ['SN-1', 'SN-2'],
      steps: [{ ...STEP_PHOTO, records: [SN1_RECORD], missing_serials: ['SN-2'] }],
      completeness: { '101': { 'SN-1': true } },
    };
    const transport = makeTransport({
      fetchView: jest.fn().mockResolvedValue(mixedView),
      createRecord: jest.fn().mockRejectedValue(CONFLICT_ERROR),
      supersedeRecord: jest.fn().mockResolvedValue({ ...SN1_RECORD, id: 901, attachment_document_id: 502 }),
      uploadAttachment: jest
        .fn()
        .mockResolvedValueOnce(uploadResult(501))
        .mockResolvedValueOnce(uploadResult(502)),
    });
    const { showToast } = renderPanel(transport);
    await screen.findByTestId('kiosk-steps-progress');

    // Prime the create-path cache: fail a first record on the empty SN-2 slot.
    fireEvent.click(screen.getByTestId('kiosk-serial-SN-2'));
    const file = new File(['abcd'], 'evidence.jpg', { type: 'image/jpeg' });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-record-101'));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('error', expect.any(String)));
    expect(transport.uploadAttachment).toHaveBeenCalledTimes(1);

    // Correct SN-1's existing record with the SAME File object: the supersede
    // must mint its own upload (502), never the create-primed 501.
    fireEvent.click(screen.getByTestId('kiosk-serial-SN-1'));
    fireEvent.click(await screen.findByRole('button', { name: /^correct$/i }));
    fireEvent.change(screen.getByLabelText(/reason for correction/i), { target: { value: 'Wrong fixture' } });
    pickFile(file);
    fireEvent.click(screen.getByTestId('kiosk-supersede-save'));
    await waitFor(() => expect(transport.supersedeRecord).toHaveBeenCalledTimes(1));

    expect(transport.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(transport.uploadAttachment).toHaveBeenLastCalledWith(31, 101, file);
    expect(transport.supersedeRecord.mock.calls[0][3]).toEqual({
      reason: 'Wrong fixture',
      attachment_document_id: 502,
    });
  });
});
