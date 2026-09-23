import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankIntakeFile, HankIntakePurchaseOrderDraft } from '../../types/hankIntake';
import type { HankTask } from '../../types/hankTasks';
import type EntityPicker from '../operations/EntityPicker';
import { HankDocumentPurchaseOrder } from './HankDocumentPurchaseOrder';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankIntakePurchaseOrderDraft: jest.fn(),
    getHankCapabilities: jest.fn(),
    getParts: jest.fn(),
    createHankTask: jest.fn(),
    executeHankTask: jest.fn(),
    getHankTask: jest.fn(),
    cancelHankTask: jest.fn(),
  },
}));
jest.mock('./HankSourceFile', () => ({ HankSourceFile: () => <button>Review source document</button> }));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled, kind }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="">Choose {kind}</option>
      <option value="7">{kind} 7</option>
      <option value="8">{kind} 8</option>
    </select>
  ),
}));
const mocked = jest.mocked(api);
const file = { id: 51, company_id: 4, filename: 'our-order.docx', source_format: 'docx' } as HankIntakeFile;
const draft: HankIntakePurchaseOrderDraft = {
  file_id: 51,
  file_version: 3,
  company_id: 4,
  filename: file.filename,
  po_number: 'PO-WORD-9',
  order_date: '2026-09-20',
  required_date: '2026-10-01',
  vendor_id: 7,
  vendors: [{ id: 7, code: 'SUP', name: 'Supplier', reason: 'Exact printed name' }],
  lines: [7, 8].map((id, source_line_index) => ({
    source_line_index,
    description: `Material ${id}`,
    part_number: `BAR-${id}`,
    quantity: '12',
    unit_price: '4.25',
    unit_of_measure: 'EA',
    lot_number: null,
    heat_number: null,
    confidence: 'high',
    evidence: [{ page: 1, locator: `Table 1, row ${source_line_index + 2}`, excerpt: `BAR-${id} 12 EA 4.25` }],
    part_id: id,
    candidates: [{ id, part_number: `BAR-${id}`, name: 'Steel bar', unit_of_measure: 'EA' }],
    quantity_ordered: 12,
    unit_price_amount: 4.25,
    warnings: [],
  })),
  warnings: [],
  has_duplicates: false,
  blocked_reason: null,
  can_ready_for_receiving: true,
  existing_purchase_orders: [],
};
const task: HankTask = {
  id: 61,
  company_id: 4,
  kind: 'draft_purchase_order',
  title: 'Import PO-WORD-9',
  status: 'awaiting_review',
  version: 1,
  input: { ready_for_receiving: true },
  preview: {
    summary: 'Review this PO before creating it.',
    changes: ['Create issued PO-WORD-9; add it to Receiving.'],
    warnings: [],
    references: [],
  },
  result: null,
  error_message: null,
  created_at: '2026-09-23T14:00:00Z',
  updated_at: '2026-09-23T14:00:00Z',
  completed_at: null,
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.sig`);
}
function mount() {
  return render(
    <MemoryRouter>
      <HankDocumentPurchaseOrder file={file} onNavigate={jest.fn()} />
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  Object.defineProperty(crypto, 'randomUUID', {
    configurable: true,
    value: jest.fn(() => '11111111-1111-4111-8111-111111111111'),
  });
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue(draft);
  mocked.getParts.mockResolvedValue(
    [7, 8].map(id => ({ id, part_number: `BAR-${id}`, name: 'Steel bar', unit_of_measure: 'EA' })) as Awaited<
      ReturnType<typeof api.getParts>
    >
  );
  mocked.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: true,
    can_watch: true,
    allowed_kinds: ['draft_purchase_order'],
  });
  mocked.createHankTask.mockResolvedValue(task);
  mocked.executeHankTask.mockResolvedValue({
    ...task,
    status: 'completed',
    version: 2,
    result: {
      summary: 'Created PO-WORD-9 and added it to Receiving.',
      warnings: [],
      references: [{ type: 'purchase_order', id: 91, label: 'Open Receiving', url: '/receiving?po=91' }],
    },
  });
});
it('reviews every Word line and saves source version, units and issuance intent before a separate confirmation', async () => {
  mount();
  expect(await screen.findByLabelText(/Printed PO number/)).toHaveValue('PO-WORD-9');
  expect(screen.getByText('Table 1, row 2: BAR-7 12 EA 4.25')).toBeInTheDocument();
  expect(screen.queryByText(/Page 1:/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('Add to Receiving')).toBeChecked();
  fireEvent.change(screen.getByLabelText(/Quantity for line 2/), { target: { value: '14' } });
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  await screen.findByText('Review this PO before creating it.');
  expect(mocked.createHankTask.mock.calls[0][0]).toMatchObject({
    expected_company_id: 4,
    kind: 'draft_purchase_order',
    input: {
      source_intake_file_id: 51,
      source_intake_version: 3,
      po_number: 'PO-WORD-9',
      vendor_id: 7,
      order_date: '2026-09-20',
      required_date: '2026-10-01',
      ready_for_receiving: true,
      lines: [
        { source_line_index: 0, part_id: 7, quantity_ordered: 12, unit_price: 4.25, unit_of_measure: 'EA' },
        { source_line_index: 1, part_id: 8, quantity_ordered: 14, unit_price: 4.25, unit_of_measure: 'EA' },
      ],
    },
  });
  expect(mocked.executeHankTask).not.toHaveBeenCalled();
  expect(mocked.getParts).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Create purchase order and add to Receiving' }));
  expect(await screen.findByRole('link', { name: 'Open Receiving' })).toHaveAttribute('href', '/receiving?po=91');
  expect(mocked.executeHankTask).toHaveBeenCalledWith(
    61,
    { expected_company_id: 4, expected_version: 1 },
    expect.any(AbortSignal)
  );
});
it('does not infer missing quantities or prices and requires employee review of uncertain lines', async () => {
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue({
    ...draft,
    lines: [
      {
        ...draft.lines[0],
        quantity_ordered: null,
        unit_price_amount: null,
        warnings: ['Check unclear quantities in the source.'],
      },
    ],
  });
  mount();
  expect(await screen.findByLabelText(/Quantity for line 1/)).toHaveValue(null);
  expect(screen.getByLabelText(/Unit price for line 1/)).toHaveValue(null);
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Each line needs an existing part');
  expect(mocked.createHankTask).not.toHaveBeenCalled();
});
it('leaves issuance disabled when the current role can only create a draft', async () => {
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue({ ...draft, can_ready_for_receiving: false });
  mount();
  await screen.findByLabelText(/Printed PO number/);
  expect(screen.queryByLabelText('Add to Receiving')).not.toBeInTheDocument();
  expect(screen.getByText(/A purchasing approver must issue/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  await waitFor(() => expect(mocked.createHankTask).toHaveBeenCalled());
  expect(mocked.createHankTask.mock.calls[0][0].input).toMatchObject({ ready_for_receiving: false });
});
it('blocks a duplicate PO and links to the existing record', async () => {
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue({
    ...draft,
    blocked_reason: 'This PO is already imported.',
    existing_purchase_orders: [{ id: 91, po_number: 'PO-WORD-9', href: '/purchasing/91' }],
  });
  mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('already imported');
  expect(screen.getByRole('link', { name: 'Review existing PO-WORD-9' })).toHaveAttribute('href', '/purchasing/91');
  expect(screen.getByRole('button', { name: 'Prepare purchase order for review' })).toBeDisabled();
});
it('retries a lost response with the identical immutable proposal and request key', async () => {
  mocked.createHankTask.mockRejectedValueOnce(new Error('Response lost'));
  mount();
  await screen.findByLabelText(/Printed PO number/);
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  const retry = await screen.findByRole('button', { name: 'Retry saving purchase order proposal' });
  expect(screen.getByLabelText(/Printed PO number/)).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Refresh purchase order suggestions' })).toBeDisabled();
  const first = mocked.createHankTask.mock.calls[0][0];
  fireEvent.click(retry);
  await screen.findByText('Review this PO before creating it.');
  expect(mocked.createHankTask.mock.calls[1][0]).toEqual(first);
  expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
});
it('ignores source data from another tenant', async () => {
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue({ ...draft, company_id: 9 });
  mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('another company');
  expect(screen.queryByLabelText(/Printed PO number/)).not.toBeInTheDocument();
});
it('ignores a late response after switching company', async () => {
  let resolve!: (value: HankIntakePurchaseOrderDraft) => void;
  mocked.getHankIntakePurchaseOrderDraft.mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  mount();
  session(9);
  await act(async () => {
    resolve(draft);
  });
  expect(screen.queryByLabelText(/Printed PO number/)).not.toBeInTheDocument();
});
it('reloads duplicate and source-version checks when starting again after completion', async () => {
  mount();
  await screen.findByLabelText(/Printed PO number/);
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Create purchase order and add to Receiving' }));
  await screen.findByRole('link', { name: 'Open Receiving' });
  mocked.getHankIntakePurchaseOrderDraft.mockResolvedValue({ ...draft, blocked_reason: 'Already imported.' });
  fireEvent.click(screen.getByRole('button', { name: 'Start another task' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Already imported.');
  expect(mocked.getHankIntakePurchaseOrderDraft).toHaveBeenCalledTimes(2);
});

it('clears both quantity and price when a newly selected part uses different stocking units', async () => {
  mocked.getParts.mockResolvedValue([
    { id: 7, part_number: 'BAR-7', name: 'Steel bar', unit_of_measure: 'EA' },
    { id: 8, part_number: 'BAR-8', name: 'Steel bar', unit_of_measure: 'EA' },
    { id: 9, part_number: 'BAR-IN', name: 'Bar by length', unit_of_measure: 'IN' },
  ] as Awaited<ReturnType<typeof api.getParts>>);
  mount();
  await screen.findByLabelText(/Printed PO number/);
  fireEvent.click(screen.getByRole('combobox', { name: /Part for line 1/ }));
  fireEvent.click(await screen.findByRole('option', { name: /BAR-IN/ }));
  expect(screen.getByLabelText(/Stocking unit for line 1/)).toHaveValue('IN');
  expect(screen.getByLabelText(/Quantity for line 1/)).toHaveValue(null);
  expect(screen.getByLabelText(/Unit price for line 1/)).toHaveValue(null);
  fireEvent.click(screen.getByRole('button', { name: 'Prepare purchase order for review' }));
  expect(mocked.createHankTask).not.toHaveBeenCalled();
});
it('retains reviewed values when choosing a part with an equivalent stocking-unit alias', async () => {
  mocked.getParts.mockResolvedValue([
    { id: 7, part_number: 'BAR-7', name: 'Steel bar', unit_of_measure: 'EA' },
    { id: 8, part_number: 'BAR-8', name: 'Steel bar', unit_of_measure: 'each' },
  ] as Awaited<ReturnType<typeof api.getParts>>);
  mount();
  await screen.findByLabelText(/Printed PO number/);
  fireEvent.click(screen.getByRole('combobox', { name: /Part for line 1/ }));
  fireEvent.click(await screen.findByRole('option', { name: /BAR-8/ }));
  expect(screen.getByLabelText(/Stocking unit for line 1/)).toHaveValue('each');
  expect(screen.getByLabelText(/Quantity for line 1/)).toHaveValue(12);
  expect(screen.getByLabelText(/Unit price for line 1/)).toHaveValue(4.25);
});
