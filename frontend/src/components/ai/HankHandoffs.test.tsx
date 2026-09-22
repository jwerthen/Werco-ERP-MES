import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankHandoff } from '../../types/hankWork';
import type EntityPicker from '../operations/EntityPicker';
import { HankHandoffs } from './HankHandoffs';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    getHankHandoffPeople: jest.fn(),
    getHankHandoffs: jest.fn(),
    getHankHandoff: jest.fn(),
    createHankHandoff: jest.fn(),
    commandHankHandoff: jest.fn(),
    attachHankHandoffPhoto: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">Choose job</option>
      <option value="7">WO-7</option>
    </select>
  ),
}));
jest.mock('./HankDocumentPicker', () => ({ HankDocumentPicker: () => <p>Document choices</p> }));
jest.mock('./HankSourceFile', () => ({ HankSourceFile: ({ filename }: { filename: string }) => <p>{filename}</p> }));
const mocked = jest.mocked(api);
const handoff: HankHandoff = {
  id: 12,
  company_id: 4,
  version: 1,
  status: 'open',
  work_order_id: 7,
  work_order_number: 'WO-7',
  sender: { id: 17, name: 'Alex' },
  recipient: { id: 22, name: 'Morgan' },
  summary: 'Finish inspection',
  completed_work: 'Machining complete',
  remaining_work: 'Inspect remaining pieces',
  problems: '',
  quantity_remaining: 3,
  document_ids: [],
  document_references: [],
  attachments: [],
  created_at: '2026-09-22T14:00:00Z',
  updated_at: '2026-09-22T14:00:00Z',
  can_acknowledge: true,
  can_complete: false,
  can_cancel: true,
};
const scope = (cid: number) =>
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub: '17', cid, ro: false, type: 'access' }))}.s`);
const conflict = { isAxiosError: true, response: { status: 409, data: { detail: 'Saved handoff changed.' } } };
function setup(initialId?: number) {
  const onBusyChange = jest.fn();
  const view = render(
    <MemoryRouter>
      <HankHandoffs initialId={initialId} workOrderId={7} onNavigate={jest.fn()} onBusyChange={onBusyChange} />
    </MemoryRouter>
  );
  return { ...view, onBusyChange };
}
async function fill() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'New handoff' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'New handoff' }));
  fireEvent.change(screen.getByLabelText(/Hand off to/), { target: { value: '22' } });
  fireEvent.change(screen.getByLabelText(/^summary/), { target: { value: 'Finish inspection' } });
}
beforeEach(() => {
  jest.resetAllMocks();
  scope(4);
  mocked.getHankCapabilities.mockResolvedValue({ company_id: 4, can_watch: true, can_write: true, allowed_kinds: [] });
  mocked.getHankHandoffPeople.mockResolvedValue({ people: [{ id: 22, name: 'Morgan', role: 'operator' }] });
  mocked.getHankHandoffs.mockResolvedValue({ handoffs: [], has_more: false, next_before_id: null });
  mocked.getHankHandoff.mockResolvedValue(handoff);
  mocked.createHankHandoff.mockResolvedValue(handoff);
});

it('sends only the explicit recipient and preserves the confirmed receipt without a redundant read', async () => {
  setup();
  await fill();
  expect(mocked.createHankHandoff).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Send handoff' }));
  await screen.findByRole('heading', { name: 'Finish inspection' });
  expect(mocked.createHankHandoff).toHaveBeenCalledWith(
    expect.objectContaining({
      expected_company_id: 4,
      recipient_id: 22,
      work_order_id: 7,
      summary: 'Finish inspection',
      document_ids: [],
    }),
    expect.any(AbortSignal)
  );
  expect(mocked.getHankHandoff).not.toHaveBeenCalled();
  expect(screen.getByRole('link', { name: 'WO-7' })).toHaveAttribute('href', '/work-orders/7');
});
it('locks navigation after uncertain create and retries the same UUID and content', async () => {
  mocked.createHankHandoff.mockRejectedValueOnce(new Error('lost response'));
  const { onBusyChange } = setup();
  await fill();
  fireEvent.click(screen.getByRole('button', { name: 'Send handoff' }));
  await screen.findByText(/request was not confirmed/);
  expect(screen.getByRole('button', { name: 'New handoff' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Your handoffs' })).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  fireEvent.click(screen.getByRole('button', { name: 'Retry same handoff' }));
  await screen.findByRole('heading', { name: 'Finish inspection' });
  expect(mocked.createHankHandoff.mock.calls[1][0]).toEqual(mocked.createHankHandoff.mock.calls[0][0]);
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
});
it('requires acknowledgement before completion and shows only the saved result', async () => {
  const acknowledged = {
    ...handoff,
    status: 'acknowledged' as const,
    version: 2,
    can_acknowledge: false,
    can_complete: true,
  };
  mocked.commandHankHandoff.mockResolvedValueOnce(acknowledged).mockResolvedValueOnce({
    ...acknowledged,
    status: 'completed',
    version: 3,
    can_complete: false,
    can_cancel: false,
  });
  setup(12);
  fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge handoff' }));
  await screen.findByRole('button', { name: 'Mark handoff finished' });
  expect(mocked.commandHankHandoff).toHaveBeenNthCalledWith(
    1,
    12,
    'acknowledge',
    { expected_company_id: 4, expected_version: 1 },
    expect.any(AbortSignal)
  );
  fireEvent.click(screen.getByRole('button', { name: 'Mark handoff finished' }));
  await screen.findByText(/Alex → Morgan · completed/);
  expect(mocked.commandHankHandoff).toHaveBeenNthCalledWith(
    2,
    12,
    'complete',
    { expected_company_id: 4, expected_version: 2 },
    expect.any(AbortSignal)
  );
});
it('clears a stale detail context when starting a new handoff', async () => {
  mocked.commandHankHandoff.mockRejectedValueOnce(conflict);
  setup(12);
  fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge handoff' }));
  await screen.findByText('Saved handoff changed.');
  expect(screen.getByRole('button', { name: 'Acknowledge handoff' })).toBeDisabled();
  await fill();
  fireEvent.click(screen.getByRole('button', { name: 'Send handoff' }));
  await waitFor(() => expect(mocked.createHankHandoff).toHaveBeenCalledTimes(1));
  await screen.findByRole('heading', { name: 'Finish inspection' });
});
it('recovers an uncertain photo with its original key and version after refreshing', async () => {
  mocked.attachHankHandoffPhoto.mockRejectedValueOnce(new Error('lost'));
  const withPhoto = {
    ...handoff,
    version: 2,
    attachments: [{ id: 'photo-key', filename: 'setup.png', url: '/source', mime_type: 'image/png' }],
  };
  mocked.attachHankHandoffPhoto.mockResolvedValueOnce(withPhoto);
  setup(12);
  await screen.findByRole('heading', { name: 'Finish inspection' });
  fireEvent.change(screen.getByLabelText('Add a handoff photo'), {
    target: { files: [new File(['photo'], 'setup.png', { type: 'image/png' })] },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Attach photo' }));
  await screen.findByText(/request was not confirmed/);
  expect(screen.getByRole('button', { name: 'New handoff' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Retry same photo' })).toBeDisabled();
  mocked.getHankHandoff.mockResolvedValueOnce({
    ...handoff,
    version: 2,
    status: 'completed',
    can_acknowledge: false,
    can_cancel: false,
  });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh handoff' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Retry same photo' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Retry same photo' }));
  await screen.findByText('setup.png');
  const first = mocked.attachHankHandoffPhoto.mock.calls[0][1];
  const second = mocked.attachHankHandoffPhoto.mock.calls[1][1];
  expect(second.get('request_key')).toBe(first.get('request_key'));
  expect(second.get('expected_version')).toBe('1');
});
it('aborts and suppresses a late create response when the company changes', async () => {
  let resolve!: (value: HankHandoff) => void;
  mocked.createHankHandoff.mockReturnValueOnce(
    new Promise(r => {
      resolve = r;
    })
  );
  setup();
  await fill();
  fireEvent.click(screen.getByRole('button', { name: 'Send handoff' }));
  await waitFor(() => expect(mocked.createHankHandoff).toHaveBeenCalled());
  const signal = mocked.createHankHandoff.mock.calls[0][1];
  act(() => {
    scope(9);
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    resolve(handoff);
  });
  expect(screen.queryByRole('heading', { name: 'Finish inspection' })).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});
it('disables creation in a read-only capability context', async () => {
  mocked.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_watch: false,
    can_write: false,
    allowed_kinds: [],
  });
  setup();
  await screen.findByText('No handoffs in this view.');
  expect(screen.getByRole('button', { name: 'New handoff' })).toBeDisabled();
});

it('ignores a second concurrent submit without replacing the uncertain request key', async () => {
  let reject!: (reason: Error) => void;
  mocked.createHankHandoff.mockReturnValueOnce(
    new Promise((_resolve, fail) => {
      reject = fail;
    })
  );
  setup();
  await fill();
  const form = screen.getByRole('form', { name: 'Create handoff' });
  fireEvent.submit(form);
  fireEvent.submit(form);
  await waitFor(() => expect(mocked.createHankHandoff).toHaveBeenCalledTimes(1));
  const sentBody = mocked.createHankHandoff.mock.calls[0][0];
  await act(async () => {
    reject(new Error('lost'));
  });
  fireEvent.click(screen.getByRole('button', { name: 'Retry same handoff' }));
  await screen.findByRole('heading', { name: 'Finish inspection' });
  expect(mocked.createHankHandoff.mock.calls[1][0]).toEqual(sentBody);
});
