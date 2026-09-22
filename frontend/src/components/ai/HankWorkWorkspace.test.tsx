import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankRoutineRun, HankWorkQueue } from '../../types/hankWork';
import type { HankEvidencePanel } from './HankEvidencePanel';
import type { HankOperationalTask } from './HankOperationalTask';
import type { HankRoutines } from './HankRoutines';
import type { HankJobScan } from './HankJobScan';
import { HankWorkWorkspace } from './HankWorkWorkspace';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getHankWorkQueue: jest.fn(), getWorkOrder: jest.fn(), getPurchaseOrder: jest.fn() },
}));
jest.mock('./HankEvidencePanel', () => ({
  HankEvidencePanel: ({
    kind,
    workOrderId,
    purchaseOrderId,
    onBusyChange,
  }: React.ComponentProps<typeof HankEvidencePanel>) => (
    <section aria-label="Evidence probe">
      <p>
        {kind} job {workOrderId || 'none'} PO {purchaseOrderId || 'none'}
      </p>
      <button onClick={() => onBusyChange?.(true)}>Begin evidence read</button>
      <button onClick={() => onBusyChange?.(false)}>Finish evidence read</button>
    </section>
  ),
}));
jest.mock('./HankOperationalTask', () => ({
  HankOperationalTask: ({
    kind,
    workOrderId,
    purchaseOrderId,
    operationId,
    onBusyChange,
  }: React.ComponentProps<typeof HankOperationalTask>) => (
    <section aria-label="Action probe">
      <p>
        {kind} job {workOrderId || 'none'} PO {purchaseOrderId || 'none'} operation {operationId || 'none'}
      </p>
      <button onClick={() => onBusyChange?.(true)}>Begin action</button>
      <button onClick={() => onBusyChange?.(false)}>Finish action</button>
    </section>
  ),
}));
jest.mock('./HankDocumentIntake', () => ({ HankDocumentIntake: () => <p>Intake workspace</p> }));
jest.mock('./HankHandoffs', () => ({ HankHandoffs: () => <p>Handoff workspace</p> }));
const mockRun: HankRoutineRun = {
  id: 19,
  company_id: 4,
  routine_id: 2,
  routine_version: 3,
  title: 'Receiving sequence',
  status: 'active',
  version: 1,
  current_step: 0,
  steps: [{ kind: 'receive_delivery', title: 'Receive', instruction: 'Inspect actual delivery' }],
  work_order_id: 8,
  purchase_order_id: 12,
  results: [],
  created_at: '2026-09-22T15:00:00Z',
  updated_at: '2026-09-22T15:00:00Z',
  completed_at: null,
  can_edit: true,
};
jest.mock('./HankRoutines', () => ({
  HankRoutines: ({ initialId, onOpenStep }: React.ComponentProps<typeof HankRoutines>) => (
    <section aria-label="Routine probe">
      <p>Routine {initialId || 'library'}</p>
      <button onClick={() => onOpenStep('receive_delivery', mockRun)}>Open receiving step</button>
    </section>
  ),
}));
jest.mock('./HankJobScan', () => ({
  HankJobScan: ({ onSelect }: React.ComponentProps<typeof HankJobScan>) => (
    <button onClick={() => onSelect({ workOrderId: 8, operationId: 81, label: 'WO-8 · Mill' })}>
      Scan WO-8 operation
    </button>
  ),
}));
const mocked = jest.mocked(api);
const queue: HankWorkQueue = {
  checked_at: '2026-09-22T15:00:00Z',
  truncated: true,
  items: [
    {
      key: 'handoff:12',
      kind: 'handoff',
      id: 12,
      title: 'Shift handoff',
      state: 'waiting_on_you',
      status: 'open',
      url: '/?hank_work=handoff&hank_id=12',
      updated_at: '2026-09-22T15:00:00Z',
    },
    {
      key: 'task:21',
      kind: 'task',
      id: 21,
      title: 'Watch job PDF',
      state: 'waiting_on_other',
      status: 'watching',
      url: '/?hank_task=21',
      updated_at: '2026-09-22T15:00:00Z',
    },
  ],
};
function scope(cid = 4) {
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.s`);
}
function show(props: Partial<React.ComponentProps<typeof HankWorkWorkspace>> = {}) {
  const onBusyChange = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankWorkWorkspace context={{}} onNavigate={jest.fn()} onBusyChange={onBusyChange} {...props} />
      </MemoryRouter>
    ),
    onBusyChange,
  };
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  scope();
  mocked.getHankWorkQueue.mockResolvedValue(queue);
  mocked.getWorkOrder.mockResolvedValue({ work_order_number: 'WO-7' });
  mocked.getPurchaseOrder.mockResolvedValue({ po_number: 'PO-11' });
});

it('shows actual saved state, exact links and the bounded coverage note', async () => {
  show();
  await screen.findByRole('link', { name: /Shift handoff/ });
  expect(screen.getByRole('link', { name: /Shift handoff/ })).toHaveAttribute('href', '/?hank_work=handoff&hank_id=12');
  expect(screen.getByRole('link', { name: /Watch job PDF/ })).toHaveTextContent('Waiting for a condition');
  expect(screen.getByText(/This view is limited/)).toBeInTheDocument();
  expect(mocked.getHankWorkQueue).toHaveBeenCalledWith(undefined, expect.any(AbortSignal));
});

it('requests an exact state filter and clears the previous result while loading it', async () => {
  let resolve!: (value: HankWorkQueue) => void;
  show();
  await screen.findByRole('link', { name: /Shift handoff/ });
  mocked.getHankWorkQueue.mockReturnValueOnce(
    new Promise(done => {
      resolve = done;
    })
  );
  fireEvent.change(screen.getByLabelText('Work state'), { target: { value: 'finished' } });
  await waitFor(() => expect(mocked.getHankWorkQueue).toHaveBeenLastCalledWith('finished', expect.any(AbortSignal)));
  expect(screen.queryByRole('link', { name: /Shift handoff/ })).not.toBeInTheDocument();
  await act(async () => resolve({ checked_at: queue.checked_at, items: [], truncated: false }));
  expect(screen.getByText('No saved work in this state.')).toBeInTheDocument();
  expect(screen.queryByText(/This view is limited/)).not.toBeInTheDocument();
});

it('recovers a failed queue read using the explicit refresh action', async () => {
  mocked.getHankWorkQueue.mockRejectedValueOnce(new Error('Offline'));
  show();
  await screen.findByRole('alert');
  expect(screen.getByRole('alert')).toHaveTextContent('work queue could not be loaded');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh queue' }));
  await screen.findByRole('link', { name: /Shift handoff/ });
  expect(mocked.getHankWorkQueue).toHaveBeenCalledTimes(2);
});

it('prefills the current purchase order and a scanned job replaces that unrelated context', async () => {
  show({ context: { purchaseOrderId: 11 } });
  await screen.findByText('For PO-11');
  fireEvent.click(screen.getByRole('button', { name: 'Receive this PO' }));
  expect(screen.getByText('receive_delivery job none PO 11 operation none')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Back to Work' }));
  fireEvent.click(screen.getByText('Use a traveler barcode'));
  fireEvent.click(screen.getByRole('button', { name: 'Scan WO-8 operation' }));
  await screen.findByText('For WO-8 · Mill');
  fireEvent.click(screen.getByRole('button', { name: 'Report this job' }));
  expect(screen.getByText('report_production job 8 PO none operation 81')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Back to Work' }));
  fireEvent.click(screen.getByRole('button', { name: 'Clear scanned job' }));
  await screen.findByText('For PO-11');
});

it('opens a routine step with its saved context and returns to the exact saved run', async () => {
  show({ context: { workOrderId: 7, purchaseOrderId: 11 } });
  await screen.findByText('For WO-7');
  fireEvent.click(screen.getByRole('button', { name: /Follow an approved routine/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Open receiving step' }));
  expect(screen.getByText('receive_delivery job 8 PO 12 operation none')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Return to routine' }));
  expect(screen.getByText('Routine 19')).toBeInTheDocument();
});

it('keeps a child mutation mounted while disabling back and routine-return navigation', async () => {
  const { onBusyChange } = show();
  fireEvent.click(screen.getByRole('button', { name: /Follow an approved routine/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Open receiving step' }));
  fireEvent.click(screen.getByRole('button', { name: 'Begin action' }));
  expect(screen.getByRole('button', { name: 'Back to Work' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Return to routine' })).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: 'Finish action' }));
  expect(screen.getByRole('button', { name: 'Return to routine' })).toBeEnabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
});

it('discards a pending source label after scanning a different job', async () => {
  let resolve!: (value: { work_order_number: string }) => void;
  mocked.getWorkOrder.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  show({ context: { workOrderId: 7 } });
  await waitFor(() => expect(mocked.getWorkOrder).toHaveBeenCalledTimes(1));
  const signal = mocked.getWorkOrder.mock.calls[0][1];
  fireEvent.click(screen.getByText('Use a traveler barcode'));
  fireEvent.click(screen.getByRole('button', { name: 'Scan WO-8 operation' }));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve({ work_order_number: 'OLD PRIVATE WO-7' }));
  expect(screen.queryByText(/OLD PRIVATE/)).not.toBeInTheDocument();
  expect(screen.getByText('For WO-8 · Mill')).toBeInTheDocument();
});

it('aborts and hides old-company queue results after a session change', async () => {
  let resolve!: (value: HankWorkQueue) => void;
  mocked.getHankWorkQueue.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  show();
  await waitFor(() => expect(mocked.getHankWorkQueue).toHaveBeenCalledTimes(1));
  const signal = mocked.getHankWorkQueue.mock.calls[0][1];
  scope(5);
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(queue));
  expect(screen.queryByRole('link', { name: /Shift handoff/ })).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});
