/**
 * Backflush Preview panel — the dry run on a work order's detail page.
 *
 * What this suite guards is the panel's one job: telling the truth about what a
 * completion would take off the shelf, including which HEAT. That lot number
 * lands on the as-built genealogy record, so a panel showing a different lot
 * than the engine will draw is worse than no panel at all.
 *
 *  - it fetches NOTHING on mount. WorkOrderDetail renders it on every work
 *    order, most of which will never backflush anything, and six existing suites
 *    render that page with hand-written api mocks — a request on the render path
 *    would be both a wasted read and a suite-wide breakage;
 *  - the lots a line would draw render in DRAW ORDER, with their quantities;
 *  - a SUPPRESSED line is normal, not an error, and the reasons are not
 *    interchangeable: `converged` is the healthy steady state while
 *    `already_issued` is a permanent legacy fence, so only the latter is
 *    coloured for alarm;
 *  - `requires_opt_in` rows are labelled as a forecast rather than a
 *    commitment when the part has not opted in — and TIE rows are not, because
 *    a tie is its own opt-in;
 *  - a shortage and skipped HELD stock are distinguishable (a purchasing signal
 *    versus an MRB one);
 *  - basis 0 is explained as the engine's real answer rather than left looking
 *    like an empty table;
 *  - diagnostics render from `detail`, never a prettified `code`.
 */

import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import BackflushPreviewPanel from './BackflushPreviewPanel';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type { BackflushPreviewLine, BackflushPreviewLot, BackflushPreviewResponse } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrderBackflushPreview: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

/**
 * One preview lot. `is_shortfall` defaults false: the writer posts the covered
 * takes and, only when stock cannot cover the draw, one extra row against the
 * lot it drives negative. A fixture that omitted the flag entirely would let the
 * panel stop distinguishing them without a test noticing.
 */
const makeLot = (overrides: Partial<BackflushPreviewLot> = {}): BackflushPreviewLot => ({
  inventory_item_id: 91,
  lot_number: 'HEAT-OLDEST',
  location: 'RAW-A',
  quantity: 10,
  unit_cost: 2,
  is_shortfall: false,
  ...overrides,
});

const makeLine = (overrides: Partial<BackflushPreviewLine> = {}): BackflushPreviewLine => ({
  component_part_id: 55,
  component_part_number: 'SHT-.125-304',
  component_part_name: '.125 304 sheet',
  unit_of_measure: 'sheets',
  source: 'bom_routing',
  requires_opt_in: true,
  allocation_id: null,
  required_quantity: 25,
  already_issued: 0,
  delta_quantity: 25,
  suppressed: false,
  suppression_reason: null,
  available_quantity: 30,
  shortfall: 0,
  would_go_negative: false,
  held_quantity_skipped: 0,
  held_lot_numbers: [],
  pinned_inventory_item_id: null,
  pinned_lot_number: null,
  pinned_lot_is_held: false,
  shortfall_creates_placeholder: false,
  lots: [
    makeLot(),
    makeLot({ inventory_item_id: 92, lot_number: 'HEAT-MIDDLE' }),
    makeLot({ inventory_item_id: 93, lot_number: 'HEAT-NEWEST', location: 'RAW-B', quantity: 5 }),
  ],
  ...overrides,
});

const makePreview = (overrides: Partial<BackflushPreviewResponse> = {}): BackflushPreviewResponse => ({
  work_order_id: 42,
  work_order_number: 'WO-0042',
  part_id: 7,
  part_number: 'PN-7001',
  backflush_components: true,
  basis: 5,
  lines: [makeLine()],
  blockers: [],
  advisories: [],
  ...overrides,
});

const axiosError = (status: number, detail: unknown) =>
  Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });

const renderPanel = () =>
  render(
    <ToastProvider>
      <BackflushPreviewPanel workOrderId={42} />
    </ToastProvider>
  );

const rowFor = (partNumber: string) => screen.getByText(partNumber).closest('tr') as HTMLElement;

const runDryRun = async () => {
  await userEvent.click(screen.getByRole('button', { name: /run dry run/i }));
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getWorkOrderBackflushPreview.mockResolvedValue(makePreview());
});

