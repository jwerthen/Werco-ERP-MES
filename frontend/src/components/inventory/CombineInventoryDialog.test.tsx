/**
 * Combine dialog — the properties that make it safe to point at real stock.
 *
 * This screen moves on-hand between two part numbers and writes 2N ledger rows.
 * The tests below pin the behaviours that are CORRECTNESS rather than styling,
 * each for a reason that only shows up against a live shop:
 *
 *  1. **NON-OPTIMISTIC.** Nothing is folded locally before the response. While
 *     the post is in flight the dialog stays up in a pending state; on a 409 it
 *     stays OPEN with the server's verbatim `detail` and every typed value
 *     intact; the caller only ever receives the SERVER's result object.
 *  2. **Confirm is dead while a refusal stands**, and dead while any flagged
 *     part is un-acknowledged — the acknowledgement is the entire control the
 *     owner asked for, so a submit that could skip it is the defect.
 *  3. **The quantity is the server's number.** It pre-fills from
 *     `default_quantity`, capped at `max_combinable_quantity` (offering a
 *     number the server would then refuse is worse than offering none), and
 *     asking for more than the open ties allow blocks the submit.
 *  4. **The compare-and-swap travels.** `expected_source_part_number` /
 *     `expected_target_part_number` come from the PREVIEW, not the picker list —
 *     a `Part` maps no version column, so those strings are the only
 *     concurrency control there is.
 *  5. **A partial result is a `warning`, never a `success`.** A success toast
 *     would hide the shortfall; an error toast would claim a failure that did
 *     not happen and send someone hunting a combine that exists.
 *
 * TWO TESTS HERE WERE `it.failing`, AND THEY ARE NOW THE REGRESSION GUARDS for
 * the blocker this whole screen shipped with. THE BUG WAS: `submitBlockedReason`
 * read `if (blockers.length > 0) return '…cleared first'` before it looked at a
 * single thing the operator had done, and the preview is read exactly ONCE, with
 * no quantity and no acknowledgements (`build_combine_preview` passes
 * `acknowledged_ids=()` and `quantity=None → default_quantity` deliberately — the
 * dialog needs the blocker in order to know it must render the checkbox). So
 * `flagged_part_not_acknowledged` sat in `blockers` for the life of the dialog and
 * ticking the box left Combine dead — shipping the owner's acknowledgement GATE as
 * the BAN the owner explicitly rejected — and `open_work_order_reservation` fires
 * on exactly the same condition as `max_combinable_quantity < default_quantity`,
 * so whenever the cap bit at all, pressing the dialog's own "Use 60" button cleared
 * the amber notice and left the button dead for good.
 *
 * The fix is `CLIENT_SATISFIABLE_BLOCKER_CODES` — a three-code ALLOWLIST, subtracted
 * from the server's list to give `standingBlockers`. Those two tests are now plain
 * `it`s and must stay green; the tests below them pin the allowlist's edges, above
 * all that `target_row_not_available` can NEVER be satisfied here. Do not "fix" any
 * of them by weakening the fixtures: a preview that carries `flagged_parts` without
 * `flagged_part_not_acknowledged`, or `max_combinable_quantity < default_quantity`
 * without `open_work_order_reservation`, is a response the server cannot produce.
 *
 * 6. **A refusal the client cannot re-evaluate stays HARD.** The allowlist is an
 *    allowlist, not a denylist: an unrecognised code — including one a newer
 *    server invents — keeps the button dead rather than being waved through by a
 *    stale build.
 *
 * The toast hook is mocked rather than driven through a real `<ToastProvider>`
 * because the VARIANT is the assertion here: rendering a provider proves the
 * text appeared but cannot tell `warning` from `success` without reading a
 * colour class, which is exactly the kind of styling coupling that makes a
 * correctness test brittle.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import CombineInventoryDialog, { CombinePartOption } from './CombineInventoryDialog';
import api from '../../services/api';
import { selectComboBoxOption } from '../../test-utils/comboBox';
import type {
  CombinePartStockSummary,
  CombineStockLine,
  InventoryCombinePreview,
  InventoryCombineResult,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewInventoryCombine: jest.fn(),
    combineInventory: jest.fn(),
  },
}));

const mockShowToast = jest.fn();
jest.mock('../ui/Toast', () => ({
  ...jest.requireActual<typeof import('../ui/Toast')>('../ui/Toast'),
  useToast: () => ({ showToast: mockShowToast }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// The owner's actual case: a numbering recut left 92 sheets under the old
// number and 141 under the new one. 233 sheets exist; 233 must still exist.
const SOURCE_NUMBER = '.0625-60X144-304SS';
const TARGET_NUMBER = 'SH-A240-304-0.0625-60X144-2B';

const parts: CombinePartOption[] = [
  { id: 41, part_number: SOURCE_NUMBER, name: '16GA 304 SS SHEET 60X144', unit_of_measure: 'sheets' },
  { id: 42, part_number: TARGET_NUMBER, name: 'SHEET A240 304 2B 0.0625 60X144', unit_of_measure: 'sheets' },
];

function line(over: Partial<CombineStockLine> & { inventory_item_id: number }): CombineStockLine {
  return {
    location: 'RECV-01',
    warehouse: 'MAIN',
    lot_number: 'RCV-20260813-005',
    serial_number: null,
    quantity_on_hand: 92,
    quantity_allocated: 0,
    quantity_available: 92,
    unit_cost: 210.5,
    status: 'available',
    eligible: true,
    ineligible_reason: null,
    ...over,
  };
}

function sourceSummary(over: Partial<CombinePartStockSummary> = {}): CombinePartStockSummary {
  return {
    part_id: 41,
    part_number: SOURCE_NUMBER,
    name: '16GA 304 SS SHEET 60X144',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    is_active: true,
    status: 'active',
    is_deleted: false,
    total_on_hand: 92,
    total_allocated: 0,
    total_available: 92,
    eligible_available: 92,
    lines: [line({ inventory_item_id: 501 })],
    ...over,
  };
}

function targetSummary(over: Partial<CombinePartStockSummary> = {}): CombinePartStockSummary {
  return {
    part_id: 42,
    part_number: TARGET_NUMBER,
    name: 'SHEET A240 304 2B 0.0625 60X144',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    is_active: true,
    status: 'active',
    is_deleted: false,
    total_on_hand: 141,
    total_allocated: 0,
    total_available: 141,
    eligible_available: 141,
    lines: [
      line({
        inventory_item_id: 601,
        location: 'RM-A2',
        lot_number: 'HT-88231',
        quantity_on_hand: 141,
        quantity_available: 141,
        unit_cost: 205,
      }),
    ],
    ...over,
  };
}

function preview(over: Partial<InventoryCombinePreview> = {}): InventoryCombinePreview {
  return {
    source: sourceSummary(),
    target: targetSummary(),
    unit_of_measure_match: true,
    default_quantity: 92,
    max_combinable_quantity: 92,
    eligible: true,
    blockers: [],
    advisories: [],
    flagged_parts: [],
    open_source_reservations: [],
    reserved_quantity: 0,
    cost: {
      source_weighted_unit_cost: 210.5,
      target_weighted_unit_cost: 205,
      differs: true,
      note: 'A newly created target row inherits the source unit cost; an existing target row keeps its own.',
    },
    ...over,
  };
}

/**
 * A preview of a pair where one part carries a flagged token.
 *
 * SERVER-REALISTIC, and the shape matters: `build_combine_preview` runs the
 * blocker probes with `acknowledged_ids=()`, so a non-empty `flagged_parts`
 * ALWAYS arrives with `flagged_part_not_acknowledged` sitting in `blockers`.
 * A fixture that carried the flagged parts without the blocker would be a mock
 * contradicting the endpoint it stands in for.
 */
