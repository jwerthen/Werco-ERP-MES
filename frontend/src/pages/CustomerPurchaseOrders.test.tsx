import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router-dom';
import api from '../services/api';
import CustomerPurchaseOrders from './CustomerPurchaseOrders';
import { customerPoDashboardUrl } from '../utils/customerPurchaseOrders';
import type {
  CustomerPODashboardResponse,
  CustomerPOListResponse,
  CustomerPOStats,
  CustomerPOWorkOrder,
} from '../types/customerPurchaseOrders';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getCustomerPurchaseOrders: jest.fn(), getCustomerPODashboard: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;
const stats: CustomerPOStats = {
  total_work_orders: 52,
  active_work_orders: 32,
  completed_work_orders: 19,
  cancelled_work_orders: 1,
  in_progress_work_orders: 20,
  on_hold_work_orders: 3,
  overdue_work_orders: 5,
  due_today_work_orders: 2,
  completion_percent: (19 / 51) * 100,
  next_due_date: '2026-10-28',
};
const wo: CustomerPOWorkOrder = {
  id: 42,
  work_order_number: 'WO-0042',
  part_id: 10,
  part_number: 'BRACKET-1',
  part_name: 'Bracket',
  work_order_type: 'production',
  status: 'in_progress',
  priority: 2,
  quantity_ordered: 50,
  quantity_complete: 12,
  quantity_scrapped: 1,
  operation_count: 3,
  operations_complete: 1,
  operation_progress_percent: 40,
  customer_name: 'Acme & Co',
  customer_po: 'PO/10 & 20%_',
  po_line_item: '002',
  po_date: '2026-10-01',
  must_ship_by: '2026-10-27',
  due_date: '2026-10-29',
  unit_number: 'WELD-20',
  is_overdue: true,
  current_operations: [
    { id: 10, operation_number: 'OP20', name: 'Weld frame', status: 'in_progress', work_center_name: 'Weld cell 2' },
  ],
};
const dashboard: CustomerPODashboardResponse = {
  customer_name: 'Acme & Co',
  customer_po: 'PO/10 & 20%_',
  stats,
  items: [wo],
  total: 52,
  skip: 0,
  limit: 25,
  has_next: true,
  as_of_date: '2026-10-28',
};
const overview: CustomerPOListResponse = {
  items: [{ customer_name: dashboard.customer_name, customer_po: dashboard.customer_po, stats }],
  total: 1,
  skip: 0,
  limit: 25,
  has_next: false,
  as_of_date: '2026-10-28',
};
function Navigator() {
  const navigate = useNavigate();
  return <button onClick={() => navigate(customerPoDashboardUrl('Another customer', 'PO-B'))}>Switch PO</button>;
}
function renderPage(url = '/work-orders/customer-pos') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <CustomerPurchaseOrders />
      <Navigator />
    </MemoryRouter>
  );
}
const selectedUrl = customerPoDashboardUrl(dashboard.customer_name, dashboard.customer_po);

beforeEach(() => {
  jest.clearAllMocks();
  window.innerWidth = 1440;
  sessionStorage.setItem('user', JSON.stringify({ id: 1, company_id: 1 }));
  mockedApi.getCustomerPurchaseOrders.mockResolvedValue(overview);
  mockedApi.getCustomerPODashboard.mockResolvedValue(dashboard);
});
afterEach(() => {
  jest.useRealTimers();
  sessionStorage.clear();
});

it('opens a PO using its exact customer and encoded PO number', async () => {
  renderPage();
  const link = await screen.findByRole('link', { name: dashboard.customer_po });
  expect(link).toHaveAttribute('href', selectedUrl);
  expect(mockedApi.getCustomerPODashboard).not.toHaveBeenCalled();
  fireEvent.click(link);
  expect(await screen.findByRole('heading', { name: `PO ${dashboard.customer_po}` })).toBeInTheDocument();
  await screen.findByRole('link', { name: wo.work_order_number });
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledWith(
    {
      customer_name: 'Acme & Co',
      customer_po: 'PO/10 & 20%_',
      skip: 0,
      limit: 25,
    },
    expect.any(AbortSignal)
  );
});

