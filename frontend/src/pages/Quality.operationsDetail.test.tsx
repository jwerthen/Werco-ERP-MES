import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Quality from './Quality';
import api from '../services/api';
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { id: 1, role: 'quality' } }) }));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getNCR: jest.fn(),
    updateNCR: jest.fn(),
  },
}));
const record = {
  id: 7,
  ncr_number: 'NCR-007',
  title: 'Incoming surface defect',
  description: 'Full inspection finding with detailed acceptance evidence.',
  status: 'under_review',
  disposition: 'pending',
  source: 'incoming_inspection',
  quantity_affected: 4,
  quantity_rejected: 1,
  version: 0,
  updated_at: '2026-09-06T12:00:00Z',
  created_at: '2026-09-06T10:00:00Z',
};
beforeEach(() => {
  jest.clearAllMocks();
  const mock = api as jest.Mocked<typeof api>;
  mock.getNCRs.mockResolvedValue([record]);
  mock.getCARs.mockResolvedValue([]);
  mock.getFAIs.mockResolvedValue([]);
  mock.getQualitySummary.mockResolvedValue({ open_ncrs: 1, open_cars: 0, pending_fais: 0 });
  mock.getParts.mockResolvedValue([]);
  mock.getNCR.mockResolvedValue(record);
  mock.updateNCR.mockResolvedValue({ ...record, status: 'closed' });
});
test('an NCR deep link mounts full detail and saves a supported closure with conflict token', async () => {
  render(
    <MemoryRouter initialEntries={['/quality?tab=ncr&ncr=7']}>
      <Quality />
    </MemoryRouter>
  );
  const dialog = await screen.findByRole('dialog', { name: 'Quality record details' });
  expect(await within(dialog).findByText(record.description)).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText('Status'), { target: { value: 'closed' } });
  fireEvent.change(within(dialog).getByLabelText('Disposition'), { target: { value: 'scrap' } });
  fireEvent.change(within(dialog).getByLabelText('Root cause'), {
    target: { value: 'Handling fixtures damaged the outer finished surface.' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  await waitFor(() =>
    expect(api.updateNCR).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ status: 'closed', disposition: 'scrap', expected_updated_at: record.updated_at })
    )
  );
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});