function flaggedPreview(): InventoryCombinePreview {
  return preview({
    eligible: false,
    flagged_parts: [
      { part_id: 42, part_number: TARGET_NUMBER, matched_token: 'housing', field: 'name' },
      { part_id: 42, part_number: TARGET_NUMBER, matched_token: 'housing', field: 'part_number' },
    ],
    blockers: [
      {
        code: 'flagged_part_not_acknowledged',
        detail:
          `These parts look like test or housing parts and need an explicit confirmation ` +
          `before they can be combined: '${TARGET_NUMBER}' (matched 'housing').`,
      },
    ],
  });
}

/**
 * A preview where open work-order ties cap the fold below the whole pile.
 *
 * Also server-realistic, and for a reason worth stating: `max_combinable_quantity`
 * is `min(eligible_available, total_on_hand - reserved)`, and the reservation
 * blocker fires when `total_on_hand - default_quantity < reserved`. Those two
 * conditions are algebraically the same one — so `max_combinable_quantity <
 * default_quantity` NEVER happens without `open_work_order_reservation` also
 * being in `blockers`. Any fixture that separates them describes a response the
 * server cannot produce.
 */
function reservedPreview(): InventoryCombinePreview {
  return preview({
    eligible: false,
    default_quantity: 92,
    max_combinable_quantity: 60,
    reserved_quantity: 32,
    open_source_reservations: [
      {
        work_order_id: 3311,
        work_order_number: 'WO-20260813-011',
        work_order_status: 'in_progress',
        outstanding_quantity: 32,
      },
    ],
    blockers: [
      {
        code: 'open_work_order_reservation',
        detail:
          `Open work orders still expect 32 of '${SOURCE_NUMBER}' and this combine would leave 0. ` +
          'Untie or re-tie them first: WO-20260813-011 (32).',
      },
    ],
  });
}

const HELD_TARGET_DETAIL =
  `'${TARGET_NUMBER}' already has stock at RECV-01 lot RCV-20260813-005 that is not available ` +
  `(material is on hold, not available, status 'on_hold'). 92 of usable material would be folded onto it ` +
  'and become unusable too. Release that hold, or move this material to a different location or lot first.';

