/**
 * The in-process inspection sign-off control shared by Work Order Detail and Shop Floor.
 * The server verb is gated (roles, people only, required audit), so the control is
 * NON-optimistic: the chip reads "Inspected" only after a 200, a refusal keeps the
 * dialog open with the server's own text, and nothing is offered to a role the
 * route refuses.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { InspectionSignOffControl, canSignOffInspection } from './InspectionSignOff';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import type { OperationInspectionResponse, UserRole } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    signOffOperationInspection: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

const signed: OperationInspectionResponse = {
  message: 'Operation inspection recorded',
  operation_id: 501,
  work_order_id: 41,
  requires_inspection: true,
  inspection_complete: true,
  inspection_type: 'in_process',
  signed_off_by_id: 9,
  signed_off_by_name: 'Dana Quill',
  signed_off_at: '2026-11-23T15:04:05.123456Z',
};

function renderControl(overrides: Partial<React.ComponentProps<typeof InspectionSignOffControl>> = {}) {
  const onSignedOff = jest.fn();
  render(
    <ToastProvider>
      <InspectionSignOffControl
        operationId={501}
        operationLabel="Op 20 — Deburr"
        requiresInspection
        inspectionComplete={false}
        canSignOff
        onSignedOff={onSignedOff}
        {...overrides}
      />
    </ToastProvider>
  );
  return { onSignedOff };
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('canSignOffInspection', () => {
  it.each(['admin', 'manager', 'supervisor', 'quality', 'platform_admin'])('allows %s', role => {
    expect(canSignOffInspection({ role: role as UserRole, is_superuser: false })).toBe(true);
  });

  it.each(['operator', 'viewer', 'shipping'])('refuses %s', role => {
    expect(canSignOffInspection({ role: role as UserRole, is_superuser: false })).toBe(false);
  });

  it('allows a superuser and refuses no user', () => {
    expect(canSignOffInspection({ role: 'operator', is_superuser: true })).toBe(true);
    expect(canSignOffInspection(null)).toBe(false);
  });
});

describe('InspectionSignOffControl', () => {
  it('renders nothing on an operation that is not an inspection point', () => {
    const { container } = render(
      <ToastProvider>
        <InspectionSignOffControl
          operationId={1}
          operationLabel="Op 10"
          requiresInspection={false}
          inspectionComplete={false}
          canSignOff
        />
      </ToastProvider>
    );
    expect(container.textContent).toBe('');
  });

  it('shows the chip but no Sign off to a role the route refuses', () => {
    renderControl({ canSignOff: false });
    expect(screen.getByText('inspection required')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign off/i })).not.toBeInTheDocument();
  });

  it('shows Inspected and no button once the inspection is signed', () => {
    renderControl({ inspectionComplete: true });
    expect(screen.getByText('inspected')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign off/i })).not.toBeInTheDocument();
  });

  it('signs off with type and notes, then shows who signed and when', async () => {
    let resolve!: (value: OperationInspectionResponse) => void;
    mocked.signOffOperationInspection.mockReturnValue(new Promise(r => (resolve = r)));
    const { onSignedOff } = renderControl();

    fireEvent.click(screen.getByRole('button', { name: 'Sign off inspection for Op 20 — Deburr' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Inspection type'), { target: { value: 'final' } });
    fireEvent.change(within(dialog).getByLabelText('Notes (optional)'), { target: { value: '  bore in tolerance  ' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Sign off' }));

    expect(mocked.signOffOperationInspection).toHaveBeenCalledWith(501, {
      inspection_type: 'final',
      notes: 'bore in tolerance',
    });
    // Non-optimistic: still "required" while the request is in flight, and Cancel is locked.
    expect(screen.getByText('inspection required')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();

    resolve(signed);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('inspected')).toBeInTheDocument();
    expect(screen.getByText(/by Dana Quill, Nov 23, 2026/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign off inspection/i })).not.toBeInTheDocument();
    expect(onSignedOff).toHaveBeenCalledWith(signed);
  });

  it('sends no notes key when the notes field is blank', async () => {
    mocked.signOffOperationInspection.mockResolvedValue(signed);
    renderControl();

    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Sign off' }));

    await waitFor(() =>
      expect(mocked.signOffOperationInspection).toHaveBeenCalledWith(501, { inspection_type: 'in_process' })
    );
  });

  it('keeps the dialog open and shows the server text on a refusal', async () => {
    mocked.signOffOperationInspection.mockRejectedValue({
      response: {
        status: 503,
        data: {
          detail: 'The audit record for this inspection sign-off could not be saved; nothing was signed off. Retry.',
        },
      },
    });
    const { onSignedOff } = renderControl();

    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Sign off' }));

    expect(await screen.findByText(/nothing was signed off\. Retry\./)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('inspection required')).toBeInTheDocument();
    expect(onSignedOff).not.toHaveBeenCalled();
  });

  it('refuses Escape and a second submit while the request is in flight', async () => {
    let resolve!: (value: OperationInspectionResponse) => void;
    mocked.signOffOperationInspection.mockReturnValue(new Promise(r => (resolve = r)));
    renderControl();

    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    const dialog = screen.getByRole('dialog');
    const submit = within(dialog).getByRole('button', { name: 'Sign off' });
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.submit(submit.closest('form') as HTMLFormElement);
    fireEvent.keyDown(window, { key: 'Escape' });

    expect(mocked.signOffOperationInspection).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Notes (optional)')).toBeDisabled();

    resolve(signed);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('Cancel closes the dialog without calling the server', () => {
    const { onSignedOff } = renderControl();

    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mocked.signOffOperationInspection).not.toHaveBeenCalled();
    expect(onSignedOff).not.toHaveBeenCalled();
    expect(screen.getByText('inspection required')).toBeInTheDocument();
  });

  it('a refused sign-off can be retried from the same dialog', async () => {
    mocked.signOffOperationInspection
      .mockRejectedValueOnce({ response: { status: 503, data: { detail: 'nothing was signed off. Retry.' } } })
      .mockResolvedValueOnce(signed);
    const { onSignedOff } = renderControl();

    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Notes (optional)'), { target: { value: 'bore ok' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Sign off' }));
    expect(await screen.findByText(/nothing was signed off\. Retry\./)).toBeInTheDocument();

    // The notes survive the refusal, so the retry sends the same evidence.
    expect(within(screen.getByRole('dialog')).getByLabelText('Notes (optional)')).toHaveValue('bore ok');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Sign off' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mocked.signOffOperationInspection).toHaveBeenLastCalledWith(501, {
      inspection_type: 'in_process',
      notes: 'bore ok',
    });
    expect(screen.getByText('inspected')).toBeInTheDocument();
    expect(onSignedOff).toHaveBeenCalledTimes(1);
  });

  it('labels the notes field and caps it at the server limit', () => {
    renderControl();
    fireEvent.click(screen.getByRole('button', { name: /sign off inspection/i }));
    const notes = within(screen.getByRole('dialog')).getByLabelText('Notes (optional)');
    expect(notes).toHaveAttribute('maxLength', '2000');
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Sign off inspection');
  });
});
