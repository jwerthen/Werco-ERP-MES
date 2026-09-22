import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankTask, HankTaskList } from '../../types/hankTasks';
import type { HankTaskWorkflowProps } from './HankTaskWorkflow';
import type { HankWatchWorkflowProps } from './HankWatchWorkflow';
import { HankTaskWorkspace } from './HankTaskWorkspace';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getHankTask: jest.fn(), getHankTasks: jest.fn() },
}));
jest.mock('./HankTaskWorkflow', () => ({
  HankTaskWorkflow: ({ initialTask, onNavigate, onBusyChange, onTaskChanged }: HankTaskWorkflowProps) => (
    <div>
      <p>{initialTask ? `Loaded task: ${initialTask.title}` : 'New task form'}</p>
      <button type="button" onClick={onNavigate}>
        Open record
      </button>
      <button type="button" onClick={() => onBusyChange?.(true)}>
        Start write
      </button>
      <button type="button" onClick={() => onBusyChange?.(false)}>
        Finish write
      </button>
      {initialTask && (
        <button
          type="button"
          onClick={() => {
            onTaskChanged?.({
              ...initialTask,
              status: 'completed',
              version: initialTask.version + 1,
              result: {
                summary: 'Document attached.',
                warnings: [],
                references: [{ type: 'document', id: 41, label: 'DOC-41', url: '/documents?document=41' }],
              },
            });
            onBusyChange?.(false);
          }}
        >
          Receive completion
        </button>
      )}
    </div>
  ),
}));
const mockedApi = jest.mocked(api);
jest.mock('./HankWatchWorkflow', () => ({
  HankWatchWorkflow: ({ initialTask, onBusyChange, onTaskChanged }: HankWatchWorkflowProps) => (
    <div>
      <p>{initialTask ? `Loaded follow-up: ${initialTask.title}` : 'New follow-up form'}</p>
      <button type="button" onClick={() => onBusyChange?.(true)}>
        Start follow-up write
      </button>
      {initialTask && (
        <button
          type="button"
          onClick={() => {
            onTaskChanged?.({ ...initialTask, status: 'snoozed' });
            onBusyChange?.(false);
          }}
        >
          Receive snoozed follow-up
        </button>
      )}
    </div>
  ),
}));
const task: HankTask = {
  id: 41,
  company_id: 4,
  kind: 'attach_document',
  title: 'Attach DOC-41 to WO-7',
  status: 'awaiting_review',
  version: 1,
  input: { document_id: 41, work_order_id: 7 },
  preview: { summary: 'Review document attachment.', changes: [], warnings: [], references: [] },
  result: null,
  error_message: null,
  created_at: '2026-09-22T13:30:00Z',
  updated_at: '2026-09-22T13:30:00Z',
  completed_at: null,
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}
function setCompany(cid: number) {
  sessionStorage.setItem('token', `h.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.s`);
}
function renderWorkspace(taskId?: number) {
  const onNavigate = jest.fn();
  const onBusyChange = jest.fn();
  return {
    ...render(
      <MemoryRouter>
        <HankTaskWorkspace taskId={taskId} onNavigate={onNavigate} onBusyChange={onBusyChange} />
      </MemoryRouter>
    ),
    onNavigate,
    onBusyChange,
  };
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  setCompany(4);
  mockedApi.getHankTask.mockResolvedValue(task);
  mockedApi.getHankTasks.mockResolvedValue({ tasks: [task], has_more: false, next_before_id: null });
});