/**
 * A preview whose TARGET already holds a row at the exact (location, lot) the
 * fold would land on — and that row is ON HOLD.
 *
 * THE BUG THIS FIXTURE STANDS FOR, and the reason `target_row_not_available` is
 * rendered louder than every other refusal: `_find_stock_row` resolved a landing
 * row by (company, part, location, lot) and NOTHING else — no `is_active`, no
 * `status`. Measured end-to-end, 92 AVAILABLE sheets folded onto a held target row
 * returned **200** with an empty `blockers` list and left a row at 102 still
 * `on_hold`. The totals still added up; 92 usable sheets had simply stopped being
 * drawable, behind a green success toast. That is the failure an operator is least
 * able to detect afterwards, which is why this code renders first, with heavier
 * chrome, and is NEVER client-satisfiable.
 *
 * `eligible_available: 0` on the target is server-realistic and load-bearing for
 * the panel test below: the row is the target's only stock and it is not usable.
 */
function heldTargetPreview(over: Partial<InventoryCombinePreview> = {}): InventoryCombinePreview {
  return preview({
    eligible: false,
    target: targetSummary({
      eligible_available: 0,
      lines: [
        line({
          inventory_item_id: 601,
          quantity_on_hand: 141,
          quantity_available: 141,
          unit_cost: 205,
          status: 'on_hold',
          eligible: false,
          ineligible_reason: 'material is on hold, not available',
        }),
      ],
    }),
    blockers: [{ code: 'target_row_not_available', detail: HELD_TARGET_DETAIL }],
    ...over,
  });
}

function combineResult(over: Partial<InventoryCombineResult> = {}): InventoryCombineResult {
  return {
    combine_id: 9,
    combine_number: 'COMB-000009',
    source_part_id: 41,
    source_part_number: SOURCE_NUMBER,
    target_part_id: 42,
    target_part_number: TARGET_NUMBER,
    quantity_moved: 92,
    lines_moved: 1,
    source_quantity_before: 92,
    source_quantity_after: 0,
    target_quantity_before: 141,
    target_quantity_after: 233,
    source_deactivated: false,
    lines: [
      {
        location: 'RECV-01',
        lot_number: 'RCV-20260813-005',
        quantity: 92,
        unit_cost: 210.5,
        source_inventory_item_id: 501,
        target_inventory_item_id: 777,
        target_row_created: true,
      },
    ],
    transaction_ids: [1001, 1002],
    ...over,
  };
}

function renderDialog() {
  const onClose = jest.fn();
  const onCombined = jest.fn();
  render(<CombineInventoryDialog open parts={parts} onClose={onClose} onCombined={onCombined} />);
  return { onClose, onCombined };
}

/** Pick both items, which is what triggers the preview read. */
async function pickPair(): Promise<void> {
  selectComboBoxOption(screen.getByLabelText(/Move stock OUT of/i), /^\.0625-60X144-304SS/);
  selectComboBoxOption(screen.getByLabelText(/Move stock INTO/i), /^SH-A240-304/);
  await screen.findByTestId('combine-source-summary');
}

const REASON = 'Numbering recut — same 16ga 304 sheet under two numbers';

/**
 * Type a reason and walk the two-step confirm through to the actual POST.
 *
 * The final click is wrapped in `act` so the response's trailing state updates
 * (`setSaving(false)`, `setConfirmOpen(false)`) land inside it rather than after
 * the assertions — a request left in flight simply flushes nothing.
 */
async function confirmCombine(reason: string = REASON): Promise<void> {
  fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: reason } });
  await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());
  fireEvent.click(screen.getByTestId('combine-submit'));
  // The go/no-go is a <ConfirmDialog>; its button is "Combine", while the
  // dialog's own footer button is "Combine…".
  const go = await screen.findByRole('button', { name: 'Combine' });
  await act(async () => {
    fireEvent.click(go);
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.previewInventoryCombine.mockResolvedValue(preview());
  mockedApi.combineInventory.mockResolvedValue(combineResult());
});

