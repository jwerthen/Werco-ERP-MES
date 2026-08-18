/**
 * Per-operation material tie editor — the create path that did not exist.
 *
 * One assertion here matters more than all the rest, and it is the reason this
 * file exists: **every tie created through this dialog is OPERATION-scoped.**
 *
 * The two scopes are not a preference. An operation-scoped tie consumes on the
 * per-run engine when ITS operation completes, reconciling to
 * `qty_per_run × (complete + scrapped)`; a work-order-scoped tie drains once, at
 * the end of the job, against `qty_planned`. They post under DIFFERENT ledger
 * reference shapes precisely so they can never double-issue the same material.
 * A dialog opened from a single operation row that silently omitted
 * `work_order_operation_id` would move the material at the wrong moment, in the
 * wrong quantity, against the wrong record — and fan the tie out across every
 * card of the work order on the dispatch board. So the payload is asserted for
 * every create path this component has.
 *
 * The rest guards the properties this feature has already had to defend
 * elsewhere:
 *  - it fetches NOTHING while closed (six WorkOrderDetail suites render the page
 *    that mounts it, on hand-written api mocks);
 *  - the write is server-GATED, therefore NON-OPTIMISTIC: refusals render
 *    verbatim, and an object `detail` never reaches the DOM as "[object Object]";
 *  - the deduction-timing sentence is `DEDUCTION_TIMING_NOTE` used VERBATIM —
 *    the module that owns it names the two ways this copy goes wrong, and having
 *    exactly one string to change is the whole point;
 *  - `qty_planned` defaults to `per-run × runs` but a planner's typed value wins;
 *  - the part of an existing tie is fixed, because changing what a tie points at
 *    would rewrite genealogy.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import OperationMaterialTieModal from './OperationMaterialTieModal';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import { DEDUCTION_TIMING_NOTE } from '../../utils/materialTie';
import type { MaterialAllocation, Part, WorkOrderOperation } from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getMaterials: jest.fn(),
    getMaterialAllocations: jest.fn(),
    createMaterialAllocation: jest.fn(),
    updateMaterialAllocation: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const OPERATION: WorkOrderOperation = {
  id: 71,
  sequence: 10,
  operation_number: '10',
  name: 'Laser',
  status: 'in_progress',
} as WorkOrderOperation;

const makeMaterial = (overrides: Partial<Part> = {}): Part =>
  ({
    id: 55,
    part_number: 'SHT-.125-304',
    name: '.125 304 sheet',
    part_type: 'raw_material',
    unit_of_measure: 'sheets',
    standard_cost: 90,
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

const makeTie = (overrides: Partial<MaterialAllocation> = {}): MaterialAllocation => ({
  id: 3,
  work_order_id: 42,
  work_order_operation_id: 71,
  operation_number: '10',
  detached_from_operation_id: null,
  part_id: 55,
  part_number: 'SHT-.125-304',
  part_name: '.125 304 sheet',
  source: 'manual',
  status: 'open',
  qty_per_run: 1,
  qty_planned: 3,
  unit_of_measure: 'sheets',
  qty_consumed: 0,
  pinned_inventory_item_id: null,
  pinned_lot_number: null,
  notes: null,
  created_by: 1,
  created_at: '2026-07-27T12:00:00Z',
  updated_at: '2026-07-27T12:00:00Z',
  ...overrides,
});

const axiosError = (status: number, detail: unknown) =>
  Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });

const renderModal = (
  { open = true, operationTarget = 4, operation = OPERATION as WorkOrderOperation | null } = {}
) => {
  const onClose = jest.fn();
  const onSaved = jest.fn();
  const utils = render(
    <ToastProvider>
      <OperationMaterialTieModal
        open={open}
        workOrderId={42}
        operation={operation}
        operationTarget={operationTarget}
        onClose={onClose}
        onSaved={onSaved}
      />
    </ToastProvider>
  );
  return { ...utils, onClose, onSaved };
};

const pickMaterial = async (value = '55') => {
  // The <select> renders immediately and is populated by an async load, so wait
  // for the OPTION rather than the control — otherwise the click lands on an
  // empty picker and the failure looks like a form bug rather than a race.
  const select = await screen.findByLabelText(/Material this operation consumes/i);
  await waitFor(() => expect(within(select).getByRole('option', { name: /SHT-/ })).toBeInTheDocument());
  await userEvent.selectOptions(select, value);
  return select;
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getMaterials.mockResolvedValue([makeMaterial()]);
  mockApi.getMaterialAllocations.mockResolvedValue([]);
  mockApi.createMaterialAllocation.mockResolvedValue(makeTie());
  mockApi.updateMaterialAllocation.mockResolvedValue(makeTie());
});

describe('OperationMaterialTieModal: operation scope is hard-coded', () => {
  it('sends work_order_operation_id on the create payload', async () => {
    // THE assertion. There is no control to remove the operation and no branch
    // that omits it — the two scopes consume at different moments, in different
    // quantities, under different ledger reference shapes.
    renderModal();
    await pickMaterial();
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalled());
    const [workOrderId, payload] = mockApi.createMaterialAllocation.mock.calls[0];
    expect(workOrderId).toBe(42);
    expect(payload.work_order_operation_id).toBe(71);
    expect(payload.part_id).toBe(55);
    expect(payload.source).toBe('manual');
  });

  it('never omits the operation, whatever the planner types', async () => {
    // Belt and braces on the same property: drive the form through every field
    // it exposes and re-assert. A future "scope" control added here would have
    // to break this test to land.
    renderModal({ operationTarget: 6 });
    await pickMaterial();
    await userEvent.clear(screen.getByLabelText(/Per completed run/i));
    await userEvent.type(screen.getByLabelText(/Per completed run/i), '2.5');
    await userEvent.clear(screen.getByLabelText(/Planned total/i));
    await userEvent.type(screen.getByLabelText(/Planned total/i), '15');
    await userEvent.type(screen.getByLabelText(/Notes/i), 'bar stock');
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalled());
    expect(mockApi.createMaterialAllocation.mock.calls[0][1]).toEqual({
      part_id: 55,
      work_order_operation_id: 71,
      source: 'manual',
      qty_per_run: 2.5,
      qty_planned: 15,
      notes: 'bar stock',
    });
  });
});

describe('OperationMaterialTieModal: it costs nothing while closed', () => {
  it('fetches nothing when closed', () => {
    // WorkOrderDetail mounts this once per page, always. A request on the closed
    // render path would be a wasted read on every work order AND would break the
    // six existing suites that render that page on hand-written api mocks.
    renderModal({ open: false });
    expect(mockApi.getMaterials).not.toHaveBeenCalled();
    expect(mockApi.getMaterialAllocations).not.toHaveBeenCalled();
  });

  it('fetches nothing when no operation is targeted', () => {
    renderModal({ operation: null });
    expect(mockApi.getMaterials).not.toHaveBeenCalled();
    expect(mockApi.getMaterialAllocations).not.toHaveBeenCalled();
  });

  it('loads the material list and this operation’s open ties when opened', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie(),
      // Another operation's tie, and a cancelled one: neither belongs in a
      // dialog scoped to THIS operation, and a cancelled row is a tombstone the
      // ledger resolves against, not something to edit.
      makeTie({ id: 4, work_order_operation_id: 99, part_number: 'OTHER-OP' }),
      makeTie({ id: 5, status: 'cancelled', part_number: 'CANCELLED-TIE' }),
    ]);
    renderModal();

    await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());
    expect(mockApi.getMaterialAllocations).toHaveBeenCalledWith(42, false);
    const existing = await screen.findByText(/Already tied to this operation/i);
    const list = existing.parentElement as HTMLElement;
    expect(within(list).getByText('SHT-.125-304')).toBeInTheDocument();
    expect(within(list).queryByText('OTHER-OP')).not.toBeInTheDocument();
    expect(within(list).queryByText('CANCELLED-TIE')).not.toBeInTheDocument();
  });

  it('keeps the form usable when the material list cannot be read', async () => {
    // Advisory, not fatal: the server remains the authority on any write, so a
    // failed picker read must not hide the form behind an error page.
    mockApi.getMaterials.mockRejectedValue(axiosError(500, 'Materials unavailable'));
    renderModal();

    expect(await screen.findByText(/Materials unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /tie material/i })).toBeEnabled();
  });
});

/**
 * The material list defaults to RAW STOCK — and this is the one picker where
 * the escape hatch is part of the flow.
 *
 * `/materials` serves all four material-supply types, three of which are bought
 * COMPONENTS (the seeded catalog types bolts and nuts as `purchased`), so the
 * default view narrows to raw stock. But a weld op really does eat wire and gas
 * and an assembly op really does eat rivets — those are typed `consumable` /
 * `hardware`, and tying them here is exactly what this dialog was built for. So
 * the toggle must always be reachable, never conditional on the raw-stock list
 * being empty.
 *
 * The one rule with no escape hatch is the exclusion of parts the shop
 * PRODUCES: a work order's own output is not an input to it, and consumption
 * never auto-reverses.
 */
