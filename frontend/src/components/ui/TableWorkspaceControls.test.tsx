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
  expect(await screen.findByRole('alert')).toHaveTextContent('Team view changed elsewhere. Reload.');
  expect(mocked.saveTeamWorkspaceRecord).toHaveBeenCalledWith(
    'work-orders',
    'team-priority',
    expect.objectContaining({ version: 3 })
  );
  expect(mocked.saveWorkspaceRecord).not.toHaveBeenCalled();
  expect(screen.getByRole('combobox', { name: 'Saved views' })).toHaveValue('team:team-priority');
});