describe('CombineInventoryDialog — what the preview puts on screen', () => {
  it('renders both sides of the fold: on-hand, lot, location, unit and cost', async () => {
    renderDialog();
    await pickPair();

    expect(mockedApi.previewInventoryCombine).toHaveBeenCalledWith({
      source_part_id: 41,
      target_part_id: 42,
    });

    const source = screen.getByTestId('combine-source-summary');
    const target = screen.getByTestId('combine-target-summary');

    // NOTE: <DataTable> renders the desktop table AND the mobile cards into the
    // DOM (CSS hides one per breakpoint; jsdom applies no breakpoints), so every
    // cell value legitimately appears more than once. All-variants throughout.
    expect(within(source).getAllByText(SOURCE_NUMBER).length).toBeGreaterThan(0);
    expect(within(source).getAllByText('92').length).toBeGreaterThan(0);
    expect(within(source).getAllByText('RECV-01').length).toBeGreaterThan(0);
    expect(within(source).getAllByText('RCV-20260813-005').length).toBeGreaterThan(0);
    expect(within(source).getByText(/Counted in sheets/i)).toBeInTheDocument();

    expect(within(target).getAllByText(TARGET_NUMBER).length).toBeGreaterThan(0);
    expect(within(target).getAllByText('141').length).toBeGreaterThan(0);
    expect(within(target).getAllByText('HT-88231').length).toBeGreaterThan(0);

    // Costs are disclosed, never reblended. Scoped to the cost line: the same
    // unit cost also renders in the lot table, so a bare text query is ambiguous.
    const costLine = screen.getByText(/these differ\./i);
    expect(costLine).toHaveTextContent('$210.50');
    expect(costLine).toHaveTextContent('$205.00');
    expect(screen.getByText(/inherits the source unit cost/i)).toBeInTheDocument();
  });

  it('says which lots are staying behind, and why, row by row', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(
      preview({
        source: sourceSummary({
          total_on_hand: 112,
          total_available: 112,
          eligible_available: 92,
          lines: [
            line({ inventory_item_id: 501 }),
            line({
              inventory_item_id: 502,
              location: 'QUAR-01',
              lot_number: 'RCV-20260814-002',
              quantity_on_hand: 20,
              quantity_available: 20,
              status: 'quarantine',
              eligible: false,
              ineligible_reason: 'Quarantined — not available to move',
            }),
          ],
        }),
      })
    );

    renderDialog();
    await pickPair();

    const source = screen.getByTestId('combine-source-summary');
    expect(within(source).getAllByText('Quarantined — not available to move').length).toBeGreaterThan(0);
    // The ceiling on the fold is the ELIGIBLE pile, not the on-hand total.
    expect(screen.getByTestId('combine-quantity')).toHaveValue(92);
  });

  it('lists the open jobs still tied to the source, with what they still need', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());

    renderDialog();
    await pickPair();

    expect(screen.getByText(/32 sheets reserved/i)).toBeInTheDocument();
    expect(screen.getAllByText('WO-20260813-011').length).toBeGreaterThan(0);
    expect(screen.getAllByText('32').length).toBeGreaterThan(0);
    expect(screen.getByText(/Re-tie them to SH-A240-304-0\.0625-60X144-2B/i)).toBeInTheDocument();
  });

  it('renders <ErrorState> with Retry when the preview read fails, and offers nothing to submit', async () => {
    mockedApi.previewInventoryCombine.mockRejectedValueOnce({
      response: { status: 404, data: { detail: 'Part 41 was not found in this company.' } },
    });

    renderDialog();
    selectComboBoxOption(screen.getByLabelText(/Move stock OUT of/i), /^\.0625-60X144-304SS/);
    selectComboBoxOption(screen.getByLabelText(/Move stock INTO/i), /^SH-A240-304/);

    expect(await screen.findByText('Part 41 was not found in this company.')).toBeInTheDocument();
    // No preview means no compare-and-swap value, so there is nothing to send.
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(screen.queryByTestId('combine-quantity')).not.toBeInTheDocument();

    mockedApi.previewInventoryCombine.mockResolvedValue(preview());
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    await screen.findByTestId('combine-source-summary');
    expect(mockedApi.previewInventoryCombine).toHaveBeenCalledTimes(2);
  });
});

