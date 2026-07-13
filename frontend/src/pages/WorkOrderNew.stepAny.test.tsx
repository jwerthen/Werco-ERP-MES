/**
 * WorkOrderNew — setup/run-time inputs use step="any" (go-live blocker FIX 2).
 *
 * The four setup/run-time minute inputs were changed from step="0.1" to
 * step="any" so an auto-populated 2-decimal minute value (e.g. 9.99, derived
 * from routing run_hours_per_unit) no longer trips HTML5 stepMismatch and
 * silently blocks the submit. This guards that the inputs carry step="any"
 * (and NOT the old step="0.1"), plus a behavioral check that a 2-decimal
 * value is accepted as valid rather than reported as a step mismatch.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getParts: jest.fn(),
    getBOMs: jest.fn(),
    getWorkCenters: jest.fn(),
    getCustomerNames: jest.fn(),
    getPartReadiness: jest.fn(),
    getRoutingByPart: jest.fn(),
    previewWorkOrderOperations: jest.fn(),
    createWorkOrder: jest.fn(),
    createCustomer: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const PART = {
  id: 1,
  part_number: 'PN-7731',
  name: 'Bracket, hinge',
  part_type: 'manufactured',
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getParts.mockResolvedValue([PART]);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getPartReadiness.mockResolvedValue({ ready: true, blockers: [], warnings: [], checks: {} });
  // No routing → manual-entry path exposes the "Add First Operation" affordance.
  mockedApi.getRoutingByPart.mockResolvedValue(null);
});

async function renderPage() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/work-orders/new']}>
        <WorkOrderNew />
      </MemoryRouter>
    </ToastProvider>
  );
  await screen.findByTestId('wo-serial-numbers');
}

async function selectPart() {
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'PN-7731' } });
  const option = await screen.findByRole('option', { name: /PN-7731/i });
  fireEvent.mouseDown(option);
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(1));
}

describe('WorkOrderNew setup/run-time inputs', () => {
  it('renders the minute inputs with step="any" (not the old step="0.1")', async () => {
    await renderPage();
    await selectPart();

    // Manual-entry path: reveal the operation row (and its time inputs).
    fireEvent.click(await screen.findByRole('button', { name: /add first operation/i }));

    const setupInputs = await screen.findAllByLabelText(/setup time in minutes/i);
    const runInputs = screen.getAllByLabelText(/run time in minutes/i);
    const timeInputs = [...setupInputs, ...runInputs];
    expect(timeInputs.length).toBeGreaterThan(0);

    timeInputs.forEach((input) => {
      expect(input).toHaveAttribute('step', 'any');
      expect(input).not.toHaveAttribute('step', '0.1');
    });
  });

  it('accepts a 2-decimal minute value without a stepMismatch block', async () => {
    await renderPage();
    await selectPart();
    fireEvent.click(await screen.findByRole('button', { name: /add first operation/i }));

    const setupInput = (await screen.findAllByLabelText(/setup time in minutes/i))[0] as HTMLInputElement;

    // A 2-decimal minute value (the kind auto-populated from routing) must not
    // be flagged as an invalid HTML5 step increment.
    fireEvent.change(setupInput, { target: { value: '9.99' } });

    expect(setupInput).toHaveValue(9.99);
    expect(setupInput.validity.stepMismatch).toBe(false);
    expect(setupInput).toBeValid();
  });
});
