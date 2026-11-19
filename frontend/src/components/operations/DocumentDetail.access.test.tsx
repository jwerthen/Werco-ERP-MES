/**
 * Document detail: restricted document viewing (AS9100D 7.5.3.1).
 *
 *   1. a 404 reads "Document not available": the server answers 404 for a document
 *      the caller may not view, so the UI never says which it was, and offers no Retry;
 *   2. the record shows its access level; Change access is Admin / Quality only;
 *   3. a change shows the server's level (non-optimistic) and, when nobody can open the
 *      export-controlled result, keeps a warning on the record;
 *   4. a revision upload asks for the predecessor's level.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import DocumentDetail from './DocumentDetail';
import api from '../../services/api';
import type { UserRole } from '../../types';

let mockRole: UserRole = 'quality';
jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false }),
}));
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDocument: jest.fn(),
    getDocumentRevisions: jest.fn(),
    downloadDocument: jest.fn(),
    uploadDocument: jest.fn(),
    changeDocumentAccessLevel: jest.fn(),
  },
}));
const mocked = api as jest.Mocked<typeof api>;

const doc = {
  id: 2,
  document_number: 'DWG-2',
  revision: 'B',
  title: 'Fixture drawing',
  document_type: 'drawing',
  status: 'released',
  file_name: 'drawing.pdf',
  mime_type: 'application/pdf',
  part_id: 17,
  previous_revision_id: null,
  access_level: 'restricted',
};
const props = { id: 2, onClose: jest.fn(), onSaved: jest.fn(), onSelect: jest.fn() };
const view = () => (
  <MemoryRouter>
    <DocumentDetail {...props} />
  </MemoryRouter>
);

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
  URL.createObjectURL = jest.fn().mockReturnValue('blob:local-preview');
  URL.revokeObjectURL = jest.fn();
  mocked.getDocument.mockResolvedValue(doc);
  mocked.getDocumentRevisions.mockResolvedValue([doc]);
  mocked.downloadDocument.mockResolvedValue(new Blob(['%PDF-1.4'], { type: 'application/pdf' }));
});

it('a 404 reads "Document not available", with no retry and no error', async () => {
  mocked.getDocument.mockRejectedValue({ response: { status: 404, data: { detail: 'Document not found' } } });
  render(view());
  expect(await screen.findByText('Document not available')).toBeInTheDocument();
  expect(screen.getByText('This document does not exist or is not available to you.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Change access' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Close document' })).toBeEnabled();
});

it('a preview that 404s says the file is not available', async () => {
  mocked.downloadDocument.mockRejectedValue({ response: { status: 404, data: new Blob(['{}']) } });
  render(view());
  expect(await screen.findByText(/This file is not available to you\./)).toBeInTheDocument();
});

it.each<[UserRole, boolean]>([
  ['admin', true],
  ['quality', true],
  ['platform_admin', true],
  ['manager', false],
  ['supervisor', false],
  ['operator', false],
])('%s sees the access level; Change access offered: %s', async (role, allowed) => {
  mockRole = role;
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  const badges = screen.getAllByTestId('document-access-badge');
  expect(badges.some(badge => badge.getAttribute('data-access-level') === 'restricted')).toBe(true);
  expect(!!screen.queryByRole('button', { name: 'Change access' })).toBe(allowed);
});

it('shows the level the server returned and warns when nobody can open the result', async () => {
  mocked.changeDocumentAccessLevel.mockResolvedValue({
    ...doc,
    access_level: 'export_controlled',
    authorized_user_count: 0,
  });
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  fireEvent.click(screen.getByRole('button', { name: 'Change access' }));
  const dialog = await screen.findByRole('dialog', { name: /Change access — DWG-2 Rev B/ });
  fireEvent.click(within(dialog).getByRole('radio', { name: /Export-controlled/ }));
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Customer drawing is ITAR' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));

  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Change access/ })).not.toBeInTheDocument());
  expect(mocked.changeDocumentAccessLevel).toHaveBeenCalledWith(2, {
    access_level: 'export_controlled',
    reason: 'Customer drawing is ITAR',
  });
  expect(
    screen
      .getAllByTestId('document-access-badge')
      .some(b => b.getAttribute('data-access-level') === 'export_controlled')
  ).toBe(true);
  expect(screen.getByText(/Nobody in the company can open an export-controlled document yet/)).toBeInTheDocument();
  expect(props.onSaved).toHaveBeenCalledTimes(1);
});

it('a refused change leaves the record at its level', async () => {
  mocked.changeDocumentAccessLevel.mockRejectedValue({
    response: { status: 409, data: { detail: 'The document is already at that access level.' } },
  });
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  fireEvent.click(screen.getByRole('button', { name: 'Change access' }));
  const dialog = await screen.findByRole('dialog', { name: /Change access/ });
  fireEvent.click(within(dialog).getByRole('radio', { name: /Standard/ }));
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'Not proprietary' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent('The document is already at that access level.');
  expect(props.onSaved).not.toHaveBeenCalled();
});

it('a revision upload asks for the predecessor level', async () => {
  mocked.uploadDocument.mockResolvedValue({ ...doc, id: 3, revision: 'C' });
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  fireEvent.click(screen.getByRole('button', { name: 'Upload new revision' }));
  expect(screen.getByText(/its access level \(Restricted\)/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/New revision/), { target: { value: 'C' } });
  fireEvent.change(screen.getByLabelText(/What changed/), { target: { value: 'New hole' } });
  fireEvent.change(screen.getByLabelText(/Revision file/), {
    target: { files: [new File(['%PDF'], 'c.pdf', { type: 'application/pdf' })] },
  });
  fireEvent.submit(screen.getByRole('button', { name: 'Save revision' }).closest('form')!);
  await waitFor(() => expect(props.onSelect).toHaveBeenCalledWith(3));
  expect((mocked.uploadDocument.mock.calls[0][0] as FormData).get('access_level')).toBe('restricted');
});
