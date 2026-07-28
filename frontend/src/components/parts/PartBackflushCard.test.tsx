/**
 * Automatic BOM Backflush card — the opt-in switch on a part's detail page.
 *
 * The assertions here are the compliance-shaped ones, not the cosmetic ones.
 * Turning this flag on makes stock move by itself, forever after, with nothing
 * asking first — the flip IS the consent — so what this suite guards is that the
 * card cannot mislead anyone about what the server will do:
 *
 *  - the state chip reads the PART, never the readiness response, so a stale or
 *    failed readiness read can never make an opted-in part look off;
 *  - blockers render from `detail` (the server's own sentence, the same one it
 *    joins into its 409) and never from a prettified `code` — a card that
 *    invented its own wording could drift from the refusal a user is about to
 *    get;
 *  - the write is server-GATED, therefore strictly NON-OPTIMISTIC: nothing in
 *    the UI moves until the server answers, a 409 leaves the chip exactly where
 *    it was, and the refusal renders VERBATIM and in full;
 *  - the confirm button is deliberately NOT disabled on a known-blocked part —
 *    `eligible` is a snapshot, not authorisation (the BOM is mutable by other
 *    people between the read and the write), and a dead button says nothing
 *    about why;
 *  - on success the card adopts the part the SERVER handed back rather than a
 *    locally toggled copy, because parts carry no working optimistic lock;
 *  - the toggle is gated on `parts:edit`, matching the server's own trio;
 *  - the card states plainly that it checks the BOM only, so a clean verdict
 *    here is not read as clean everywhere.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { PartBackflushCard, showsBackflushCard } from './PartBackflushCard';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { BackflushDiagnostic, Part, PartBackflushReadiness } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPartBackflushReadiness: jest.fn(),
    setPartBackflush: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const makePart = (overrides: Partial<Part> = {}): Part =>
  ({
    id: 7,
    part_number: 'PN-7001',
    name: 'Titanium Bracket',
    part_type: 'manufactured',
    unit_of_measure: 'each',
    standard_cost: 42.5,
    is_critical: false,
    requires_inspection: false,
    backflush_components: false,
    is_active: true,
    status: 'active',
    version: 0,
    created_at: '2026-07-27T12:00:00Z',
    updated_at: '2026-07-27T12:00:00Z',
    ...overrides,
  } as Part);

const makeDiagnostic = (overrides: Partial<BackflushDiagnostic> = {}): BackflushDiagnostic => ({
  code: 'zero_bom_quantity',
  severity: 'blocking',
  detail: 'BOM line 10 for RAW-304 has quantity 0, which would be treated as 1 per unit. State the real quantity',
  bom_item_id: 11,
  component_part_id: 55,
  component_part_number: 'RAW-304',
  operation_id: null,
  ...overrides,
});

const makeReadiness = (overrides: Partial<PartBackflushReadiness> = {}): PartBackflushReadiness => ({
  part_id: 7,
  part_number: 'PN-7001',
  backflush_components: false,
  eligible: true,
  blockers: [],
  advisories: [],
  ...overrides,
});

/** An Axios-shaped rejection, so the card's `detail` extraction is exercised for real. */
const axiosError = (status: number, detail: unknown) =>
  Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });

const renderCard = (part: Part, canEdit = true) => {
  const onPartUpdated = jest.fn();
  const utils = render(
    <MemoryRouter>
      <ToastProvider>
        <PartBackflushCard part={part} canEdit={canEdit} onPartUpdated={onPartUpdated} />
      </ToastProvider>
    </MemoryRouter>
  );
  return { ...utils, onPartUpdated };
};

const stateChip = () => screen.getByTestId('part-backflush-state');

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getPartBackflushReadiness.mockResolvedValue(makeReadiness());
});