it('shows global PO totals, routing progress, current station, quantities and detail links', async () => {
  renderPage(selectedUrl);
  const table = await screen.findByRole('table');
  expect(screen.getByText('52')).toBeInTheDocument();
  expect(screen.getByText(/19 of 51 non-cancelled work orders/)).toBeInTheDocument();
  expect(screen.getByRole('progressbar', { name: 'Jobs complete' })).toHaveAttribute(
    'aria-valuenow',
    String(stats.completion_percent)
  );
  expect(within(table).getByRole('progressbar', { name: '1/3 ops complete' })).toHaveAttribute('aria-valuenow', '40');
  expect(within(table).getByText('OP20 · Weld frame')).toBeInTheDocument();
  expect(within(table).getByText(/Weld cell 2/)).toBeInTheDocument();
  expect(within(table).getByText('12 / 50')).toBeInTheDocument();
  expect(within(table).getByText('1 scrapped')).toBeInTheDocument();
  expect(within(table).getByText('PO line 002')).toBeInTheDocument();
  expect(within(table).getByText('WELD-20')).toBeInTheDocument();
  expect(within(table).getByRole('link', { name: 'WO-0042' })).toHaveAttribute('href', '/work-orders/42');
  expect(within(table).getByText('Overdue')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  await waitFor(() =>
    expect(mockedApi.getCustomerPODashboard).toHaveBeenLastCalledWith(
      expect.objectContaining({ skip: 25, limit: 25, customer_po: dashboard.customer_po }),
      expect.any(AbortSignal)
    )
  );
});

it('keeps completed and cancelled jobs visible and labels child laser quantities separately on phones', async () => {
  window.innerWidth = 390;
  mockedApi.getCustomerPODashboard.mockResolvedValue({
    ...dashboard,
    items: [
      {
        ...wo,
        id: 43,
        work_order_number: 'WO-0043',
        work_order_type: 'laser_cutting',
        parent_work_order_id: 42,
        status: 'complete',
        quantity_ordered: 2,
        quantity_complete: 2,
      },
      { ...wo, id: 44, work_order_number: 'WO-0044', status: 'cancelled' },
    ],
  });
  renderPage(selectedUrl);
  expect(await screen.findByRole('link', { name: 'WO-0043' })).toHaveAttribute('href', '/work-orders/43');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
  expect(screen.getByText('sheet runs complete')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'View parent' })).toHaveAttribute('href', '/work-orders/42');
  expect(screen.getByText('Work finished')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'WO-0044' })).toBeInTheDocument();
  expect(screen.queryByText('OP20 · Weld frame')).not.toBeInTheDocument();
});

it('loads unassigned-customer POs without broadening the customer filter', async () => {
  renderPage(customerPoDashboardUrl(null, 'PO-NULL'));
  await waitFor(() =>
    expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledWith(
      { customer_name: '', customer_po: 'PO-NULL', skip: 0, limit: 25 },
      expect.any(AbortSignal)
    )
  );
  expect(screen.getByText('No customer assigned · Production dashboard')).toBeInTheDocument();
});

