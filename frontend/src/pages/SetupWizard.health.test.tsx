import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SetupWizard from './SetupWizard';
import api from '../services/api';
jest.mock('../services/api', () => ({ __esModule: true, default: { getSetupHealth: jest.fn() } }));
beforeEach(() => jest.clearAllMocks());
it('never reports production readiness when initial health is unavailable', async () => {
  (api.getSetupHealth as jest.Mock).mockRejectedValue(new Error('offline'));
  render(
    <MemoryRouter>
      <SetupWizard />
    </MemoryRouter>
  );
  expect(await screen.findByText('Setup health is unavailable')).toBeInTheDocument();
  expect(screen.queryByText('No blocking issues')).not.toBeInTheDocument();
  expect(screen.queryByText(/ready for production/)).not.toBeInTheDocument();
  expect(screen.getByText('Setup reference checklist')).toBeInTheDocument();
});
it('retains the last checklist but withdraws readiness after refresh fails', async () => {
  (api.getSetupHealth as jest.Mock)
    .mockResolvedValueOnce({ progress: 100, counts: {}, steps: [], issues: [] })
    .mockRejectedValue(new Error('offline'));
  render(
    <MemoryRouter>
      <SetupWizard />
    </MemoryRouter>
  );
  await screen.findByText('No blocking issues');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  expect(await screen.findByText(/Current master-data health is unknown/)).toBeInTheDocument();
  expect(screen.getByText('100%')).toBeInTheDocument();
  expect(screen.queryByText(/ready for production/)).not.toBeInTheDocument();
});