describe('HankTaskWorkspace', () => {
  it('starts an explicit follow-up and blocks navigation while its write is pending', async () => {
    renderWorkspace();
    await screen.findByRole('button', { name: new RegExp(task.title) });
    fireEvent.click(screen.getByRole('button', { name: 'New follow-up' }));
    expect(screen.getByText('New follow-up form')).toBeInTheDocument();
    expect(screen.queryByText('New task form')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Start follow-up write' }));
    expect(screen.getByRole('button', { name: 'New task' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'New follow-up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Back to task inbox' })).toBeDisabled();
  });

  it('opens a watch deep link with follow-up controls and refreshes the inbox after a command', async () => {
    const watch: HankTask = { ...task, kind: 'watch_work_order', status: 'watching', title: 'Follow WO-7' };
    mockedApi.getHankTask.mockResolvedValue(watch);
    mockedApi.getHankTasks.mockResolvedValue({
      tasks: [{ ...watch, status: 'snoozed' }],
      has_more: false,
      next_before_id: null,
    });
    renderWorkspace(41);
    await screen.findByText('Loaded follow-up: Follow WO-7');
    expect(screen.queryByText('New task form')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Receive snoozed follow-up' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back to task inbox' }));
    expect(await screen.findByRole('button', { name: /Follow WO-7 snoozed/ })).toBeInTheDocument();
    expect(screen.getByText('Waiting for first check')).toBeInTheDocument();
  });

  it('sends Watching and Snoozed filters to the server and describes a stopped unchecked follow-up truthfully', async () => {
    mockedApi.getHankTasks.mockResolvedValue({ tasks: [], has_more: false, next_before_id: null });
    renderWorkspace();
    await screen.findByText(/No saved tasks yet/);
    fireEvent.change(screen.getByLabelText('Task status'), { target: { value: 'watching' } });
    await screen.findByText('No watching tasks.');
    expect(mockedApi.getHankTasks).toHaveBeenLastCalledWith({ limit: 20, status: 'watching' }, expect.any(AbortSignal));
    fireEvent.change(screen.getByLabelText('Task status'), { target: { value: 'snoozed' } });
    await screen.findByText('No snoozed tasks.');
    expect(mockedApi.getHankTasks).toHaveBeenLastCalledWith({ limit: 20, status: 'snoozed' }, expect.any(AbortSignal));
    mockedApi.getHankTasks.mockResolvedValue({
      tasks: [{ ...task, kind: 'watch_work_order', status: 'cancelled' }],
      has_more: false,
      next_before_id: null,
    });
    fireEvent.change(screen.getByLabelText('Task status'), { target: { value: 'cancelled' } });
    await screen.findByText('Not checked');
    expect(screen.queryByText('Waiting for first check')).not.toBeInTheDocument();
  });

  it('shows saved tasks first and starts a new workflow only when selected', async () => {
    renderWorkspace();
    expect(await screen.findByRole('button', { name: new RegExp(task.title) })).toBeInTheDocument();
    expect(screen.queryByText('New task form')).not.toBeInTheDocument();
    expect(mockedApi.getHankTasks).toHaveBeenCalledWith({ limit: 20 }, expect.any(AbortSignal));
    fireEvent.click(screen.getByRole('button', { name: 'New task' }));
    expect(screen.getByText('New task form')).toBeInTheDocument();
    expect(mockedApi.getHankTask).not.toHaveBeenCalled();
  });

  it('loads a task deep link before rendering the workflow and forwards interaction callbacks', async () => {
    const pending = deferred<HankTask>();
    mockedApi.getHankTask.mockReturnValue(pending.promise);
    const { onNavigate, onBusyChange } = renderWorkspace(41);
    expect(screen.getByRole('status')).toHaveTextContent('Loading your task');
    expect(screen.queryByText('New task form')).not.toBeInTheDocument();
    expect(mockedApi.getHankTask).toHaveBeenCalledWith(41, expect.any(AbortSignal));
    expect(mockedApi.getHankTasks).not.toHaveBeenCalled();
    await act(async () => pending.resolve(task));
    expect(screen.getByText(`Loaded task: ${task.title}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open record' }));
    fireEvent.click(screen.getByRole('button', { name: 'Start write' }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onBusyChange).toHaveBeenCalledWith(true);
  });

  it('keeps a failed deep-link load visible and retries the same task', async () => {
    mockedApi.getHankTask.mockRejectedValueOnce(new Error('Unavailable'));
    renderWorkspace(41);
    expect(await screen.findByRole('alert')).toHaveTextContent('This task could not be loaded');
    expect(screen.queryByText('New task form')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry task' }));
    await screen.findByText(`Loaded task: ${task.title}`);
    expect(mockedApi.getHankTask.mock.calls.map(call => call[0])).toEqual([41, 41]);
  });

  it('aborts the previous task load and ignores its late response when the selected task changes', async () => {
    const old = deferred<HankTask>();
    mockedApi.getHankTask.mockReturnValueOnce(old.promise);
    const { rerender, onNavigate, onBusyChange } = renderWorkspace(41);
    const signal = mockedApi.getHankTask.mock.calls[0][1];
    mockedApi.getHankTask.mockResolvedValueOnce({ ...task, id: 42, title: 'Current task' });
    rerender(
      <MemoryRouter>
        <HankTaskWorkspace taskId={42} onNavigate={onNavigate} onBusyChange={onBusyChange} />
      </MemoryRouter>
    );
    expect(signal?.aborted).toBe(true);
    await screen.findByText('Loaded task: Current task');
    await act(async () => old.resolve(task));
    expect(screen.queryByText(`Loaded task: ${task.title}`)).not.toBeInTheDocument();
    expect(screen.getByText('Loaded task: Current task')).toBeInTheDocument();
  });

  it('suppresses data returned after the active company changes', async () => {
    const old = deferred<HankTask>();
    mockedApi.getHankTask.mockReturnValue(old.promise);
    renderWorkspace(41);
    setCompany(5);
    await act(async () => old.resolve(task));
    expect(screen.queryByText(`Loaded task: ${task.title}`)).not.toBeInTheDocument();
  });

  it('aborts an unresolved deep link when unmounted', () => {
    mockedApi.getHankTask.mockReturnValue(new Promise(() => undefined));
    const { unmount } = renderWorkspace(41);
    const signal = mockedApi.getHankTask.mock.calls[0][1];
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('uses the backend status filter and resets pagination when the filter changes', async () => {
    mockedApi.getHankTasks.mockResolvedValueOnce({ tasks: [task], has_more: true, next_before_id: 41 });
    renderWorkspace();
    await screen.findByRole('button', { name: new RegExp(task.title) });
    mockedApi.getHankTasks.mockResolvedValueOnce({
      tasks: [{ ...task, id: 37, title: 'Saved completed task', status: 'completed' }],
      has_more: true,
      next_before_id: 37,
    });
    fireEvent.change(screen.getByLabelText('Task status'), { target: { value: 'completed' } });
    expect(screen.queryByRole('button', { name: new RegExp(task.title) })).not.toBeInTheDocument();
    await screen.findByRole('button', { name: /Saved completed task/ });
    expect(mockedApi.getHankTasks).toHaveBeenLastCalledWith(
      { limit: 20, status: 'completed' },
      expect.any(AbortSignal)
    );
    mockedApi.getHankTasks.mockResolvedValueOnce({
      tasks: [{ ...task, id: 31, title: 'Older completed task', status: 'completed' }],
      has_more: false,
      next_before_id: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Load older tasks' }));
    await screen.findByRole('button', { name: /Older completed task/ });
    expect(mockedApi.getHankTasks).toHaveBeenLastCalledWith(
      { limit: 20, status: 'completed', before_id: 37 },
      expect.any(AbortSignal)
    );
    expect(screen.getByRole('button', { name: /Saved completed task/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load older tasks' })).not.toBeInTheDocument();
    expect(screen.getByText(/Showing 2 loaded tasks/)).toBeInTheDocument();
  });

  it('ignores a late page from the previous filter', async () => {
    const old = deferred<HankTaskList>();
    mockedApi.getHankTasks.mockReturnValueOnce(old.promise);
    renderWorkspace();
    const signal = mockedApi.getHankTasks.mock.calls[0][1];
    mockedApi.getHankTasks.mockResolvedValueOnce({ tasks: [], has_more: false, next_before_id: null });
    fireEvent.change(screen.getByLabelText('Task status'), { target: { value: 'cancelled' } });
    expect(signal?.aborted).toBe(true);
    await screen.findByText('No cancelled tasks.');
    await act(async () => old.resolve({ tasks: [task], has_more: true, next_before_id: 41 }));
    expect(screen.queryByRole('button', { name: new RegExp(task.title) })).not.toBeInTheDocument();
    expect(screen.getByText('No cancelled tasks.')).toBeInTheDocument();
  });

  it('keeps previously loaded tasks and retries the same cursor after a paging error', async () => {
    mockedApi.getHankTasks.mockResolvedValueOnce({ tasks: [task], has_more: true, next_before_id: 41 });
    renderWorkspace();
    await screen.findByRole('button', { name: new RegExp(task.title) });
    mockedApi.getHankTasks.mockRejectedValueOnce(new Error('Offline'));
    fireEvent.click(screen.getByRole('button', { name: 'Load older tasks' }));
    await screen.findByText('Older tasks could not be loaded.');
    expect(screen.getByRole('button', { name: new RegExp(task.title) })).toBeInTheDocument();
    mockedApi.getHankTasks.mockResolvedValueOnce({
      tasks: [task, { ...task, id: 40, title: 'Earlier proposal' }],
      has_more: false,
      next_before_id: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading tasks' }));
    await screen.findByRole('button', { name: /Earlier proposal/ });
    expect(mockedApi.getHankTasks.mock.calls.slice(1).map(call => call[0])).toEqual([
      { limit: 20, before_id: 41 },
      { limit: 20, before_id: 41 },
    ]);
    expect(screen.getAllByRole('button', { name: new RegExp(task.title) })).toHaveLength(1);
    expect(screen.getByText(/Showing 2 loaded tasks/)).toBeInTheDocument();
  });

  it('recovers the initial inbox request without pretending it is empty', async () => {
    mockedApi.getHankTasks.mockRejectedValueOnce(new Error('Unavailable'));
    renderWorkspace();
    await screen.findByText('Your task inbox could not be loaded.');
    expect(screen.queryByText(/No saved tasks yet/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading tasks' }));
    await screen.findByRole('button', { name: new RegExp(task.title) });
    expect(mockedApi.getHankTasks).toHaveBeenCalledTimes(2);
  });

  it('loads a fresh task detail from an inbox row and refreshes saved receipts when returning', async () => {
    const { onNavigate } = renderWorkspace();
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(task.title) }));
    await screen.findByText(`Loaded task: ${task.title}`);
    expect(mockedApi.getHankTask).toHaveBeenCalledWith(41, expect.any(AbortSignal));
    fireEvent.click(screen.getByRole('button', { name: 'Receive completion' }));
    mockedApi.getHankTasks.mockResolvedValueOnce({
      tasks: [
        {
          ...task,
          status: 'completed',
          version: 2,
          result: {
            summary: 'Document attached.',
            warnings: [],
            references: [{ type: 'document', id: 41, label: 'DOC-41', url: '/documents?document=41' }],
          },
        },
      ],
      has_more: false,
      next_before_id: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Back to task inbox' }));
    const result = await screen.findByRole('link', { name: 'DOC-41' });
    expect(mockedApi.getHankTasks).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Document attached.')).toBeInTheDocument();
    expect(result).toHaveAttribute('href', '/documents?document=41');
    fireEvent.click(result);
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it('locks navigation while a write is pending and defers a changed deep link until it finishes', async () => {
    const { rerender, onNavigate, onBusyChange } = renderWorkspace(41);
    await screen.findByText(`Loaded task: ${task.title}`);
    fireEvent.click(screen.getByRole('button', { name: 'Start write' }));
    expect(screen.getByRole('button', { name: 'New task' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Back to task inbox' })).toBeDisabled();
    mockedApi.getHankTask.mockResolvedValueOnce({ ...task, id: 42, title: 'Next selected task' });
    rerender(
      <MemoryRouter>
        <HankTaskWorkspace taskId={42} onNavigate={onNavigate} onBusyChange={onBusyChange} />
      </MemoryRouter>
    );
    expect(mockedApi.getHankTask).toHaveBeenCalledTimes(1);
    expect(screen.getByText(`Loaded task: ${task.title}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Finish write' }));
    await screen.findByText('Loaded task: Next selected task');
    expect(mockedApi.getHankTask).toHaveBeenLastCalledWith(42, expect.any(AbortSignal));
  });

  it('synchronously aborts old-company inbox requests and clears previously visible records', async () => {
    renderWorkspace();
    await screen.findByRole('button', { name: new RegExp(task.title) });
    const pending = deferred<HankTaskList>();
    mockedApi.getHankTasks.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh inbox' }));
    const signal = mockedApi.getHankTasks.mock.calls[1][1];
    expect(signal?.aborted).toBe(false);
    setCompany(5);
    act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole('alert')).toHaveTextContent('Your session changed');
    expect(screen.queryByRole('button', { name: new RegExp(task.title) })).not.toBeInTheDocument();
    await act(async () => pending.resolve({ tasks: [task], has_more: false, next_before_id: null }));
    expect(screen.queryByRole('button', { name: new RegExp(task.title) })).not.toBeInTheDocument();
  });
});
