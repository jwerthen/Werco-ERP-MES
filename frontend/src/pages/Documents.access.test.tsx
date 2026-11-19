/**
 * Documents page: restricted document viewing (AS9100D 7.5.3.1).
 *
 *   1. every row carries its access level, and the access filter is a URL param;
 *   2. upload sends the chosen `access_level`, and a response saved ABOVE it is a
 *      warning (the upload succeeded, not at the level asked for), never a plain success;
 *   3. the Access review panel is Admin / Quality only, lists the server's rows with
 *      a link that opens the document, and counts the part-less ones it cannot judge.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import Documents from './Documents';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import type { UserRole } from '../types';

let mockRole: UserRole = 'quality';
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));
jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getDocuments: jest.fn(),
    getParts: jest.fn(),
    getDocumentTypes: jest.fn(),
    uploadDocument: jest.fn(),
    deleteDocument: jest.fn(),
    getDocumentAccessReview: jest.fn(),
    // The detail opened from the review panel.
    getDocument: jest.fn(),
    getDocumentRevisions: jest.fn(),
    downloadDocument: jest.fn(),
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const rows = [
  {
    id: 1,
    document_number: 'DOC-001',
    revision: 'A',
    title: 'Open procedure',
    document_type: 'procedure',
    status: 'released',
    created_at: '2026-09-13',
    access_level: 'standard',
  },
  {
    id: 2,
    document_number: 'DOC-002',
    revision: 'A',
    title: 'Supervisor sheet',
    document_type: 'work_instruction',
    status: 'released',
    created_at: '2026-09-14',
    access_level: 'restricted',
  },
  {
    id: 3,
    document_number: 'DOC-003',
    revision: 'B',
    title: 'ITAR drawing',
    document_type: 'drawing',
    status: 'released',
    created_at: '2026-09-15',
    access_level: 'export_controlled',
  },
];

let location = '';
function LocationProbe() {
  const current = useLocation();
  location = `${current.pathname}${current.search}`;
  return null;
}

function renderPage(entry = '/documents') {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[entry]}>
        <Documents />
        <LocationProbe />
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
  mockedApi.getParts.mockResolvedValue([{ id: 7, part_number: 'PN-700', name: 'Bracket' }] as any);
  mockedApi.getDocumentTypes.mockResolvedValue([
    { value: 'drawing', label: 'Drawing' },
    { value: 'procedure', label: 'Procedure' },
  ]);
  mockedApi.getDocuments.mockResolvedValue(rows);
  mockedApi.getDocumentAccessReview.mockResolvedValue({ documents: [], unlinked_count: 0 });
});

it('marks each row with its access level and filters by level through the URL', async () => {
  renderPage();
  const table = await screen.findByRole('table');
  await within(table).findByText('ITAR drawing');
  const levels = within(table)
    .getAllByTestId('document-access-badge')
    .map(badge => badge.getAttribute('data-access-level'));
  expect(levels.sort()).toEqual(['export_controlled', 'restricted', 'standard']);

  fireEvent.change(screen.getByRole('combobox', { name: 'Filter by access level' }), {
    target: { value: 'export_controlled' },
  });
  await waitFor(() => expect(within(table).queryByText('Open procedure')).not.toBeInTheDocument());
  expect(within(table).getByText('ITAR drawing')).toBeInTheDocument();
  expect(location).toContain('access=export_controlled');
});

it('opens with the filter from the URL', async () => {
  renderPage('/documents?access=restricted');
  const table = await screen.findByRole('table');
  expect(await within(table).findByText('Supervisor sheet')).toBeInTheDocument();
  expect(within(table).queryByText('ITAR drawing')).not.toBeInTheDocument();
  expect(screen.getByRole('combobox', { name: 'Filter by access level' })).toHaveValue('restricted');
});

it('sends the chosen access level on upload and warns when the server raised it', async () => {
  mockedApi.uploadDocument.mockResolvedValue({
    ...rows[2],
    id: 9,
    access_level: 'export_controlled',
    access_level_requested: 'restricted',
    access_level_raised: true,
    access_level_raised_because: ['export_controlled_customer'],
  });
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Upload Document' }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText(/File/), {
    target: { files: [new File(['%PDF'], 'bracket.pdf', { type: 'application/pdf' })] },
  });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /Title/ }), { target: { value: 'Bracket drawing' } });
  fireEvent.change(within(dialog).getByRole('combobox', { name: /Associated Part/ }), { target: { value: '7' } });
  const level = within(dialog).getByRole('combobox', { name: /Access level/ });
  expect(level).toHaveValue('standard');
  fireEvent.change(level, { target: { value: 'restricted' } });
  fireEvent.submit(dialog.querySelector('form')!);

  await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
  expect((mockedApi.uploadDocument.mock.calls[0][0] as FormData).get('access_level')).toBe('restricted');
  expect(
    await screen.findByText(
      "Document uploaded as Export-controlled, not Restricted: the linked part's customer is export-controlled, so the server raised it."
    )
  ).toBeInTheDocument();
  expect(screen.queryByText('Document uploaded')).not.toBeInTheDocument();
});

it('a plain success when the server kept the requested level', async () => {
  mockedApi.uploadDocument.mockResolvedValue({ ...rows[0], id: 10, access_level: 'standard' });
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'Upload Document' }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText(/File/), {
    target: { files: [new File(['%PDF'], 'proc.pdf', { type: 'application/pdf' })] },
  });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /Title/ }), { target: { value: 'Procedure' } });
  fireEvent.change(within(dialog).getByRole('combobox', { name: /^Type/ }), { target: { value: 'procedure' } });
  fireEvent.submit(dialog.querySelector('form')!);
  expect(await screen.findByText('Document uploaded')).toBeInTheDocument();
  expect((mockedApi.uploadDocument.mock.calls[0][0] as FormData).get('access_level')).toBe('standard');
});

it.each<UserRole>(['manager', 'supervisor', 'operator', 'viewer'])('%s gets no access review', async role => {
  mockRole = role;
  renderPage('/documents?review=1');
  await screen.findByRole('table');
  expect(screen.queryByRole('button', { name: 'Access review' })).not.toBeInTheDocument();
  expect(screen.queryByTestId('document-access-review')).not.toBeInTheDocument();
  expect(mockedApi.getDocumentAccessReview).not.toHaveBeenCalled();
});

it.each<UserRole>(['admin', 'quality', 'platform_admin'])(
  '%s opens the access review, sees its rows and opens a document from it',
  async role => {
    mockRole = role;
    mockedApi.getDocumentAccessReview.mockResolvedValue({
      documents: [
        {
          id: 1,
          document_number: 'DOC-001',
          revision: 'A',
          title: 'Open procedure',
          part_number: 'PN-700',
          customer_name: 'Acme Aero',
          access_level: 'standard',
        },
      ],
      unlinked_count: 2,
    });
    mockedApi.getDocument.mockResolvedValue({ ...rows[0], mime_type: 'text/plain' });
    mockedApi.getDocumentRevisions.mockResolvedValue([rows[0]]);
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Access review' }));
    const panel = await screen.findByTestId('document-access-review');
    expect(location).toContain('review=1');
    expect(await within(panel).findByText('1 document to review.')).toBeInTheDocument();
    expect(within(panel).getAllByText('Acme Aero').length).toBeGreaterThan(0);
    expect(within(panel).getByText(/2 documents have no part link/)).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Open DOC-001 Rev A' }));
    await waitFor(() => expect(location).toContain('document=1'));
    await waitFor(() => expect(mockedApi.getDocument).toHaveBeenCalledWith(1));
  }
);

it('an empty review says nothing needs review', async () => {
  renderPage('/documents?review=1');
  const panel = await screen.findByTestId('document-access-review');
  expect((await within(panel).findAllByText('No documents need review')).length).toBeGreaterThan(0);
  expect(within(panel).queryByText(/to review\./)).not.toBeInTheDocument();
});