describe('OperationMaterialTieModal: raw stock by default, everything else one click away', () => {
  const RAW_SHEET = makeMaterial();
  const WELD_WIRE = makeMaterial({
    id: 56,
    part_number: 'CON-MIG-035',
    name: 'MIG wire, 33 lb coil',
    part_type: 'consumable',
    unit_of_measure: 'lb',
  });
  const RIVET = makeMaterial({
    id: 57,
    part_number: 'HW-RIV-4',
    name: 'Blind rivet 1/8',
    part_type: 'hardware',
    unit_of_measure: 'ea',
  });
  const MANUFACTURED_BRACKET = makeMaterial({
    id: 58,
    part_number: 'BRK-100',
    name: 'Bracket, sheet metal',
    part_type: 'manufactured',
  });

  /** Every option in the material <select>, minus the "Select material…" row. */
  const listedOptions = (): string[] =>
    Array.from((screen.getByLabelText(/Material this operation consumes/i) as HTMLSelectElement).options)
      .filter((option) => option.value !== '')
      .map((option) => option.textContent ?? '');

  const toggle = () => screen.getByRole('button', { name: /^Show (all materials \(\d+ more\)|raw stock only)$/ });

  const openWithCatalog = async (parts = [RAW_SHEET, WELD_WIRE, RIVET, MANUFACTURED_BRACKET]) => {
    mockApi.getMaterials.mockResolvedValue(parts);
    const rendered = renderModal();
    await screen.findByLabelText(/Material this operation consumes/i);
    await waitFor(() => expect(mockApi.getMaterials).toHaveBeenCalled());
    return rendered;
  };

  it('lists only raw stock, and says so in the help text', async () => {
    await openWithCatalog();

    await waitFor(() => expect(listedOptions()).toEqual(['SHT-.125-304 — .125 304 sheet']));
    expect(screen.getByText(/Raw stock only by default/i)).toBeInTheDocument();
    // Wire + rivets are hidden, not excluded; the bracket is excluded, so it is
    // not part of the promise the count makes.
    expect(toggle()).toHaveTextContent('Show all materials (2 more)');
  });

  it('reveals the consumables and hardware the toggle promises', async () => {
    await openWithCatalog();
    await waitFor(() => expect(listedOptions()).toHaveLength(1));

    fireEvent.click(toggle());

    expect(listedOptions()).toEqual([
      'SHT-.125-304 — .125 304 sheet',
      'CON-MIG-035 — MIG wire, 33 lb coil',
      'HW-RIV-4 — Blind rivet 1/8',
    ]);
    expect(screen.getByText(/hardware and consumables included/i)).toBeInTheDocument();
    expect(toggle()).toHaveTextContent('Show raw stock only');
  });

  it('offers the toggle even when the raw-stock list is not empty', async () => {
    // Stated as its own assertion because the tempting "only show the escape
    // hatch when the default view is empty" shortcut would break the weld and
    // assembly flows this dialog exists to serve.
    await openWithCatalog();

    await waitFor(() => expect(listedOptions()).toHaveLength(1));
    expect(toggle()).toBeEnabled();
  });

  it('never offers a part the shop PRODUCES, at either tier', async () => {
    await openWithCatalog();
    await waitFor(() => expect(listedOptions()).toHaveLength(1));

    expect(listedOptions()).not.toContain('BRK-100 — Bracket, sheet metal');
    fireEvent.click(toggle());
    expect(listedOptions()).not.toContain('BRK-100 — Bracket, sheet metal');
  });

  it('keeps a consumable picked through the escape hatch when the filter narrows again', async () => {
    // A <select> whose value is missing from its options renders blank, so
    // without the pin this sequence silently discards the pick — and the form
    // would then refuse to submit for "no material" the planner did choose.
    await openWithCatalog();
    await waitFor(() => expect(listedOptions()).toHaveLength(1));

    fireEvent.click(toggle());
    await userEvent.selectOptions(screen.getByLabelText(/Material this operation consumes/i), '56');
    fireEvent.click(toggle());

    expect(screen.getByLabelText(/Material this operation consumes/i)).toHaveValue('56');
    expect(listedOptions()).toContain('CON-MIG-035 — MIG wire, 33 lb coil');
    // Pinned, therefore already on screen, therefore not still "hidden".
    expect(toggle()).toHaveTextContent('Show all materials (1 more)');
    // And the pinned option still carries its own UoM into the quantity label.
    expect(screen.getByLabelText(/Per completed run \(lb\)/i)).toBeInTheDocument();
  });

  it('ties the consumable the planner picked through the hatch', async () => {
    // The end-to-end point of the escape hatch: a weld op really does consume
    // wire, and the tie has to reach the API as an operation-scoped allocation.
    await openWithCatalog();
    await waitFor(() => expect(listedOptions()).toHaveLength(1));

    fireEvent.click(toggle());
    await userEvent.selectOptions(screen.getByLabelText(/Material this operation consumes/i), '56');
    await userEvent.type(screen.getByLabelText(/Per completed run/i), '2');
    await userEvent.click(screen.getByRole('button', { name: /^Tie material$/i }));

    await waitFor(() => expect(mockApi.createMaterialAllocation).toHaveBeenCalledTimes(1));
    expect(mockApi.createMaterialAllocation).toHaveBeenCalledWith(
      42,
      expect.objectContaining({ part_id: 56, work_order_operation_id: 71 })
    );
  });

  it('shows the tie controls when the catalog holds no raw stock at all', async () => {
    // Nothing in the default view, everything behind the toggle — the field and
    // the toggle both have to survive that, or the dialog is unusable for a shop
    // whose stock is typed `purchased`.
    await openWithCatalog([WELD_WIRE, RIVET]);

    await waitFor(() => expect(toggle()).toHaveTextContent('Show all materials (2 more)'));
    expect(listedOptions()).toEqual([]);

    fireEvent.click(toggle());
    expect(listedOptions()).toHaveLength(2);
  });

  it('leaves the EDIT branch alone — the part is fixed, so there is nothing to filter', async () => {
    // Editing shows the part as read-only text (changing what a tie points at
    // would rewrite genealogy), so the picker and its toggle are absent by
    // design rather than by omission.
    mockApi.getMaterials.mockResolvedValue([RAW_SHEET, WELD_WIRE]);
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ part_id: 56, part_number: 'CON-MIG-035', part_name: 'MIG wire, 33 lb coil' }),
    ]);
    renderModal();

    await userEvent.click(await screen.findByRole('button', { name: /^Edit$/ }));

    expect(screen.queryByLabelText(/Material this operation consumes/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show all materials/ })).not.toBeInTheDocument();
    // The tie's own part is still named in full, filter or no filter.
    expect(screen.getByLabelText('Material')).toHaveValue('CON-MIG-035 — MIG wire, 33 lb coil');
  });
});