describe('BackflushPreviewPanel: it costs nothing until asked', () => {
  it('fetches nothing on mount', () => {
    // Load-bearing twice over: it is one extra read against the BOM, the routing
    // and the stock ledger on a page most users open for other reasons, AND a
    // request here would break every WorkOrderDetail suite whose hand-written
    // api mock does not know this method (there is no shared mock factory).
    renderPanel();
    expect(mockApi.getWorkOrderBackflushPreview).not.toHaveBeenCalled();
    expect(screen.getByText(/Not loaded/i)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('loads on demand and can be re-run', async () => {
    renderPanel();
    await runDryRun();

    await waitFor(() => expect(mockApi.getWorkOrderBackflushPreview).toHaveBeenCalledWith(42));
    await userEvent.click(await screen.findByRole('button', { name: /refresh/i }));
    await waitFor(() => expect(mockApi.getWorkOrderBackflushPreview).toHaveBeenCalledTimes(2));
  });

  it('renders a retryable error state and never a half-populated table', async () => {
    mockApi.getWorkOrderBackflushPreview
      .mockRejectedValueOnce(axiosError(404, 'Work order not found'))
      .mockResolvedValueOnce(makePreview());
    renderPanel();
    await runDryRun();

    expect(await screen.findByText('Work order not found')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(await screen.findByText('SHT-.125-304')).toBeInTheDocument();
  });
});

describe('BackflushPreviewPanel: the lots are the point', () => {
  it('lists every lot the draw would walk, in order, with its quantity', async () => {
    // The one failure this panel exists to prevent: promising a heat the engine
    // will never touch. The server plans these with the writer's own FIFO
    // predicate, so the panel's job is to show them unaltered and in sequence.
    renderPanel();
    await runDryRun();

    const row = within(rowFor('SHT-.125-304'));
    const lots = await row.findAllByText(/HEAT-/);
    expect(lots.map((el) => el.textContent)).toEqual(['HEAT-OLDEST10', 'HEAT-MIDDLE10', 'HEAT-NEWEST5']);
  });

  it('shows target, already posted and what would post now as three distinct figures', async () => {
    // Deliberately a partly-posted line, so the three columns hold three
    // DIFFERENT numbers. `delta = target - already posted` is what actually
    // moves; a panel that showed only the target would over-state a job that has
    // already had material posted against it, which is precisely the state
    // reconcile-to-target produces on every replay.
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            required_quantity: 25,
            already_issued: 5,
            delta_quantity: 20,
            lots: [makeLot({ quantity: 20 })],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const row = within(rowFor('SHT-.125-304'));
    expect(await row.findByText('25 sheets')).toBeInTheDocument();
    expect(row.getByText('5 sheets')).toBeInTheDocument();
    expect(row.getByText('20 sheets')).toBeInTheDocument();
    expect(screen.getByTestId('backflush-preview-flag')).toHaveTextContent('On');
    expect(screen.getByText('PN-7001')).toBeInTheDocument();
  });

  it('marks a pinned tie line and never treats it as needing opt-in', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            source: 'work_order_tie',
            requires_opt_in: false,
            allocation_id: 3,
            pinned_inventory_item_id: 91,
            pinned_lot_number: 'HEAT-OLDEST',
            required_quantity: 5,
            delta_quantity: 5,
            lots: [makeLot({ quantity: 5 })],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const row = within(rowFor('SHT-.125-304'));
    expect(await row.findByText('Tie')).toBeInTheDocument();
    expect(row.getByText('pinned')).toBeInTheDocument();
    // A tie IS its own opt-in — it consumes whether or not the part opted in, so
    // labelling it "needs opt-in" would be a flat falsehood about what moves.
    expect(row.queryByText('needs opt-in')).not.toBeInTheDocument();
  });

  it('renders the empty state when a completion would move nothing', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(makePreview({ lines: [] }));
    renderPanel();
    await runDryRun();

    expect(await screen.findByText('Nothing would be consumed')).toBeInTheDocument();
  });
});

describe('BackflushPreviewPanel: suppression is normal, except when it is not', () => {
  it('glosses `converged` without alarm and shows no lots for it', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            suppressed: true,
            suppression_reason: 'converged',
            already_issued: 25,
            delta_quantity: 0,
            lots: [],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const flag = await screen.findByTestId('backflush-suppressed-55');
    expect(flag).toHaveTextContent('Already covered');
    // The leg reconciles to target and never auto-reverses, so a line whose
    // ledger already holds the whole target is the healthy steady state.
    expect(flag.className).not.toMatch(/fd-amber/);
    expect(within(rowFor('SHT-.125-304')).queryByText(/HEAT-/)).not.toBeInTheDocument();
  });

  it('flags `already_issued` — the permanent legacy fence — for attention', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [makeLine({ suppressed: true, suppression_reason: 'already_issued', delta_quantity: 0, lots: [] })],
      })
    );
    renderPanel();
    await runDryRun();

    const flag = await screen.findByTestId('backflush-suppressed-55');
    expect(flag).toHaveTextContent('Fenced out (legacy)');
    // Nothing further will EVER post for this component on this job — that is
    // not a steady state, it is a dead end somebody has to know about.
    expect(flag.className).toMatch(/fd-amber/);
  });

  it('names an open operation tie as the owner rather than reading as a failure', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({ suppressed: true, suppression_reason: 'open_operation_tie', delta_quantity: 0, lots: [] }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const flag = await screen.findByTestId('backflush-suppressed-55');
    expect(flag).toHaveTextContent('Owned by a tie');
    expect(flag.className).not.toMatch(/fd-amber/);
  });
});