describe('CombineInventoryDialog — the confirm gate', () => {
  it('disables confirm while a blocker stands, and shows the server detail verbatim', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(
      preview({
        eligible: false,
        blockers: [
          {
            code: 'no_available_stock',
            detail: `${SOURCE_NUMBER} has no available stock to combine.`,
          },
        ],
      })
    );

    renderDialog();
    await pickPair();

    expect(screen.getByText(`${SOURCE_NUMBER} has no available stock to combine.`)).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    await waitFor(() =>
      expect(screen.getByTestId('combine-blocked-reason')).toHaveTextContent(/refusals above have to be cleared/i)
    );
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(mockedApi.combineInventory).not.toHaveBeenCalled();
  });

  it('renders one acknowledgement tick per flagged PART, not one per match', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(flaggedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });

    // The same part matched twice (number AND name). The acknowledgement is per
    // part id, so two rows must NOT render two checkboxes — one of them would
    // look unticked forever.
    expect(screen.getAllByTestId(/^combine-ack-/)).toHaveLength(1);
    expect(screen.getByTestId('combine-ack-42')).not.toBeChecked();
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    // The server's own sentence, which names the token that matched.
    expect(screen.getByText(/need an explicit confirmation/i)).toBeInTheDocument();
  });

  /**
   * REGRESSION GUARD — the acknowledgement gate must stay a GATE, not a ban.
   *
   * WAS `it.failing`. THE BUG: `build_combine_preview` calls
   * `_combine_blockers(..., acknowledged_ids=())` ON PURPOSE (its own comment says
   * so: the dialog needs the blocker in order to know it must render the
   * checkbox), so `flagged_part_not_acknowledged` is in `blockers` for the WHOLE
   * life of the dialog — the preview is never re-read. `submitBlockedReason`
   * short-circuited on `blockers.length > 0` before it ever looked at
   * `unacknowledged`, so ticking the box cleared the message and left the button
   * dead, while the server would have accepted the request (the write path re-runs
   * `_combine_blockers` with the submitted `acknowledge_flagged_part_ids`).
   *
   * Net effect of the bug: a part whose number or name carries "test" or "housing"
   * could not be combined from this screen AT ALL — the exact case for which the
   * owner asked for an acknowledgement rather than a ban.
   *
   * If this ever goes red again, the fix is in `CLIENT_SATISFIABLE_BLOCKER_CODES` /
   * `standingBlockers`, not in this test.
   */
  it('acknowledging every flagged part revives the combine and sends the ids', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(flaggedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.click(screen.getByTestId('combine-ack-42'));

    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());

    fireEvent.click(screen.getByTestId('combine-submit'));
    const go = await screen.findByRole('button', { name: 'Combine' });
    await act(async () => {
      fireEvent.click(go);
    });
    expect(mockedApi.combineInventory.mock.calls[0][0].acknowledge_flagged_part_ids).toEqual([42]);
  });

  it('moves the satisfied refusal out of the red list instead of dropping it', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(flaggedPreview());

    renderDialog();
    await pickPair();

    // Before the tick: it is a standing refusal, in the red list.
    expect(within(screen.getByTestId('combine-blockers')).getByText(/need an explicit confirmation/i)).toBeInTheDocument();
    expect(screen.queryByTestId('combine-cleared-blockers')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('combine-ack-42'));

    // After: still on screen — the sentence is the only statement of what the
    // operator just agreed to — but quiet, and not beside a live Combine button
    // claiming the action is refused.
    await waitFor(() => expect(screen.getByTestId('combine-cleared-blockers')).toBeInTheDocument());
    expect(
      within(screen.getByTestId('combine-cleared-blockers')).getByText(/need an explicit confirmation/i)
    ).toBeInTheDocument();
    expect(screen.queryByTestId('combine-blockers')).not.toBeInTheDocument();
    expect(screen.getAllByText(/the server checks this again on submit/i).length).toBeGreaterThan(0);
  });

  it('unticking an acknowledgement puts the refusal straight back', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(flaggedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });

    fireEvent.click(screen.getByTestId('combine-ack-42'));
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());

    // The subtraction is derived state, never a latch: un-ticking has to re-arm
    // the refusal, or one stray click would leave the gate permanently open.
    fireEvent.click(screen.getByTestId('combine-ack-42'));
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeDisabled());
    expect(screen.getByTestId('combine-blockers')).toBeInTheDocument();
  });

  it('requires a reason long enough to mean something', async () => {
    renderDialog();
    await pickPair();

    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: '     ' } });
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(screen.getByTestId('combine-blocked-reason')).toHaveTextContent(/reason is required/i);

    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());
  });
});

