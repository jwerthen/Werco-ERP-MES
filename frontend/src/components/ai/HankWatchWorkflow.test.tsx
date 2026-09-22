import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankTask } from '../../types/hankTasks';
import type EntityPicker from '../operations/EntityPicker';
import { formatCentralDateTime } from '../../utils/centralTime';
import { HankWatchWorkflow } from './HankWatchWorkflow';
import { DEFAULT_HANK_PREFERENCES } from '../../types/hankPreferences';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankCapabilities: jest.fn(),
    getHankPreferences: jest.fn(),
    getDocumentTypes: jest.fn(),
    getHankTask: jest.fn(),
    createHankWatch: jest.fn(),
    checkHankWatch: jest.fn(),
    snoozeHankWatch: jest.fn(),
    resumeHankWatch: jest.fn(),
    cancelHankWatch: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, onChange, disabled }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="">Select a record</option>
      <option value="7">WO-1007 · Brackets</option>
      <option value="8">WO-1008 · Plates</option>
    </select>
  ),
}));
const mockedApi = jest.mocked(api);
const UUID = '11111111-1111-4111-8111-111111111111';
const watching: HankTask = {
  id: 41,
  company_id: 4,
  kind: 'watch_work_order',
  title: 'Follow WO-1007',
  status: 'watching',
  version: 1,
  input: { work_order_id: 7, condition: 'blockers_cleared', document_type: null },
  preview: {
    summary: 'Follow active blockers on WO-1007.',
    changes: ['Wait until no active blockers remain.'],
    warnings: ['Cleared blockers do not prove job readiness.'],
    references: [{ type: 'work_order', id: 7, label: 'WO-1007', url: '/work-orders/7' }],
  },
  result: null,
  error_message: null,
  created_at: '2026-09-22T13:30:00Z',
  updated_at: '2026-09-22T13:30:00Z',
  completed_at: null,
  last_checked_at: null,
  snoozed_until: null,
};
const checked = { ...watching, last_checked_at: '2026-09-22T13:35:00Z' };
const snoozed: HankTask = { ...watching, version: 2, status: 'snoozed', snoozed_until: '2026-09-22T14:35:00Z' };
const completed: HankTask = {
  ...checked,
  status: 'completed',
  version: 2,
  completed_at: checked.last_checked_at,
  result: {
    summary: 'No active blockers remain on WO-1007.',
    warnings: ['Review the job before starting work.'],
    references: [{ type: 'work_order', id: 7, label: 'WO-1007 result', url: '/work-orders/7' }],
  },
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}
function setCompany(cid: number, ro = false) {
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub: '17', cid, ro, type: 'access' }))}.s`);
}
function responseError(status: number, detail: string) {
  return { isAxiosError: true, response: { status, data: { detail } } };
}
function renderWorkflow(initialTask?: HankTask) {
  const onNavigate = jest.fn();
  const onBusyChange = jest.fn();
  const onTaskChanged = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankWatchWorkflow
          initialTask={initialTask}
          onNavigate={onNavigate}
          onBusyChange={onBusyChange}
          onTaskChanged={onTaskChanged}
        />
      </MemoryRouter>
    ),
    onNavigate,
    onBusyChange,
    onTaskChanged,
  };
}
async function fillJob() {
  fireEvent.change(await screen.findByLabelText(/Work order to follow/), { target: { value: '7' } });
}
function start() {
  fireEvent.click(screen.getByRole('button', { name: 'Start follow-up' }));
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  setCompany(4);
  Object.defineProperty(crypto, 'randomUUID', { configurable: true, value: jest.fn(() => UUID) });
  mockedApi.getHankCapabilities.mockResolvedValue({
    company_id: 4,
    can_write: false,
    can_watch: true,
    allowed_kinds: [],
  });
  mockedApi.getHankPreferences.mockResolvedValue({
    company_id: 4,
    version: 0,
    preferences: DEFAULT_HANK_PREFERENCES,
    updated_at: null,
    can_edit: true,
  });
  mockedApi.getDocumentTypes.mockResolvedValue([
    { value: 'drawing', label: 'Drawing' },
    { value: 'certificate', label: 'Certificate' },
  ]);
  mockedApi.createHankWatch.mockResolvedValue(watching);
  mockedApi.getHankTask.mockResolvedValue(watching);
  mockedApi.checkHankWatch.mockResolvedValue(checked);
  mockedApi.snoozeHankWatch.mockResolvedValue(snoozed);
  mockedApi.resumeHankWatch.mockResolvedValue({ ...watching, version: 3 });
  mockedApi.cancelHankWatch.mockResolvedValue({ ...watching, status: 'cancelled', version: 4 });
});

describe('HankWatchWorkflow', () => {
  it('shows muted alerts without changing the saved watch and refreshes the current preference separately', async () => {
    mockedApi.getHankPreferences.mockResolvedValueOnce({
      company_id: 4,
      version: 1,
      preferences: { ...DEFAULT_HANK_PREFERENCES, follow_up_alerts: false },
      updated_at: null,
      can_edit: true,
    });
    renderWorkflow(watching);
    await screen.findByText(
      'Follow-up alerts are off in your current Hank preferences. Completed results stay in Tasks.'
    );
    expect(screen.queryByText(/enable private in-app alerts/)).not.toBeInTheDocument();
    expect(screen.getByText(watching.preview.summary)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh follow-up status' }));
    await screen.findByText('Your current Hank preferences enable private in-app alerts when a follow-up completes.');
    expect(mockedApi.createHankWatch).not.toHaveBeenCalled();
    expect(mockedApi.resumeHankWatch).not.toHaveBeenCalled();
    expect(mockedApi.checkHankWatch).not.toHaveBeenCalled();
  });

  it('uses neutral alert wording when preferences fail and still allows saving a follow-up', async () => {
    mockedApi.getHankPreferences.mockRejectedValue(new Error('Unavailable'));
    renderWorkflow();
    await fillJob();
    await screen.findByRole('button', { name: 'Retry alert preference' });
    expect(screen.getByText('In-app alerts follow your Hank preferences; results stay in Tasks.')).toBeInTheDocument();
    expect(screen.queryByText(/enable private in-app alerts/)).not.toBeInTheDocument();
    start();
    await screen.findByText(watching.preview.summary);
    expect(mockedApi.createHankWatch).toHaveBeenCalledTimes(1);
  });

  it('shows the exact condition and starts only after explicit submission, independently of action write access', async () => {
    const pending = deferred<HankTask>();
    mockedApi.createHankWatch.mockReturnValue(pending.promise);
    const { onTaskChanged, onBusyChange } = renderWorkflow();
    await fillJob();
    expect(screen.getByText('Follow the selected work order until no active blockers remain.')).toBeInTheDocument();
    expect(screen.getByText('Cleared blockers do not establish that the job is ready to run.')).toBeInTheDocument();
    expect(mockedApi.createHankWatch).not.toHaveBeenCalled();
    start();
    await waitFor(() =>
      expect(mockedApi.createHankWatch).toHaveBeenCalledWith(
        {
          expected_company_id: 4,
          request_key: UUID,
          work_order_id: 7,
          condition: 'blockers_cleared',
          document_type: null,
        },
        expect.any(AbortSignal)
      )
    );
    expect(screen.queryByText(watching.preview.summary)).not.toBeInTheDocument();
    expect(onBusyChange).toHaveBeenCalledWith(true);
    await act(async () => pending.resolve(watching));
    expect(screen.getByText(watching.preview.summary)).toBeInTheDocument();
    expect(screen.getByText('Waiting for first check')).toBeInTheDocument();
    expect(onTaskChanged).toHaveBeenLastCalledWith(watching);
    expect(onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it('submits a selected PDF type and distinguishes attachment arrival from approval', async () => {
    renderWorkflow();
    await fillJob();
    fireEvent.change(screen.getByLabelText(/Follow until/), { target: { value: 'pdf_attached' } });
    await screen.findByRole('option', { name: 'Drawing' });
    fireEvent.change(screen.getByLabelText('PDF document type'), { target: { value: 'drawing' } });
    expect(
      screen.getByText('Follow the selected work order until a new PDF of type Drawing is attached.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('A matching attachment does not confirm its contents, approval, or release.')
    ).toBeInTheDocument();
    start();
    await waitFor(() =>
      expect(mockedApi.createHankWatch).toHaveBeenCalledWith(
        expect.objectContaining({ condition: 'pdf_attached', document_type: 'drawing' }),
        expect.any(AbortSignal)
      )
    );
  });

  it('recovers failed optional document choices and submits any PDF with an explicit null type', async () => {
    mockedApi.getDocumentTypes.mockRejectedValueOnce(new Error('Unavailable'));
    renderWorkflow();
    await fillJob();
    fireEvent.change(screen.getByLabelText(/Follow until/), { target: { value: 'pdf_attached' } });
    await screen.findByText(/Document types could not be loaded/);
    fireEvent.click(screen.getByRole('button', { name: 'retry document types' }));
    await screen.findByRole('option', { name: 'Drawing' });
    start();
    await waitFor(() =>
      expect(mockedApi.createHankWatch).toHaveBeenCalledWith(
        expect.objectContaining({ condition: 'pdf_attached', document_type: null }),
        expect.any(AbortSignal)
      )
    );
  });

  it('gates creation and all write controls by capabilities and read-only session claims', async () => {
    mockedApi.getHankCapabilities.mockResolvedValue({
      company_id: 4,
      can_write: true,
      can_watch: false,
      allowed_kinds: [],
    });
    const first = renderWorkflow();
    await screen.findByText(/Follow-ups require an interactive session/);
    expect(screen.queryByRole('button', { name: 'Start follow-up' })).not.toBeInTheDocument();
    first.unmount();
    setCompany(4, true);
    mockedApi.getHankCapabilities.mockResolvedValue({
      company_id: 4,
      can_write: true,
      can_watch: true,
      allowed_kinds: [],
    });
    renderWorkflow(watching);
    await screen.findByText(watching.preview.summary);
    expect(screen.queryByRole('button', { name: 'Check now' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop follow-up' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh follow-up status' })).toBeEnabled();
    expect(mockedApi.createHankWatch).not.toHaveBeenCalled();
    expect(mockedApi.checkHankWatch).not.toHaveBeenCalled();
  });

  it('recovers capability loading and validates the selected job before creating', async () => {
    mockedApi.getHankCapabilities.mockRejectedValueOnce(new Error('Unavailable'));
    renderWorkflow();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry follow-up access' }));
    await screen.findByRole('button', { name: 'Start follow-up' });
    start();
    await screen.findByText('Select a work order.');
    expect(mockedApi.createHankWatch).not.toHaveBeenCalled();
  });

  it('retains the identical request UUID and frozen inputs after an uncertain creation', async () => {
    mockedApi.createHankWatch.mockRejectedValueOnce(new Error('Lost response'));
    renderWorkflow();
    await fillJob();
    start();
    const retry = await screen.findByRole('button', { name: 'Retry starting follow-up' });
    await waitFor(() => expect(retry).toBeEnabled());
    const body = mockedApi.createHankWatch.mock.calls[0][0];
    expect(screen.getByLabelText(/Work order to follow/)).toBeDisabled();
    expect(screen.getByLabelText(/Follow until/)).toBeDisabled();
    fireEvent.click(retry);
    await screen.findByText(watching.preview.summary);
    expect(mockedApi.createHankWatch.mock.calls[1][0]).toEqual(body);
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it('allows correcting a confirmed refusal with a new request key', async () => {
    jest
      .mocked(crypto.randomUUID)
      .mockReturnValueOnce(UUID)
      .mockReturnValueOnce('22222222-2222-4222-8222-222222222222');
    mockedApi.createHankWatch.mockRejectedValueOnce(responseError(422, 'Job is no longer active.'));
    renderWorkflow();
    await fillJob();
    start();
    await screen.findByText('Job is no longer active.');
    expect(screen.getByLabelText(/Work order to follow/)).toBeEnabled();
    fireEvent.change(screen.getByLabelText(/Work order to follow/), { target: { value: '8' } });
    start();
    await screen.findByText(watching.preview.summary);
    expect(mockedApi.createHankWatch.mock.calls[1][0].work_order_id).toBe(8);
    expect(mockedApi.createHankWatch.mock.calls[1][0].request_key).not.toBe(UUID);
  });

  it('checks without inventing a completion and shows the actual Central check timestamp', async () => {
    renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    await screen.findByText('Check complete. The condition is not yet met.');
    expect(screen.getByText(`Last checked ${formatCentralDateTime(checked.last_checked_at)}`)).toBeInTheDocument();
    expect(screen.queryByText('Waiting for first check')).not.toBeInTheDocument();
    expect(mockedApi.checkHankWatch).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 1 },
      expect.any(AbortSignal)
    );
    expect(screen.queryByText('Follow-up result')).not.toBeInTheDocument();
  });

  it('uses server versions to snooze, resume, and stop and waits for each receipt', async () => {
    const stopped = deferred<HankTask>();
    mockedApi.cancelHankWatch.mockReturnValue(stopped.promise);
    renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Snooze 1 hour' }));
    await screen.findByText(`Snoozed until ${formatCentralDateTime(snoozed.snoozed_until!)}`);
    expect(screen.queryByRole('button', { name: 'Check now' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await screen.findByText(/Follow-up resumed/);
    expect(mockedApi.resumeHankWatch).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 2 },
      expect.any(AbortSignal)
    );
    fireEvent.click(screen.getByRole('button', { name: 'Stop follow-up' }));
    expect(mockedApi.cancelHankWatch).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 3 },
      expect.any(AbortSignal)
    );
    expect(screen.queryByText(/cancelled · Updated/)).not.toBeInTheDocument();
    await act(async () => stopped.resolve({ ...watching, status: 'cancelled', version: 4 }));
    expect(screen.getByText(/cancelled · Updated/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Stop follow-up' })).not.toBeInTheDocument();
  });

  it('renders the saved completed result with its warnings and source link', async () => {
    mockedApi.checkHankWatch.mockResolvedValue(completed);
    const { onNavigate } = renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    await screen.findByText(completed.result!.summary);
    expect(screen.getByText('Review the job before starting work.')).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'WO-1007 result' });
    expect(link).toHaveAttribute('href', '/work-orders/7');
    fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Check now' })).not.toBeInTheDocument();
  });

  it('requires refresh after an uncertain command, recovering a completed result without checking again', async () => {
    mockedApi.checkHankWatch.mockRejectedValueOnce(new Error('Lost response'));
    mockedApi.getHankTask.mockResolvedValue(completed);
    renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    await screen.findByText(/Refresh follow-up status before taking another action/);
    expect(screen.getByRole('button', { name: 'Check now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop follow-up' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh follow-up status' }));
    await screen.findByText(completed.result!.summary);
    expect(mockedApi.checkHankWatch).toHaveBeenCalledTimes(1);
    expect(mockedApi.getHankTask).toHaveBeenCalledWith(41, expect.any(AbortSignal));
  });

  it('recovers a stale version before resuming a needs-attention follow-up', async () => {
    mockedApi.snoozeHankWatch.mockRejectedValueOnce(responseError(409, 'Follow-up changed.'));
    mockedApi.getHankTask.mockResolvedValue({
      ...watching,
      version: 5,
      status: 'needs_attention',
      error_message: 'Job was unavailable during a check.',
    });
    renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Snooze 1 hour' }));
    await screen.findByText('Follow-up changed.');
    expect(screen.getByRole('button', { name: 'Check now' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh follow-up status' }));
    await screen.findByText('Job was unavailable during a check.');
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await screen.findByText(/Follow-up resumed/);
    expect(mockedApi.resumeHankWatch).toHaveBeenCalledWith(
      41,
      { expected_company_id: 4, expected_version: 5 },
      expect.any(AbortSignal)
    );
  });

  it('aborts writes synchronously on company change, removes old data, and ignores late completion', async () => {
    const pending = deferred<HankTask>();
    mockedApi.checkHankWatch.mockReturnValue(pending.promise);
    const { onTaskChanged } = renderWorkflow(watching);
    fireEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    const signal = mockedApi.checkHankWatch.mock.calls[0][2];
    setCompany(5);
    act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole('alert')).toHaveTextContent('Your session changed');
    expect(screen.queryByText(watching.preview.summary)).not.toBeInTheDocument();
    await act(async () => pending.resolve(completed));
    expect(onTaskChanged).not.toHaveBeenCalled();
  });

  it('aborts creation on unmount and rejects any late receipt', async () => {
    const pending = deferred<HankTask>();
    mockedApi.createHankWatch.mockReturnValue(pending.promise);
    const { unmount, onTaskChanged } = renderWorkflow();
    await fillJob();
    start();
    await waitFor(() => expect(mockedApi.createHankWatch).toHaveBeenCalledTimes(1));
    const signal = mockedApi.createHankWatch.mock.calls[0][1];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(watching));
    expect(onTaskChanged).not.toHaveBeenCalled();
  });

  it('never displays a foreign-company follow-up passed into the workflow', async () => {
    renderWorkflow({ ...watching, company_id: 5 });
    expect(await screen.findByRole('alert')).toHaveTextContent('does not belong to this workspace');
    expect(screen.queryByText(watching.preview.summary)).not.toBeInTheDocument();
  });
});
