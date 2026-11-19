/**
 * Documents → Change access (AS9100D 7.5.3.1, Admin / Quality).
 *
 * Locks the contract with `POST /documents/{id}/access-level`, which applies to the document's
 * WHOLE revision chain:
 *   1. before anything is saved, the dialog says the level applies to every revision, with the
 *      count when the caller knows the history;
 *   2. the reason is required and is sent trimmed with the chosen level;
 *   3. an unchanged level cannot be submitted (the server would 409);
 *   4. non-optimistic: a refusal (e.g. the 409 for lowering a chain with a revision the caller
 *      cannot see) is shown verbatim, the dialog stays up and onChanged is never called;
 *   5. the notice after saving says how many revisions changed, from the server's
 *      `changed_document_ids` / `chain_size`;
 *   6. an export-controlled result with `authorized_user_count: 0` warns that nobody in the
 *      company can open it (a warning toast, not a success).
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

function renderDialog({
  onChanged = jest.fn(),
  onClose = jest.fn(),
  revisionCount,
}: { onChanged?: jest.Mock; onClose?: jest.Mock; revisionCount?: number } = {}) {
  render(
    <ToastProvider>
      <DocumentAccessDialog document={doc} revisionCount={revisionCount} onClose={onClose} onChanged={onChanged} />
    </ToastProvider>
  );
  return { onChanged, onClose, dialog: screen.getByRole('dialog') };
}

/** Pick a level, give a reason and save. */
function submit(dialog: HTMLElement, level: RegExp, reason: string) {
  fireEvent.click(within(dialog).getByRole('radio', { name: level }));
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: reason } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('before saving: the change covers the whole revision chain', () => {
  it('names the revision count when the history is known', () => {
    const { dialog } = renderDialog({ revisionCount: 3 });
    expect(within(dialog).getByTestId('document-access-chain-note')).toHaveTextContent(
      'Applies to all 3 revisions of this document, not just Rev C.'
    );
  });

  it('says every revision when the history is not known', () => {
    const { dialog } = renderDialog();
    expect(within(dialog).getByTestId('document-access-chain-note')).toHaveTextContent(
      'Applies to every revision of this document, not just Rev C.'
    );
  });

  it('a single-revision document says it has one so far', () => {
    const { dialog } = renderDialog({ revisionCount: 1 });
    expect(within(dialog).getByTestId('document-access-chain-note')).toHaveTextContent(
      'Applies to every revision of this document. It has one revision so far.'
    );
  });
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

it('sends the chosen level with the trimmed reason, hands the result back and says every revision changed', async () => {
  const result: DocumentAccessChangeResult = {
    ...doc,
    access_level: 'restricted',
    authorized_user_count: 3,
    changed_document_ids: [12, 11, 10],
    chain_size: 3,
  };
  mockApi.changeDocumentAccessLevel.mockResolvedValue(result);
  const { dialog, onChanged } = renderDialog({ revisionCount: 3 });

  submit(dialog, /Restricted/, '  Customer-proprietary  ');

  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(result));
  expect(mockApi.changeDocumentAccessLevel).toHaveBeenCalledWith(12, {
    access_level: 'restricted',
    reason: 'Customer-proprietary',
  });
  expect(await screen.findByText('DWG-12 is now Restricted on all 3 revisions.')).toBeInTheDocument();
});

it('says how many revisions changed when some were already at the level', async () => {
  mockApi.changeDocumentAccessLevel.mockResolvedValue({
    ...doc,
    access_level: 'restricted',
    authorized_user_count: 3,
    changed_document_ids: [12, 11],
    chain_size: 3,
  });
  const { dialog } = renderDialog({ revisionCount: 3 });
  submit(dialog, /Restricted/, 'Customer-proprietary');
  expect(await screen.findByText('DWG-12 is now Restricted: 2 of 3 revisions changed.')).toBeInTheDocument();
});

it('names just the revision when the response carries no chain counts', async () => {
  mockApi.changeDocumentAccessLevel.mockResolvedValue({ ...doc, access_level: 'restricted', authorized_user_count: 3 });
  const { dialog } = renderDialog();
  submit(dialog, /Restricted/, 'Customer-proprietary');
  expect(await screen.findByText('DWG-12 Rev C is now Restricted.')).toBeInTheDocument();
});

it.each([
  [403, 'An API token cannot change document access.'],
  [409, 'Some revisions of this document are not visible to you, so its access level cannot be lowered.'],
])('shows a %s refusal verbatim and stays open without reporting a change', async (status, detail) => {
  mockApi.changeDocumentAccessLevel.mockRejectedValue({ response: { status, data: { detail } } });
  const { dialog, onChanged, onClose } = renderDialog();

  submit(dialog, /Export-controlled/, 'ITAR drawing');

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
  expect(onChanged).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(within(dialog).getByLabelText(/Reason/)).toHaveValue('ITAR drawing');
});

it('warns, until dismissed, when nobody in the company can open the export-controlled document', async () => {
  const result: DocumentAccessChangeResult = {
    ...doc,
    access_level: 'export_controlled',
    authorized_user_count: 0,
    changed_document_ids: [12, 11],
    chain_size: 2,
  };
  mockApi.changeDocumentAccessLevel.mockResolvedValue(result);
  const { dialog, onChanged } = renderDialog({ revisionCount: 2 });

  fireEvent.click(within(dialog).getByRole('radio', { name: /Export-controlled/ }));
  // Choosing export-controlled says what it costs before anything is saved.
  expect(within(dialog).getByText(/you lose access too/)).toBeInTheDocument();
  fireEvent.change(within(dialog).getByLabelText(/Reason/), { target: { value: 'ITAR drawing' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save access level' }));

  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(result));
  expect(
    await screen.findByText(`DWG-12 is now Export-controlled on all 2 revisions. ${NOBODY_AUTHORIZED_WARNING}`)
  ).toBeInTheDocument();
});

it('nobodyCanOpen only fires for an export-controlled result with no authorized people', () => {
  expect(nobodyCanOpen({ ...doc, access_level: 'export_controlled', authorized_user_count: 0 })).toBe(true);
  expect(nobodyCanOpen({ ...doc, access_level: 'export_controlled', authorized_user_count: 2 })).toBe(false);
  expect(nobodyCanOpen({ ...doc, access_level: 'restricted', authorized_user_count: 0 })).toBe(false);
});
