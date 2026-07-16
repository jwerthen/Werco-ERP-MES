/**
 * Purchasing — Edit Vendor modal: editable Vendor Code.
 *
 * The Edit Vendor modal gained a required "Vendor Code" FormField wired into
 * editVendorForm and the PUT payload (`code: trim() || undefined`). This locks:
 *  - opening the Edit modal prefills the Vendor Code input from the vendor row
 *  - editing the code and saving sends the trimmed code (case preserved — the
 *    server normalizes to uppercase) in the api.updateVendor payload, alongside
 *    the vendor's own optimistic-lock version.
 *
 * Follows Purchasing.cockpit.test.tsx (api-client mock + fixtures) and
 * Customers.doubleSubmit.test.tsx (modal form interaction via fireEvent).
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import Purchasing from './Purchasing';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    getDocuments: jest.fn(),
    getDocumentTypes: jest.fn(),
    updateVendor: jest.fn(),
  },
}));

// The page gates its create/send actions by role (useAuth), so tests need an
// authenticated user; manager sees every action.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'manager' }, isAuthenticated: true, isLoading: false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const vendors = [
  {
    id: 1,
    code: 'VND-001',
    name: 'Acme Aerospace',
    contact_name: 'Pat Lee',
    email: 'pat@acme.test',
    phone: '555-0101',
    address_line1: '1 Flight Way',
    city: 'Wichita',
    state: 'KS',
    postal_code: '67201',
    country: 'US',
    payment_terms: 'NET 30',
    is_approved: true,
    is_as9100_certified: true,
    is_iso9001_certified: false,
    is_active: true,
    notes: '',
    version: 0,
  },
  {
    id: 2,
    code: 'VND-002',
    name: 'Bravo Metals',
    contact_name: 'Sam Roe',
    email: 'sam@bravo.test',
    is_approved: false,
    is_as9100_certified: false,
    is_iso9001_certified: false,
    is_active: true,
    payment_terms: '',
    version: 3,
  },
];

const renderPurchasing = () =>
  render(
    <MemoryRouter initialEntries={['/purchasing']}>
      <Purchasing />
    </MemoryRouter>
  );

/** Load the page, switch to the Vendors tab, and open the Edit modal for `vendorName`. */
async function openEditModalFor(vendorName: string) {
  renderPurchasing();
  await screen.findByRole('heading', { name: 'Purchasing & Receiving' });

  fireEvent.click(screen.getByRole('button', { name: /^Vendors/i }));

  const row = (await screen.findByText(vendorName)).closest('tr')!;
  fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));

  // Modal is open (portals to document.body).
  await screen.findByRole('heading', { name: 'Edit Vendor' });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getVendors.mockResolvedValue(vendors as any);
  mockedApi.getPurchaseOrders.mockResolvedValue([] as any);
  mockedApi.getParts.mockResolvedValue([] as any);
  mockedApi.getDocuments.mockResolvedValue([] as any);
  mockedApi.getDocumentTypes.mockResolvedValue([] as any);
  mockedApi.updateVendor.mockResolvedValue({} as any);
});

test('opening the Edit modal shows a Vendor Code input prefilled with the vendor code', async () => {
  await openEditModalFor('Acme Aerospace');

  const codeInput = screen.getByLabelText(/vendor code/i);
  expect(codeInput).toBeRequired();
  expect(codeInput).toHaveValue('VND-001');

  // The modal also kicked off the vendor-documents load for the right vendor.
  await waitFor(() => expect(mockedApi.getDocuments).toHaveBeenCalledWith({ vendor_id: 1 }));
});

test('editing the code and saving sends the trimmed code and the vendor version in the PUT payload', async () => {
  await openEditModalFor('Bravo Metals');

  const codeInput = screen.getByLabelText(/vendor code/i);
  expect(codeInput).toHaveValue('VND-002');

  // Case is preserved client-side (the API uppercases); outer whitespace is trimmed.
  fireEvent.change(codeInput, { target: { value: '  brv-777  ' } });
  expect(codeInput).toHaveValue('  brv-777  ');

  fireEvent.submit(codeInput.closest('form')!);

  await waitFor(() => expect(mockedApi.updateVendor).toHaveBeenCalledTimes(1));
  expect(mockedApi.updateVendor).toHaveBeenCalledWith(
    2,
    expect.objectContaining({
      version: 3,
      code: 'brv-777',
      name: 'Bravo Metals',
    })
  );

  // Success path: the modal closes and the list refetches.
  await waitFor(() => expect(mockedApi.getVendors).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('heading', { name: 'Edit Vendor' })).not.toBeInTheDocument();
});