it('shows an initial error with retry and a truthful empty result', async () => {
  mockedApi.getCustomerPurchaseOrders.mockRejectedValueOnce(new Error('Offline'));
  mockedApi.getCustomerPurchaseOrders.mockResolvedValue({ ...overview, items: [], total: 0 });
  renderPage();
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load customer purchase orders');
  expect(screen.queryByText('No customer purchase orders')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No customer purchase orders')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('retains but marks stale data when a refresh fails', async () => {
  renderPage(selectedUrl);
  await screen.findByRole('link', { name: wo.work_order_number });
  mockedApi.getCustomerPODashboard.mockRejectedValueOnce(new Error('Offline'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Showing the last successful update');
  expect(screen.getByRole('link', { name: wo.work_order_number })).toBeInTheDocument();
});

it('discards a previous PO response after navigation', async () => {
  let resolveOld!: (value: CustomerPODashboardResponse) => void;
  mockedApi.getCustomerPODashboard.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  mockedApi.getCustomerPODashboard.mockResolvedValueOnce({
    ...dashboard,
    customer_po: 'PO-B',
    customer_name: 'Another customer',
    items: [{ ...wo, id: 99, work_order_number: 'WO-NEW' }],
  });
  renderPage(selectedUrl);
  fireEvent.click(screen.getByRole('button', { name: 'Switch PO' }));
  await screen.findByRole('link', { name: 'WO-NEW' });
  await act(async () => resolveOld(dashboard));
  expect(screen.getByRole('link', { name: 'WO-NEW' })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: wo.work_order_number })).not.toBeInTheDocument();
});

it('clears the old company view on auth changes and discards its pending response', async () => {
  let resolveOld!: (value: CustomerPODashboardResponse) => void;
  mockedApi.getCustomerPODashboard.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  mockedApi.getCustomerPODashboard.mockResolvedValueOnce({ ...dashboard, items: [], total: 0 });
  renderPage(selectedUrl);
  act(() => {
    sessionStorage.setItem('user', JSON.stringify({ id: 2, company_id: 2 }));
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  await screen.findByText('No attached work orders');
  await act(async () => resolveOld(dashboard));
  expect(screen.queryByRole('link', { name: wo.work_order_number })).not.toBeInTheDocument();
});

it('immediately clears old company rows when only the credential changes and rejects its pending refresh', async () => {
  let resolveOld!: (value: CustomerPODashboardResponse) => void;
  let resolveNew!: (value: CustomerPODashboardResponse) => void;
  const savedHomeUser = sessionStorage.getItem('user');
  sessionStorage.setItem('token', 'home-company-credential');
  mockedApi.getCustomerPODashboard.mockResolvedValueOnce(dashboard);
  mockedApi.getCustomerPODashboard.mockReturnValueOnce(
    new Promise(resolve => {
      resolveOld = resolve;
    })
  );
  mockedApi.getCustomerPODashboard.mockReturnValueOnce(
    new Promise(resolve => {
      resolveNew = resolve;
    })
  );
  renderPage(selectedUrl);
  await screen.findByRole('link', { name: wo.work_order_number });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledTimes(2);

  act(() => {
    // CompanyContext emits this before changing viewingCompanyId and never
    // changes the user's home company field during a platform switch.
    sessionStorage.setItem('token', 'other-company-credential');
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(sessionStorage.getItem('user')).toBe(savedHomeUser);
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledTimes(3);
  expect(screen.queryByRole('link', { name: wo.work_order_number })).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Loading purchase order dashboard');

  await act(async () => resolveOld(dashboard));
  expect(screen.queryByRole('link', { name: wo.work_order_number })).not.toBeInTheDocument();
  await act(async () => resolveNew({ ...dashboard, items: [{ ...wo, id: 99, work_order_number: 'WO-NEW-COMPANY' }] }));
  expect(screen.getByRole('link', { name: 'WO-NEW-COMPANY' })).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: wo.work_order_number })).not.toBeInTheDocument();
});

it('refreshes on focus and every 30 seconds', async () => {
  jest.useFakeTimers();
  renderPage(selectedUrl);
  await act(async () => {});
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledTimes(1);
  await act(async () => {
    jest.advanceTimersByTime(30_000);
  });
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledTimes(2);
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
  });
  expect(mockedApi.getCustomerPODashboard).toHaveBeenCalledTimes(3);
});

it('searches on the server and returns to the first page', async () => {
  jest.useFakeTimers();
  renderPage('/work-orders/customer-pos?page=2');
  await act(async () => {});
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a customer purchase order' }), {
    target: { value: 'customer 20' },
  });
  await act(async () => {
    jest.advanceTimersByTime(300);
  });
  expect(mockedApi.getCustomerPurchaseOrders).toHaveBeenLastCalledWith(
    { search: 'customer 20', skip: 0, limit: 25 },
    expect.any(AbortSignal)
  );
});
