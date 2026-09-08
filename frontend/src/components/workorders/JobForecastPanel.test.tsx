import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import JobForecastPanel from './JobForecastPanel';
import { DeliveryPrediction } from '../../types/jobPlanning';

jest.mock('../../services/api', () => ({ __esModule: true, default: { predictDelivery: jest.fn() } }));
const predict = api.predictDelivery as jest.MockedFunction<typeof api.predictDelivery>;
const prediction: DeliveryPrediction = {
  predicted_completion: null,
  basis: 'Calendar-day estimate; not a committed promise.',
  warnings: [],
  operations: [],
  materials: {
    status: 'unknown',
    ready_date: null,
    basis: 'Stock and incoming supply are not reserved.',
    warnings: ['Supplier arrival is unconfirmed.'],
    lines: [
      {
        part_id: 8,
        part_number: 'MAT-8',
        unit_of_measure: 'each',
        required_quantity: 10,
        covered_quantity: 4,
        shortage_quantity: 6,
        reason: 'Insufficient confirmed supply.',
        sources: [{ kind: 'purchase_order', id: 11, label: 'PO-11', quantity: 4, available_date: '2026-09-10' }],
      },
    ],
  },
};
beforeEach(() => jest.clearAllMocks());

test('unknown completion stays unknown with inspectable material evidence', async () => {
  predict.mockResolvedValue(prediction);
  render(
    <MemoryRouter>
      <JobForecastPanel workOrderId={7} />
    </MemoryRouter>
  );
  expect(predict).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Completion forecast' }));
  await screen.findByText('Supplier arrival is unconfirmed.');
  expect(screen.getAllByText('Unknown')).toHaveLength(2);
  fireEvent.click(screen.getByText('Material coverage · 1 requirements'));
  expect(screen.getByText('4 covered · 6 unresolved')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'PO-11' })).toHaveAttribute('href', '/purchasing?po=11');
});

test('retry preserves last forecast and labels it stale until refresh succeeds', async () => {
  predict
    .mockResolvedValueOnce(prediction)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce({ ...prediction, predicted_completion: '2026-09-15T00:00:00Z' });
  render(
    <MemoryRouter>
      <JobForecastPanel workOrderId={7} />
    </MemoryRouter>
  );
  fireEvent.click(screen.getByRole('button', { name: 'Completion forecast' }));
  await screen.findByText('Supplier arrival is unconfirmed.');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh forecast' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('last result is stale');
  fireEvent.click(screen.getByRole('button', { name: 'Retry forecast' }));
  await screen.findByText(/Sep 15/);
});