describe('OperationMaterialTieModal: the planned-total default', () => {
  it('defaults planned total to per-run × runs, and a typed value wins', async () => {
    // The same derivation the nest modal uses, off the same `runs` figure the
    // Operations table shows in its Qty column — so a planner is not shown one
    // number and told another.
    renderModal({ operationTarget: 6 });
    await pickMaterial();

    const planned = screen.getByLabelText(/Planned total/i) as HTMLInputElement;
    expect(planned.value).toBe('6'); // blank per-run means 1 per run

    await userEvent.clear(screen.getByLabelText(/Per completed run/i));
    await userEvent.type(screen.getByLabelText(/Per completed run/i), '3');
    expect((screen.getByLabelText(/Planned total/i) as HTMLInputElement).value).toBe('18');

    await userEvent.clear(planned);
    await userEvent.type(planned, '20');
    // Once touched, the derivation stops overwriting the planner.
    await userEvent.clear(screen.getByLabelText(/Per completed run/i));
    await userEvent.type(screen.getByLabelText(/Per completed run/i), '5');
    expect((screen.getByLabelText(/Planned total/i) as HTMLInputElement).value).toBe('20');
  });

  it('refuses to submit without a material or with a non-positive plan', async () => {
    renderModal({ operationTarget: 0 });

    await userEvent.click(await screen.findByRole('button', { name: /tie material/i }));
    expect(await screen.findByTestId('op-tie-error')).toHaveTextContent(
      /Pick the material this operation consumes/i
    );
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();

    await pickMaterial();
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));
    expect(await screen.findByTestId('op-tie-error')).toHaveTextContent(/Planned total must be greater than zero/i);
    expect(mockApi.createMaterialAllocation).not.toHaveBeenCalled();
  });
});

