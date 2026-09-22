import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankRoutine, HankRoutineRun } from '../../types/hankWork';
import type EntityPicker from '../operations/EntityPicker';
import { HankRoutines } from './HankRoutines';
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    getHankRoutines: jest.fn(),
    getHankRoutine: jest.fn(),
    getHankRoutineRuns: jest.fn(),
    getHankRoutineRun: jest.fn(),
    createHankRoutine: jest.fn(),
    updateHankRoutine: jest.fn(),
    commandHankRoutine: jest.fn(),
    startHankRoutine: jest.fn(),
    advanceHankRoutine: jest.fn(),
    cancelHankRoutineRun: jest.fn(),
    getHankTasks: jest.fn(),
    getHankIntakes: jest.fn(),
    getHankHandoffs: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">None</option>
      <option value="7">WO-7</option>
    </select>
  ),
}));
jest.mock('./HankPurchaseOrderPicker', () => ({
  HankPurchaseOrderPicker: ({
    id,
    value,
    onChange,
    disabled,
  }: {
    id: string;
    value: string;
    onChange: (v: string) => void;
    disabled?: boolean;
  }) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">None</option>
      <option value="10">PO-10</option>
    </select>
  ),
}));
const mocked = jest.mocked(api);
const routine: HankRoutine = {
  id: 4,
  company_id: 4,
  version: 2,
  status: 'approved',
  title: 'Job start',
  description: 'Review the source records.',
  steps: [{ kind: 'readiness', title: 'Review readiness', instruction: 'Check recorded blockers.' }],
  created_by: 17,
  approved_by: 17,
  approved_at: '2026-09-22T14:00:00Z',
  created_at: '2026-09-22T14:00:00Z',
  updated_at: '2026-09-22T14:00:00Z',
  can_manage: true,
  can_approve: true,
};
const run: HankRoutineRun = {
  id: 21,
  company_id: 4,
  routine_id: 4,
  routine_version: 2,
  title: 'Job start run',
  status: 'active',
  version: 1,
  current_step: 0,
  steps: routine.steps,
  work_order_id: 7,
  purchase_order_id: null,
  results: [],
  created_at: '2026-09-22T14:00:00Z',
  updated_at: '2026-09-22T14:00:00Z',
  completed_at: null,
  can_edit: true,
};
const scope = (cid: number) =>
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.s`);
function setup(initialId?: number) {
  const onBusyChange = jest.fn();
  const onOpenStep = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankRoutines
          initialId={initialId}
          workOrderId={7}
          onNavigate={jest.fn()}
          onBusyChange={onBusyChange}
          onOpenStep={onOpenStep}
        />
      </MemoryRouter>
    ),
    onBusyChange,
    onOpenStep,
  };
}
beforeEach(() => {
  jest.resetAllMocks();
  scope(4);
  mocked.getHankCapabilities.mockResolvedValue({ company_id: 4, can_watch: true, can_write: true, allowed_kinds: [] });
  mocked.getHankRoutines.mockResolvedValue({
    routines: [routine],
    templates: [
      {
        title: 'Receiving packet',
        description: 'Review receiving.',
        steps: [{ kind: 'checklist', title: 'Check labels', instruction: 'Match the traveler.' }],
      },
    ],
    can_manage: true,
    can_approve: true,
  });
  mocked.getHankRoutineRuns.mockResolvedValue({ runs: [], has_more: false, next_before_id: null });
  mocked.getHankRoutineRun.mockResolvedValue(run);
  mocked.getHankRoutine.mockResolvedValue(routine);
  mocked.startHankRoutine.mockResolvedValue(run);
});
it('uses a template as a draft and requires a separate explicit version approval', async () => {
  const draft = { ...routine, title: 'Receiving packet', status: 'draft' as const, version: 1 };
  mocked.createHankRoutine.mockResolvedValue(draft);
  mocked.commandHankRoutine.mockResolvedValue({ ...draft, status: 'approved', version: 2 });
  setup();
  fireEvent.click(await screen.findByText('Prepare a routine draft'));
  fireEvent.click(screen.getByRole('button', { name: 'Use Receiving packet template' }));
  expect(screen.getByLabelText(/Routine title/)).toHaveValue('Receiving packet');
  fireEvent.click(screen.getByRole('button', { name: 'Save routine draft' }));
  await screen.findByRole('button', { name: 'Approve this version' });
  expect(mocked.commandHankRoutine).not.toHaveBeenCalled();
  expect(mocked.createHankRoutine).toHaveBeenCalledWith(
    expect.objectContaining({
      expected_company_id: 4,
      title: 'Receiving packet',
      steps: [{ kind: 'checklist', title: 'Check labels', instruction: 'Match the traveler.' }],
    }),
    expect.any(AbortSignal)
  );
  fireEvent.click(screen.getByRole('button', { name: 'Approve this version' }));
  await screen.findByRole('button', { name: 'Start approved routine' });
  expect(mocked.commandHankRoutine).toHaveBeenCalledWith(
    4,
    'approve',
    { expected_company_id: 4, expected_version: 1 },
    expect.any(AbortSignal)
  );
});
it('preserves the same approved-version start request after an uncertain response', async () => {
  mocked.startHankRoutine.mockRejectedValueOnce(new Error('lost'));
  const { onBusyChange } = setup();
  fireEvent.click(await screen.findByRole('button', { name: /Job start approved/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Start approved routine' }));
  await screen.findByText(/request was not confirmed/);
  expect(screen.getByRole('button', { name: 'Routine library' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Edit routine' })).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: 'Retry starting this routine' }));
  await screen.findByRole('heading', { name: 'Job start run' });
  expect(mocked.startHankRoutine.mock.calls[1][1]).toEqual(mocked.startHankRoutine.mock.calls[0][1]);
  expect(mocked.startHankRoutine.mock.calls[0][1]).toMatchObject({
    expected_company_id: 4,
    expected_version: 2,
    work_order_id: 7,
  });
});
it('requires a review note and advances only from the saved server result', async () => {
  mocked.advanceHankRoutine.mockResolvedValue({
    ...run,
    status: 'completed',
    version: 2,
    current_step: 1,
    results: [
      {
        step_index: 0,
        note: 'No open blockers in checked records.',
        completed_at: '2026-09-22T14:05:00Z',
        evidence: [],
      },
    ],
    completed_at: '2026-09-22T14:05:00Z',
  });
  const { onOpenStep } = setup(21);
  await screen.findByRole('heading', { name: 'Job start run' });
  fireEvent.click(screen.getByRole('button', { name: 'Open current step workspace' }));
  expect(onOpenStep).toHaveBeenCalledWith('readiness', run);
  fireEvent.click(screen.getByRole('button', { name: 'Confirm this step' }));
  await waitFor(() => expect(mocked.advanceHankRoutine).not.toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText(/Step review note/), {
    target: { value: 'No open blockers in checked records.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm this step' }));
  await waitFor(() =>
    expect(mocked.advanceHankRoutine).toHaveBeenCalledWith(
      21,
      { expected_company_id: 4, expected_version: 1, note: 'No open blockers in checked records.' },
      expect.any(AbortSignal)
    )
  );
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm this step' })).not.toBeInTheDocument());
});
it('returns a deep-linked saved run to the library without reopening it', async () => {
  setup(21);
  await screen.findByRole('heading', { name: 'Job start run' });
  fireEvent.click(screen.getByRole('button', { name: 'Routine library' }));
  await screen.findByText('Your saved runs');
  expect(screen.queryByRole('heading', { name: 'Job start run' })).not.toBeInTheDocument();
  expect(mocked.getHankRoutineRun).toHaveBeenCalledTimes(1);
});
it('blocks stale version writes until refreshing the saved run', async () => {
  mocked.advanceHankRoutine.mockRejectedValueOnce({
    isAxiosError: true,
    response: { status: 409, data: { detail: 'Routine run changed.' } },
  });
  setup(21);
  await screen.findByRole('heading', { name: 'Job start run' });
  fireEvent.change(screen.getByLabelText(/Step review note/), { target: { value: 'Reviewed source.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm this step' }));
  await screen.findByText('Routine run changed.');
  expect(screen.getByRole('button', { name: 'Confirm this step' })).toBeDisabled();
  mocked.getHankRoutineRun.mockResolvedValueOnce({ ...run, version: 2 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh routine run' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm this step' })).toBeEnabled());
});
it('uses only completed receipt choices matching the current action kind', async () => {
  const actionRun = {
    ...run,
    steps: [{ kind: 'draft_shipment' as const, title: 'Draft shipment', instruction: 'Save a draft shipment.' }],
  };
  mocked.getHankRoutineRun.mockResolvedValue(actionRun);
  const task = {
    id: 31,
    company_id: 4,
    kind: 'draft_shipment' as const,
    status: 'completed' as const,
    title: 'Saved shipment',
    version: 2,
    input: { work_order_id: 7 },
    preview: { summary: '', changes: [], warnings: [], references: [] },
    result: { summary: 'Saved.', warnings: [], references: [] },
    error_message: null,
    created_at: '2026-09-22T14:00:00Z',
    updated_at: '2026-09-22T14:00:00Z',
    completed_at: '2026-09-22T14:00:00Z',
  };
  mocked.getHankTasks.mockResolvedValue({
    tasks: [
      task,
      { ...task, id: 32, title: 'Wrong job', input: { work_order_id: 8 } },
      { ...task, id: 33, title: 'Wrong kind', kind: 'receive_delivery' },
    ],
    has_more: false,
    next_before_id: null,
  });
  mocked.advanceHankRoutine.mockResolvedValue({ ...actionRun, current_step: 1, status: 'completed', version: 2 });
  setup(21);
  await screen.findByRole('option', { name: 'Saved shipment' });
  expect(screen.queryByRole('option', { name: 'Wrong job' })).not.toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'Wrong kind' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Completed evidence'), { target: { value: '31' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm this step' }));
  await waitFor(() =>
    expect(mocked.advanceHankRoutine).toHaveBeenCalledWith(
      21,
      expect.objectContaining({ task_id: 31, expected_version: 1 }),
      expect.any(AbortSignal)
    )
  );
});
it('aborts a saved-run read and ignores the old company result', async () => {
  let resolve!: (r: HankRoutineRun) => void;
  mocked.getHankRoutineRun.mockReturnValueOnce(
    new Promise(r => {
      resolve = r;
    })
  );
  setup(21);
  await waitFor(() => expect(mocked.getHankRoutineRun).toHaveBeenCalled());
  const signal = mocked.getHankRoutineRun.mock.calls[0][1];
  act(() => {
    scope(9);
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    resolve(run);
  });
  expect(screen.queryByRole('heading', { name: 'Job start run' })).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});

it('refreshes a refused start version and creates a new key only after that definitive refusal', async () => {
  mocked.startHankRoutine.mockRejectedValueOnce({
    isAxiosError: true,
    response: { status: 409, data: { detail: 'Approved version changed.' } },
  });
  setup();
  fireEvent.click(await screen.findByRole('button', { name: /Job start approved/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Start approved routine' }));
  await screen.findByText('Approved version changed.');
  expect(screen.getByRole('button', { name: 'Start approved routine' })).toBeDisabled();
  mocked.getHankRoutine.mockResolvedValueOnce({ ...routine, version: 3 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh routine' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start approved routine' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Start approved routine' }));
  await screen.findByRole('heading', { name: 'Job start run' });
  expect(mocked.startHankRoutine.mock.calls[1][1].expected_version).toBe(3);
  expect(mocked.startHankRoutine.mock.calls[1][1].request_key).not.toBe(
    mocked.startHankRoutine.mock.calls[0][1].request_key
  );
});

it('cancels only the selected saved run and waits for its saved cancelled status', async () => {
  mocked.cancelHankRoutineRun.mockResolvedValueOnce({ ...run, status: 'cancelled', version: 2, can_edit: false });
  setup(21);
  fireEvent.click(await screen.findByRole('button', { name: 'Cancel routine run' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm this step' })).not.toBeInTheDocument());
  expect(mocked.cancelHankRoutineRun).toHaveBeenCalledWith(
    21,
    { expected_company_id: 4, expected_version: 1 },
    expect.any(AbortSignal)
  );
});

it('clears approval in the visible saved result after editing an approved routine', async () => {
  mocked.updateHankRoutine.mockResolvedValueOnce({
    ...routine,
    title: 'Revised job start',
    status: 'draft',
    version: 3,
    approved_at: null,
    approved_by: null,
  });
  setup();
  fireEvent.click(await screen.findByRole('button', { name: /Job start approved/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit routine' }));
  fireEvent.change(screen.getByLabelText(/Routine title/), { target: { value: 'Revised job start' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save routine draft' }));
  await screen.findByRole('heading', { name: 'Revised job start' });
  expect(mocked.updateHankRoutine).toHaveBeenCalledWith(
    4,
    expect.objectContaining({ expected_company_id: 4, expected_version: 2, title: 'Revised job start' }),
    expect.any(AbortSignal)
  );
  expect(screen.queryByRole('button', { name: 'Start approved routine' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Approve this version' })).toBeInTheDocument();
});

it('freezes a newly authored draft until its uncertain save is recovered with the same key', async () => {
  mocked.createHankRoutine
    .mockRejectedValueOnce(new Error('lost'))
    .mockResolvedValueOnce({ ...routine, title: 'Receiving packet', status: 'draft', version: 1 });
  const { onBusyChange } = setup();
  fireEvent.click(await screen.findByText('Prepare a routine draft'));
  fireEvent.click(screen.getByRole('button', { name: 'Use Receiving packet template' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save routine draft' }));
  await screen.findByText(/request was not confirmed/);
  expect(screen.getByRole('button', { name: 'Routine library' })).toBeDisabled();
  expect(screen.getByLabelText(/Routine title/)).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: 'Retry same routine draft' }));
  await screen.findByRole('heading', { name: 'Receiving packet' });
  expect(mocked.createHankRoutine.mock.calls[1][0]).toEqual(mocked.createHankRoutine.mock.calls[0][0]);
  await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
});
