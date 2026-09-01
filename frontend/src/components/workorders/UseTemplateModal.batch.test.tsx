/**
 * UseTemplateModal — SEVERAL drafts from one template, each with its own Unit #.
 *
 * A weld assembly is built ONE UNIT PER WORK ORDER: each unit carries its own
 * Unit #, its own traveler, its own labor and its own quality record. "Five of
 * them" is therefore five work orders, not one with a quantity of five — those
 * are different plans, and only the first can be reported against per unit.
 *
 * What this file pins, and why each would regress silently:
 *
 * 1. **Raising the count re-prefills the quantity to 1 — but only while the
 *    planner has not typed in that field.** A template saved from a qty-8 job
 *    would otherwise turn "make 5" into 5 × 8 = 40 pieces in silence. BOTH halves
 *    are asserted: the auto-set, and the fact that it never overwrites a typed
 *    value. An auto-set that respects nothing is as wrong as none at all.
 *
 * 2. **The batch line states the outcome in PIECES before the click.** It is the
 *    check on the auto-set above: a field the form changed on its own is only
 *    acceptable if its consequence is on screen in the planner's own units.
 *
 * 3. **All three pre-submit gates fire and issue NO request.** They mirror the
 *    server's 422s so a mis-pasted spreadsheet column costs no round trip. The
 *    "no request" half is the load-bearing one — a gate that renders a message
 *    and submits anyway is worse than no gate, because the message reads as a
 *    refusal that did not happen. The third gate is the COUNT CAP, and it is the
 *    one the form does not otherwise stop: `max` on a `type="number"` input is
 *    enforced only by a native form submit, and this dialog submits from a click
 *    handler, so 25 can be typed and sent. It also has to run BEFORE the list
 *    gates — a correctly pasted 25-line column clears the length check and would
 *    otherwise earn the server's raw pydantic sentence about a request field.
 *
 * 4. **A single use puts EXACTLY the bytes on the wire it always has.** `count`
 *    and `unit_numbers` are omitted, not sent as 1 and `[]`. The body is
 *    `extra="forbid"` server-side, and the one-at-a-time path is what every
 *    deployed client already exercises.
 *
 * 5. **A clean batch does NOT navigate.** Five drafts have five numbers and no
 *    single destination, so the dialog stays up and renders the number → Unit #
 *    table the planner is about to write onto travelers. It exists nowhere else
 *    in the app.
 *
 * 6. **A partial batch says the omissions apply to EVERY draft.** The skip lists
 *    are the union across the copies — they ran one plan, so an omission belongs
 *    to the plan — and read without that line they describe only the one work
 *    order the report names.
 *
 * 7. **A nest-bearing template is one at a time, said on the field.** The server
 *    refuses `count > 1` there with a 409; nobody should have to earn that
 *    refusal to learn it.
 *
 * Mocks `services/api`; the wire shapes are pinned in
 * `services/api.workOrderTemplates.test.ts`. Read both together — an assertion
 * here is against the DECLARED return type, never against what the server sends.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import UseTemplateModal, { MAX_TEMPLATE_USE_COUNT } from './UseTemplateModal';
import api from '../../services/api';
import { ToastProvider } from '../ui';
import type {
  WorkOrder,
  WorkOrderDuplicateSkippedAllocation,
  WorkOrderTemplate,
  WorkOrderTemplatePlan,
  WorkOrderTemplateUseResult,
} from '../../types';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    useWorkOrderTemplate: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

// A real Miratech-shaped unit number. Digits only, and deliberately NOT part of a
// walking sequence — the values in every list below are mutually underivable, so a
// misalignment by one position cannot look like a correct result.
const UNIT = '2410048';

const makePlan = (overrides: Partial<WorkOrderTemplatePlan> = {}): WorkOrderTemplatePlan => ({
  available: true,
  unavailable_reason: null,
  source_work_order_number: 'WO-20260501-004',
  source_status: 'complete',
  work_order_type: 'production',
  sequential_operations: true,
  priority: 3,
  operation_count: 4,
  nest_count: 0,
  planned_runs_total: 0,
  open_material_tie_count: 1,
  work_centers: ['WELD-1'],
  source_quantity_ordered: 50,
  ...overrides,
});

const makeTemplate = (overrides: Partial<WorkOrderTemplate> = {}): WorkOrderTemplate => ({
  id: 7,
  name: 'Weld assembly set',
  notes: null,
  source_work_order_id: 42,
  // Eight, on purpose: the auto-set to 1 is only observable against a prefill that
  // is not 1, and 5 × 8 = 40 is the silent multiplication it exists to prevent.
  default_quantity: 8,
  created_at: '2026-08-20T12:00:00Z',
  updated_at: '2026-08-20T12:00:00Z',
  created_by: 3,
  plan: makePlan(),
  ...overrides,
});

const nestTemplate = makeTemplate({
  name: 'Miratech nest group',
  default_quantity: null,
  plan: makePlan({
    work_order_type: 'laser_cutting',
    sequential_operations: false,
    nest_count: 21,
    planned_runs_total: 63,
    operation_count: 21,
    source_quantity_ordered: 63,
    work_centers: ['LASER-1'],
  }),
});

const makeWorkOrder = (overrides: Partial<WorkOrder> = {}): WorkOrder => ({
  id: 501,
  version: 1,
  work_order_number: 'WO-20260901-001',
  part_id: 10,
  work_order_type: 'production',
  quantity_ordered: 1,
  quantity_complete: 0,
  quantity_scrapped: 0,
  status: 'draft',
  priority: 3,
  estimated_hours: 0,
  actual_hours: 0,
  created_at: '2026-09-01T12:00:00Z',
  updated_at: '2026-09-01T12:00:00Z',
  operations: [],
  ...overrides,
});

/**
 * A batch envelope, built the way the SERVER builds it: `work_orders[0]` IS
 * `work_order`, and `created_count` equals the list length. A fixture that let
 * those disagree would let a test pass against a shape the server never sends —
 * the exact failure mode `api.workOrderTemplates.test.ts` exists to catch.
 */
