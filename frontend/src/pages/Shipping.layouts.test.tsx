import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Shipping from './Shipping';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getShipments: jest.fn(), getReadyToShip: jest.fn(), updateShipment: jest.fn(), createShipment: jest.fn() },
}));
jest.mock('../hooks/usePermissions', () => ({ __esModule: true, default: () => ({ can: () => true }) }));
jest.mock('../components/shipping/ShipmentTrackingPanel', () => () => null);
jest.mock('../components/shipping/ScheduleShipmentModal', () => () => null);

const mocked = api as jest.Mocked<typeof api>;
const shipment = {
  id: 9,
  shipment_number: 'SHP-LAYOUT-9',
  work_order_id: 3,
  work_order_number: 'WO-LAYOUT-3',
  part_number: 'PART-LAYOUT',
  customer_name: 'Synthetic customer',
  quantity_shipped: 2,
  status: 'pending',
  created_at: '2026-09-07T12:00:00Z',
};
const view = () =>
  render(
    <MemoryRouter>
      <Shipping />
    </MemoryRouter>
  );
beforeEach(() => {
  jest.clearAllMocks();
  mocked.getShipments.mockResolvedValue([shipment]);
  mocked.getReadyToShip.mockResolvedValue([]);
});

test('page identity survives loading and a failed load before retry succeeds', async () => {
  mocked.getShipments.mockRejectedValueOnce(new Error('Unavailable'));
  const error = jest.spyOn(console, 'error').mockImplementation(() => {});
  view();
  expect(screen.getByRole('heading', { level: 1, name: 'Shipping' })).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Loading shipping');
  const alert = await screen.findByRole('alert');
  expect(screen.getByRole('heading', { level: 1, name: 'Shipping' })).toBeInTheDocument();
  fireEvent.click(within(alert).getByRole('button', { name: /retry/i }));
  expect((await screen.findAllByRole('button', { name: 'Edit / Cancel' })).length).toBeGreaterThan(0);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  error.mockRestore();
});

test('closing pending shipment preserves its allocation and exposes labelled record context', async () => {
  view();
  fireEvent.click((await screen.findAllByRole('button', { name: 'Edit / Cancel' }))[0]);
  const dialog = screen.getByRole('dialog', { name: 'Pending shipment SHP-LAYOUT-9' });
  expect(within(dialog).getByText('WO-LAYOUT-3')).toBeInTheDocument();
  expect(within(dialog).getByText('Part').nextElementSibling).toHaveTextContent('PART-LAYOUT');
  expect(within(dialog).getByRole('button', { name: 'Cancel shipment' })).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close pending shipment' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(mocked.updateShipment).not.toHaveBeenCalled();
});

test('pending save cannot be closed; a failed update keeps quantity and an enabled close control', async () => {
  let fail!: (error: unknown) => void;
  mocked.updateShipment.mockImplementation(
    () =>
      new Promise((_, reject) => {
        fail = reject;
      })
  );
  view();
  fireEvent.click((await screen.findAllByRole('button', { name: 'Edit / Cancel' }))[0]);
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Quantity allocated' }), { target: { value: '3' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save quantity' }));
  await waitFor(() => expect(mocked.updateShipment).toHaveBeenCalled());
  expect(within(dialog).getByRole('button', { name: 'Close pending shipment' })).toBeDisabled();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(dialog).toBeInTheDocument();
  await act(async () => fail({ response: { data: { detail: 'Quantity was reserved elsewhere' } } }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Quantity was reserved elsewhere');
  expect(within(dialog).getByRole('spinbutton', { name: 'Quantity allocated' })).toHaveValue(3);
  expect(within(dialog).getByRole('button', { name: 'Close pending shipment' })).toBeEnabled();
});

test('manual shipment keeps close and Escape blocked until creation finishes', async () => {
  mocked.getReadyToShip.mockResolvedValue([
    {
      work_order_id: 3,
      work_order_number: 'WO-LAYOUT-3',
      part_number: 'PART-LAYOUT',
      customer_name: 'Synthetic customer',
      quantity_complete: 5,
      quantity_remaining: 3,
      quantity_reserved: 2,
      quantity_shipped: 0,
    },
  ]);
  let fail!: (error: unknown) => void;
  mocked.createShipment.mockImplementation(
    () =>
      new Promise((_, reject) => {
        fail = reject;
      })
  );
  view();
  fireEvent.click((await screen.findAllByRole('button', { name: 'Manual' }))[0]);
  const dialog = screen.getByRole('dialog', { name: 'Create Shipment (Manual)' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create Shipment' }));
  await waitFor(() => expect(mocked.createShipment).toHaveBeenCalled());
  expect(within(dialog).getByRole('button', { name: 'Close manual shipment' })).toBeDisabled();
  expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(dialog).toBeInTheDocument();
  await act(async () => fail({ response: { data: { detail: 'Unable to reserve quantity' } } }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Unable to reserve quantity');
  expect(within(dialog).getByRole('spinbutton', { name: 'Qty to Ship' })).toHaveValue(3);
  expect(within(dialog).getByRole('button', { name: 'Close manual shipment' })).toBeEnabled();
});
