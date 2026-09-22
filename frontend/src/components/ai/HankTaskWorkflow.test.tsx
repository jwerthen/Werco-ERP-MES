import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    allowed_kinds: ['repeat_job', 'draft_purchase_order', 'attach_document'],
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
