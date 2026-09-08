import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import MRPPurchaseBatchReview from './MRPPurchaseBatchReview';
import { MRPPurchaseReviewLine } from '../types/mrpBatch';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { reviewMRPPurchaseBatch: jest.fn(), createMRPPurchaseBatch: jest.fn() },
}));
const review = api.reviewMRPPurchaseBatch as jest.Mock;
const create = api.createMRPPurchaseBatch as jest.Mock;
const line: MRPPurchaseReviewLine = {
  action_id: 1,
  part_number: 'BUY-001',
  part_name: 'Metal',
  mrp_run_number: 'MRP-001',
  quantity: 10,
  due_date: '2026-09-15',
  review_token: 'a'.repeat(64),
  vendor_id: 2,
  unit_price: 3,
  blocked_reason: null,
  existing_draft: null,
  vendors: [{ id: 2, code: 'SUP', name: 'Metal Supplier' }],
};
const onCreated = jest.fn();
function mount() {
  render(
    <MemoryRouter>
      <MRPPurchaseBatchReview actionIds={[1, 2]} onClose={jest.fn()} onCreated={onCreated} />
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  review.mockResolvedValue({
    lines: [line, { ...line, action_id: 2, part_number: 'BUY-002', quantity: 5 }],
    max_lines: 25,
  });
  create.mockResolvedValue({
    drafts: [],
    purchase_orders: [
      {
        id: 12,
        number: 'PO-12',
        url: '/purchasing?po=12',
        vendor_id: 2,
        action_ids: [1, 2],
        total: 45,
        status: 'draft',
      },
    ],
    replayed: false,
  });
});

test('reviews grouped supplier costs and requires acknowledgement before creating draft POs', async () => {
  mount();
  await screen.findByText('Metal Supplier: 2 lines · $45.00');
  expect(screen.getByRole('button', { name: 'Create purchase drafts' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.change(screen.getByLabelText('Quantity for BUY-001'), { target: { value: '2.5' } });
  expect(screen.getByRole('checkbox')).not.toBeChecked();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Create purchase drafts' }));
  await screen.findByRole('link', { name: 'Open PO-12' });
  expect(create).toHaveBeenCalledWith(
    expect.objectContaining({
      lines: expect.arrayContaining([expect.objectContaining({ action_id: 1, quantity: 2.5, vendor_id: 2 })]),
    })
  );
  expect(onCreated).toHaveBeenCalledTimes(1);
});

test('unknown response retains the exact reviewed batch for safe retry and prevents double submission', async () => {
  let reject!: (reason: unknown) => void;
  create.mockImplementationOnce(
    () =>
      new Promise((_resolve, rejectPromise) => {
        reject = rejectPromise;
      })
  );
  mount();
  await screen.findByText('Metal Supplier: 2 lines · $45.00');
  fireEvent.click(screen.getByRole('checkbox'));
  const button = screen.getByRole('button', { name: 'Create purchase drafts' });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(create).toHaveBeenCalledTimes(1);
  await act(async () => reject(new Error('interrupted')));
  await screen.findByText(/result could not be confirmed/);
  expect(screen.getByLabelText('Quantity for BUY-001')).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Reload review' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry same purchase batch' }));
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[1][0]).toEqual(create.mock.calls[0][0]);
});

test('a stale or already supplied line blocks the entire selected batch', async () => {
  review.mockResolvedValue({ lines: [{ ...line, blocked_reason: 'A newer MRP run is available.' }] });
  mount();
  await screen.findByText('A newer MRP run is available.');
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Create purchase drafts' })).toBeDisabled();
  expect(create).not.toHaveBeenCalled();
});
