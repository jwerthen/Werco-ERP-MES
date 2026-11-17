import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankTask } from '../../types/hankTasks';
import type EntityPicker from '../operations/EntityPicker';
import { HankTaskWorkflow } from './HankTaskWorkflow';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    createHankTask: jest.fn(),
    executeHankTask: jest.fn(),
    cancelHankTask: jest.fn(),
    getHankTask: jest.fn(),
    getDocuments: jest.fn(),
    searchMentionableUsers: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled, kind }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="">Select a record</option>
      <option value="7">{kind} — source record</option>
      <option value="8">{kind} — another record</option>
    </select>
  ),
}));
const mockedApi = jest.mocked(api);
const UUID = '11111111-1111-4111-8111-111111111111';
// What an Admin / Manager / Supervisor is offered (plus attach_document for Admin / Manager).
const ALL_BASIC_KINDS = [
  'repeat_job',
  'draft_purchase_order',
  'attach_document',
  'change_due_date',
  'add_note',
] as const;
// A VIEWER (and an OPERATOR or SHIPPING user, for the basic form): a note is their one write.
const NOTE_ONLY_CAPABILITIES = {
  company_id: 4,
  can_write: true,
  can_watch: false,
  allowed_kinds: ['add_note' as const],
};
const proposal: HankTask = {
  id: 41,
  company_id: 4,
  kind: 'repeat_job',
  title: 'Repeat WO-1007',
  status: 'awaiting_review',
  version: 1,
  input: { source_work_order_id: 7, quantity_ordered: 3, due_date: null },
  preview: {
    summary: 'Create a draft job for three brackets.',
    changes: ['Copy the existing operations into a new draft.'],
    warnings: ['Review the copied routing before releasing the job.'],
    references: [{ type: 'work_order', id: 7, label: 'Source WO-1007', url: '/work-orders/7' }],
  },
  result: null,
  error_message: null,
  created_at: '2026-09-22T13:30:00Z',
  updated_at: '2026-09-22T13:30:00Z',
  completed_at: null,
};
const completed: HankTask = {
  ...proposal,
  status: 'completed',
  version: 2,
  completed_at: '2026-09-22T13:31:00Z',
  result: {
    summary: 'Created draft WO-1010.',
    warnings: ['Release still requires review.'],
    references: [{ type: 'work_order', id: 10, label: 'WO-1010', url: '/work-orders/10' }],
  },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
function setCompany(cid: number) {
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.signature`
  );
}
function responseError(status: number, detail: string) {
  return { isAxiosError: true, response: { status, data: { detail } } };
}
function renderWorkflow(props: Partial<React.ComponentProps<typeof HankTaskWorkflow>> = {}) {
  const onNavigate = jest.fn();
  const onBusyChange = jest.fn();
  const onTaskChanged = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankTaskWorkflow
          onNavigate={onNavigate}
          onBusyChange={onBusyChange}
          onTaskChanged={onTaskChanged}
          {...props}
        />
      </MemoryRouter>
    ),
    onNavigate,
    onBusyChange,
    onTaskChanged,
  };
}
async function fillRepeat() {
  await screen.findByRole('form', { name: 'Prepare a task with Hank' });
  fireEvent.change(screen.getByLabelText(/Job to repeat/), { target: { value: '7' } });
  fireEvent.change(screen.getByLabelText(/Quantity/), { target: { value: '3' } });
}
function prepare() {
  fireEvent.click(screen.getByRole('button', { name: 'Prepare task for review' }));
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  setCompany(4);
  Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: jest.fn(() => UUID) });
  mockedApi.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: true,
    can_watch: true,
    allowed_kinds: [...ALL_BASIC_KINDS],
  });
  mockedApi.createHankTask.mockResolvedValue(proposal);
  mockedApi.executeHankTask.mockResolvedValue(completed);
  mockedApi.cancelHankTask.mockResolvedValue({ ...proposal, status: 'cancelled', version: 2 });
  mockedApi.getHankTask.mockResolvedValue(proposal);
  mockedApi.getDocuments.mockResolvedValue([
    {
      id: 12,
      document_number: 'DOC-0012',
      title: 'Bracket drawing',
      revision: 'B',
      status: 'released',
      file_name: 'bracket.pdf',
      mime_type: 'application/pdf',
    },
  ]);
  mockedApi.searchMentionableUsers.mockResolvedValue([
    { id: 5, display_name: 'Jamie Okafor' },
    { id: 6, display_name: 'Jana Whitfield' },
  ]);
});

describe('HankTaskWorkflow', () => {
  it('offers only server-authorized actions and keeps read-only users out of create and execute', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue({
      company_id: 4,
      can_write: true,
      can_watch: true,
      allowed_kinds: ['attach_document'],
    });
    const first = renderWorkflow();
    await screen.findByRole('option', { name: 'Attach a PDF to a job' });
    expect(screen.queryByRole('option', { name: 'Repeat a job' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Draft a purchase order' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Change a due date' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Add a note to a job' })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /^Task/ })).toHaveValue('attach_document');
    first.unmount();
    mockedApi.getHankCapabilities.mockResolvedValue({
      company_id: 4,
      can_write: false,
      can_watch: false,
      allowed_kinds: [],
    });
    renderWorkflow({ initialTask: proposal });
    await screen.findByText(proposal.preview.summary);
    expect(screen.queryByRole('button', { name: 'Create draft work order' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel proposal' })).not.toBeInTheDocument();
    expect(mockedApi.createHankTask).not.toHaveBeenCalled();
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });

  it('recovers capabilities failure without submitting anything', async () => {
    mockedApi.getHankCapabilities.mockRejectedValueOnce(new Error('Offline'));
    renderWorkflow();
    expect(await screen.findByRole('alert')).toHaveTextContent('available tasks could not be loaded');
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading tasks' }));
    await screen.findByRole('form', { name: 'Prepare a task with Hank' });
    expect(mockedApi.getHankCapabilities).toHaveBeenCalledTimes(2);
    expect(mockedApi.createHankTask).not.toHaveBeenCalled();
  });

  it('saves a proposal then requires an explicit execute before showing a real receipt', async () => {
    const execution = deferred<HankTask>();
    mockedApi.executeHankTask.mockReturnValue(execution.promise);
    const { onTaskChanged, onBusyChange, onNavigate } = renderWorkflow();
    await fillRepeat();
    prepare();
    await screen.findByText(proposal.preview.summary);
    expect(mockedApi.createHankTask).toHaveBeenCalledWith(
      {
        expected_company_id: 4,
        request_key: UUID,
        kind: 'repeat_job',
        input: { source_work_order_id: 7, quantity_ordered: 3, due_date: null },
      },
      expect.any(AbortSignal)
    );
    expect(screen.getByText(proposal.preview.warnings[0])).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Source WO-1007' })).toHaveAttribute('href', '/work-orders/7');
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Create draft work order' }));
    expect(mockedApi.executeHankTask).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 1 },
      expect.any(AbortSignal)
    );
    expect(screen.queryByText('Created draft WO-1010.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Completing task…/ }));
    expect(mockedApi.executeHankTask).toHaveBeenCalledTimes(1);
    await act(async () => execution.resolve(completed));
    expect(screen.getByText('Created draft WO-1010.')).toBeInTheDocument();
    expect(screen.getByText('Release still requires review.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'WO-1010' }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onTaskChanged).toHaveBeenLastCalledWith(completed);
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
    expect(screen.queryByRole('button', { name: 'Create draft work order' })).not.toBeInTheDocument();
  });

  it('keeps the same frozen body and UUID when a proposal response is lost', async () => {
    mockedApi.createHankTask.mockRejectedValueOnce(new Error('Response lost'));
    renderWorkflow();
    await fillRepeat();
    prepare();
    await screen.findByRole('button', { name: 'Retry saving proposal' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry saving proposal' })).toBeEnabled());
    const firstBody = mockedApi.createHankTask.mock.calls[0][0];
    expect(screen.getByLabelText(/Quantity/)).toBeDisabled();
    expect(screen.getByRole('combobox', { name: /^Task/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry saving proposal' }));
    await screen.findByText(proposal.preview.summary);
    expect(mockedApi.createHankTask.mock.calls[1][0]).toEqual(firstBody);
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });

  it('re-enables editing after a confirmed validation refusal and creates a new request for changed input', async () => {
    jest
      .mocked(crypto.randomUUID)
      .mockReturnValueOnce(UUID)
      .mockReturnValueOnce('22222222-2222-4222-8222-222222222222');
    mockedApi.createHankTask.mockRejectedValueOnce(responseError(422, 'Source job cannot be repeated.'));
    renderWorkflow();
    await fillRepeat();
    prepare();
    await screen.findByText('Source job cannot be repeated.');
    expect(screen.getByLabelText(/Job to repeat/)).toBeEnabled();
    fireEvent.change(screen.getByLabelText(/Job to repeat/), { target: { value: '8' } });
    prepare();
    await screen.findByText(proposal.preview.summary);
    expect(mockedApi.createHankTask.mock.calls[1][0].input).toMatchObject({ source_work_order_id: 8 });
    expect(mockedApi.createHankTask.mock.calls[1][0].request_key).not.toBe(
      mockedApi.createHankTask.mock.calls[0][0].request_key
    );
    expect(crypto.randomUUID).toHaveBeenCalledTimes(2);
  });

  it('recovers an uncertain execution through status refresh without executing twice', async () => {
    mockedApi.executeHankTask.mockRejectedValueOnce(new Error('Response lost'));
    mockedApi.getHankTask.mockResolvedValue(completed);
    renderWorkflow({ initialTask: proposal });
    fireEvent.click(await screen.findByRole('button', { name: 'Create draft work order' }));
    await screen.findByText(/Refresh its status before taking another action/);
    expect(screen.getByRole('button', { name: 'Create draft work order' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel proposal' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh task status' }));
    await screen.findByText('Created draft WO-1010.');
    expect(mockedApi.getHankTask).toHaveBeenCalledWith(41, expect.any(AbortSignal));
    expect(mockedApi.executeHankTask).toHaveBeenCalledTimes(1);
  });

  it('does not re-enable a stale preview after a 409 and a successful status refresh', async () => {
    mockedApi.executeHankTask.mockRejectedValueOnce(responseError(409, 'Source records changed.'));
    renderWorkflow({ initialTask: proposal });
    fireEvent.click(await screen.findByRole('button', { name: 'Create draft work order' }));
    await screen.findByText('Source records changed.');
    expect(screen.getByRole('button', { name: 'Create draft work order' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh task status' }));
    await waitFor(() => expect(mockedApi.getHankTask).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh task status' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Create draft work order' })).toBeDisabled();
    expect(screen.getByText(/This preview is out of date/)).toBeInTheDocument();
    expect(mockedApi.executeHankTask).toHaveBeenCalledTimes(1);
  });

  it('cancels only after server confirmation and allows a new proposal afterward', async () => {
    const cancellation = deferred<HankTask>();
    mockedApi.cancelHankTask.mockReturnValue(cancellation.promise);
    renderWorkflow({ initialTask: proposal });
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel proposal' }));
    expect(mockedApi.cancelHankTask).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 1 },
      expect.any(AbortSignal)
    );
    expect(screen.queryByText(/cancelled · Updated/)).not.toBeInTheDocument();
    await act(async () => cancellation.resolve({ ...proposal, status: 'cancelled', version: 2 }));
    expect(screen.getByText(/cancelled · Updated/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Start another task' }));
    expect(screen.getByRole('form', { name: 'Prepare a task with Hank' })).toBeInTheDocument();
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });

  it('submits a one-line draft PO with explicit quantity, price and required date', async () => {
    renderWorkflow();
    fireEvent.change(await screen.findByRole('combobox', { name: /^Task/ }), {
      target: { value: 'draft_purchase_order' },
    });
    fireEvent.change(screen.getByLabelText(/Vendor/), { target: { value: '7' } });
    fireEvent.change(screen.getByLabelText(/Part to order/), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText(/Quantity/), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText(/Unit price/), { target: { value: '2.75' } });
    fireEvent.change(screen.getByLabelText(/Required date/), { target: { value: '2026-10-03' } });
    prepare();
    await waitFor(() =>
      expect(mockedApi.createHankTask).toHaveBeenCalledWith(
        {
          expected_company_id: 4,
          request_key: UUID,
          kind: 'draft_purchase_order',
          input: {
            vendor_id: 7,
            required_date: '2026-10-03',
            lines: [{ part_id: 8, quantity_ordered: 12, unit_price: 2.75, required_date: '2026-10-03' }],
          },
        },
        expect.any(AbortSignal)
      )
    );
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });

  it('uses bounded document search and explicitly selected PDF and target job', async () => {
    renderWorkflow();
    fireEvent.change(await screen.findByRole('combobox', { name: /^Task/ }), { target: { value: 'attach_document' } });
    await screen.findByRole('option', { name: /DOC-0012 · Rev B · Bracket drawing/ });
    expect(mockedApi.getDocuments).toHaveBeenCalledWith({ search: undefined, limit: 25 });
    fireEvent.change(screen.getByLabelText(/Find a PDF/), { target: { value: 'Bracket' } });
    await waitFor(() => expect(mockedApi.getDocuments).toHaveBeenLastCalledWith({ search: 'Bracket', limit: 25 }));
    await waitFor(() => expect(screen.getByLabelText(/PDF to attach/)).toBeEnabled());
    fireEvent.change(screen.getByLabelText(/PDF to attach/), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText(/Target work order/), { target: { value: '8' } });
    prepare();
    await waitFor(() =>
      expect(mockedApi.createHankTask).toHaveBeenCalledWith(
        {
          expected_company_id: 4,
          request_key: UUID,
          kind: 'attach_document',
          input: { document_id: 12, work_order_id: 8 },
        },
        expect.any(AbortSignal)
      )
    );
  });

  it('validates missing selections and nonpositive quantity without creating a task', async () => {
    renderWorkflow();
    await screen.findByRole('form', { name: 'Prepare a task with Hank' });
    fireEvent.change(screen.getByLabelText(/Quantity/), { target: { value: '0' } });
    prepare();
    await screen.findByText('Select a record from the available choices.');
    expect(screen.getByText('Enter a quantity greater than zero.')).toBeInTheDocument();
    expect(mockedApi.createHankTask).not.toHaveBeenCalled();
  });

  it('aborts a write synchronously on company change and suppresses a late completion', async () => {
    const pending = deferred<HankTask>();
    mockedApi.executeHankTask.mockReturnValue(pending.promise);
    const { onTaskChanged } = renderWorkflow({ initialTask: proposal });
    fireEvent.click(await screen.findByRole('button', { name: 'Create draft work order' }));
    const signal = mockedApi.executeHankTask.mock.calls[0][2];
    expect(signal?.aborted).toBe(false);
    setCompany(5);
    act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(completed));
    expect(onTaskChanged).not.toHaveBeenCalled();
    expect(screen.queryByText('Created draft WO-1010.')).not.toBeInTheDocument();
  });

  it('aborts a pending write on unmount and suppresses its late proposal response', async () => {
    const pending = deferred<HankTask>();
    mockedApi.createHankTask.mockReturnValue(pending.promise);
    const { unmount, onTaskChanged } = renderWorkflow();
    await fillRepeat();
    prepare();
    await waitFor(() => expect(mockedApi.createHankTask).toHaveBeenCalledTimes(1));
    const signal = mockedApi.createHankTask.mock.calls[0][1];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(proposal));
    expect(onTaskChanged).not.toHaveBeenCalled();
  });

  it('refuses to display or execute a task from a different company', async () => {
    renderWorkflow({ initialTask: { ...proposal, company_id: 5 } });
    expect(await screen.findByRole('alert')).toHaveTextContent('different company');
    expect(screen.queryByText(proposal.preview.summary)).not.toBeInTheDocument();
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });
});

const dueDateTask: HankTask = {
  ...proposal,
  id: 52,
  kind: 'change_due_date',
  title: 'Move due date of WO-1008',
  input: { work_order_id: 8, new_due_date: '2026-12-01', reason: 'Customer pulled the order in' },
  preview: {
    summary: 'Change the due date of WO-1008 to 2026-12-01.',
    changes: ['Due date: 2026-11-20 → 2026-12-01.', 'Reason: Customer pulled the order in'],
    warnings: [
      'Only the due date changes. Scheduled dates, priority and the dispatch plan are not edited.',
      'The promise date does not move: must ship by 2026-11-25 stays as it is, and it takes precedence over the due date for on-time delivery.',
    ],
    references: [{ type: 'work_order', id: 8, label: 'WO-1008', url: '/work-orders/8' }],
  },
};
const noteTask: HankTask = {
  ...proposal,
  id: 53,
  kind: 'add_note',
  title: 'Add a note to WO-1008',
  input: { work_order_id: 8, body: 'Check the weld\nbefore paint @Jamie Okafor', mentioned_user_ids: [5] },
  preview: {
    summary: 'Add a note to WO-1008.',
    changes: ['Note: Check the weld\nbefore paint @Jamie Okafor', 'Mentions: Jamie Okafor.'],
    warnings: [
      'Everyone who can see this job can read the note. It is posted under your name.',
      'Mentioned people are notified in the app and by email; the email does not include the note text.',
    ],
    references: [{ type: 'work_order', id: 8, label: 'WO-1008', url: '/work-orders/8' }],
  },
};

const optionNames = (select: HTMLElement) =>
  within(select)
    .getAllByRole('option')
    .map(option => option.textContent);
async function chooseKind(name: string, value: string) {
  const select = await screen.findByRole('combobox', { name: /^Task/ });
  expect(within(select).getByRole('option', { name })).toBeInTheDocument();
  fireEvent.change(select, { target: { value } });
}

describe('HankTaskWorkflow — change a due date and add a note', () => {
  it('offers both kinds when the server allows them, with no quantity on either form', async () => {
    renderWorkflow();
    const select = await screen.findByRole('combobox', { name: /^Task/ });
    expect(optionNames(select)).toEqual([
      'Repeat a job',
      'Draft a purchase order',
      'Attach a PDF to a job',
      'Change a due date',
      'Add a note to a job',
    ]);
    fireEvent.change(select, { target: { value: 'change_due_date' } });
    expect(screen.getByLabelText(/New due date/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Quantity/)).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: 'add_note' } });
    expect(screen.getByRole('combobox', { name: /^Note/ })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Quantity/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/New due date/)).not.toBeInTheDocument();
  });

  it('shows a note-only user exactly one task: the note form', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue(NOTE_ONLY_CAPABILITIES);
    renderWorkflow();
    const select = await screen.findByRole('combobox', { name: /^Task/ });
    expect(optionNames(select)).toEqual(['Add a note to a job']);
    expect(select).toHaveValue('add_note');
    expect(screen.getByRole('combobox', { name: /^Note/ })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Work order/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Quantity/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Job to repeat/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/New due date/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No basic task templates/)).not.toBeInTheDocument();
  });

  it('requires a work order, a new due date and a non-blank reason before saving a due-date change', async () => {
    renderWorkflow();
    await chooseKind('Change a due date', 'change_due_date');
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: '   ' } });
    prepare();
    await screen.findByText('Give a reason for moving the due date.');
    expect(screen.getByText('Select a record from the available choices.')).toBeInTheDocument();
    expect(screen.getByText('Choose the new due date.')).toBeInTheDocument();
    expect(screen.queryByText('Enter a quantity greater than zero.')).not.toBeInTheDocument();
    expect(mockedApi.createHankTask).not.toHaveBeenCalled();
  });

  it('builds the exact due-date input: due_date only, reason trimmed, no quantity and no must_ship_by', async () => {
    renderWorkflow();
    await chooseKind('Change a due date', 'change_due_date');
    fireEvent.change(screen.getByLabelText(/^Work order/), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText(/New due date/), { target: { value: '2026-12-01' } });
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: '  Customer pulled the order in  ' } });
    prepare();
    await waitFor(() => expect(mockedApi.createHankTask).toHaveBeenCalledTimes(1));
    const body = mockedApi.createHankTask.mock.calls[0][0];
    expect(body).toEqual({
      expected_company_id: 4,
      request_key: UUID,
      kind: 'change_due_date',
      input: { work_order_id: 8, new_due_date: '2026-12-01', reason: 'Customer pulled the order in' },
    });
    expect(body.input).not.toHaveProperty('must_ship_by');
    expect(body.input).not.toHaveProperty('quantity_ordered');
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
  });

  it('refuses a blank note without saving anything', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue(NOTE_ONLY_CAPABILITIES);
    renderWorkflow();
    await screen.findByRole('combobox', { name: /^Note/ });
    fireEvent.change(screen.getByLabelText(/^Work order/), { target: { value: '8' } });
    fireEvent.change(screen.getByRole('combobox', { name: /^Note/ }), { target: { value: '  \n  ' } });
    prepare();
    await screen.findByText('Write the note.');
    expect(mockedApi.createHankTask).not.toHaveBeenCalled();
  });

  it('turns the @mention chips into mentioned_user_ids and sends the trimmed note', async () => {
    const user = userEvent.setup();
    mockedApi.getHankCapabilities.mockResolvedValue(NOTE_ONLY_CAPABILITIES);
    renderWorkflow();
    const note = await screen.findByRole('combobox', { name: /^Note/ });
    fireEvent.change(screen.getByLabelText(/^Work order/), { target: { value: '8' } });
    await user.type(note, 'Check the weld before paint @ja');
    await waitFor(() => expect(note).toHaveAttribute('aria-expanded', 'true'));
    expect(mockedApi.searchMentionableUsers).toHaveBeenCalledWith('ja', 8);
    await user.keyboard('{Enter}');
    expect(note).toHaveValue('Check the weld before paint @Jamie Okafor ');
    expect(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' })).toBeInTheDocument();
    prepare();
    await waitFor(() =>
      expect(mockedApi.createHankTask).toHaveBeenCalledWith(
        {
          expected_company_id: 4,
          request_key: UUID,
          kind: 'add_note',
          input: { work_order_id: 8, body: 'Check the weld before paint @Jamie Okafor', mentioned_user_ids: [5] },
        },
        expect.any(AbortSignal)
      )
    );
  });

  it('sends no mention for a removed chip, even though the @Name text stays in the note', async () => {
    const user = userEvent.setup();
    renderWorkflow();
    await chooseKind('Add a note to a job', 'add_note');
    const note = screen.getByRole('combobox', { name: /^Note/ });
    fireEvent.change(screen.getByLabelText(/^Work order/), { target: { value: '7' } });
    await user.type(note, '@ja');
    await waitFor(() => expect(note).toHaveAttribute('aria-expanded', 'true'));
    await user.keyboard('{Enter}');
    await user.click(screen.getByRole('button', { name: 'Remove mention of Jamie Okafor' }));
    prepare();
    await waitFor(() => expect(mockedApi.createHankTask).toHaveBeenCalledTimes(1));
    expect(mockedApi.createHankTask.mock.calls[0][0]).toMatchObject({
      kind: 'add_note',
      input: { work_order_id: 7, body: '@Jamie Okafor', mentioned_user_ids: [] },
    });
  });

  it('still builds a bare attach_document input after due-date fields were filled (no fall-through)', async () => {
    renderWorkflow();
    await chooseKind('Change a due date', 'change_due_date');
    fireEvent.change(screen.getByLabelText(/^Work order/), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText(/New due date/), { target: { value: '2026-12-01' } });
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Customer pulled the order in' } });
    fireEvent.change(screen.getByRole('combobox', { name: /^Task/ }), { target: { value: 'attach_document' } });
    await waitFor(() => expect(screen.getByLabelText(/PDF to attach/)).toBeEnabled());
    fireEvent.change(screen.getByLabelText(/PDF to attach/), { target: { value: '12' } });
    prepare();
    await waitFor(() =>
      expect(mockedApi.createHankTask).toHaveBeenCalledWith(
        {
          expected_company_id: 4,
          request_key: UUID,
          kind: 'attach_document',
          input: { document_id: 12, work_order_id: 8 },
        },
        expect.any(AbortSignal)
      )
    );
  });

  it('renders the due-date preview (old → new, must-ship-by warning) and executes only on confirm', async () => {
    mockedApi.executeHankTask.mockResolvedValue({
      ...dueDateTask,
      status: 'completed',
      version: 2,
      result: {
        summary: 'Moved the due date of WO-1008 from 2026-11-20 to 2026-12-01.',
        warnings: ['Must ship by 2026-11-25 was not changed.'],
        references: dueDateTask.preview.references,
      },
    });
    renderWorkflow({ initialTask: dueDateTask });
    await screen.findByText('Change the due date of WO-1008 to 2026-12-01.');
    expect(screen.getByText('Due date: 2026-11-20 → 2026-12-01.')).toBeInTheDocument();
    expect(screen.getByText('Reason: Customer pulled the order in')).toBeInTheDocument();
    expect(screen.getByText(/must ship by 2026-11-25 stays as it is/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'WO-1008' })).toHaveAttribute('href', '/work-orders/8');
    expect(mockedApi.executeHankTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Change due date' }));
    expect(mockedApi.executeHankTask).toHaveBeenCalledWith(
      52,
      { expected_company_id: 4, expected_version: 1 },
      expect.any(AbortSignal)
    );
    await screen.findByText('Moved the due date of WO-1008 from 2026-11-20 to 2026-12-01.');
    expect(screen.getByText('Must ship by 2026-11-25 was not changed.')).toBeInTheDocument();
  });

  it('renders the note preview verbatim with its mentions, and lets a note-only user post it', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue(NOTE_ONLY_CAPABILITIES);
    mockedApi.executeHankTask.mockResolvedValue({
      ...noteTask,
      status: 'completed',
      version: 2,
      result: { summary: 'Added a note to WO-1008.', warnings: [], references: noteTask.preview.references },
    });
    renderWorkflow({ initialTask: noteTask });
    await screen.findByText('Add a note to WO-1008.');
    const noteLine = screen.getByText('Note: Check the weld before paint @Jamie Okafor');
    // Verbatim: the line break the author typed reaches the reviewer.
    expect(noteLine.textContent).toBe('Note: Check the weld\nbefore paint @Jamie Okafor');
    expect(screen.getByText('Mentions: Jamie Okafor.')).toBeInTheDocument();
    expect(screen.getByText(/the email does not include the note text/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Post note' }));
    expect(mockedApi.executeHankTask).toHaveBeenCalledWith(
      53,
      { expected_company_id: 4, expected_version: 1 },
      expect.any(AbortSignal)
    );
    await screen.findByText('Added a note to WO-1008.');
  });

  it('lets a note-only user review a due-date change but not execute it', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue(NOTE_ONLY_CAPABILITIES);
    renderWorkflow({ initialTask: dueDateTask });
    await screen.findByText('Change the due date of WO-1008 to 2026-12-01.');
    expect(screen.queryByRole('button', { name: 'Change due date' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel proposal' })).not.toBeInTheDocument();
    expect(screen.getByText(/They do not allow you to execute it/)).toBeInTheDocument();
  });
});
