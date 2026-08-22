/**
 * Renumber dialog — the properties that make it safe to hand to a shop.
 *
 * 1. NON-OPTIMISTIC. The dialog never patches anything locally; the caller gets the
 *    SERVER's result. On refusal it stays OPEN with the server's verbatim detail
 *    and the typed value intact, because a 409 must not cost someone their typing.
 * 2. BLOCKERS DISABLE SUBMIT, and render the server's `detail` verbatim rather
 *    than a prettified code — so the screen can never disagree with the 409 the
 *    operator would get.
 * 3. THE REASON IS REQUIRED. Every identity-affecting verb in this system requires
 *    one, and a whitespace-only reason must not satisfy it.
 * 4. THE SHEET WARNING RENDERS. This is the owner's actual case: for sheet and
 *    plate the part number IS the material spec, and losing it silently stops the
 *    sheet being suggested for nests with no error anywhere.
 * 5. COMPARE-AND-SWAP. The request must carry the number the client last READ, not
 *    the one being typed — it is the only concurrency control a Part has.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import RenumberPartDialog from './RenumberPartDialog';
import api from '../../services/api';
import { ToastProvider } from '../ui/Toast';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPartRenumberImpact: jest.fn(),
    renumberPart: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const part = {
  id: 7,
  part_number: '0.250-60X120-A36',
  name: 'Hot rolled plate',
  part_type: 'raw_material',
  unit_of_measure: 'sheets',
  is_active: true,
  status: 'active',
} as any;

const EMPTY_SHEET = {
  is_sheet_like_before: false,
  is_sheet_like_after: false,
  thickness_before: null,
  thickness_after: null,
  sheet_size_before: null,
  sheet_size_after: null,
  alloy_before: null,
  alloy_after: null,
};

function impact(overrides: Partial<any> = {}) {
  return {
    part_id: 7,
    current_part_number: '0.250-60X120-A36',
    normalized_new_part_number: null,
    eligible: true,
    blockers: [],
    advisories: [],
    open_work_order_count: 0,
    operations_with_stale_prefix: 0,
    operations_needing_repair: 0,
    existing_aliases: [],
    sheet: EMPTY_SHEET,
    ...overrides,
  };
}

function renderDialog(onRenumbered = jest.fn()) {
  const onClose = jest.fn();
  render(
    <ToastProvider>
      <RenumberPartDialog open part={part} onClose={onClose} onRenumbered={onRenumbered} />
    </ToastProvider>
  );
  return { onClose, onRenumbered };
}

async function fillValid() {
  fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'RM-1042' } });
  fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: 'Customer revised the print' } });
  await waitFor(() => expect(screen.getByRole('button', { name: /Renumber part/i })).toBeEnabled());
}

describe('RenumberPartDialog', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getPartRenumberImpact.mockResolvedValue(impact() as any);
    mockedApi.renumberPart.mockResolvedValue({
      part_id: 7,
      part_number: 'RM-1042',
      previous_part_number: '0.250-60X120-A36',
      alias_id: 3,
      alias_created: true,
      alias_reclaimed: false,
      no_op: false,
      work_orders_repaired: 0,
      operations_with_stale_prefix: 0,
      sheet_spec_changed: true,
    } as any);
  });

  it('sends the number the client last READ as the compare-and-swap precondition', async () => {
    renderDialog();
    await fillValid();
    fireEvent.click(screen.getByRole('button', { name: /Renumber part/i }));

    await waitFor(() => expect(mockedApi.renumberPart).toHaveBeenCalledTimes(1));
    expect(mockedApi.renumberPart).toHaveBeenCalledWith(7, {
      new_part_number: 'RM-1042',
      // The CURRENT number, not the typed one — Part has no version column, so
      // this string is the entire concurrency control.
      expected_part_number: '0.250-60X120-A36',
      reason: 'Customer revised the print',
    });
  });

  it('hands the caller the SERVER result and does not patch locally', async () => {
    const { onRenumbered } = renderDialog();
    await fillValid();
    fireEvent.click(screen.getByRole('button', { name: /Renumber part/i }));

    await waitFor(() => expect(onRenumbered).toHaveBeenCalledTimes(1));
    expect(onRenumbered).toHaveBeenCalledWith(
      expect.objectContaining({ part_number: 'RM-1042', previous_part_number: '0.250-60X120-A36' })
    );
  });

  it('stays open with the server detail on a refusal, keeping the typed value', async () => {
    mockedApi.renumberPart.mockRejectedValue({
      response: { status: 409, data: { detail: "Part number 'RM-1042' is already used by RM-1042 (Other stock)." } },
    });

    const { onRenumbered } = renderDialog();
    await fillValid();
    fireEvent.click(screen.getByRole('button', { name: /Renumber part/i }));

    expect(
      await screen.findByText("Part number 'RM-1042' is already used by RM-1042 (Other stock).")
    ).toBeInTheDocument();
    expect(onRenumbered).not.toHaveBeenCalled();
    // The draft survives, so a retry costs no re-typing.
    expect(screen.getByLabelText(/New part number/i)).toHaveValue('RM-1042');
  });

  it('disables submit while a blocker stands, and shows the server detail verbatim', async () => {
    mockedApi.getPartRenumberImpact.mockResolvedValue(
      impact({
        eligible: false,
        blockers: [
          { code: 'RETIRED_ALIAS', detail: "Part number 'RM-1042' is a retired number and still points at an existing part." },
        ],
      }) as any
    );

    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'RM-1042' } });
    fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: 'because' } });

    expect(
      await screen.findByText("Part number 'RM-1042' is a retired number and still points at an existing part.")
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: /Renumber part/i })).toBeDisabled());
  });

  it('requires a non-blank reason', async () => {
    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'RM-1042' } });
    await waitFor(() => expect(mockedApi.getPartRenumberImpact).toHaveBeenCalled());

    // Whitespace only must not satisfy it — min_length=1 alone would pass "   ".
    fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: /Renumber part/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: 'real reason' } });
    await waitFor(() => expect(screen.getByRole('button', { name: /Renumber part/i })).toBeEnabled());
  });

  it('refuses to submit the number the part already has', async () => {
    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), {
      target: { value: '0.250-60X120-A36' },
    });
    fireEvent.change(screen.getByLabelText(/Reason/i), { target: { value: 'no change' } });

    expect(await screen.findByText(/already this part's number/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Renumber part/i })).toBeDisabled();
  });

  it('shows the sheet spec advisory and the before/after the matcher reads', async () => {
    mockedApi.getPartRenumberImpact.mockResolvedValue(
      impact({
        advisories: [
          {
            code: 'SHEET_SPEC_LOST',
            detail:
              'This number states the material spec and the new one does not. The nest screen reads thickness, size and grade out of the part number to suggest a sheet.',
          },
        ],
        sheet: {
          ...EMPTY_SHEET,
          is_sheet_like_before: true,
          thickness_before: '0.250',
          sheet_size_before: '60X120',
          alloy_before: 'A36',
        },
      }) as any
    );

    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'RM-1042' } });

    expect(await screen.findByText(/the nest screen reads thickness/i)).toBeInTheDocument();
    // The before/after panel must show what is LOST, in the planner's terms.
    expect(screen.getByText(/0\.250 · 60X120 · A36/)).toBeInTheDocument();
    expect(screen.getByText(/nothing — no thickness, size or grade/)).toBeInTheDocument();
  });

  it('renders no sheet panel for a part the matcher never reads', async () => {
    mockedApi.getPartRenumberImpact.mockResolvedValue(impact() as any);
    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'NEW-456' } });

    await waitFor(() => expect(mockedApi.getPartRenumberImpact).toHaveBeenCalled());
    expect(screen.queryByText(/What the nest screen reads/i)).not.toBeInTheDocument();
  });

  it('tells the operator what still shows the old number on the floor', async () => {
    mockedApi.getPartRenumberImpact.mockResolvedValue(
      impact({ operations_with_stale_prefix: 3 }) as any
    );
    renderDialog();
    fireEvent.change(screen.getByLabelText(/New part number/i), { target: { value: 'RM-1042' } });

    expect(await screen.findByText(/3 operations on open jobs still show/i)).toBeInTheDocument();
  });

  it('lists numbers that already resolve to this part', async () => {
    mockedApi.getPartRenumberImpact.mockResolvedValue(
      impact({ existing_aliases: ['LEGACY-1', 'LEGACY-2'] }) as any
    );
    renderDialog();
    expect(await screen.findByText(/LEGACY-1, LEGACY-2/)).toBeInTheDocument();
  });
});