describe('OperationMaterialTieModal: server-gated, therefore non-optimistic', () => {
  it('renders a 409 refusal verbatim and does not close or report a save', async () => {
    const refusal =
      'SHT-.125-304 is already tied to operation 10 on this work order (allocation 3). Edit that tie instead.';
    mockApi.createMaterialAllocation.mockRejectedValue(axiosError(409, refusal));
    const { onClose, onSaved } = renderModal();

    await pickMaterial();
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));

    // The refusal NAMES the allocation to edit instead — useless unless read.
    expect(await screen.findByTestId('op-tie-error')).toHaveTextContent(refusal);
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('never puts an object detail into the DOM as [object Object]', async () => {
    // A structured 409 body passes through the Axios interceptor untouched, so
    // the component has to render it through `toDisplayString`.
    mockApi.createMaterialAllocation.mockRejectedValue(
      axiosError(409, { code: 'duplicate_tie', message: 'Already tied on operation 10', allocation_id: 3 })
    );
    renderModal();

    await pickMaterial();
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));

    const error = await screen.findByTestId('op-tie-error');
    expect(error.textContent).not.toContain('[object Object]');
    expect(error).toHaveTextContent(/Already tied on operation 10/);
  });

  it('reports a successful create so the caller can refresh the tie list', async () => {
    // A tie write does not bump `work_orders.updated_at`, which is the Material
    // Ties panel's other freshness seam — so without this callback the list
    // sitting directly beneath the Operations table would go stale.
    const { onClose, onSaved } = renderModal();

    await pickMaterial();
    await userEvent.click(screen.getByRole('button', { name: /tie material/i }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/Tied SHT-.125-304 to Op 10/i)).toBeInTheDocument();
  });
});

