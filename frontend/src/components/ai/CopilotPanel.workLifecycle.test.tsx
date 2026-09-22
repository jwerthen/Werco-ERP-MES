import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { Link, MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import { CopilotPanel } from './CopilotPanel';
import type { HankHandoffs } from './HankHandoffs';
import type { HankDocumentIntake } from './HankDocumentIntake';
import type { HankRoutines } from './HankRoutines';
import type { HankTaskWorkspace } from './HankTaskWorkspace';
import type { HankDocumentUpload } from './HankDocumentUpload';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getHankWorkQueue: jest.fn(), getWorkOrder: jest.fn() },
}));
jest.mock('../../hooks/usePermissions', () => ({ usePermissions: () => ({ role: 'manager', isSuperuser: false }) }));
const mockUnmountHandoff = jest.fn();
jest.mock('./HankHandoffs', () => ({
  HankHandoffs: ({ initialId, onBusyChange }: React.ComponentProps<typeof HankHandoffs>) => {
    React.useEffect(() => () => mockUnmountHandoff(), []);
    return (
      <section aria-label="Saved handoff test">
        <p>Handoff {initialId}</p>
        <button onClick={() => onBusyChange?.(true)}>Start handoff mutation</button>
        <button onClick={() => onBusyChange?.(false)}>Finish handoff mutation</button>
      </section>
    );
  },
}));
jest.mock('./HankDocumentIntake', () => ({
  HankDocumentIntake: ({ initialId }: React.ComponentProps<typeof HankDocumentIntake>) => (
    <p>Intake file {initialId}</p>
  ),
}));
jest.mock('./HankRoutines', () => ({
  HankRoutines: ({ initialId }: React.ComponentProps<typeof HankRoutines>) => <p>Routine run {initialId}</p>,
}));
jest.mock('./HankTaskWorkspace', () => ({
  HankTaskWorkspace: ({ taskId }: React.ComponentProps<typeof HankTaskWorkspace>) => <p>Task {taskId}</p>,
}));
jest.mock('./HankDocumentUpload', () => ({
  HankDocumentUpload: ({ onCancel, onBusyChange }: React.ComponentProps<typeof HankDocumentUpload>) => (
    <section aria-label="Legacy PDF filing test">
      <button onClick={onCancel}>Cancel filing</button>
      <button onClick={() => onBusyChange?.(true)}>Begin PDF save</button>
      <button onClick={() => onBusyChange?.(false)}>Finish PDF save</button>
    </section>
  ),
}));
jest.mock('./HankVoiceInput', () => ({ HankVoiceInput: () => null }));
const mocked = jest.mocked(api);
function panel(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Link to="/?hank_work=intake&hank_id=19">Saved intake notification</Link>
      <Link to="/?hank_work=handoff&hank_id=24">Saved handoff notification</Link>
      <Link to="/?hank_work=routine&hank_id=31">Saved routine notification</Link>
      <Link to="/?hank_task=41">Saved task notification</Link>
      <CopilotPanel isOpen onClose={jest.fn()} />
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  sessionStorage.setItem(
    'token',
    `header.${btoa(JSON.stringify({ sub: '17', cid: 4, ro: false, type: 'access' }))}.sig`
  );
  mocked.getHankWorkQueue.mockResolvedValue({ checked_at: '2026-09-22T16:00:00Z', items: [], truncated: false });
  mocked.getWorkOrder.mockResolvedValue({ work_order_number: 'WO-7' });
});

it.each([
  ['intake', 'Intake file'],
  ['handoff', 'Handoff'],
  ['routine', 'Routine run'],
])('opens the exact saved %s record from its deep link', async (area, label) => {
  panel(`/?hank_work=${area}&hank_id=17`);
  expect(await screen.findByText(`${label} 17`)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Work' })).toHaveAttribute('aria-pressed', 'true');
});

it('defers a new work link until the pending mutation settles, preserving its mounted form', async () => {
  panel('/?hank_work=handoff&hank_id=17');
  fireEvent.click(await screen.findByRole('button', { name: 'Start handoff mutation' }));
  fireEvent.click(screen.getByRole('link', { name: 'Saved intake notification' }));
  expect(screen.getByText('Handoff 17')).toBeInTheDocument();
  expect(screen.queryByText('Intake file 19')).not.toBeInTheDocument();
  expect(mockUnmountHandoff).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Chat' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Finish handoff mutation' }));
  expect(await screen.findByText('Intake file 19')).toBeInTheDocument();
  expect(mockUnmountHandoff).toHaveBeenCalledTimes(1);
});

it('defers task navigation as well as work navigation until the current write settles', async () => {
  panel('/?hank_work=handoff&hank_id=17');
  fireEvent.click(await screen.findByRole('button', { name: 'Start handoff mutation' }));
  fireEvent.click(screen.getByRole('link', { name: 'Saved task notification' }));
  expect(screen.getByText('Handoff 17')).toBeInTheDocument();
  expect(mockUnmountHandoff).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Finish handoff mutation' }));
  expect(await screen.findByText('Task 41')).toBeInTheDocument();
});

it('opens a saved work link when legacy PDF filing is open but idle', async () => {
  panel();
  fireEvent.click(screen.getByRole('button', { name: 'Upload PDF' }));
  expect(screen.getByRole('region', { name: 'Legacy PDF filing test' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('link', { name: 'Saved routine notification' }));
  expect(await screen.findByText('Routine run 31')).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: 'Legacy PDF filing test' })).not.toBeInTheDocument();
});

it('waits for a pending PDF save before accepting a saved work link', async () => {
  panel();
  fireEvent.click(screen.getByRole('button', { name: 'Upload PDF' }));
  fireEvent.click(screen.getByRole('button', { name: 'Begin PDF save' }));
  fireEvent.click(screen.getByRole('link', { name: 'Saved handoff notification' }));
  expect(screen.queryByText('Handoff 24')).not.toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Legacy PDF filing test' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Finish PDF save' }));
  expect(await screen.findByText('Handoff 24')).toBeInTheDocument();
});

it.each(['0', '-1', '1.5', 'nope', '9007199254740992'])('does not open invalid saved-work id %s', value => {
  panel(`/?hank_work=intake&hank_id=${value}`);
  expect(screen.getByLabelText('Ask Hank')).toBeInTheDocument();
  expect(screen.queryByText(/Intake file/)).not.toBeInTheDocument();
});
