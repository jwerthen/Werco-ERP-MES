import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '../services/api';
import WorkOrderDetail from './WorkOrderDetail';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getWorkOrder: jest.fn(),
    getOperationDetails: jest.fn(),
    getMaterialRequirements: jest.fn(),
    getWorkOrderBlockers: jest.fn(),
    getActiveUsers: jest.fn(),
    getUsers: jest.fn(),
    getDocuments: jest.fn(),
    getMaterialAllocations: jest.fn(),
    getWorkCenters: jest.fn(),
    downloadDocument: jest.fn(),
    fetchLaserNestDocument: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 1, role: 'admin', is_superuser: true }, isAuthenticated: true }),
}));
jest.mock('../hooks/useWebSocket', () => ({ useWebSocket: jest.fn() }));
jest.mock('../services/realtime', () => ({ getAccessToken: () => null }));
// The shared PDF renderer has its own rendering/pagination tests. Here we check
// which authorized document reaches it and the popup's interaction/lifecycle.
jest.mock('../components/ui/PdfPreview', () => ({
  __esModule: true,
  default: ({ url, fileName }: { url: string; fileName: string }) => (
    <a href={url} download={fileName}>
      Download PDF
    </a>
  ),
}));

const mockedApi = api as jest.Mocked<typeof api>;
const documents = [101, 102].map((id, index) => ({
  id,
  title: `0660${index + 5}`,
  file_name: `0660${index + 5}.pdf`,
  document_number: `DRA-${id}`,
  revision: 'A',
  document_type: 'drawing',
  mime_type: 'application/pdf',
  status: 'approved',
  file_size: 262144,
  work_order_id: 42,
  created_at: '2026-09-16T12:00:00Z',
}));
const operation = {
  id: 71,
  version: 1,
  work_order_id: 42,
  work_center_id: 5,
  work_center_name: 'Laser',
  sequence: 10,
  operation_number: 'OP10',
  name: 'Laser nest',
  status: 'ready',
  quantity_complete: 0,
  quantity_scrapped: 0,
  estimated_hours: 1,
  laser_nest: {
    id: 501,
    nest_name: 'Sheet 1',
    cnc_number: '06606',
    planned_runs: 3,
    completed_runs: 0,
    remaining_runs: 3,
    has_document: true,
    document_file_name: '06606-nest.pdf',
  },
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/work-orders/42']}>
      <Routes>
        <Route path="/work-orders/:id" element={<WorkOrderDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.resetAllMocks();
  mockedApi.getWorkOrder.mockResolvedValue({
    id: 42,
    version: 1,
    work_order_number: 'WO-0042',
    part_id: null,
    work_order_type: 'laser_cutting',
    quantity_ordered: 3,
    quantity_complete: 0,
    quantity_scrapped: 0,
    status: 'released',
    priority: 3,
    estimated_hours: 1,
    actual_hours: 0,
    created_at: '2026-09-16T12:00:00Z',
    updated_at: '2026-09-16T12:00:00Z',
    operations: [operation],
  });
  mockedApi.getOperationDetails.mockResolvedValue({ all_operations: [] });
  mockedApi.getMaterialRequirements.mockResolvedValue(null);
  mockedApi.getWorkOrderBlockers.mockResolvedValue([]);
  mockedApi.getActiveUsers.mockResolvedValue([]);
  mockedApi.getUsers.mockResolvedValue([]);
  mockedApi.getMaterialAllocations.mockResolvedValue([]);
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getDocuments.mockImplementation(async params => (params?.work_order_id ? documents : []));
  mockedApi.downloadDocument.mockResolvedValue(new Blob(['pdf'], { type: 'application/pdf' }));
  mockedApi.fetchLaserNestDocument.mockResolvedValue('blob:nest');
  jest.spyOn(window.URL, 'createObjectURL').mockReturnValue('blob:drawing');
  jest.spyOn(window.URL, 'revokeObjectURL').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

it('loads only the clicked attachment in a popup, closes with Escape, and reopens another drawing', async () => {
  renderDetail();
  const trigger = await screen.findByRole('button', { name: 'Preview 06606' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(mockedApi.downloadDocument).not.toHaveBeenCalled();

  trigger.focus();
  fireEvent.click(trigger);
  const dialog = await screen.findByRole('dialog', { name: 'Preview 06606' });
  expect(await within(dialog).findByRole('link', { name: 'Download PDF' })).toHaveAttribute('download', '06606.pdf');
  expect(mockedApi.downloadDocument).toHaveBeenCalledWith(102);
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:drawing');

  fireEvent.click(screen.getByRole('button', { name: 'Preview 06605' }));
  const nextDialog = await screen.findByRole('dialog', { name: 'Preview 06605' });
  expect(await within(nextDialog).findByRole('link', { name: 'Download PDF' })).toHaveAttribute(
    'download',
    '06605.pdf'
  );
  expect(mockedApi.downloadDocument).toHaveBeenLastCalledWith(101);
  fireEvent.click(within(nextDialog).getByRole('button', { name: 'Close preview' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('discards a late download after closing without replacing the next nest preview', async () => {
  let resolveDownload!: (value: Blob) => void;
  mockedApi.downloadDocument.mockReturnValueOnce(
    new Promise(resolve => {
      resolveDownload = resolve;
    })
  );
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: 'Preview 06605' }));
  expect(await screen.findByText('Loading PDF preview…')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
  fireEvent.click(screen.getByRole('button', { name: 'Preview nest 06606' }));
  const dialog = await screen.findByRole('dialog', { name: 'Preview Nest 06606' });
  expect(await within(dialog).findByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:nest');

  await act(async () => resolveDownload(new Blob(['late PDF'])));
  expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:drawing');
  expect(within(dialog).getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:nest');
  expect(mockedApi.fetchLaserNestDocument).toHaveBeenCalledWith(501);
  fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
  expect(window.URL.revokeObjectURL).toHaveBeenCalledWith('blob:nest');
});

it('reports download failures in the popup and lets the user retry', async () => {
  mockedApi.downloadDocument.mockRejectedValueOnce(new Error('Network unavailable'));
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: 'Preview 06605' }));
  const dialog = await screen.findByRole('dialog', { name: 'Preview 06605' });
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Could not load PDF preview');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
  expect(await within(dialog).findByRole('link', { name: 'Download PDF' })).toBeInTheDocument();
  expect(mockedApi.downloadDocument).toHaveBeenCalledTimes(2);
});

it('opens View PDF in the same popup and dismisses it through the backdrop', async () => {
  renderDetail();
  fireEvent.click(await screen.findByRole('button', { name: 'View PDF' }));
  const dialog = await screen.findByRole('dialog', { name: 'Preview Nest 06606' });
  expect(await within(dialog).findByRole('link', { name: 'Download PDF' })).toHaveAttribute(
    'download',
    '06606-nest.pdf'
  );
  fireEvent.click(dialog.parentElement!);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});
