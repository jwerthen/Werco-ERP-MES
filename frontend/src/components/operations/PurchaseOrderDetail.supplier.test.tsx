import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PurchaseOrderDetail from './PurchaseOrderDetail';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getPurchaseOrder: jest.fn(),
    updateSupplierConfirmation: jest.fn(),
  },
}));
jest.mock('../../hooks/usePermissions', () => ({ __esModule: true, default: () => ({ can: () => true }) }));
jest.mock('./EntityPicker', () => ({ __esModule: true, default: ({ id }: { id: string }) => <input id={id} /> }));
const record = { id: 7, po_number: 'PO-TEST', status: 'sent', updated_at: '2026-09-07T10:00:00Z', lines: [] };
beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  (api.getPurchaseOrder as jest.Mock).mockResolvedValue(record);
});
afterEach(() => jest.restoreAllMocks());

test('parent close preserves dirty supplier response and blocks close during save; success stays reviewable', async () => {
  let resolveSave!: (value: typeof record) => void;
  (api.updateSupplierConfirmation as jest.Mock).mockReturnValue(
    new Promise(resolve => {
      resolveSave = resolve;
    })
  );
  const onClose = jest.fn();
  const onSaved = jest.fn();
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  render(
    <MemoryRouter>
      <PurchaseOrderDetail id={7} onClose={onClose} onSaved={onSaved} onLoaded={jest.fn()} />
    </MemoryRouter>
  );
  const note = await screen.findByLabelText('Supplier response or follow-up reason');
  fireEvent.change(note, { target: { value: 'Keep supplier evidence until saved' } });
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(confirm).toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Save supplier response' }));
  expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled();
  await act(async () => {
    resolveSave({ ...record, updated_at: '2026-09-07T11:00:00Z' });
  });
  await screen.findByText('Supplier follow-up saved.');
  expect(onSaved).not.toHaveBeenCalled();
  confirm.mockClear();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Close' })).not.toBeDisabled());
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(confirm).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(1);
});
