import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import MRPSupplyReview from './MRPSupplyReview';
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getMRPSupplyReview: jest.fn(), createMRPSupplyDraft: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;
const review = {
  action_id: 3,
  mrp_run_id: 4,
  mrp_run_number: 'MRP-4',
  part_id: 10,
  part_number: 'BUY-10',
  part_name: 'Sheet stock',
  kind: 'purchase_order',
  quantity: 5,
  source_quantity: 8,
  required_date: '2026-10-10',
  due_date: '2026-10-10',
  review_token: 'a'.repeat(64),
  blocked_reason: null,
  vendor_id: 8,
  unit_price: 12.5,
  vendors: [{ id: 8, code: 'VENDOR', name: 'Materials Supply' }],
  work_center_id: null,
  work_centers: [{ id: 2, code: 'CNC-2', name: 'Mill' }],
  routing: [],
  existing_draft: null,
};
const draft = {
  action_id: 3,
  mrp_run_id: 4,
  kind: 'purchase_order',
  id: 9,
  number: 'PO-9',
  url: '/purchasing?po=9',
  status: 'draft',
  quantity: 5,
};
const onCreated = jest.fn();
function mount() {
  return render(
    <MemoryRouter>
      <MRPSupplyReview actionId={3} onClose={jest.fn()} onCreated={onCreated} />
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getMRPSupplyReview.mockResolvedValue(review);
  mockedApi.createMRPSupplyDraft.mockResolvedValue(draft);
});
test('prefills current shortage and submits reviewed values to a linked draft', async () => {
  mount();
  expect(await screen.findByRole('spinbutton', { name: 'Quantity' })).toHaveValue(5);
  expect(screen.getByRole('combobox', { name: 'Supplier' })).toHaveValue('8');
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Quantity' }), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: 'Create PO draft' }));
  expect(await screen.findByRole('link', { name: 'Open PO-9' })).toHaveAttribute('href', '/purchasing?po=9');
  expect(mockedApi.createMRPSupplyDraft).toHaveBeenCalledWith(
    3,
    expect.objectContaining({ quantity: 4, vendor_id: 8, unit_price: 12.5, review_token: review.review_token })
  );
  expect(onCreated).toHaveBeenCalledWith(draft);
});
test('uncertain response retries exact key and payload with fields retained', async () => {
  mockedApi.createMRPSupplyDraft.mockRejectedValueOnce(new Error('lost response'));
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Create PO draft' }));
  const retry = await screen.findByRole('button', { name: 'Retry same draft request' });
  expect(screen.getByRole('spinbutton', { name: 'Quantity' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Reload review' })).not.toBeInTheDocument();
  fireEvent.click(retry);
  await screen.findByRole('link', { name: 'Open PO-9' });
  expect(mockedApi.createMRPSupplyDraft.mock.calls[1]).toEqual(mockedApi.createMRPSupplyDraft.mock.calls[0]);
});
test('stale shortage rejection reloads current review without reporting success', async () => {
  mockedApi.createMRPSupplyDraft.mockRejectedValueOnce({
    response: { status: 409, data: { detail: { message: 'Inventory changed. Reload the review.' } } },
  });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Create PO draft' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Inventory changed');
  expect(onCreated).not.toHaveBeenCalled();
  mockedApi.getMRPSupplyReview.mockResolvedValueOnce({ ...review, quantity: 2, review_token: 'b'.repeat(64) });
  fireEvent.click(screen.getByRole('button', { name: 'Reload review' }));
  await waitFor(() => expect(screen.getByRole('spinbutton', { name: 'Quantity' })).toHaveValue(2));
});
test('existing draft recovery links the document and prevents another creation', async () => {
  mockedApi.getMRPSupplyReview.mockResolvedValueOnce({ ...review, existing_draft: draft });
  mount();
  await screen.findByRole('link', { name: 'Open PO-9' });
  expect(screen.queryByRole('button', { name: 'Create PO draft' })).not.toBeInTheDocument();
  expect(mockedApi.createMRPSupplyDraft).not.toHaveBeenCalled();
});
test('manufacture review displays routing and submits work center without purchase UI', async () => {
  mockedApi.getMRPSupplyReview.mockResolvedValueOnce({
    ...review,
    kind: 'work_order',
    work_center_id: 2,
    routing: [{ id: 8, sequence: 10, name: 'Machine part', work_center_name: 'Mill' }],
  });
  mount();
  expect(await screen.findByText('Machine part · Mill')).toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Supplier' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Create WO draft' }));
  await waitFor(() =>
    expect(mockedApi.createMRPSupplyDraft).toHaveBeenCalledWith(3, expect.objectContaining({ work_center_id: 2 }))
  );
});
test('superseded run offers reload and disables creation', async () => {
  mockedApi.getMRPSupplyReview.mockResolvedValueOnce({ ...review, blocked_reason: 'A newer run is available.' });
  mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('newer run');
  expect(screen.getByRole('button', { name: 'Create PO draft' })).toBeDisabled();
});
