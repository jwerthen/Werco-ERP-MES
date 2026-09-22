import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import { CopilotPanel } from './CopilotPanel';
import type { HankTaskWorkspace } from './HankTaskWorkspace';
import type { HankPreferencesProps } from './HankPreferences';

jest.mock('../../services/api', () => ({ __esModule: true, default: { copilotChatStream: jest.fn() } }));
jest.mock('../../hooks/usePermissions', () => ({ usePermissions: () => ({ role: 'manager', isSuperuser: false }) }));
jest.mock('./HankTaskWorkspace', () => ({
  HankTaskWorkspace: ({ taskId, onNavigate, onBusyChange }: React.ComponentProps<typeof HankTaskWorkspace>) => (
    <section aria-label="Task workspace test">
      <p>{taskId ? `Review task ${taskId}` : 'Prepare a new task'}</p>
      <button type="button" onClick={() => onBusyChange(true)}>
        Begin pending task action
      </button>
      <button type="button" onClick={() => onBusyChange(false)}>
        Finish pending task action
      </button>
      <button type="button" onClick={onNavigate}>
        Open task result
      </button>
    </section>
  ),
}));
jest.mock('./HankPreferences', () => ({
  HankPreferences: ({ onBusyChange }: HankPreferencesProps) => (
    <section aria-label="Preferences test">
      <button type="button" onClick={() => onBusyChange?.(true)}>
        Begin preference save
      </button>
      <button type="button" onClick={() => onBusyChange?.(false)}>
        Finish preference save
      </button>
    </section>
  ),
}));
const mockedApi = jest.mocked(api);
function renderPanel(path = '/') {
  const onClose = jest.fn();
  return {
    ...render(
      <MemoryRouter initialEntries={[path]}>
        <Link to="/?hank_task=41">Task notification</Link>
        <CopilotPanel isOpen onClose={onClose} />
      </MemoryRouter>
    ),
    onClose,
  };
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  mockedApi.copilotChatStream.mockResolvedValue({
    answer: 'A proposal is ready for your review.',
    references: [{ type: 'hank_task', id: 41, label: 'Review proposed job', url: '/?hank_task=41' }],
    tool_trace: [],
    interaction_id: null,
    rounds: 1,
    truncated: false,
  });
});

describe('Hank task workspace integration', () => {
  it('opens Preferences and preserves the chat after saving without redirecting to an old task link', async () => {
    renderPanel('/?hank_task=41');
    await screen.findByText('Review task 41');
    fireEvent.click(screen.getByRole('button', { name: 'Preferences' }));
    expect(screen.getByRole('region', { name: 'Preferences test' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Ask Hank')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Begin preference save' }));
    expect(screen.getByRole('button', { name: 'Chat' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Tasks' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'My shift' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Upload PDF' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Finish preference save' }));
    expect(screen.getByRole('region', { name: 'Preferences test' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Chat' })).toBeEnabled();
  });

  it('reopens the same task on a new navigation but waits for a pending preference save', async () => {
    renderPanel('/?hank_task=41');
    await screen.findByText('Review task 41');
    fireEvent.click(screen.getByRole('button', { name: 'Preferences' }));
    fireEvent.click(screen.getByRole('button', { name: 'Begin preference save' }));
    fireEvent.click(screen.getByRole('link', { name: 'Task notification' }));
    expect(screen.getByRole('region', { name: 'Preferences test' })).toBeInTheDocument();
    expect(screen.queryByText('Review task 41')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Finish preference save' }));
    expect(await screen.findByText('Review task 41')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Preferences' }));
    fireEvent.click(screen.getByRole('link', { name: 'Task notification' }));
    expect(await screen.findByText('Review task 41')).toBeInTheDocument();
  });

  it('opens Tasks on demand and preserves chat when returning', async () => {
    renderPanel();
    expect(screen.queryByRole('region', { name: 'Task workspace test' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Ask Hank'), { target: { value: 'Prepare another bracket job' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByText('A proposal is ready for your review.');
    fireEvent.click(screen.getByRole('button', { name: 'Tasks' }));
    expect(screen.getByText('Prepare a new task')).toBeInTheDocument();
    expect(screen.queryByLabelText('Ask Hank')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Chat' }));
    expect(screen.getByText('A proposal is ready for your review.')).toBeInTheDocument();
  });

  it('loads a positive task id from a deep link automatically', async () => {
    renderPanel('/?hank_task=41');
    expect(await screen.findByText('Review task 41')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-pressed', 'true');
  });

  it.each(['0', '-1', 'nope', '1.5', '9007199254740992'])('ignores invalid task id %s', value => {
    renderPanel(`/?hank_task=${value}`);
    expect(screen.getByLabelText('Ask Hank')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Task workspace test' })).not.toBeInTheDocument();
  });

  it('opens a chat-generated proposal inside Hank without closing the drawer', async () => {
    const { onClose } = renderPanel();
    fireEvent.change(screen.getByLabelText('Ask Hank'), { target: { value: 'Repeat WO-1007' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    fireEvent.click(await screen.findByRole('link', { name: 'Review proposed job' }));
    expect(await screen.findByText('Review task 41')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Open task result' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('prevents tab and upload switches while a task action is in flight', async () => {
    renderPanel('/?hank_task=41');
    fireEvent.click(await screen.findByRole('button', { name: 'Begin pending task action' }));
    expect(screen.getByRole('button', { name: 'Chat' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'My shift' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Upload PDF' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Finish pending task action' }));
    expect(screen.getByRole('button', { name: 'Chat' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Upload PDF' })).toBeEnabled();
  });
});
