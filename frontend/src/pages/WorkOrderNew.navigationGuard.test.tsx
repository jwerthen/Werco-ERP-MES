import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom';
import WorkOrderNew from './WorkOrderNew';
import { UnsavedChangesProvider } from '../context/UnsavedChangesContext';
import { ToastProvider } from '../components/ui/Toast';
import api from '../services/api';
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
class RouterRequest {
  url: string;
  signal?: AbortSignal;
  method: string;
  constructor(url: string, init: RequestInit = {}) {
    this.url = url;
    this.signal = init.signal ?? undefined;
    this.method = init.method ?? 'GET';
  }
}
beforeAll(() => {
  global.Request = RouterRequest as unknown as typeof Request;
});
it('protects the actual draft after failed creation validation through App wildcard/descendant routing', async () => {
  const mockedApi = api as jest.Mocked<typeof api>;
  mockedApi.getParts.mockResolvedValue([
    {
      id: 1,
      part_number: 'WERCO-001-01',
      name: 'Plate',
      revision: 'A',
      part_type: 'manufactured',
      status: 'active',
      is_active: true,
      unit_of_measure: 'EA',
    },
  ] as any);
  mockedApi.getBOMs.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getPartReadiness.mockResolvedValue({
    ready: false,
    blockers: ['No active routing'],
    warnings: [],
    checks: {},
  });
  mockedApi.getRoutingByPart.mockResolvedValue(null);
  const router = createMemoryRouter(
    [
      {
        path: '*',
        element: (
          <ToastProvider>
            <UnsavedChangesProvider>
              <Routes>
                <Route path="/work-orders/new" element={<WorkOrderNew />} />
                <Route path="/work-orders" element={<p>Work order list destination</p>} />
              </Routes>
            </UnsavedChangesProvider>
          </ToastProvider>
        ),
      },
    ],
    { initialEntries: ['/work-orders/new'] }
  );
  render(<RouterProvider router={router} />);
  await screen.findByTestId('wo-serial-numbers');
  fireEvent.change(screen.getByRole('combobox', { name: /^Part/ }), { target: { value: 'WERCO-001-01' } });
  fireEvent.mouseDown(await screen.findByRole('option', { name: /WERCO-001-01/ }));
  await waitFor(() => expect(mockedApi.getPartReadiness).toHaveBeenCalledWith(1));
  fireEvent.click(screen.getByRole('button', { name: /Create Work Order/ }));
  expect(mockedApi.createWorkOrder).not.toHaveBeenCalled();
  fireEvent.click(
    within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getByRole('link', { name: 'Work Orders' })
  );
  expect(await screen.findByRole('dialog', { name: 'Leave with unsaved changes?' })).toBeInTheDocument();
  fireEvent.click(screen.getByText('Stay and keep editing'));
  expect(screen.getByRole('combobox', { name: /^Part/ })).toHaveValue('WERCO-001-01 - Plate');
  expect(router.state.location.pathname).toBe('/work-orders/new');
});