describe('BackflushPreviewPanel: shortage, held stock and opt-in state', () => {
  it('distinguishes a shortage from segregated stock that was skipped', async () => {
    // Different remedies: "short 15" sends someone to Purchasing, "60 held"
    // sends them to MRB. Collapsing the two into one warning would send half of
    // them to the wrong place.
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            available_quantity: 10,
            shortfall: 15,
            would_go_negative: true,
            held_quantity_skipped: 60,
            held_lot_numbers: ['HEAT-HELD'],
            lots: [makeLot(), makeLot({ quantity: 15, is_shortfall: true })],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const row = within(rowFor('SHT-.125-304'));
    expect(await screen.findByTestId('backflush-short-55')).toHaveTextContent('short 15');
    expect(row.getByText('60 held')).toBeInTheDocument();

    // The unmet 15 is a LOT ROW, not merely a scalar. The completion posts a
    // SECOND issue for it against the last lot it drew, driving that lot
    // negative and writing THAT lot number onto the as-built genealogy — so the
    // panel has to show a named heat contributing 25, not 10. Listing only the
    // covered takes is how this panel would come to under-state a heat.
    const shortfallChip = row.getByTitle(/more than this part has/i);
    expect(shortfallChip).toHaveTextContent('HEAT-OLDEST');
    expect(shortfallChip).toHaveTextContent('15');
    expect(shortfallChip).toHaveTextContent(/short/i);
  });

  it('flags a pinned lot that has since been put on hold', async () => {
    // The one thing the writer's own shortage disclosure structurally cannot say.
    // A pin is a lot-directed instruction, so the draw is NOT short and
    // `held_quantity_skipped` stays zero — the completion consumes the
    // quarantined heat anyway and records HELD_MATERIAL_CONSUMED. Without a
    // dedicated flag this row renders as a clean pinned line over material about
    // to go into product, which is the single most consequential thing a
    // pre-completion dry run could be silent about.
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            source: 'work_order_tie',
            requires_opt_in: false,
            pinned_inventory_item_id: 91,
            pinned_lot_number: 'HEAT-Q',
            pinned_lot_is_held: true,
            held_quantity_skipped: 0,
            would_go_negative: false,
            lots: [makeLot({ lot_number: 'HEAT-Q', quantity: 25 })],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const flag = await screen.findByTestId('backflush-pinned-held-55');
    expect(flag).toHaveTextContent(/pinned lot held/i);
    expect(flag).toHaveAttribute('title', expect.stringContaining('HEAT-Q'));
    expect(flag).toHaveAttribute('title', expect.stringContaining('consumes it ANYWAY'));
    // It must not be confused with the OTHER held-stock condition, which has a
    // different remedy: that one is stock the engine SKIPS, this one it draws.
    expect(within(rowFor('SHT-.125-304')).queryByTitle(/segregated/i)).not.toBeInTheDocument();
  });

  it('says a draw against no stock at all mints a placeholder row, and names no heat', async () => {
    // `lots` is empty because there is nothing to name — the completion creates a
    // lot-less `InventoryItem` at the finished-goods location and posts the whole
    // draw against it. "No eligible lot" would be the wrong sentence: it implies
    // stock exists that policy excluded, which sends the reader to MRB instead of
    // to receiving.
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        lines: [
          makeLine({
            available_quantity: 0,
            shortfall: 25,
            would_go_negative: true,
            shortfall_creates_placeholder: true,
            lots: [],
          }),
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const row = within(rowFor('SHT-.125-304'));
    expect(await screen.findByTestId('backflush-short-55')).toHaveTextContent('short 25');
    const placeholder = row.getByText(/no stock — placeholder row/i);
    expect(placeholder).toHaveAttribute('title', expect.stringContaining('names no heat'));
    expect(row.queryByText(/no eligible lot/i)).not.toBeInTheDocument();
  });

  it('says the BOM rows are a forecast while the part has not opted in', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(makePreview({ backflush_components: false }));
    renderPanel();
    await runDryRun();

    expect(await screen.findByTestId('backflush-preview-flag')).toHaveTextContent('Off');
    expect(screen.getByText(/a forecast, not a commitment/i)).toBeInTheDocument();
    expect(within(rowFor('SHT-.125-304')).getByText('needs opt-in')).toBeInTheDocument();
  });

  it('explains a zero basis instead of showing a bare empty table', async () => {
    // `basis` is quantity_complete + operation scrap. Nothing produced means the
    // resolver genuinely returns no demand — the engine's real answer, not a gap
    // in the preview, and the difference matters to whoever reads it.
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(makePreview({ basis: 0, lines: [] }));
    renderPanel();
    await runDryRun();

    expect(await screen.findByText(/Nothing has been produced on this work order yet/i)).toBeInTheDocument();
    expect(screen.getByText(/That is the engine’s real answer/i)).toBeInTheDocument();
  });

  it('renders resolver diagnostics from their `detail` sentence', async () => {
    mockApi.getWorkOrderBackflushPreview.mockResolvedValue(
      makePreview({
        blockers: [
          {
            code: 'operation_names_own_part',
            severity: 'blocking',
            detail: 'operation 20 names the work order’s own part as a component. Clear that operation’s component',
            bom_item_id: null,
            component_part_id: 7,
            component_part_number: 'PN-7001',
            operation_id: 88,
          },
        ],
        advisories: [
          {
            code: 'zero_quantity_ordered',
            severity: 'advisory',
            detail: 'Work order WO-0042 has an ordered quantity of 0, so routing component demand is a job total.',
            bom_item_id: null,
            component_part_id: null,
            component_part_number: null,
            operation_id: null,
          },
        ],
      })
    );
    renderPanel();
    await runDryRun();

    const blockers = within(await screen.findByTestId('backflush-preview-blockers'));
    expect(blockers.getByText(/names the work order’s own part/)).toBeInTheDocument();
    expect(blockers.getByText(/operation_names_own_part/)).toBeInTheDocument();

    const advisories = within(screen.getByTestId('backflush-preview-advisories'));
    expect(advisories.getByText(/ordered quantity of 0/)).toBeInTheDocument();
  });

  it('states that running it writes nothing', async () => {
    // The panel re-fetches freely and sits on a page anyone can open. Saying so
    // is what makes that safe to believe rather than merely true.
    renderPanel();
    await runDryRun();
    expect(await screen.findByText(/no stock movement, no ledger row, no audit entry/i)).toBeInTheDocument();
  });
});
