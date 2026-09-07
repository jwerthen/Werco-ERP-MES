import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import { DocumentDelivery } from '../types/documentDelivery';
import { UserRole } from '../types';
import { setCustomPermissions } from '../utils/permissions';
import Purchasing from './Purchasing';

jest.mock('../components/ui/PdfPreview', () => ({
  __esModule: true,
  default: ({ title, url }: { title: string; url: string }) => <div title={title}><a href={url}>Download PDF</a></div>,
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getVendors: jest.fn(),
    getPurchaseOrders: jest.fn(),
    getParts: jest.fn(),
    listWorkspaceRecords: jest.fn(),
    listDocumentDeliveries: jest.fn(),
    previewDocumentDelivery: jest.fn(),
    sendDocumentDelivery: jest.fn(),
    getDocumentDeliveryAttachment: jest.fn(),
    reconcileDocumentDelivery: jest.fn(),
  },
}));
let mockAuthUser: { id: number; role: UserRole; is_superuser: boolean };
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: mockAuthUser, isAuthenticated: true, isLoading: false }),
}));
const mockedApi = api as jest.Mocked<typeof api>;
const delivery: DocumentDelivery = {
  id: 11,
  entity_type: 'purchase_order',
  entity_id: 10,
  document_number: 'PO-1001',
  recipient: 'buyer@example.test',
  subject: 'Purchase order PO-1001',
  body: 'Please review the attached purchase order.',
  attachment_name: 'PO-1001.pdf',
  attachment_sha256: 'abc',
  attachment_size: 500,
  status: 'prepared',
  status_detail: null,
  version: 1,
  provider_message_id: null,
  created_at: '2026-09-07T12:00:00',
  attempted_at: null,
  accepted_at: null,
  send_available: false,
  unavailable_reason: 'Your current role or read-only company context cannot send this document.',
  delivered: null,
  replayed: false,
  manually_verified: false,
  verified_at: null,
  verification_note: null,
};
function mount() {
  return render(
    <MemoryRouter>
      <Purchasing />
    </MemoryRouter>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  mockAuthUser = { id: 1, role: 'manager', is_superuser: false };
  setCustomPermissions(null);
  mockedApi.getVendors.mockResolvedValue([]);
  mockedApi.getPurchaseOrders.mockResolvedValue([
    {
      id: 10,
      po_number: 'PO-1001',
      vendor_id: 1,
      vendor_name: 'Example vendor',
      status: 'draft',
      order_date: '2026-09-07',
      total: 125,
      line_count: 1,
    },
  ]);
  mockedApi.getParts.mockResolvedValue([]);
  mockedApi.listWorkspaceRecords.mockResolvedValue([]);
  mockedApi.listDocumentDeliveries.mockResolvedValue([delivery]);
  mockedApi.getDocumentDeliveryAttachment.mockResolvedValue(new Blob(['PDF'], { type: 'application/pdf' }));
  URL.createObjectURL = jest.fn(() => 'blob:reviewed-po');
  URL.revokeObjectURL = jest.fn();
});
afterEach(() => setCustomPermissions(null));

test('manager with view but no approval can open existing history and PDF without email mutations', async () => {
  setCustomPermissions({ manager: ['purchasing:view'] });
  mount();
  const emailButtons = await screen.findAllByRole('button', { name: 'Email PO-1001' });
  fireEvent.click(emailButtons[0]);
  await screen.findByTitle('Reviewed document PDF');
  expect(mockedApi.listDocumentDeliveries).toHaveBeenCalledWith('purchase_order', 10);
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:reviewed-po');
  expect(screen.getByRole('button', { name: 'Prepare new email' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByLabelText('Message')).toHaveAttribute('readonly');
  expect(mockedApi.previewDocumentDelivery).not.toHaveBeenCalled();
  expect(mockedApi.sendDocumentDelivery).not.toHaveBeenCalled();
  expect(mockedApi.reconcileDocumentDelivery).not.toHaveBeenCalled();
});

test('view access alone does not give a supervisor access to privileged delivery history', async () => {
  mockAuthUser.role = 'supervisor';
  setCustomPermissions({ supervisor: ['purchasing:view', 'purchasing:approve'] });
  mount();
  await waitFor(() => expect(screen.getAllByText('PO-1001').length).toBeGreaterThan(0));
  expect(screen.queryByRole('button', { name: 'Email PO-1001' })).not.toBeInTheDocument();
  expect(mockedApi.listDocumentDeliveries).not.toHaveBeenCalled();
});

test('a manager whose customized permissions remove view cannot open delivery history', async () => {
  setCustomPermissions({ manager: ['purchasing:approve'] });
  mount();
  await waitFor(() => expect(screen.getAllByText('PO-1001').length).toBeGreaterThan(0));
  expect(screen.queryByRole('button', { name: 'Email PO-1001' })).not.toBeInTheDocument();
  expect(mockedApi.listDocumentDeliveries).not.toHaveBeenCalled();
});