const makeBatchResult = (
  units: (string | null)[],
  overrides: Partial<WorkOrderTemplateUseResult> = {}
): WorkOrderTemplateUseResult => {
  const workOrders = units.map((unit, index) =>
    makeWorkOrder({
      id: 501 + index,
      work_order_number: `WO-20260901-${String(index + 1).padStart(3, '0')}`,
      unit_number: unit,
    })
  );
  return {
    work_order: workOrders[0],
    created_count: workOrders.length,
    work_orders: workOrders,
    skipped_operations: [],
    skipped_material_allocations: [],
    ...overrides,
  };
};

const skippedTie = (
  overrides: Partial<WorkOrderDuplicateSkippedAllocation> = {}
): WorkOrderDuplicateSkippedAllocation => ({
  source_allocation_id: 9,
  part_id: 55,
  source_work_order_operation_id: null,
  reason: 'part_not_available',
  ...overrides,
});

function renderModal({ template = makeTemplate() }: { template?: WorkOrderTemplate | null } = {}) {
  const onClose = jest.fn();
  const onUsed = jest.fn();
  const utils = render(
    <ToastProvider>
      <UseTemplateModal open template={template} onClose={onClose} onUsed={onUsed} />
    </ToastProvider>
  );
  return { ...utils, onClose, onUsed };
}

const countInput = () => screen.getByLabelText('Work orders to create') as HTMLInputElement;
const quantityInput = () => screen.getByLabelText(/^Quantity/i) as HTMLInputElement;
const unitsTextarea = () => screen.getByLabelText(/Unit numbers/i) as HTMLTextAreaElement;
const createButton = () => screen.getByRole('button', { name: /Creat/i });
const errorBox = () => screen.getByTestId('use-template-error');

/** Ask for `count` drafts. Typing, not `fireEvent`, so the touched-ref logic is real. */
async function setCount(count: number) {
  await userEvent.clear(countInput());
  await userEvent.type(countInput(), String(count));
}

/** Paste a column of unit numbers, which is what a planner actually does. */
function pasteUnits(lines: string[]) {
  fireEvent.change(unitsTextarea(), { target: { value: lines.join('\n') } });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.useWorkOrderTemplate.mockResolvedValue(makeBatchResult([UNIT]));
});

