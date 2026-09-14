import React from 'react';
import { render, screen } from '@testing-library/react';
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
    expect(screen.getByRole('button', { name: 'Preview / History' })).toBeInTheDocument();
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
