import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityRecordDetail from './QualityRecordDetail';
import api from '../../services/api';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNCR: jest.fn(),
    getCAR: jest.fn(),
    updateNCR: jest.fn(),
    updateCAR: jest.fn(),
  },
}));
jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  default: () => ({ can: () => true }),
}));

const mocked = api as jest.Mocked<typeof api>;
const closedNcr = {
  id: 7,
  ncr_number: 'NCR-20261105-001',
  title: 'Edge burr on bracket',
  description: 'Burr found on the outside edge at final inspection.',
  status: 'closed',
  disposition: 'scrap',
  quantity_affected: 5,
  quantity_rejected: 5,
  root_cause: 'Fixture wear let the edge burr through deburr.',
  containment_action: null,
  closed_date: '2026-11-01',
  updated_at: '2026-11-01T15:00:00Z',
  version: 0,
};
const props = { kind: 'ncr' as const, id: 7, onClose: jest.fn(), onSaved: jest.fn() };
const view = () => (
  <MemoryRouter>
    <QualityRecordDetail {...props} />
  </MemoryRouter>
);

beforeEach(() => {
  jest.clearAllMocks();
});

test('a closed record keeps only its status editable and reopens with a status-only change', async () => {
  mocked.getNCR.mockResolvedValue(closedNcr);
  mocked.updateNCR.mockResolvedValue({ ...closedNcr, status: 'open', closed_date: null });
  render(view());

  expect(await screen.findByText(/This record is closed and read-only/)).toBeInTheDocument();
  expect(screen.getByLabelText('Status')).toBeEnabled();
  expect(screen.getByLabelText('Disposition')).toBeDisabled();
  expect(screen.getByLabelText('Root cause')).toBeDisabled();
  expect(screen.getByLabelText('Containment action')).toBeDisabled();

  fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'open' } });
  fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

  await waitFor(() => expect(mocked.updateNCR).toHaveBeenCalledTimes(1));
  const [, payload] = mocked.updateNCR.mock.calls[0];
  expect(payload).toMatchObject({
    status: 'open',
    disposition: 'scrap',
    root_cause: closedNcr.root_cause,
    expected_updated_at: closedNcr.updated_at,
  });
  expect(props.onSaved).toHaveBeenCalled();
});

test('an open record keeps every field editable', async () => {
  mocked.getNCR.mockResolvedValue({ ...closedNcr, status: 'open', closed_date: null });
  render(view());

  expect(await screen.findByLabelText('Disposition')).toBeEnabled();
  expect(screen.getByLabelText('Root cause')).toBeEnabled();
  expect(screen.queryByText(/This record is closed and read-only/)).not.toBeInTheDocument();
});

const closedCar = {
  id: 9,
  car_number: 'CAR-20261105-001',
  car_type: 'corrective',
  title: 'Corrective action for edge burr',
  problem_description: 'Repeated edge burrs at final inspection.',
  status: 'closed',
  priority: 3,
  root_cause_analysis: null,
  root_cause: 'Worn deburr fixture.',
  containment_action: null,
  corrective_action: 'Replace and add fixture to PM.',
  preventive_action: null,
  verification_method: 'Inspect 3 consecutive lots.',
  verification_results: 'No burrs in 3 lots.',
  effectiveness_check: null,
  due_date: null,
  verification_due: null,
  closed_date: '2026-11-01',
  created_at: '2026-10-01T15:00:00Z',
  updated_at: '2026-11-01T15:00:00Z',
};

test('a closed CAR keeps only its status editable', async () => {
  mocked.getCAR.mockResolvedValue(closedCar);
  render(
    <MemoryRouter>
      <QualityRecordDetail kind="car" id={9} onClose={jest.fn()} onSaved={jest.fn()} />
    </MemoryRouter>
  );

  expect(await screen.findByText(/This record is closed and read-only/)).toBeInTheDocument();
  expect(screen.getByLabelText('Status')).toBeEnabled();
  expect(screen.getByLabelText('Priority (1 highest)')).toBeDisabled();
  expect(screen.getByLabelText('Due date')).toBeDisabled();
  expect(screen.getByLabelText('Corrective action')).toBeDisabled();
  expect(screen.getByLabelText('Verification results')).toBeDisabled();
});

test('a refused save shows the server detail and neither reports success nor closes', async () => {
  const onSaved = jest.fn();
  const onClose = jest.fn();
  const detail =
    'NCR NCR-20261105-001 is closed; its record cannot be edited. Reopen it first by changing only its status, then make the edit.';
  mocked.getNCR.mockResolvedValue({ ...closedNcr, status: 'open', closed_date: null });
  mocked.updateNCR.mockRejectedValue({ response: { status: 409, data: { detail } } });
  render(
    <MemoryRouter>
      <QualityRecordDetail kind="ncr" id={7} onClose={onClose} onSaved={onSaved} />
    </MemoryRouter>
  );

  fireEvent.change(await screen.findByLabelText('Containment action'), { target: { value: 'Quarantined lot' } });
  fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent(detail);
  expect(mocked.updateNCR).toHaveBeenCalledTimes(1);
  expect(onSaved).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  // The edit is still on screen to retry after a reload, not silently discarded.
  expect(screen.getByLabelText('Containment action')).toHaveValue('Quarantined lot');
});
