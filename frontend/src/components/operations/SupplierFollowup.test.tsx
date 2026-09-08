import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import SupplierFollowup from './SupplierFollowup';
import api from '../../services/api';
jest.mock('../../services/api', () => ({ __esModule: true, default: { updateSupplierConfirmation: jest.fn() } }));
jest.mock('../../hooks/useUnsavedChanges', () => ({ __esModule: true, default: () => ({ markSaved: jest.fn() }) }));
jest.mock('./EntityPicker', () => ({
  __esModule: true,
  default: (props: { id: string; value: number; disabled?: boolean; onChange: (value: string) => void }) => (
    <select id={props.id} value={props.value} disabled={props.disabled} onChange={e => props.onChange(e.target.value)}>
      <option value="">None</option>
      <option value="1">Buyer</option>
    </select>
  ),
}));
const record = { id: 7, updated_at: '2026-09-07T10:00:00Z', required_date: '2026-09-10', expected_date: '2026-09-11' };
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
});
test('supplier evidence has separate dates and authorized owner; successful second save uses fresh concurrency token', async () => {
  const save = api.updateSupplierConfirmation as jest.Mock;
  save.mockResolvedValue({ ...record, updated_at: '2026-09-07T11:00:00Z' });
  render(<SupplierFollowup record={record} canEdit onSaved={jest.fn()} />);
  expect(screen.getByLabelText('Supplier-confirmed arrival date')).toBeDisabled();
  fireEvent.click(screen.getByLabelText('Supplier acknowledged this order'));
  fireEvent.change(screen.getByLabelText('Supplier-confirmed arrival date'), { target: { value: '2026-09-12' } });
  fireEvent.change(screen.getByLabelText('Follow-up owner'), { target: { value: '1' } });
  fireEvent.change(screen.getByLabelText('Supplier response or follow-up reason'), {
    target: { value: 'Supplier confirms revised arrival' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save supplier response' }));
  await screen.findByText('Supplier follow-up saved.');
  expect(save).toHaveBeenLastCalledWith(
    7,
    expect.objectContaining({
      supplier_confirmed_date: '2026-09-12',
      follow_up_owner_id: 1,
      expected_updated_at: record.updated_at,
    })
  );
  expect(save.mock.calls[0][1]).not.toHaveProperty('required_date');
  fireEvent.click(screen.getByRole('button', { name: 'Save supplier response' }));
  await screen.findByText('Supplier follow-up saved.');
  expect(save.mock.calls[1][1].expected_updated_at).toBe('2026-09-07T11:00:00Z');
});
test('conflict keeps revised promise and reason; read-only users have no action', async () => {
  (api.updateSupplierConfirmation as jest.Mock).mockRejectedValue({
    response: { data: { detail: 'Purchase order changed. Reload.' } },
  });
  const view = render(<SupplierFollowup record={record} canEdit onSaved={jest.fn()} />);
  fireEvent.change(screen.getByLabelText('Supplier response or follow-up reason'), {
    target: { value: 'Keep this evidence' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save supplier response' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Supplier response or follow-up reason')).toHaveValue('Keep this evidence');
  view.unmount();
  render(<SupplierFollowup record={record} canEdit={false} onSaved={jest.fn()} />);
  expect(screen.queryByRole('button', { name: 'Save supplier response' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Supplier acknowledged this order')).toBeDisabled();
});
