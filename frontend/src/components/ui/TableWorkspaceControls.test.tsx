import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import { useTableWorkspace } from '../../hooks/useTableWorkspace';
import { TableWorkspaceControls } from './TableWorkspaceControls';
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listWorkspaceRecords: jest.fn(),
    listTeamWorkspaceRecords: jest.fn(),
    saveWorkspaceRecord: jest.fn(),
    saveTeamWorkspaceRecord: jest.fn(),
    deleteWorkspaceRecord: jest.fn(),
    deleteTeamWorkspaceRecord: jest.fn(),
  },
}));
const mocked = api as jest.Mocked<typeof api>;
const view = {
  key: 'team-priority',
  namespace: 'work-orders',
  kind: 'view' as const,
  name: 'Priority',
  version: 3,
  updated_at: '2026-09-07',
  data: {
    table: 'orders',
    layout: { order: ['id'], hidden: [], dense: false, sort: null },
    filters: { status: 'released' },
  },
};
const apply = jest.fn();
function Screen() {
  const workspace = useTableWorkspace(
    'work-orders',
    'orders',
    [{ key: 'id', header: 'Order' }],
    { status: 'on_hold' },
    apply
  );
  return <TableWorkspaceControls workspace={workspace} />;
}
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  mocked.listWorkspaceRecords.mockResolvedValue([]);
  mocked.listTeamWorkspaceRecords.mockResolvedValue({ items: [view], can_manage: false });
});
afterEach(() => sessionStorage.clear());

test('permitted reader can apply shared view while manager-only controls stay absent', async () => {
  render(<Screen />);
  const select = screen.getByRole('combobox', { name: 'Saved views' });
  await waitFor(() => expect(select).toBeEnabled());
  fireEvent.change(select, { target: { value: 'team:team-priority' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply view' }));
  expect(apply).toHaveBeenCalledWith({ status: 'released' });
  expect(screen.queryByRole('button', { name: 'Update view' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Remove view' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Save current view' }));
  expect(screen.queryByRole('combobox', { name: 'View visibility' })).not.toBeInTheDocument();
});

test('manager explicitly selects team sharing; pending write blocks duplication and failed CAS is durable', async () => {
  mocked.listTeamWorkspaceRecords.mockResolvedValue({ items: [view], can_manage: true });
  mocked.saveTeamWorkspaceRecord.mockRejectedValue({
    response: { data: { detail: 'Team view changed elsewhere. Reload.' } },
  });
  render(<Screen />);
  const select = screen.getByRole('combobox', { name: 'Saved views' });
  await waitFor(() => expect(select).toBeEnabled());
  fireEvent.change(select, { target: { value: 'team:team-priority' } });
  fireEvent.click(screen.getByRole('button', { name: 'Update view' }));
  expect(screen.getByRole('button', { name: 'Update view' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Update view' })).toHaveAccessibleDescription('Saving a workspace change. Please wait.');
  expect(await screen.findByRole('alert')).toHaveTextContent('Team view changed elsewhere. Reload.');
  expect(mocked.saveTeamWorkspaceRecord).toHaveBeenCalledWith(
    'work-orders',
    'team-priority',
    expect.objectContaining({ version: 3 })
  );
  expect(mocked.saveWorkspaceRecord).not.toHaveBeenCalled();
  expect(screen.getByRole('combobox', { name: 'Saved views' })).toHaveValue('team:team-priority');
});

test('explains disabled apply and save actions until their required inputs are supplied', async () => {
  render(<Screen />);
  const select = screen.getByRole('combobox', { name: 'Saved views' });
  await waitFor(() => expect(select).toBeEnabled());
  const applyButton = screen.getByRole('button', { name: 'Apply view' });
  expect(applyButton).toBeDisabled();
  expect(screen.getByText('Select a saved view to apply it.')).toBeVisible();
  expect(applyButton).toHaveAccessibleDescription('Select a saved view to apply it.');
  fireEvent.change(select, { target: { value: 'team:team-priority' } });
  expect(applyButton).toBeEnabled();
  expect(applyButton).not.toHaveAttribute('aria-describedby');
  expect(screen.queryByText('Select a saved view to apply it.')).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Save current view' }));
  const saveButton = screen.getByRole('button', { name: 'Save view' });
  expect(saveButton).toBeDisabled();
  expect(saveButton).toHaveAccessibleDescription('Enter a view name to save.');
  expect(screen.getByText('Enter a view name to save.')).toBeVisible();
  fireEvent.change(screen.getByRole('textbox', { name: 'View name' }), { target: { value: '   ' } });
  expect(saveButton).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: 'View name' }), { target: { value: 'Open priority jobs' } });
  expect(saveButton).toBeEnabled();
  expect(saveButton).not.toHaveAttribute('aria-describedby');
  expect(mocked.saveWorkspaceRecord).not.toHaveBeenCalled();
});

test('explains loading gates while table options remain usable', () => {
  mocked.listWorkspaceRecords.mockImplementation(() => new Promise(() => {}));
  mocked.listTeamWorkspaceRecords.mockImplementation(() => new Promise(() => {}));
  render(<Screen />);
  expect(screen.getByRole('combobox', { name: 'Saved views' })).toHaveAccessibleDescription('Loading your saved views…');
  expect(screen.getByRole('button', { name: 'Apply view' })).toHaveAccessibleDescription('Loading your saved views…');
  fireEvent.click(screen.getByRole('button', { name: 'Table options' }));
  expect(screen.getByRole('button', { name: 'Save layout' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save layout' })).toHaveAccessibleDescription('Loading your saved views…');
  expect(screen.getByRole('button', { name: 'Reset layout' })).toBeEnabled();
  expect(screen.getByRole('checkbox', { name: 'Order' })).toHaveAttribute('title', 'Required column: always visible.');
  expect(screen.getByRole('button', { name: 'Move Order up' })).toHaveAttribute('title', 'Already the first column.');
  expect(screen.getByRole('button', { name: 'Move Order down' })).toHaveAttribute('title', 'Already the last column.');
});

test('explains why a session without an account identity cannot save or apply views', () => {
  sessionStorage.removeItem('user');
  render(<Screen />);
  expect(screen.getByRole('button', { name: 'Apply view' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Apply view' })).toHaveAccessibleDescription('Sign in to save or apply views.');
  fireEvent.click(screen.getByRole('button', { name: 'Save current view' }));
  expect(screen.getByRole('button', { name: 'Save view' })).toHaveAccessibleDescription('Sign in to save or apply views.');
});