describe('UseTemplateModal: how many work orders', () => {
  it('offers a count field that starts at one, so the click-once case is unchanged', async () => {
    renderModal();

    expect(countInput()).toHaveValue(1);
    expect(countInput()).toBeEnabled();
    // The batch-only controls are absent until they mean something: refusing a
    // submit over a field nobody can see would be worse than dropping a paste.
    expect(screen.queryByTestId('use-template-batch-total')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Unit numbers/i)).not.toBeInTheDocument();
  });

  it('raises to five, rewrites an untouched quantity prefill to 1, and states the total', async () => {
    // Without the rewrite, a template saved from a qty-8 job turns "make 5" into
    // 40 pieces across five work orders, silently. The sentence is the check on
    // the rewrite: the form changed a number nobody asked it to change.
    renderModal();
    await waitFor(() => expect(quantityInput()).toHaveValue(8));

    await setCount(5);

    expect(quantityInput()).toHaveValue(1);
    expect(screen.getByTestId('use-template-batch-total')).toHaveTextContent(
      '5 draft work orders, quantity 1 each — 5 pieces in total, one work order number per unit.'
    );
    // Named for what it now is: the size of EACH work order, not a total to split.
    expect(screen.getByText('Quantity per work order')).toBeInTheDocument();
    expect(createButton()).toHaveTextContent('Create 5 draft work orders');
  });

  it('never overwrites a quantity the planner typed', async () => {
    // The other half of the rule. An auto-set that ignores what somebody chose is
    // as wrong as no auto-set: this planner means five work orders of four.
    renderModal();
    await waitFor(() => expect(quantityInput()).toBeEnabled());

    await userEvent.clear(quantityInput());
    await userEvent.type(quantityInput(), '4');
    await setCount(5);

    expect(quantityInput()).toHaveValue(4);
    expect(screen.getByTestId('use-template-batch-total')).toHaveTextContent(
      '5 draft work orders, quantity 4 each — 20 pieces in total, one work order number per unit.'
    );
  });

  it('blocks a count above the cap with its own message, and issues NO request', async () => {
    // `max={20}` on the input is not a gate here: it is enforced only by a native
    // form submit, and this dialog submits from a click handler. Left to the
    // server, 25 comes back as a raw pydantic sentence about a request field
    // ("Input should be less than or equal to 20") — true, but it names neither
    // the limit's reason nor the way out of it.
    renderModal();
    await setCount(25);

    await userEvent.click(createButton());

    expect(errorBox()).toHaveTextContent(
      'You asked for 25 work orders, and 20 is the most one click may create. Run the template again for the rest.'
    );
    expect(mockApi.useWorkOrderTemplate).not.toHaveBeenCalled();
  });

  it('sends the cap itself, so 20 is the limit rather than one below it', async () => {
    // Non-vacuity for the refusal above: the gate refuses ABOVE the cap, and the
    // boundary value is the one an off-by-one would take away from the planner.
    mockApi.useWorkOrderTemplate.mockResolvedValue(makeBatchResult(new Array(20).fill(null)));
    renderModal();
    await setCount(MAX_TEMPLATE_USE_COUNT);

    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
    const [, payload] = mockApi.useWorkOrderTemplate.mock.calls[0];
    expect(payload).toEqual({ due_date: null, quantity_ordered: 1, count: 20 });
  });

  it('names the CAP, not the list length, when both are wrong at once', async () => {
    // The ORDER of the gates, and the reason the count check runs first. With the
    // list gates ahead of it this reads "You listed 3 unit numbers for 25 work
    // orders" — which sends the planner off to paste 22 more lines and then
    // refuses them anyway, because 25 was never creatable. The cap is the fact
    // that has to reach them first; the list is downstream of a number that has
    // to change.
    renderModal();
    await setCount(25);
    pasteUnits([UNIT, 'K-9812', 'SN00042']);

    await userEvent.click(createButton());

    expect(errorBox()).toHaveTextContent(/20 is the most one click may create/);
    expect(errorBox()).not.toHaveTextContent(/You listed 3 unit numbers/);
    expect(mockApi.useWorkOrderTemplate).not.toHaveBeenCalled();
  });

  it('locks the count on a nest-bearing template and says why', async () => {
    // Same reason the quantity beside it is locked: a laser job's quantity IS the
    // sum of its nests' planned runs, so "five of it" is more runs on the nests,
    // not five work orders each claiming the same sheets. The server refuses 409.
    renderModal({ template: nestTemplate });

    expect(countInput()).toBeDisabled();
    expect(countInput()).toHaveValue(1);
    expect(screen.getByText(/One at a time\./)).toBeInTheDocument();
    expect(screen.getByText(/more runs on the nests, not more work orders/)).toBeInTheDocument();
    // And the batch controls never appear for it, so there is no way to ask.
    expect(screen.queryByTestId('use-template-batch-total')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Unit numbers/i)).not.toBeInTheDocument();
  });
});

