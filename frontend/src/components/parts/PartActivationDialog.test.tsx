/**
 * Part activate / deactivate dialog — the properties that keep a switch-off from
 * turning into a delete.
 *
 *  1. **NON-OPTIMISTIC.** These two verbs are server-GATED (409 while the part
 *     still holds stock, 404 on a soft-deleted one), so nothing is flipped
 *     locally: the dialog stays up in a pending state while the post is in
 *     flight, stays OPEN with the server's verbatim `detail` on a refusal, and
 *     the caller only ever receives the SERVER's `{is_active, status}` pair.
 *  2. **A reason is mandatory on the way OUT and optional on the way back IN.**
 *     Switching a part off takes it off every picker on the floor and someone
 *     will ask why; a whitespace-only string must not satisfy it.
 *  3. **The acknowledgement is what the 409 is asking for**, so it has to be one
 *     tick away from the refusal — and it must travel on the request.
 *  4. **The copy says "not deleted".** This is the acceptance item: an empty
 *     leftover SKU is markable Inactive WITHOUT `is_deleted` ever being set, and
 *     the screen has to say so or the operator reaches for Delete instead.
 *  5. **A soft-deleted part is refused, and the refusal routes to Restore.**
 *     `is_active` doubles as the soft-delete MASK (`delete_material` sets
 *     `is_deleted` AND `is_active=False` AND `status="obsolete"` together), so a
 *     verb that could write `is_active = True` on a tombstoned row would be
 *     clearing half a delete — the 2026-08-16 `Vendor` trap in the parts table.
 *     Do not relax that test.
 *
 * The toast hook is mocked rather than driven through a real `<ToastProvider>`
 * because the VARIANT is the assertion (`info` for a no-op, `success` for a real
 * change), which a rendered toast cannot distinguish without reading a colour.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import PartActivationDialog, { ActivationPart } from './PartActivationDialog';
import api from '../../services/api';
import type { PartActivationResult } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    activatePart: jest.fn(),
    deactivatePart: jest.fn(),
  },
}));

const mockShowToast = jest.fn();
jest.mock('../ui/Toast', () => ({
  ...jest.requireActual<typeof import('../ui/Toast')>('../ui/Toast'),
  useToast: () => ({ showToast: mockShowToast }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// The owner's actual leftover: an empty SKU from the numbering recut.
const LEFTOVER = '0.0625-48X120-304SS';

const activePart: ActivationPart = {
  id: 88,
  part_number: LEFTOVER,
  name: '16GA 304 SS SHEET 48X120',
  is_active: true,
  status: 'active',
};

const inactivePart: ActivationPart = { ...activePart, is_active: false, status: 'obsolete' };

function result(over: Partial<PartActivationResult> = {}): PartActivationResult {
  return {
    part_id: 88,
    part_number: LEFTOVER,
    is_active: false,
    status: 'obsolete',
    no_op: false,
    ...over,
  };
}

function renderDialog(part: ActivationPart = activePart) {
  const onClose = jest.fn();
  const onChanged = jest.fn();
  render(<PartActivationDialog open part={part} onClose={onClose} onChanged={onChanged} />);
  return { onClose, onChanged };
}

const REASON = 'Combined onto SH-A240-304-0.0625-60X144-2B; number retired';

/**
 * Submit, with the click wrapped in `act` so the response's trailing state
 * updates land inside it rather than after the assertions. A request left in
 * flight simply flushes nothing.
 */
