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
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

function fillField(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe('LaserNestManualModal', () => {
  beforeEach(() => {
    jest.clearAllMocks();
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

    const pdf = new File(['%PDF-1.4'], 'nest.pdf', { type: 'application/pdf' });
    fireEvent.change(screen.getByLabelText(/reference pdf/i), { target: { files: [pdf] } });

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

    const pdf = new File(['%PDF-1.4'], 'nest.pdf', { type: 'application/pdf' });
    fireEvent.change(screen.getByLabelText(/reference pdf/i), { target: { files: [pdf] } });

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
});