describe('UseTemplateModal: the unit-number list', () => {
  it('counts how many of the required entries are filled', async () => {
    renderModal();
    await setCount(5);

    pasteUnits([UNIT, '', 'K-9812', '', 'SN00042']);

    expect(screen.getByTestId('use-template-unit-count')).toHaveTextContent('3 of 5 unit numbers entered');
  });

  it('blocks a list longer than the count, with the exact message, and issues NO request', async () => {
    // A positional list one entry out shifts every unit after the gap onto the
    // wrong job — invisible afterwards, and the wrong build identity then travels
    // to the kiosk, the dispatch board and the TV wall.
    renderModal();
    await setCount(5);
    pasteUnits([UNIT, 'K-9812', 'SN00042', '2410099', 'X7', 'B-2', 'C-3', 'D-4']);

    await userEvent.click(createButton());

    expect(errorBox()).toHaveTextContent(
      'You listed 8 unit numbers for 5 work orders. Make the two match, or clear the unit numbers and add them later.'
    );
    expect(mockApi.useWorkOrderTemplate).not.toHaveBeenCalled();
  });

  it('blocks a repeated unit number, case-insensitively, and issues NO request', async () => {
    // `ab-1` and `AB-1` name the same physical build. Compared on the trimmed,
    // lowercased value for the same reason the server does.
    renderModal();
    await setCount(3);
    pasteUnits(['ab-1', 'K-9812', '  AB-1  ']);

    await userEvent.click(createButton());

    expect(errorBox()).toHaveTextContent(
      'Two work orders would carry the same unit number ("AB-1"). A unit number identifies one physical build — fix the list.'
    );
    expect(mockApi.useWorkOrderTemplate).not.toHaveBeenCalled();
  });

  it('accepts blank lines as "no unit yet" and sends them as null', async () => {
    // NULL and "" are not the same claim about a build, and two work orders with
    // no unit yet is an ordinary state rather than a collision.
    renderModal();
    await setCount(3);
    pasteUnits([`  ${UNIT}  `, '', 'K-9812']);

    await userEvent.click(createButton());

    await waitFor(() =>
      expect(mockApi.useWorkOrderTemplate).toHaveBeenCalledWith(7, {
        due_date: null,
        quantity_ordered: 1,
        count: 3,
        unit_numbers: [UNIT, null, 'K-9812'],
      })
    );
  });

  it('sends the count with no list at all when the box is left empty', async () => {
    // "Add the units later" is a legal way to run a batch, not an incomplete form.
    renderModal();
    await setCount(2);

    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
    const [, payload] = mockApi.useWorkOrderTemplate.mock.calls[0];
    expect(payload).toEqual({ due_date: null, quantity_ordered: 1, count: 2 });
  });
});

describe('UseTemplateModal: a single use is byte-for-byte what it always was', () => {
  it('OMITS count and unit_numbers entirely', async () => {
    // The server body is `extra="forbid"`, and this is the path every deployed
    // client already exercises. It must not start carrying new keys because a
    // batch feature exists beside it.
    mockApi.useWorkOrderTemplate.mockResolvedValue(makeBatchResult([null]));
    renderModal();
    await waitFor(() => expect(quantityInput()).toHaveValue(8));

    await userEvent.click(createButton());

    await waitFor(() => expect(mockApi.useWorkOrderTemplate).toHaveBeenCalled());
    const [templateId, payload] = mockApi.useWorkOrderTemplate.mock.calls[0];
    expect(templateId).toBe(7);
    expect(payload).toEqual({ due_date: null, quantity_ordered: 8 });
    expect('count' in (payload as object)).toBe(false);
    expect('unit_numbers' in (payload as object)).toBe(false);
  });

  it('still toasts, hands over and closes on one copy', async () => {
    const result = makeBatchResult([null]);
    mockApi.useWorkOrderTemplate.mockResolvedValue(result);
    const { onClose, onUsed } = renderModal();

    await userEvent.click(createButton());

    await waitFor(() => expect(onUsed).toHaveBeenCalledWith(result));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/WO-20260901-001 created as a draft/)).toBeInTheDocument();
    expect(screen.queryByTestId('use-template-batch-table')).not.toBeInTheDocument();
  });
});