describe('CombineInventoryDialog — the quantity is the server’s number', () => {
  it('pre-fills default_quantity when nothing is reserved', async () => {
    renderDialog();
    await pickPair();
    expect(screen.getByTestId('combine-quantity')).toHaveValue(92);
    expect(screen.getByLabelText(/How much to move \(sheets\)/i)).toHaveValue(92);
  });

  it('caps the pre-fill at max_combinable_quantity rather than offering a number the server would refuse', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());

    renderDialog();
    await pickPair();
    expect(screen.getByTestId('combine-quantity')).toHaveValue(60);
  });

  it('offers the safe number instead of a dead end when more than the ties allow is typed', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.change(screen.getByTestId('combine-quantity'), { target: { value: '92' } });

    expect(
      await screen.findByText(/Open jobs still need 32, so at most 60 can move without stranding one/i)
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('combine-use-max'));
    expect(screen.getByTestId('combine-quantity')).toHaveValue(60);
  });

  /**
   * REGRESSION GUARD — the reservation cap must be a CAP, not a dead end.
   *
   * WAS `it.failing`. Same mechanism as the flagged-part bug above: the dialog
   * previews WITHOUT a quantity, so the server evaluates the reservation probe
   * against `default_quantity` and returns `open_work_order_reservation` in
   * `blockers`; the dialog never re-previews on a quantity change (deliberately —
   * see its header note), and `submitBlockedReason` short-circuited on
   * `blockers.length > 0`. So lowering the quantity to `max_combinable_quantity` —
   * including by pressing the dialog's OWN "Use 60" button — cleared the amber
   * notice and left the button dead for good, even though `combine_inventory`
   * re-runs the probe against the submitted quantity and would accept 60.
   *
   * The two conditions are algebraically identical (see `reservedPreview`), so it
   * was not an edge case: whenever the cap bit at all, the screen was unusable.
   * `max_combinable_quantity`, the "Use max" button and the "Open jobs cap this at
   * N" copy exist precisely to rescue this case, and now do.
   */
  it('lowering to max_combinable_quantity revives the combine and posts that number', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());
    mockedApi.combineInventory.mockResolvedValue(
      combineResult({ quantity_moved: 60, source_quantity_after: 32, target_quantity_after: 201 })
    );

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    // The pre-fill is already the capped number, so this is the state an operator
    // lands in without touching anything.
    expect(screen.getByTestId('combine-quantity')).toHaveValue(60);

    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());

    // …and the offered number is not a second dead end: it actually posts.
    fireEvent.click(screen.getByTestId('combine-submit'));
    const go = await screen.findByRole('button', { name: 'Combine' });
    await act(async () => {
      fireEvent.click(go);
    });
    expect(mockedApi.combineInventory.mock.calls[0][0].quantity).toBe(60);
  });

  it('re-arms the refusal the moment the quantity climbs back over the cap', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());

    // 92 is within `eligible_available` but past `max_combinable_quantity`. The
    // subtraction only ever REMOVES a stale refusal; it must never latch one off.
    fireEvent.change(screen.getByTestId('combine-quantity'), { target: { value: '92' } });
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeDisabled());
    expect(screen.getByTestId('combine-blockers')).toBeInTheDocument();

    // The dialog's own "Use 60" affordance clears it again.
    fireEvent.click(screen.getByTestId('combine-use-max'));
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());
  });

  it('does not treat a quantity over eligible_available as clearing the reservation cap', async () => {
    /**
     * THE NEAR-MISS THIS PINS. `overReservationCap` is deliberately suppressed
     * while `overAvailable` stands (display ordering: one sentence beats two), so
     * a satisfaction check written as `!overReservationCap` would read TRUE for a
     * quantity blowing straight past `eligible_available`. The predicates are
     * stated positively (`withinAvailable` / `withinReservationCap`) precisely so
     * a display-only ordering can never leak into a gate.
     */
    mockedApi.previewInventoryCombine.mockResolvedValue(reservedPreview());

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.change(screen.getByTestId('combine-quantity'), { target: { value: '5000' } });

    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeDisabled());
    // Both refusals are still standing: nothing has been satisfied.
    expect(screen.getByTestId('combine-blockers')).toBeInTheDocument();
    expect(screen.queryByTestId('combine-cleared-blockers')).not.toBeInTheDocument();
    expect(mockedApi.combineInventory).not.toHaveBeenCalled();
  });

  it('refuses more than is eligible to move', async () => {
    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.change(screen.getByTestId('combine-quantity'), { target: { value: '100' } });

    await waitFor(() =>
      expect(screen.getByTestId('combine-blocked-reason')).toHaveTextContent('Only 92 is available to move.')
    );
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
  });

  it('will not let the source be deactivated while stock would remain under the old number', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(
      preview({
        source: sourceSummary({
          total_on_hand: 112,
          total_available: 112,
          eligible_available: 92,
          lines: [
            line({ inventory_item_id: 501 }),
            line({
              inventory_item_id: 502,
              location: 'HOLD-01',
              quantity_on_hand: 20,
              quantity_available: 20,
              status: 'on_hold',
              eligible: false,
              ineligible_reason: 'On hold — not available to move',
            }),
          ],
        }),
      })
    );

    renderDialog();
    await pickPair();

    // 92 eligible of 112 on hand: 20 would stay put, so the source cannot land
    // at zero and the server would 409 on `deactivate_source`.
    expect(screen.getByTestId('combine-deactivate-source')).toBeDisabled();
    expect(screen.getByText(/20 would still be on hand afterwards/i)).toBeInTheDocument();
  });
});

/**
 * The refusal that says the operator's stock is about to STOP BEING USABLE, and
 * the eligibility numbers that let them see it coming.
 *
 * Every other blocker refuses an action and leaves the shop exactly as it was.
 * This one describes a fold that would succeed mechanically and quietly convert
 * available stock into held stock — the totals still add up, so nothing on any
 * other screen would ever say what happened.
 */
