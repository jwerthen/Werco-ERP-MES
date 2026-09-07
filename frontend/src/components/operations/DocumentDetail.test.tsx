import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import DocumentDetail from './DocumentDetail';
import api from '../../services/api';
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDocument: jest.fn(),
    getDocumentRevisions: jest.fn(),
    downloadDocument: jest.fn(),
    uploadDocument: jest.fn(),
  },
}));
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
  previous_revision_id: 1,
};
const original = { ...doc, id: 1, revision: 'A', previous_revision_id: null };
const props = { id: 2, onClose: jest.fn(), onSaved: jest.fn(), onSelect: jest.fn() };
const mocked = api as jest.Mocked<typeof api>;
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn().mockReturnValue('blob:local-preview');
  URL.revokeObjectURL = jest.fn();
  mocked.getDocument.mockResolvedValue(doc);
  mocked.getDocumentRevisions.mockResolvedValue([doc, original]);
  mocked.downloadDocument.mockResolvedValue(new Blob(['%PDF-1.4 fixture'], { type: 'application/pdf' }));
});
const view = (id = 2) => (
  <MemoryRouter>
    <DocumentDetail {...props} id={id} />
  </MemoryRouter>
);
test('PDF preview and revision identity remain visible; previous revision opens the latest before upload', async () => {
  mocked.getDocument.mockResolvedValue(original);
  const mounted = render(view(1));
  expect(await screen.findByTitle('Preview Fixture drawing revision A')).toHaveAttribute('src', 'blob:local-preview');
  expect(screen.getByText('Revision history (2)')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Upload new revision' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Open latest revision to upload' }));
  expect(props.onSelect).toHaveBeenCalledWith(2);
  mounted.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-preview');
});
test('a revision upload failure keeps values and the selected file for retry', async () => {
  mocked.uploadDocument.mockRejectedValue({
    response: { data: { detail: 'A newer revision exists. Open the latest revision.' } },
  });
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  fireEvent.click(screen.getByRole('button', { name: 'Upload new revision' }));
  fireEvent.change(screen.getByLabelText(/New revision/), { target: { value: 'C' } });
  fireEvent.change(screen.getByLabelText(/What changed/), { target: { value: 'Updated fixture dimension' } });
  const file = new File(['%PDF'], 'revision-c.pdf', { type: 'application/pdf' });
  fireEvent.change(screen.getByLabelText(/Revision file/), { target: { files: [file] } });
  fireEvent.submit(screen.getByRole('button', { name: 'Save revision' }).closest('form')!);
  expect(await screen.findByRole('alert')).toHaveTextContent('A newer revision exists');
  expect(screen.getByLabelText(/New revision/)).toHaveValue('C');
  expect(screen.getByLabelText(/What changed/)).toHaveValue('Updated fixture dimension');
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeEnabled();
  const payload = mocked.uploadDocument.mock.calls[0][0] as FormData;
  expect(payload.get('previous_revision_id')).toBe('2');
  expect(payload.get('part_id')).toBe('17');
  expect(payload.get('file')).toBe(file);
  expect(props.onSelect).not.toHaveBeenCalled();
});
test('changing to an unavailable revision clears the old record and offers retry', async () => {
  const mounted = render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  mocked.getDocument.mockRejectedValueOnce({ response: { data: { detail: 'Document not found' } } });
  mounted.rerender(view(99));
  expect(await screen.findByText('Document not found')).toBeInTheDocument();
  expect(screen.queryByTitle('Preview Fixture drawing revision B')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Upload new revision' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /retry/i }));
  await waitFor(() => expect(mocked.getDocument).toHaveBeenLastCalledWith(99));
  expect(await screen.findByTitle('Preview Fixture drawing revision B')).toBeInTheDocument();
});

test('document close is available during a failed load and does not mutate a revision', async () => {
  mocked.getDocument.mockRejectedValueOnce({ response: { data: { detail: 'Document not found' } } });
  render(view());
  expect(screen.getByRole('button', { name: 'Close document' })).toBeEnabled();
  await screen.findByText('Document not found');
  fireEvent.click(screen.getByRole('button', { name: 'Close document' }));
  expect(props.onClose).toHaveBeenCalledTimes(1);
  expect(mocked.uploadDocument).not.toHaveBeenCalled();
});

test('document close preserves the existing unsaved revision guard', async () => {
  const confirm = jest.spyOn(window, 'confirm').mockReturnValue(false);
  render(view());
  await screen.findByTitle('Preview Fixture drawing revision B');
  fireEvent.click(screen.getByRole('button', { name: 'Upload new revision' }));
  fireEvent.change(screen.getByLabelText(/New revision/), { target: { value: 'C' } });
  fireEvent.click(screen.getByRole('button', { name: 'Close document' }));
  expect(confirm).toHaveBeenCalled();
  expect(props.onClose).not.toHaveBeenCalled();
  expect(screen.getByLabelText(/New revision/)).toHaveValue('C');
  confirm.mockRestore();
});
