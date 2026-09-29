import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Documents from './Documents';
import api from '../services/api';
import type { UserRole } from '../types';

let mockRole: UserRole = 'viewer';
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
  },
}));
const mockedApi = api as jest.Mocked<typeof api>;
beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'viewer';
  mockedApi.getParts.mockResolvedValue([]);
  mockedApi.getDocumentTypes.mockResolvedValue([{ value: 'drawing', label: 'Drawing' }]);
  mockedApi.getDocuments.mockResolvedValue([
    {
      id: 7,
      document_number: 'DOC-007',
      revision: 'A',
      title: 'Controlled drawing',
      document_type: 'drawing',
      file_name: 'drawing.pdf',
      status: 'released',
      created_at: '2026-09-13',
    },
  ]);
});

it.each<UserRole>(['admin', 'manager', 'platform_admin', 'quality', 'supervisor', 'operator', 'shipping', 'viewer'])(
  '%s has readable documents and the correct publish/delete controls',
  async role => {
    mockRole = role;
    render(
      <MemoryRouter>
        <Documents />
      </MemoryRouter>
    );
    expect((await screen.findAllByText('Controlled drawing')).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: 'Preview / History' })).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'Download document' })).toBeInTheDocument();
    expect(!!screen.queryByRole('button', { name: 'Upload Document' })).toBe(
      ['admin', 'manager', 'platform_admin', 'quality'].includes(role)
    );
    expect(!!screen.queryByRole('button', { name: 'Delete document' })).toBe(
      ['admin', 'manager', 'platform_admin'].includes(role)
    );
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
    expect(mockedApi.deleteDocument).not.toHaveBeenCalled();
  }
);

it('a viewer empty state does not offer document publishing', async () => {
  mockedApi.getDocuments.mockResolvedValue([]);
  render(
    <MemoryRouter>
      <Documents />
    </MemoryRouter>
  );
  expect((await screen.findAllByText('No documents')).length).toBeGreaterThan(0);
  expect(screen.queryByRole('button', { name: 'Upload Document' })).not.toBeInTheDocument();
});

it('requires an explicit valid part before uploading and sends that existing association', async () => {
  mockRole = 'quality';
  mockedApi.getParts.mockResolvedValue([{ id: 7, part_number: 'PN-700', name: 'Bracket' }] as any);
  mockedApi.uploadDocument.mockResolvedValue({});
  render(<MemoryRouter><Documents /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Upload Document' }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText(/File/), { target: { files: [new File(['drawing'], 'PN-700-drawing.pdf', { type: 'application/pdf' })] } });
  fireEvent.change(within(dialog).getByRole('textbox', { name: /Title/ }), { target: { value: 'Bracket drawing' } });
  fireEvent.submit(dialog.querySelector('form')!);
  expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
  expect(within(dialog).getByRole('alert')).toHaveTextContent('Select the associated part');
  const associatedPart = within(dialog).getByRole('combobox', { name: /Associated Part/ });
  expect(associatedPart).toBeRequired();
  expect(associatedPart).toHaveValue('');
  fireEvent.change(associatedPart, { target: { value: '7' } });
  fireEvent.submit(dialog.querySelector('form')!);
  await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
  expect(mockedApi.uploadDocument.mock.calls[0][0].get('part_id')).toBe('7');
});

it('finds documents by their linked part number and exposes the full long title', async () => {
  const title = 'Controlled manufacturing drawing with detailed revision notes for the fixture mounting assembly and inspection requirements';
  mockedApi.getParts.mockResolvedValue([{ id: 7, part_number: 'PN-700', name: 'Bracket' }] as any);
  mockedApi.getDocuments.mockResolvedValue([{ id: 7, document_number: 'DOC-007', revision: 'A', title, document_type: 'drawing', part_id: 7, created_at: '2026-09-13' }] as any);
  render(<MemoryRouter><Documents /></MemoryRouter>);
  await screen.findAllByText('PN-700');
  const search = screen.getByRole('textbox', { name: /Search documents/i });
  fireEvent.change(search, { target: { value: 'PN-700' } });
  await waitFor(() => expect(screen.getByTitle(title)).toBeInTheDocument());
  expect(within(screen.getByRole('table')).getByText('PN-700')).toBeInTheDocument();
});