async function submit(): Promise<void> {
  const button = screen.getByTestId('part-activation-submit');
  await act(async () => {
    fireEvent.click(button);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.deactivatePart.mockResolvedValue(result());
  mockedApi.activatePart.mockResolvedValue(result({ is_active: true, status: 'active' }));
});

describe('PartActivationDialog — marking a leftover SKU inactive', () => {
  it('says plainly that the part is NOT deleted and stays in the catalog', () => {
    renderDialog();

    expect(screen.getByText(/Mark inactive —/)).toHaveTextContent(LEFTOVER);
    expect(screen.getByText(/not deleted/i)).toBeInTheDocument();
    expect(screen.getByText(/The part stays in the catalog/i)).toBeInTheDocument();
    expect(screen.getByText(/Stock on hand is not moved, consumed or written off/i)).toBeInTheDocument();
  });

  it('requires a reason, and a whitespace-only one does not count', async () => {
    renderDialog();
    expect(screen.getByTestId('part-activation-submit')).toBeDisabled();

    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: '   ' } });
    expect(screen.getByTestId('part-activation-submit')).toBeDisabled();

    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    await waitFor(() => expect(screen.getByTestId('part-activation-submit')).toBeEnabled());
  });

  it('posts the trimmed reason and leaves the stock acknowledgement off by default', async () => {
    renderDialog();
    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: `  ${REASON}  ` } });
    await submit();

    expect(mockedApi.deactivatePart).toHaveBeenCalledTimes(1);
    expect(mockedApi.deactivatePart).toHaveBeenCalledWith(88, {
      reason: REASON,
      acknowledge_remaining_stock: false,
    });
    expect(mockedApi.activatePart).not.toHaveBeenCalled();
  });

  it('carries the stock acknowledgement the 409 asks for, one tick away from the refusal', async () => {
    mockedApi.deactivatePart.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: `${LEFTOVER} still has 12 on hand. Tick the acknowledgement to switch it off anyway.` },
      },
    });

    const { onChanged } = renderDialog();
    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    await submit();

    expect(
      await screen.findByText(`${LEFTOVER} still has 12 on hand. Tick the acknowledgement to switch it off anyway.`)
    ).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();

    // The refusal is one tick away from being answered, and the typing survived.
    expect(screen.getByTestId('part-activation-reason')).toHaveValue(REASON);
    fireEvent.click(screen.getByTestId('part-activation-ack-stock'));
    await submit();

    expect(mockedApi.deactivatePart).toHaveBeenLastCalledWith(88, {
      reason: REASON,
      acknowledge_remaining_stock: true,
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it('hands the caller the SERVER pair and never a locally flipped part', async () => {
    mockedApi.deactivatePart.mockResolvedValue(result({ is_active: false, status: 'obsolete' }));

    const { onChanged, onClose } = renderDialog();
    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    await submit();

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(onChanged).toHaveBeenCalledWith({
      part_id: 88,
      part_number: LEFTOVER,
      is_active: false,
      status: 'obsolete',
      no_op: false,
    });
    // Closing is the caller's job (via `open`), so a refusal can keep this up.
    expect(onClose).not.toHaveBeenCalled();
    expect(mockShowToast).toHaveBeenCalledWith('success', expect.stringContaining('nothing was deleted'));
  });

  it('stays open in a pending state while the post is in flight', async () => {
    let settle: (value: PartActivationResult) => void = () => undefined;
    mockedApi.deactivatePart.mockReturnValue(
      new Promise<PartActivationResult>((resolve) => {
        settle = resolve;
      })
    );

    const { onChanged, onClose } = renderDialog();
    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    await submit();

    expect(screen.getByTestId('part-activation-submit')).toHaveTextContent('Saving…');
    expect(screen.getByTestId('part-activation-submit')).toBeDisabled();
    expect(screen.getByTestId('part-activation-reason')).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      settle(result());
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it('reports a no-op as information, not as a change that happened', async () => {
    mockedApi.deactivatePart.mockResolvedValue(result({ no_op: true }));

    const { onChanged } = renderDialog();
    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    await submit();

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(mockShowToast).toHaveBeenCalledWith('info', expect.stringContaining('was already inactive'));
  });
});

describe('PartActivationDialog — putting a part back on the pickers', () => {
  it('flips direction off is_active, and asks for no reason', async () => {
    renderDialog(inactivePart);

    expect(screen.getByText(/Mark active —/)).toHaveTextContent(LEFTOVER);
    // Nothing to acknowledge on the permissive direction.
    expect(screen.queryByTestId('part-activation-ack-stock')).not.toBeInTheDocument();
    expect(screen.getByTestId('part-activation-submit')).toBeEnabled();

    await submit();
    expect(mockedApi.activatePart).toHaveBeenCalledWith(88, undefined);
    expect(mockedApi.deactivatePart).not.toHaveBeenCalled();
  });

  it('sends a reason when one was typed', async () => {
    renderDialog(inactivePart);
    fireEvent.change(screen.getByTestId('part-activation-reason'), {
      target: { value: 'Back in use for the Miratech job' },
    });
    await submit();

    expect(mockedApi.activatePart).toHaveBeenCalledWith(88, { reason: 'Back in use for the Miratech job' });
  });

  it('tells the operator that restoring a DELETED part is a different decision', () => {
    renderDialog(inactivePart);
    expect(screen.getByText(/restore it from the deleted view first/i)).toBeInTheDocument();
  });

  it('stays open and shows the 404 verbatim for a soft-deleted part', async () => {
    // INVARIANT 3 GUARD — do not relax this. `is_active` doubles as the
    // soft-delete mask, so a verb that could write `is_active = True` on a
    // tombstoned row would clear half a delete. The server 404s; this screen
    // must surface that refusal rather than paint the part active.
    mockedApi.activatePart.mockRejectedValue({
      response: {
        status: 404,
        data: { detail: `${LEFTOVER} is deleted. Restore it first, then mark it active.` },
      },
    });

    const { onChanged, onClose } = renderDialog(inactivePart);
    await submit();

    expect(
      await screen.findByText(`${LEFTOVER} is deleted. Restore it first, then mark it active.`)
    ).toBeInTheDocument();
    expect(screen.getByTestId('part-activation-error')).toHaveAttribute('role', 'alert');
    expect(onChanged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(mockShowToast).not.toHaveBeenCalled();
  });
});

describe('PartActivationDialog — cancelling', () => {
  it('gates Cancel behind a discard confirm once something has been typed', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { onClose } = renderDialog();

    fireEvent.change(screen.getByTestId('part-activation-reason'), { target: { value: REASON } });
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockedApi.deactivatePart).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