describe('PartBackflushCard: state and readiness', () => {
  it('reads the ON/OFF chip off the PART, not off the readiness response', async () => {
    // The readiness body echoes the flag too, and taking it from THERE would
    // mean a slow, failed or stale read could show an opted-in part as "Off" —
    // which is the one lie this card must never tell, because the shop would
    // then believe no material is moving while it is.
    mockApi.getPartBackflushReadiness.mockResolvedValue(makeReadiness({ backflush_components: false }));
    renderCard(makePart({ backflush_components: true }));

    expect(stateChip()).toHaveTextContent('On');
    await waitFor(() => expect(mockApi.getPartBackflushReadiness).toHaveBeenCalledWith(7));
    expect(stateChip()).toHaveTextContent('On');
  });

  it('renders each blocker from its `detail` sentence, never from a prettified code', async () => {
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({ eligible: false, blockers: [makeDiagnostic()] })
    );
    renderCard(makePart());

    const blockers = await screen.findByTestId('part-backflush-blockers');
    // The server's own sentence, verbatim — the same one it joins into its 409.
    expect(
      within(blockers).getByText(/BOM line 10 for RAW-304 has quantity 0/)
    ).toBeInTheDocument();
    // The code is shown as a machine key alongside, not translated into prose.
    expect(within(blockers).getByText(/zero_bom_quantity/)).toBeInTheDocument();
    expect(within(blockers).queryByText(/Zero Bom Quantity/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Cannot be enabled/i)).toBeInTheDocument();
  });

  it('separates advisories from blockers and says they are not blocking', async () => {
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({
        advisories: [
          makeDiagnostic({
            code: 'routing_only_no_bom',
            severity: 'advisory',
            detail: 'Part PN-7001 has no active BOM; its component demand would come only from routing operations.',
          }),
        ],
      })
    );
    renderCard(makePart());

    const advisories = await screen.findByTestId('part-backflush-advisories');
    expect(within(advisories).getByText(/no active BOM/)).toBeInTheDocument();
    expect(screen.getByText(/not blocking/i)).toBeInTheDocument();
    // A part with only advisories is clean, and must read as clean.
    expect(screen.queryByTestId('part-backflush-blockers')).not.toBeInTheDocument();
    expect(screen.getByText(/resolves cleanly/i)).toBeInTheDocument();
  });

  it('says out loud that it checks the BOM only', async () => {
    // Fact the card cannot answer: routing conditions need a work order. Stating
    // it is what stops a clean verdict here being read as clean everywhere.
    renderCard(makePart());
    expect(await screen.findByText(/checks the BOM only/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /backflush preview/i })).toHaveAttribute('href', '/work-orders');
  });

  it('renders a retryable error state when readiness cannot be read', async () => {
    mockApi.getPartBackflushReadiness
      .mockRejectedValueOnce(axiosError(500, 'Internal Server Error'))
      .mockResolvedValueOnce(makeReadiness());
    renderCard(makePart());

    expect(await screen.findByText('Internal Server Error')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(mockApi.getPartBackflushReadiness).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/resolves cleanly/i)).toBeInTheDocument();
  });
});