describe('CombineInventoryDialog — an unusable target row', () => {
  it('states the eligible figure on the TARGET panel, not just the source', async () => {
    /**
     * THE BUG THIS PINS: the target panel showed only `total_available`
     * (= on hand − allocated), which applies NO eligibility reduction, so a target
     * whose one stock row sat on hold read "Available 141" on a screen whose entire
     * job is to disclose what the fold will do — while the fold was about to drop
     * 92 usable sheets onto that row.
     */
    mockedApi.previewInventoryCombine.mockResolvedValue(heldTargetPreview());

    renderDialog();
    await pickPair();

    // Stated on BOTH sides now. On the source it is the ceiling on the fold; on
    // the target it is what the shop can draw once the material lands there.
    expect(screen.getByTestId('combine-source-eligible')).toHaveTextContent('92');
    expect(screen.getByTestId('combine-target-eligible')).toHaveTextContent('0');

    // …and a number that is merely present is not a number anyone reads, so the
    // shortfall is spelled out beneath the table, in TARGET-specific words.
    const withheld = screen.getByTestId('combine-target-withheld');
    expect(withheld).toHaveTextContent('141');
    expect(withheld).toHaveTextContent(/material landing on one of those rows would stop being usable/i);
    // The source is clean here, so it gets no shortfall line at all.
    expect(screen.queryByTestId('combine-source-withheld')).not.toBeInTheDocument();
  });

  it('renders the refusal as a high alarm, with the server sentence and a next step', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(heldTargetPreview());

    renderDialog();
    await pickPair();

    const alarm = screen.getByTestId('combine-blocker-alarm-target_row_not_available');
    expect(alarm).toHaveTextContent(/stock would become unusable/i);
    // The server's own sentence, verbatim — this build never paraphrases a refusal.
    expect(alarm).toHaveTextContent(HELD_TARGET_DETAIL);
    // …plus the operator-facing next step this code earns.
    expect(alarm).toHaveTextContent(/release that row on the target item first/i);
    expect(alarm).toHaveTextContent('target_row_not_available');
  });

  it('sorts the alarm above an ordinary refusal, whatever order the server sent', async () => {
    mockedApi.previewInventoryCombine.mockResolvedValue(
      heldTargetPreview({
        blockers: [
          {
            code: 'unit_of_measure_mismatch',
            detail: "These parts are stocked in different units ('sheets' vs 'pounds').",
          },
          { code: 'target_row_not_available', detail: HELD_TARGET_DETAIL },
        ],
      })
    );

    renderDialog();
    await pickPair();

    // The server listed the UoM mismatch first; the alarm is lifted regardless.
    const rendered = within(screen.getByTestId('combine-blockers')).getAllByRole('alert');
    expect(rendered[0]).toHaveAttribute('data-testid', 'combine-blocker-alarm-target_row_not_available');
    expect(rendered).toHaveLength(2);
  });

  it('stays HARD even when every client-satisfiable refusal beside it is satisfied', async () => {
    /**
     * The allowlist's most important property. `target_row_not_available` is not in
     * `CLIENT_SATISFIABLE_BLOCKER_CODES` and must never be added: the client cannot
     * re-evaluate "is that target row still on hold" from the preview it already
     * holds, and guessing wrong converts usable stock into held stock.
     */
    mockedApi.previewInventoryCombine.mockResolvedValue(
      heldTargetPreview({
        flagged_parts: [{ part_id: 42, part_number: TARGET_NUMBER, matched_token: 'housing', field: 'name' }],
        blockers: [
          { code: 'target_row_not_available', detail: HELD_TARGET_DETAIL },
          {
            code: 'flagged_part_not_acknowledged',
            detail: `These parts need an explicit confirmation: '${TARGET_NUMBER}' (matched 'housing').`,
          },
        ],
      })
    );

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.click(screen.getByTestId('combine-ack-42'));
    fireEvent.change(screen.getByTestId('combine-quantity'), { target: { value: '1' } });

    // The flagged-part refusal moved to the quiet list; the alarm did not.
    await waitFor(() => expect(screen.getByTestId('combine-cleared-blockers')).toBeInTheDocument());
    expect(screen.getByTestId('combine-blocker-alarm-target_row_not_available')).toBeInTheDocument();
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(screen.getByTestId('combine-blocked-reason')).toHaveTextContent(/refusals above have to be cleared/i);
    expect(mockedApi.combineInventory).not.toHaveBeenCalled();
  });

  it('keeps a refusal this build has never heard of hard, and still shows what it said', async () => {
    /**
     * `CLIENT_SATISFIABLE_BLOCKER_CODES` is an ALLOWLIST, so an unknown code is
     * refused by construction rather than waved through by a stale client — the
     * failure mode a denylist would have on the day the server grows a new probe.
     * The `detail` is still rendered in full: a refusal this build cannot explain
     * is disclosed, never swallowed. Only the "what to do next" line goes missing,
     * which is the safe half to lose.
     */
    mockedApi.previewInventoryCombine.mockResolvedValue(
      preview({
        eligible: false,
        blockers: [
          {
            code: 'a_probe_this_build_has_never_seen',
            detail: 'The server refused this for a reason this build does not recognise.',
          },
        ],
      })
    );

    renderDialog();
    await pickPair();
    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });

    expect(
      screen.getByText('The server refused this for a reason this build does not recognise.')
    ).toBeInTheDocument();
    expect(screen.getByText('a_probe_this_build_has_never_seen')).toBeInTheDocument();
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(screen.queryByTestId('combine-cleared-blockers')).not.toBeInTheDocument();
  });
});