describe('OperationMaterialTieModal: editing an existing tie', () => {
  it('fixes the part and sends only what changed', async () => {
    // Changing what a tie POINTS AT would rewrite genealogy: the ledger rows
    // already carry `allocation_id`, so re-pointing the row would re-attribute
    // material that has already moved. Untie and re-tie instead.
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie({ qty_per_run: 1, qty_planned: 3, notes: null })]);
    const { onSaved } = renderModal();

    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const fixed = screen.getByLabelText(/^Material$/i) as HTMLInputElement;
    expect(fixed).toHaveAttribute('readOnly');
    expect(fixed.value).toContain('SHT-.125-304');

    await userEvent.clear(screen.getByLabelText(/Planned total/i));
    await userEvent.type(screen.getByLabelText(/Planned total/i), '8');
    await userEvent.click(screen.getByRole('button', { name: /save tie/i }));

    await waitFor(() => expect(mockApi.updateMaterialAllocation).toHaveBeenCalled());
    expect(mockApi.updateMaterialAllocation.mock.calls[0].slice(0, 2)).toEqual([42, 3]);
    // ONLY the changed field — a PATCH carrying unchanged values would fight
    // anyone editing the same tie from the Material Ties panel.
    expect(mockApi.updateMaterialAllocation.mock.calls[0][2]).toEqual({ qty_planned: 8 });
    expect(onSaved).toHaveBeenCalled();
  });

  it('refuses a no-op edit rather than posting an empty patch', async () => {
    mockApi.getMaterialAllocations.mockResolvedValue([makeTie()]);
    renderModal();

    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    await userEvent.click(screen.getByRole('button', { name: /save tie/i }));

    expect(await screen.findByTestId('op-tie-error')).toHaveTextContent(/Nothing changed/i);
    expect(mockApi.updateMaterialAllocation).not.toHaveBeenCalled();
  });

  it('offers to clear a lot pin, with a real label association', async () => {
    // The checkbox uses FormField's node form so `htmlFor` points at the input
    // itself rather than a dangling generated id — `getByLabelText` finding it
    // IS the assertion (jsx-a11y is fully enforced and CI lints at zero
    // warnings, so a dangling association would be a build failure, not a nit).
    mockApi.getMaterialAllocations.mockResolvedValue([
      makeTie({ pinned_inventory_item_id: 91, pinned_lot_number: 'HEAT-7741' }),
    ]);
    renderModal();

    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const checkbox = screen.getByLabelText(/Clear the lot pin \(HEAT-7741\)/i);
    expect(checkbox).toHaveAttribute('type', 'checkbox');

    await userEvent.click(checkbox);
    await userEvent.click(screen.getByRole('button', { name: /save tie/i }));

    await waitFor(() => expect(mockApi.updateMaterialAllocation).toHaveBeenCalled());
    expect(mockApi.updateMaterialAllocation.mock.calls[0][2]).toEqual({ clear_pinned_inventory_item: true });
  });
});