describe('UseTemplateModal: a clean batch stays on screen', () => {
  const units = [UNIT, null, 'K-9812'];

  async function submitCleanBatch() {
    mockApi.useWorkOrderTemplate.mockResolvedValue(makeBatchResult(units));
    const rendered = renderModal();
    await setCount(3);
    pasteUnits([UNIT, '', 'K-9812']);
    await userEvent.click(createButton());
    await screen.findByTestId('use-template-batch-table');
    return rendered;
  }

  it('renders every work order number against its unit number', async () => {
    // The planner's next physical act is writing these onto travelers, and this
    // table is the only place the mapping is ever shown together.
    await submitCleanBatch();

    const table = within(screen.getByTestId('use-template-batch-table'));
    expect(table.getByText('WO-20260901-001')).toBeInTheDocument();
    expect(table.getByText(UNIT)).toBeInTheDocument();
    expect(table.getByText('WO-20260901-002')).toBeInTheDocument();
    expect(table.getByText('WO-20260901-003')).toBeInTheDocument();
    expect(table.getByText('K-9812')).toBeInTheDocument();
    // Not blank: a planner scanning the column has to be able to tell "no unit
    // yet" from a row that failed to render one.
    expect(table.getByText('No unit yet')).toBeInTheDocument();
    expect(screen.getByText('3 draft work orders')).toBeInTheDocument();
  });

  it('does NOT hand off or close while the table is on screen', async () => {
    // Five drafts have no single destination; navigating to one would silently
    // make the other four the ones the planner never saw.
    const { onUsed, onClose } = await submitCleanBatch();

    expect(onUsed).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // And no toast to scroll the table away behind.
    expect(screen.queryByText(/Review it, then release/)).not.toBeInTheDocument();
  });

  it('hands the whole envelope over on Done, and only then closes', async () => {
    const { onUsed, onClose } = await submitCleanBatch();

    await userEvent.click(screen.getByRole('button', { name: 'Done' }));

    expect(onUsed).toHaveBeenCalledTimes(1);
    const [envelope] = onUsed.mock.calls[0] as [WorkOrderTemplateUseResult];
    expect(envelope.created_count).toBe(3);
    expect(envelope.work_orders.map(workOrder => workOrder.work_order_number)).toEqual([
      'WO-20260901-001',
      'WO-20260901-002',
      'WO-20260901-003',
    ]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('UseTemplateModal: a batch with omissions', () => {
  async function submitPartialBatch() {
    mockApi.useWorkOrderTemplate.mockResolvedValue(
      makeBatchResult([UNIT, 'K-9812', 'SN00042'], { skipped_material_allocations: [skippedTie()] })
    );
    const rendered = renderModal();
    await setCount(3);
    await userEvent.click(createButton());
    await screen.findByTestId('duplicate-wo-skips');
    return rendered;
  }

  it('says the omissions apply to EVERY draft, above the report', async () => {
    // The skip lists are the union across the copies — they ran one plan, so an
    // omission belongs to the plan. Read without this line they describe only the
    // one work order the report names.
    await submitPartialBatch();

    const scope = screen.getByTestId('use-template-batch-skip-scope');
    expect(scope).toHaveTextContent('All 3 drafts were created');
    expect(scope).toHaveTextContent('WO-20260901-001');
    expect(scope).toHaveTextContent('WO-20260901-003');
    expect(scope).toHaveTextContent('The omissions below apply to every one of them.');

    // ABOVE the report, not after it: read second, it is a footnote to a list the
    // planner has already finished reading as news about one work order.
    const report = screen.getByTestId('duplicate-wo-skips');
    expect(scope.compareDocumentPosition(report) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('neither toasts nor hands off nor closes', async () => {
    // A toast self-dismisses on a timer and fires while the caller navigates — it
    // cannot carry the news that a job will run with no demand for its material.
    const { onUsed, onClose } = await submitPartialBatch();

    expect(onUsed).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByText(/Review it, then release/)).not.toBeInTheDocument();
  });

  it('offers Done rather than a destination it cannot deliver', async () => {
    // A button promising "Go to WO-…" would close the dialog and go nowhere: the
    // page handler does not navigate for a batch, it refreshes the list.
    await submitPartialBatch();

    expect(screen.getByRole('button', { name: 'Done' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Go to / })).not.toBeInTheDocument();
  });

  it('still names one work order in the heading for a SINGLE partial copy', async () => {
    // The single-copy behaviour is untouched: one copy has a destination, so it
    // keeps its "Go to WO-…" and its work-order-numbered heading.
    mockApi.useWorkOrderTemplate.mockResolvedValue(
      makeBatchResult([UNIT], { skipped_material_allocations: [skippedTie()] })
    );
    renderModal();

    await userEvent.click(createButton());
    await screen.findByTestId('duplicate-wo-skips');

    expect(screen.getByText('Created with omissions — WO-20260901-001')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Go to WO-20260901-001' })).toBeInTheDocument();
    expect(screen.queryByTestId('use-template-batch-skip-scope')).not.toBeInTheDocument();
  });
});
