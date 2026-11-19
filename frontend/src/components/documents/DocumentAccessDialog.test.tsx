/**
 * Documents → Change access (AS9100D 7.5.3.1, Admin / Quality).
 *
 * Locks the contract with `POST /documents/{id}/access-level`:
 *   1. the reason is required and is sent trimmed with the chosen level;
 *   2. an unchanged level cannot be submitted (the server would 409);
 *   3. non-optimistic: a refusal is shown verbatim, the dialog stays up and
 *      onChanged is never called;
 *   4. an export-controlled result with `authorized_user_count: 0` warns that
 *      nobody in the company can open it (a warning toast, not a success).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import DocumentAccessDialog, { NOBODY_AUTHORIZED_WARNING, nobodyCanOpen } from './DocumentAccessDialog';
import { ToastProvider } from '../ui/Toast';
import type { DocumentAccessChangeResult, DocumentAccessFields } from '../../types/documentAccess';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    changeDocumentAccessLevel: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const doc: DocumentAccessFields = {
  id: 12,
  document_number: 'DWG-12',
  revision: 'C',
  title: 'Bracket drawing',
  access_level: 'standard',
};

function renderDialog(onChanged = jest.fn(), onClose = jest.fn()) {
  render(
    <ToastProvider>
      <DocumentAccessDialog document={doc} onClose={onClose} onChanged={onChanged} />
    </ToastProvider>
  );
  return { onChanged, onClose, dialog: screen.getByRole('dialog') };
}

beforeEach(() => {
  jest.clearAllMocks();
});

it('cannot save the current level, and requires a reason before it calls the server', async () => {
  const { dialog } = renderDialog();
  const save = within(dialog).getByRole('button', { name: 'Save access level' });
  expect(within(dialog).getByRole('radio', { name: /Standard/ })).toBeChecked();
  expect(save).toBeDisabled();

  fireEvent.click(within(dialog).getByRole('radio', { name: /Restricted/ }));
  expect(save).toBeEnabled();
  fireEvent.submit(save.closest('form')!);
  expect(await within(dialog).findByText(/Give a reason/)).toBeInTheDocument();
  expect(mockApi.changeDocumentAccessLevel).not.toHaveBeenCalled();
});

it('sends the chosen level with the trimmed reason and hands the server result back', async () => {
  const result: DocumentAccessChangeResult = { ...doc, access_level: 'restricted', authorized_user_count: 3 };
  mockApi.changeDocumentAccessLevel.mockResolvedValue(result);
  const { dialog, onChanged } = renderDialog();

  fireEvent.click(within(dialog).getByRole('radio', { name: /Restricted/ }));
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: '  Customer-proprietary  ' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));

  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(result));
  expect(mockApi.changeDocumentAccessLevel).toHaveBeenCalledWith(12, {
    access_level: 'restricted',
    reason: 'Customer-proprietary',
  });
  expect(await screen.findByText('DWG-12 Rev C is now Restricted.')).toBeInTheDocument();
});

it('shows a refusal verbatim and stays open without reporting a change', async () => {
  mockApi.changeDocumentAccessLevel.mockRejectedValue({
    response: { status: 403, data: { detail: 'An API token cannot change document access.' } },
  });
  const { dialog, onChanged, onClose } = renderDialog();

  fireEvent.click(within(dialog).getByRole('radio', { name: /Export-controlled/ }));
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'ITAR drawing' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));

  expect(await within(dialog).findByRole('alert')).toHaveTextContent('An API token cannot change document access.');
  expect(onChanged).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(within(dialog).getByLabelText(/Reason/)).toHaveValue('ITAR drawing');
});

it('warns, until dismissed, when nobody in the company can open the export-controlled document', async () => {
  const result: DocumentAccessChangeResult = { ...doc, access_level: 'export_controlled', authorized_user_count: 0 };
  mockApi.changeDocumentAccessLevel.mockResolvedValue(result);
  const { dialog, onChanged } = renderDialog();

  fireEvent.click(within(dialog).getByRole('radio', { name: /Export-controlled/ }));
  // Choosing export-controlled says what it costs before anything is saved.
  expect(within(dialog).getByText(/you lose access too/)).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'ITAR drawing' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));

  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(result));
  expect(await screen.findByText(new RegExp(NOBODY_AUTHORIZED_WARNING.slice(0, 40)))).toBeInTheDocument();
  expect(screen.queryByText('DWG-12 Rev C is now Export-controlled.')).not.toBeInTheDocument();
});

it('nobodyCanOpen only fires for an export-controlled result with no authorized people', () => {
  expect(nobodyCanOpen({ ...doc, access_level: 'export_controlled', authorized_user_count: 0 })).toBe(true);
  expect(nobodyCanOpen({ ...doc, access_level: 'export_controlled', authorized_user_count: 2 })).toBe(false);
  expect(nobodyCanOpen({ ...doc, access_level: 'restricted', authorized_user_count: 0 })).toBe(false);
});