describe('OperationMaterialTieModal: copy', () => {
  it('uses DEDUCTION_TIMING_NOTE verbatim, never a re-worded variant', async () => {
    // `utils/materialTie.ts` owns this sentence and its docstring names the two
    // ways it goes wrong: "when WO-#### finishes" UNDERSTATES (an
    // operation-scoped tie deducts at its own operation), and "deducting now"
    // OVERSTATES outside a completion screen. One string to change if the
    // trigger ever moves again — which is the entire point of importing it.
    renderModal();
    const escaped = DEDUCTION_TIMING_NOTE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const notice = await screen.findByText(new RegExp(escaped));
    expect(notice).toBeInTheDocument();

    // And the banner around it commits to the scope in the same breath, so the
    // timing sentence is never read without the scope that explains it.
    expect(notice.textContent).toMatch(/scoped to/i);
    expect(notice.textContent).toMatch(/deduct at this operation’s completion/i);
  });
});

/**
 * Dialog title — the "Op Op 10" bug class.
 *
 * `WorkOrderOperation.operation_number` is free text; on WO-20260807-006 it is
 * literally "Op 10", and this dialog's title hard-coded its own `Op ` prefix.
 * It now routes through the shared `utils/operationLabel` helper. The `Seq {n}`
 * fallback is preserved on purpose — `sequence` is a real integer that names the
 * operation better than an em-dash would in a dialog title.
 */
describe('OperationMaterialTieModal operation label', () => {
  it('titles the dialog "Op 10" for a stored "Op 10" — never "Op Op 10"', async () => {
    renderModal({ operation: { ...OPERATION, operation_number: 'Op 10' } as WorkOrderOperation });

    const heading = await screen.findByRole('heading', { name: /Tie material to this operation/i });
    expect(heading.textContent).toContain('Op 10 · Laser');
    expect(heading.textContent).not.toMatch(/Op\s+Op/i);
  });

  it('falls back to "Seq {n}", not the em-dash, when the number is blank', async () => {
    // `undefined`, not `null`: WorkOrderOperation declares `operation_number?: string`,
    // so absent is the shape the client contract actually produces. (The helper is
    // null-safe regardless — see utils/operationLabel.test coverage.)
    renderModal({
      operation: { ...OPERATION, operation_number: undefined, sequence: 10 } as WorkOrderOperation,
    });

    const heading = await screen.findByRole('heading', { name: /Tie material to this operation/i });
    expect(heading.textContent).toContain('Seq 10 · Laser');
    expect(heading.textContent).not.toContain('Op —');
  });
});
