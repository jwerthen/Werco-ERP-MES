/**
 * Process Sheets PR 4 — serialized WO creation from the office.
 *
 * The optional "Serial numbers" textarea (one per line) mirrors the server's
 * 422 rules client-side — unique, non-empty, ≤100 chars, count equal to the
 * quantity when provided — with a live count indicator. A mismatch blocks the
 * submit before the round-trip; a server 422 (Pydantic array detail) surfaces
 * its messages verbatim, never "[object Object]".
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
  // No routing → manual-entry hint, zero operations (fine for these tests).
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

function setQuantity(value: string) {
  fireEvent.change(screen.getByLabelText(/quantity/i), { target: { value } });
}

function setSerials(text: string) {
  fireEvent.change(screen.getByTestId('wo-serial-numbers'), { target: { value: text } });
}

describe('WorkOrderNew serial numbers', () => {
  it('tracks the live count indicator and blocks a count mismatch CLIENT-side (no API call)', async () => {
    await renderPage();
    await selectPart();
    setQuantity('3');

    // Empty is fine — serials are optional.
    expect(screen.getByTestId('wo-serials-count')).toHaveTextContent(
      'No serial numbers — this work order will not be serialized.'
    );

    setSerials('SN-001\nSN-002');
    expect(screen.getByTestId('wo-serials-count')).toHaveTextContent('2 of 3 serial numbers entered');
    expect(
      screen.getByText(/2 serial numbers entered for quantity 3 — the counts must match/i)
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /create work order/i }));

    expect(await screen.findByText(/serial numbers are not valid/i)).toBeInTheDocument();
    expect(mockedApi.createWorkOrder).not.toHaveBeenCalled();
  });

  it('blocks duplicate serials client-side, naming the duplicates', async () => {
    await renderPage();
    await selectPart();
    setQuantity('2');
    setSerials('SN-001\nSN-001');

    expect(screen.getByText(/duplicate serial number: SN-001/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /create work order/i }));
    expect(await screen.findByText(/serial numbers are not valid/i)).toBeInTheDocument();
    expect(mockedApi.createWorkOrder).not.toHaveBeenCalled();
  });

  it('sends serial_numbers on create when valid, and omits the field entirely when blank', async () => {
    mockedApi.createWorkOrder.mockResolvedValue({ id: 42 });
    await renderPage();
    await selectPart();
    setQuantity('2');
    setSerials('SN-001\n SN-002 \n\n');

    fireEvent.click(screen.getByRole('button', { name: /create work order/i }));

    await waitFor(() =>
      expect(mockedApi.createWorkOrder).toHaveBeenCalledWith(
        expect.objectContaining({ serial_numbers: ['SN-001', 'SN-002'], quantity_ordered: 2 })
      )
    );

    // Blank serials → the key is absent (server treats absent as non-serialized).
    expect(mockedApi.createWorkOrder.mock.calls[0][0]).toHaveProperty('serial_numbers');
    mockedApi.createWorkOrder.mockClear();
    setSerials('');
    fireEvent.click(screen.getByRole('button', { name: /create work order/i }));
    await waitFor(() => expect(mockedApi.createWorkOrder).toHaveBeenCalled());
    expect(mockedApi.createWorkOrder.mock.calls[0][0]).not.toHaveProperty('serial_numbers');
  });

  it('surfaces a server 422 VERBATIM (Pydantic array detail joined, never [object Object])', async () => {
    mockedApi.createWorkOrder.mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: [
            {
              type: 'value_error',
              loc: ['body'],
              msg: 'Value error, serial_numbers count (2) must equal quantity_ordered (3)',
            },
          ],
        },
      },
    });
    await renderPage();
    await selectPart();
    setQuantity('2');
    setSerials('SN-001\nSN-002');

    fireEvent.click(screen.getByRole('button', { name: /create work order/i }));

    expect(
      await screen.findByText('Value error, serial_numbers count (2) must equal quantity_ordered (3)')
    ).toBeInTheDocument();
  });
});
