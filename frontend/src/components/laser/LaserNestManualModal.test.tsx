import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LaserNestManualModal from './LaserNestManualModal';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    createManualLaserNest: jest.fn(),
    updateLaserNest: jest.fn(),
    uploadDocument: jest.fn(),
    attachLaserNestDocument: jest.fn(),
    extractLaserNestFromPdf: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

function fillField(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** A minimal valid nest report PDF File for the file input. */
function pdfFile(name = 'nest.pdf') {
  return new File(['%PDF-1.4'], name, { type: 'application/pdf' });
}

/**
 * Drop a PDF on the reference-PDF input and wait for auto-extraction to fully
 * settle. Selecting a PDF puts the modal into an "Extracting…" state that
 * disables submit; we wait for that to clear so callers can then submit.
 */
async function selectPdf(file = pdfFile()) {
  fireEvent.change(screen.getByLabelText(/reference pdf/i), { target: { files: [file] } });
  // The handler awaits api.extractLaserNestFromPdf; let the microtasks drain so
  // the form is populated / hint rendered and the submit button re-enables.
  await waitFor(() => expect(mockApi.extractLaserNestFromPdf).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText(/extracting fields from the pdf/i)).not.toBeInTheDocument());
}

describe('LaserNestManualModal', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    // Default: a clean AI read. Individual tests override as needed. The
    // PDF-chain/attach-failure tests don't assert on extraction, but the modal
    // still calls it on select, so give it something resolvable by default.
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: null,
      material: null,
      thickness: null,
      sheet_size: null,
      planned_runs: null,
      confidence: 'high',
      source: 'ai',
      warning: null,
    });
  });

  it('submits the manual-create body with cnc_number and planned_runs', async () => {
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 7,
      nest_name: '8001',
      cnc_number: '8001',
      planned_runs: 5,
      completed_runs: 0,
      remaining_runs: 5,
    });
    const onSaved = jest.fn();
    const onClose = jest.fn();

    render(
      <LaserNestManualModal open workOrderId={42} onClose={onClose} onSaved={onSaved} />
    );

    fillField(/cnc number/i, '8001');
    fillField(/qty to cut/i, '5');
    fillField(/material/i, '304 SS');

    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    await waitFor(() => expect(mockApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    expect(mockApi.createManualLaserNest).toHaveBeenCalledWith(42, {
      cnc_number: '8001',
      planned_runs: 5,
      nest_name: undefined,
      material: '304 SS',
      thickness: undefined,
      sheet_size: undefined,
    });
    // No PDF chosen -> no upload/attach.
    expect(mockApi.uploadDocument).not.toHaveBeenCalled();
    expect(mockApi.attachLaserNestDocument).not.toHaveBeenCalled();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it('chains uploadDocument -> attachLaserNestDocument when a PDF is chosen', async () => {
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 11,
      nest_name: '9002',
      cnc_number: '9002',
      planned_runs: 2,
      completed_runs: 0,
      remaining_runs: 2,
    });
    mockApi.uploadDocument.mockResolvedValue({ id: 555 });
    mockApi.attachLaserNestDocument.mockResolvedValue({
      id: 11,
      nest_name: '9002',
      planned_runs: 2,
      completed_runs: 0,
      remaining_runs: 2,
      has_document: true,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);

    fillField(/cnc number/i, '9002');
    fillField(/qty to cut/i, '2');

    await selectPdf(pdfFile('nest.pdf'));

    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    await waitFor(() => expect(mockApi.attachLaserNestDocument).toHaveBeenCalledWith(11, 555));
    // Upload happened first with the right multipart fields.
    expect(mockApi.uploadDocument).toHaveBeenCalledTimes(1);
    const formData = mockApi.uploadDocument.mock.calls[0][0] as FormData;
    expect(formData.get('document_type')).toBe('drawing');
    expect(formData.get('work_order_id')).toBe('42');
    expect((formData.get('file') as File).name).toBe('nest.pdf');
  });

  it('keeps the created nest when the PDF attach fails: onSaved(warning), no second create, modal closes', async () => {
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 21,
      nest_name: '7700',
      cnc_number: '7700',
      planned_runs: 3,
      completed_runs: 0,
      remaining_runs: 3,
    });
    // The nest is created, but uploading its reference PDF blows up.
    mockApi.uploadDocument.mockRejectedValue(new Error('upload boom'));

    const onSaved = jest.fn();
    const onClose = jest.fn();

    render(<LaserNestManualModal open workOrderId={42} onClose={onClose} onSaved={onSaved} />);

    fillField(/cnc number/i, '7700');
    fillField(/qty to cut/i, '3');

    await selectPdf(pdfFile('nest.pdf'));

    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    // The create succeeded exactly once; the attach failure does not roll it back.
    await waitFor(() => expect(mockApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    // onSaved fires WITH the non-fatal warning so the parent refreshes + surfaces it.
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onSaved).toHaveBeenCalledWith(expect.stringMatching(/use attach pdf on the nest row to retry/i));
    // The upload was attempted; attach never reached (upload threw first).
    expect(mockApi.uploadDocument).toHaveBeenCalledTimes(1);
    expect(mockApi.attachLaserNestDocument).not.toHaveBeenCalled();
    // Modal closes rather than sitting positioned to re-create the nest.
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    // The fatal in-modal error banner is NOT used for this partial failure.
    expect(screen.queryByText(/failed to add laser nest/i)).not.toBeInTheDocument();
  });

  it('PATCHes an existing nest in edit mode (no create, no PDF upload field)', async () => {
    mockApi.updateLaserNest.mockResolvedValue({
      id: 3,
      nest_name: 'Nest A',
      cnc_number: '1234',
      planned_runs: 9,
      completed_runs: 1,
      remaining_runs: 8,
    });

    render(
      <LaserNestManualModal
        open
        workOrderId={42}
        nest={{
          id: 3,
          nest_name: 'Nest A',
          cnc_number: '1234',
          planned_runs: 4,
          completed_runs: 1,
          remaining_runs: 3,
        }}
        onClose={jest.fn()}
        onSaved={jest.fn()}
      />
    );

    // The reference-PDF upload field is only on the create path.
    expect(screen.queryByLabelText(/reference pdf/i)).not.toBeInTheDocument();

    fillField(/qty to cut/i, '9');
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(mockApi.updateLaserNest).toHaveBeenCalledTimes(1));
    expect(mockApi.updateLaserNest).toHaveBeenCalledWith(3, expect.objectContaining({ planned_runs: 9, cnc_number: '1234' }));
    expect(mockApi.createManualLaserNest).not.toHaveBeenCalled();
  });

  it('blocks submit when cnc_number is empty', async () => {
    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    fillField(/qty to cut/i, '3');
    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    expect(await screen.findByText(/cnc number is required/i)).toBeInTheDocument();
    expect(mockApi.createManualLaserNest).not.toHaveBeenCalled();
  });

  // --- Auto-extraction (create path) -------------------------------------

  it('the reference-PDF file input does not collide with the CNC/material/size field labels', () => {
    // Regression guard for the a11y bug that broke the suite: the helper text
    // mentioning "CNC number, material, and size" must live OUTSIDE the file
    // input's <label>, so getByLabelText resolves each field to a single input.
    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);

    expect(screen.getByLabelText(/cnc number/i)).toHaveAttribute('name', 'cnc_number');
    expect(screen.getByLabelText(/material/i)).toHaveAttribute('name', 'material');
    expect(screen.getByLabelText(/reference pdf/i)).toHaveAttribute('type', 'file');
  });

  it('selecting a PDF calls extractLaserNestFromPdf and fills the empty fields', async () => {
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: '8123',
      material: '304 SS',
      thickness: '0.125"',
      sheet_size: '48" x 96"',
      planned_runs: 4,
      confidence: 'high',
      source: 'ai',
      warning: null,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);

    const file = pdfFile('nest-8123.pdf');
    await selectPdf(file);
    expect(mockApi.extractLaserNestFromPdf).toHaveBeenCalledWith(file);

    await waitFor(() => expect(screen.getByLabelText(/cnc number/i)).toHaveValue('8123'));
    expect(screen.getByLabelText(/material/i)).toHaveValue('304 SS');
    expect(screen.getByLabelText(/thickness/i)).toHaveValue('0.125"');
    expect(screen.getByLabelText(/sheet size/i)).toHaveValue('48" x 96"');
    expect(screen.getByLabelText(/qty to cut/i)).toHaveValue(4);
  });

  it('does not clobber a field the user already typed', async () => {
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: '9999', // model read a different number...
      material: '304 SS',
      thickness: null,
      sheet_size: null,
      planned_runs: null,
      confidence: 'high',
      source: 'ai',
      warning: null,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);

    // ...but the planner already typed the CNC number by hand.
    fillField(/cnc number/i, '7001');
    await selectPdf();

    // The user-entered CNC number is preserved; the empty material field fills.
    await waitFor(() => expect(screen.getByLabelText(/material/i)).toHaveValue('304 SS'));
    expect(screen.getByLabelText(/cnc number/i)).toHaveValue('7001');
  });

  it('renders the "AI-filled — verify" hint after a clean read', async () => {
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: '8123',
      material: '304 SS',
      thickness: null,
      sheet_size: null,
      planned_runs: null,
      confidence: 'high',
      source: 'ai',
      warning: null,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await selectPdf();

    expect(await screen.findByText(/ai-filled from the pdf — verify before saving/i)).toBeInTheDocument();
    expect(screen.getByText(/high confidence/i)).toBeInTheDocument();
  });

  it('escalates the wording for a filename-only fallback', async () => {
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: '8123', // recovered from the filename stem
      material: null,
      thickness: null,
      sheet_size: null,
      planned_runs: null,
      confidence: null,
      source: 'filename',
      warning: 'PDF text was empty; used the filename.',
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await selectPdf();

    expect(
      await screen.findByText(/only the cnc number could be read from the filename/i)
    ).toBeInTheDocument();
    // The model-supplied warning is surfaced too.
    expect(screen.getByText(/pdf text was empty/i)).toBeInTheDocument();
  });

  it('escalates the wording for a low-confidence AI read', async () => {
    mockApi.extractLaserNestFromPdf.mockResolvedValue({
      cnc_number: '8123',
      material: '304 SS',
      thickness: null,
      sheet_size: null,
      planned_runs: null,
      confidence: 'low',
      source: 'ai',
      warning: null,
    });

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={jest.fn()} />);
    await selectPdf();

    expect(
      await screen.findByText(/low-confidence ai read — double-check every field/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/low confidence/i)).toBeInTheDocument();
  });

  it('swallows an extraction failure: no hint, manual entry still works', async () => {
    mockApi.extractLaserNestFromPdf.mockRejectedValue(new Error('extract boom'));
    mockApi.createManualLaserNest.mockResolvedValue({
      id: 31,
      nest_name: '6005',
      cnc_number: '6005',
      planned_runs: 1,
      completed_runs: 0,
      remaining_runs: 1,
    });
    const onSaved = jest.fn();

    render(<LaserNestManualModal open workOrderId={42} onClose={jest.fn()} onSaved={onSaved} />);

    await selectPdf();

    // No hint banner from a failed read; fields remain empty for manual entry.
    expect(screen.queryByText(/ai-filled/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/low-confidence ai read/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/cnc number/i)).toHaveValue('');

    // The planner types the fields by hand and the create still goes through —
    // the chosen PDF is still uploaded + attached.
    mockApi.uploadDocument.mockResolvedValue({ id: 777 });
    mockApi.attachLaserNestDocument.mockResolvedValue({
      id: 31,
      nest_name: '6005',
      planned_runs: 1,
      completed_runs: 0,
      remaining_runs: 1,
      has_document: true,
    });
    fillField(/cnc number/i, '6005');
    fillField(/qty to cut/i, '1');
    fireEvent.click(screen.getByRole('button', { name: /add nest/i }));

    await waitFor(() => expect(mockApi.createManualLaserNest).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mockApi.attachLaserNestDocument).toHaveBeenCalledWith(31, 777));
    expect(onSaved).toHaveBeenCalled();
  });
});