describe('PartBackflushCard: the flip is server-gated and non-optimistic', () => {
  it('does not flip the chip on a 409, and shows the refusal verbatim and in full', async () => {
    // THE test. `setPartBackflush` is a server-GATED write whose entire purpose
    // is that the server may refuse, so per the repo's optimistic-UI convention
    // it must never paint the new state and roll back: a chip reading "On" for
    // even a moment claims material will move.
    const refusal =
      'Part PN-7001 cannot enable automatic backflush: BOM line 10 for RAW-304 has quantity 0, which would be ' +
      'treated as 1 per unit. State the real quantity';
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({ eligible: false, blockers: [makeDiagnostic()] })
    );
    mockApi.setPartBackflush.mockRejectedValue(axiosError(409, refusal));
    const { onPartUpdated } = renderCard(makePart());

    await screen.findByTestId('part-backflush-blockers');
    await userEvent.click(screen.getByRole('button', { name: /turn on automatic backflush/i }));
    await userEvent.click(await screen.findByRole('button', { name: 'Turn it on' }));

    const error = await screen.findByTestId('part-backflush-error');
    // In FULL: the sentence names the BOM line to go and fix, which is the whole
    // reason the server sends a plain string rather than a code.
    expect(error).toHaveTextContent(refusal);
    expect(error).toHaveAttribute('role', 'alert');
    expect(stateChip()).toHaveTextContent('Off');
    expect(onPartUpdated).not.toHaveBeenCalled();
    // A failed call must never raise a success toast.
    expect(screen.queryByText(/now backflushes/i)).not.toBeInTheDocument();
  });

  it('offers the confirm button even when readiness already reports blockers', async () => {
    // Deliberately NOT disabled. `eligible` is a snapshot, not authorisation —
    // the BOM is mutable by other people between this read and the write, so the
    // authoritative answer is the one the server gives, and a dead button would
    // say nothing about why. The dialog surfaces the blockers instead.
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({ eligible: false, blockers: [makeDiagnostic()] })
    );
    renderCard(makePart());

    await screen.findByTestId('part-backflush-blockers');
    await userEvent.click(screen.getByRole('button', { name: /turn on automatic backflush/i }));

    const confirm = await screen.findByRole('button', { name: 'Turn it on' });
    expect(confirm).toBeEnabled();
    expect(screen.getByText(/The server will refuse this while these stand/i)).toBeInTheDocument();
  });

  it('adopts the part the SERVER returns on success and re-reads readiness', async () => {
    // Parts carry no working optimistic lock (the model maps no version column),
    // so a locally toggled copy could silently disagree with the row. The card
    // therefore hands its parent whatever the server sent back.
    const updated = makePart({ backflush_components: true });
    mockApi.setPartBackflush.mockResolvedValue(updated);
    const { onPartUpdated } = renderCard(makePart({ version: 0 }));

    await screen.findByText(/resolves cleanly/i);
    await userEvent.click(screen.getByRole('button', { name: /turn on automatic backflush/i }));
    await userEvent.click(await screen.findByRole('button', { name: 'Turn it on' }));

    await waitFor(() => expect(onPartUpdated).toHaveBeenCalledWith(updated));
    expect(mockApi.setPartBackflush).toHaveBeenCalledWith(7, 0, true);
    expect(await screen.findByText(/now backflushes its BOM components automatically/i)).toBeInTheDocument();
    // Re-read so the card and the server cannot disagree about state.
    await waitFor(() => expect(mockApi.getPartBackflushReadiness).toHaveBeenCalledTimes(2));
  });

  it('warns before enabling that consumption is automatic and never reverses itself', async () => {
    // The confirmation is the consent. If it does not say what changes, the flip
    // is a switch nobody knowingly threw.
    renderCard(makePart());
    await screen.findByText(/resolves cleanly/i);
    await userEvent.click(screen.getByRole('button', { name: /turn on automatic backflush/i }));

    expect(await screen.findByText(/take this part’s BOM\s+components out of stock by itself/i)).toBeInTheDocument();
    expect(screen.getByText(/consumption never reverses on its own/i)).toBeInTheDocument();
    expect(screen.getByText(/does not reach back and consume for work orders that have/i)).toBeInTheDocument();
  });

  it('offers disable on an enabled part and states that posted material stays posted', async () => {
    const updated = makePart({ backflush_components: false });
    mockApi.setPartBackflush.mockResolvedValue(updated);
    const { onPartUpdated } = renderCard(makePart({ backflush_components: true }));

    await screen.findByText(/resolves cleanly/i);
    await userEvent.click(screen.getByRole('button', { name: /turn off automatic backflush/i }));
    expect(await screen.findByText(/Material\s+already consumed stays consumed/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Turn it off' }));
    await waitFor(() => expect(mockApi.setPartBackflush).toHaveBeenCalledWith(7, 0, false));
    expect(onPartUpdated).toHaveBeenCalledWith(updated);
  });

  it('keeps offering the toggle on an already-enabled part whose BOM has since broken', async () => {
    // The part somebody most urgently needs to switch OFF is the one whose BOM
    // is now broken. Blockers on an enabled part are not blocking anything
    // today, and the card must say so rather than reading as a dead end.
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({ backflush_components: true, eligible: false, blockers: [makeDiagnostic()] })
    );
    renderCard(makePart({ backflush_components: true }));

    await screen.findByTestId('part-backflush-blockers');
    expect(screen.getByText(/problem with this part’s BOM/i)).toBeInTheDocument();
    expect(screen.getByText(/Turning it off is always allowed/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /turn off automatic backflush/i })).toBeEnabled();
  });
});

describe('PartBackflushCard: permission gate', () => {
  it('hides the toggle without parts:edit but still shows the state and the diagnosis', async () => {
    mockApi.getPartBackflushReadiness.mockResolvedValue(
      makeReadiness({ eligible: false, blockers: [makeDiagnostic()] })
    );
    renderCard(makePart(), false);

    await screen.findByTestId('part-backflush-blockers');
    expect(stateChip()).toHaveTextContent('Off');
    expect(screen.queryByRole('button', { name: /turn on automatic backflush/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /turn off automatic backflush/i })).not.toBeInTheDocument();
  });
});

describe('showsBackflushCard: where this card is meaningful at all', () => {
  // A purchased part, raw material, hardware item or consumable will never carry
  // a BOM, so readiness always answers `no_demand_source` — and an unconditional
  // card would stand permanently red, in a compliance-looking panel, on a page
  // where the feature does not apply. That is how people learn to ignore red
  // panels on part detail, which costs the real ones their meaning.
  it.each(['manufactured', 'assembly'] as const)('renders for a %s part', (part_type) => {
    expect(showsBackflushCard({ part_type, backflush_components: false })).toBe(true);
  });

  it.each(['purchased', 'raw_material', 'hardware', 'consumable'] as const)(
    'is hidden for a %s part, which can never have a BOM',
    (part_type) => {
      expect(showsBackflushCard({ part_type, backflush_components: false })).toBe(false);
    }
  );

  it('still renders on ANY part that already has the flag on', () => {
    // `seed_data.py`'s `Part(**data)` splat is the one path that bypasses the
    // refusal gate. Hiding the card on such a part would strand it ON with no way
    // to turn it off — and turning it off is the one direction that is never gated.
    expect(showsBackflushCard({ part_type: 'purchased', backflush_components: true })).toBe(true);
  });
});