describe('CombineInventoryDialog — non-optimistic by contract', () => {
  it('sends the part numbers the PREVIEW read as the compare-and-swap precondition', async () => {
    renderDialog();
    await pickPair();
    await confirmCombine();

    await waitFor(() => expect(mockedApi.combineInventory).toHaveBeenCalledTimes(1));
    expect(mockedApi.combineInventory).toHaveBeenCalledWith({
      source_part_id: 41,
      target_part_id: 42,
      quantity: 92,
      reason: REASON,
      // From the preview, NOT from the picker list — a Part maps no version
      // column, so these strings are the entire concurrency control.
      expected_source_part_number: SOURCE_NUMBER,
      expected_target_part_number: TARGET_NUMBER,
      acknowledge_flagged_part_ids: [],
      deactivate_source: false,
    });
  });

  it('stays open in a pending state while the post is in flight, and hands over only when it lands', async () => {
    let settle: (result: InventoryCombineResult) => void = () => undefined;
    mockedApi.combineInventory.mockReturnValue(
      new Promise<InventoryCombineResult>((resolve) => {
        settle = resolve;
      })
    );

    const { onClose, onCombined } = renderDialog();
    await pickPair();
    await confirmCombine();

    // In flight: the dialog is still up, the button is pending and dead, and
    // NOTHING has been handed to the caller.
    await waitFor(() => expect(screen.getByTestId('combine-submit')).toHaveTextContent('Combining…'));
    expect(screen.getByTestId('combine-submit')).toBeDisabled();
    expect(screen.getByTestId('combine-source-summary')).toBeInTheDocument();
    expect(onCombined).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    settle(combineResult());
    await waitFor(() => expect(onCombined).toHaveBeenCalledTimes(1));
    // The SERVER's object, never a locally folded pair of rows.
    expect(onCombined).toHaveBeenCalledWith(
      expect.objectContaining({
        combine_number: 'COMB-000009',
        source_quantity_after: 0,
        target_quantity_after: 233,
      })
    );
    // Closing is the caller's job — this dialog never closes itself.
    expect(onClose).not.toHaveBeenCalled();
  });

  it('stays OPEN on a 409, shows the server detail verbatim and keeps the typed reason', async () => {
    mockedApi.combineInventory.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail:
            'Open work orders still need 32 sheets of .0625-60X144-304SS (WO-20260813-011). Re-tie them first.',
        },
      },
    });

    const { onClose, onCombined } = renderDialog();
    await pickPair();
    await confirmCombine();

    expect(
      await screen.findByText(
        'Open work orders still need 32 sheets of .0625-60X144-304SS (WO-20260813-011). Re-tie them first.'
      )
    ).toBeInTheDocument();
    expect(onCombined).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // A refusal that costs the operator their typing is a refusal they route around.
    expect(screen.getByTestId('combine-reason')).toHaveValue(REASON);
    expect(screen.getByTestId('combine-quantity')).toHaveValue(92);
    // The go/no-go closes so the refusal is READ, not clicked past.
    expect(screen.queryByRole('button', { name: 'Combine' })).not.toBeInTheDocument();
    expect(mockShowToast).not.toHaveBeenCalledWith('success', expect.anything());
  });
});

describe('CombineInventoryDialog — success vs partial result', () => {
  it('reports a clean fold as a success, naming the combine and both new totals', async () => {
    renderDialog();
    await pickPair();
    await confirmCombine();

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledTimes(1));
    const [variant, message] = mockShowToast.mock.calls[0];
    expect(variant).toBe('success');
    expect(message).toContain('COMB-000009');
    expect(message).toContain('Moved 92 sheets');
    expect(message).toContain(`${TARGET_NUMBER} now 233`);
  });

  it('raises the WARNING variant — not success — when fewer units moved than asked for', async () => {
    mockedApi.combineInventory.mockResolvedValue(
      combineResult({ quantity_moved: 80, source_quantity_after: 12, target_quantity_after: 221 })
    );

    renderDialog();
    await pickPair();
    await confirmCombine();

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledTimes(1));
    const [variant, message] = mockShowToast.mock.calls[0];
    // `success` would hide the shortfall; `error` would claim a failure that did
    // not happen and send someone hunting a combine that exists.
    expect(variant).toBe('warning');
    expect(message).toContain('12 could not move');
  });

  it('raises the WARNING variant when stock stayed behind under the old number', async () => {
    mockedApi.combineInventory.mockResolvedValue(
      combineResult({ source_quantity_after: 20, target_quantity_after: 233 })
    );

    renderDialog();
    await pickPair();
    await confirmCombine();

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledTimes(1));
    const [variant, message] = mockShowToast.mock.calls[0];
    expect(variant).toBe('warning');
    expect(message).toContain('20 stayed under the old number');
  });

  it('raises the WARNING variant when the source was asked to go inactive and did not', async () => {
    renderDialog();
    await pickPair();

    fireEvent.change(screen.getByTestId('combine-reason'), { target: { value: REASON } });
    fireEvent.click(screen.getByTestId('combine-deactivate-source'));
    expect(screen.getByTestId('combine-deactivate-source')).toBeChecked();

    await waitFor(() => expect(screen.getByTestId('combine-submit')).toBeEnabled());
    fireEvent.click(screen.getByTestId('combine-submit'));
    const go = await screen.findByRole('button', { name: 'Combine' });
    await act(async () => {
      fireEvent.click(go);
    });

    expect(mockedApi.combineInventory).toHaveBeenCalledTimes(1);
    expect(mockedApi.combineInventory.mock.calls[0][0].deactivate_source).toBe(true);

    await waitFor(() => expect(mockShowToast).toHaveBeenCalledTimes(1));
    const [variant, message] = mockShowToast.mock.calls[0];
    expect(variant).toBe('warning');
    expect(message).toContain(`${SOURCE_NUMBER} is still active`);
  });
});

