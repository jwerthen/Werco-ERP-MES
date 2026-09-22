import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { UserRole } from '../../types';
import { HANK_PDF_MAX_BYTES } from '../../validation/hankDocument';
import { HankDocumentUpload, HankUploadedDocument } from './HankDocumentUpload';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getDocumentTypes: jest.fn(),
    getWorkOrder: jest.fn(),
    uploadDocument: jest.fn(),
  },
}));

let mockRole: UserRole = 'quality';
let mockIsSuperuser = false;
jest.mock('../../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: mockIsSuperuser }),
}));

const mockedApi = jest.mocked(api);
const documentTypes = [
  { value: 'drawing', label: 'Drawing' },
  { value: 'certificate', label: 'Certificate' },
];
const savedDocument: HankUploadedDocument = {
  id: 42,
  document_number: 'DOC-0042',
  title: 'Final inspection',
  revision: 'B',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderUpload(props: Partial<React.ComponentProps<typeof HankDocumentUpload>> = {}) {
  const onUploaded = jest.fn();
  const onCancel = jest.fn();
  const onBusyChange = jest.fn();
  const allProps = { onUploaded, onCancel, onBusyChange, ...props };
  return { ...render(<HankDocumentUpload {...allProps} />), onUploaded, onCancel, onBusyChange };
}

function chooseFile(file = new File(['%PDF-1.7\nfixture'], 'Final_inspection.pdf', { type: 'application/pdf' })) {
  fireEvent.change(screen.getByLabelText(/PDF file/), { target: { files: [file] } });
  return file;
}

async function fillValidForm() {
  await screen.findByRole('option', { name: 'Drawing' });
  const file = chooseFile();
  fireEvent.change(screen.getByLabelText(/Document type/), { target: { value: 'drawing' } });
  return file;
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: 'Upload and release PDF' }));
}

beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  mockRole = 'quality';
  mockIsSuperuser = false;
  mockedApi.getDocumentTypes.mockResolvedValue(documentTypes);
  mockedApi.getWorkOrder.mockResolvedValue({ id: 7, work_order_number: 'WO-1007' });
  mockedApi.uploadDocument.mockResolvedValue(savedDocument);
});

describe('HankDocumentUpload', () => {
  it.each<UserRole>(['operator', 'supervisor', 'shipping', 'viewer'])(
    'does not offer document publishing to %s',
    async role => {
      mockRole = role;
      const { onUploaded } = renderUpload();
      await act(async () => undefined);
      expect(screen.getByText(/requires an Admin, Manager, or Quality role/)).toBeInTheDocument();
      expect(screen.queryByLabelText(/PDF file/)).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Upload and release PDF' })).not.toBeInTheDocument();
      expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
      expect(onUploaded).not.toHaveBeenCalled();
    }
  );

  it.each<UserRole>(['admin', 'manager', 'quality', 'platform_admin'])('offers the release form to %s', async role => {
    mockRole = role;
    renderUpload();
    await screen.findByRole('option', { name: 'Drawing' });
    expect(screen.getByRole('form', { name: 'File a PDF with Hank' })).toBeInTheDocument();
  });

  it('honors the existing superuser publishing allowance', async () => {
    mockRole = 'operator';
    mockIsSuperuser = true;
    renderUpload();
    await screen.findByRole('option', { name: 'Drawing' });
    expect(screen.getByLabelText(/PDF file/)).toBeInTheDocument();
  });

  it('prefills a readable title and initial revision while requiring a document type', async () => {
    renderUpload();
    await screen.findByRole('option', { name: 'Drawing' });
    chooseFile(new File(['%PDF'], 'Final_inspection-check.PDF', { type: 'application/pdf' }));
    expect(screen.getByLabelText(/Document title/)).toHaveValue('Final inspection check');
    expect(screen.getByLabelText(/Revision/)).toHaveValue('A');
    expect(screen.getByLabelText(/Document type/)).toHaveValue('');
    expect(screen.getByText(/creates a new, released document under your name/)).toBeInTheDocument();
    submit();
    expect(await screen.findByText('Choose a document type.')).toBeInTheDocument();
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/Document title/), { target: { value: 'Employee chosen title' } });
    chooseFile(new File(['%PDF'], 'Replacement.pdf', { type: 'application/pdf' }));
    expect(screen.getByLabelText(/Document title/)).toHaveValue('Employee chosen title');
  });

  it('uploads exactly the reviewed fields and emits its receipt only after the server confirms', async () => {
    const pending = deferred<HankUploadedDocument>();
    mockedApi.uploadDocument.mockReturnValue(pending.promise);
    const { onUploaded, onBusyChange } = renderUpload();
    const file = await fillValidForm();
    fireEvent.change(screen.getByLabelText(/Document title/), { target: { value: '  Final inspection  ' } });
    fireEvent.change(screen.getByLabelText(/Revision/), { target: { value: ' B ' } });
    fireEvent.change(screen.getByLabelText(/Notes/), { target: { value: '  Signed by QC  ' } });
    submit();

    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    const data = mockedApi.uploadDocument.mock.calls[0][0];
    expect(data).toBeInstanceOf(FormData);
    expect(Array.from(data.entries())).toEqual([
      ['file', file],
      ['title', 'Final inspection'],
      ['document_type', 'drawing'],
      ['revision', 'B'],
      ['description', 'Signed by QC'],
    ]);
    expect(onBusyChange).toHaveBeenCalledWith(true);
    expect(onUploaded).not.toHaveBeenCalled();
    expect(screen.getByRole('form')).toHaveAttribute('aria-busy', 'true');

    await act(async () => pending.resolve(savedDocument));
    expect(onUploaded).toHaveBeenCalledTimes(1);
    expect(onUploaded).toHaveBeenCalledWith(savedDocument);
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    expect(screen.getByRole('form')).toHaveAttribute('aria-busy', 'false');
  });

  it.each([false, true])('attaches to the verified job only when the employee opts in (%s)', async attach => {
    const { onUploaded } = renderUpload({ workOrderId: 7 });
    await fillValidForm();
    const checkbox = await screen.findByRole('checkbox', { name: 'Also attach to WO-1007' });
    expect(checkbox).not.toBeChecked();
    if (attach) fireEvent.click(checkbox);
    submit();
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(savedDocument));
    expect(mockedApi.getWorkOrder).toHaveBeenCalledWith(7);
    const data = mockedApi.uploadDocument.mock.calls[0][0];
    expect(data.get('work_order_id')).toBe(attach ? '7' : null);
    expect(Array.from(data.keys())).toEqual([
      'file',
      'title',
      'document_type',
      'revision',
      'description',
      ...(attach ? ['work_order_id'] : []),
    ]);
  });

  it('pins an explicitly reviewed job attachment when navigation changes behind the form', async () => {
    const props = { onUploaded: jest.fn(), onCancel: jest.fn() };
    const { rerender } = render(<HankDocumentUpload {...props} workOrderId={7} />);
    await fillValidForm();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Also attach to WO-1007' }));
    rerender(<HankDocumentUpload {...props} workOrderId={8} />);
    submit();
    await waitFor(() => expect(props.onUploaded).toHaveBeenCalled());
    expect(mockedApi.uploadDocument.mock.calls[0][0].get('work_order_id')).toBe('7');
    expect(mockedApi.getWorkOrder).not.toHaveBeenCalledWith(8);
  });

  it('keeps library uploads available when the job cannot be resolved', async () => {
    mockedApi.getWorkOrder.mockRejectedValue(new Error('Job not accessible'));
    const { onUploaded } = renderUpload({ workOrderId: 7 });
    await fillValidForm();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    submit();
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
    expect(mockedApi.uploadDocument.mock.calls[0][0].has('work_order_id')).toBe(false);
  });

  it('retains the selected file and entered fields after a server refusal, and shows its detail', async () => {
    mockedApi.uploadDocument.mockRejectedValueOnce({
      isAxiosError: true,
      response: { data: { detail: 'This work order no longer accepts attachments.' } },
    });
    const { onUploaded, onBusyChange } = renderUpload();
    const file = await fillValidForm();
    fireEvent.change(screen.getByLabelText(/Revision/), { target: { value: 'B' } });
    fireEvent.change(screen.getByLabelText(/Notes/), { target: { value: 'Retain this inspection note' } });
    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('This work order no longer accepts attachments.');
    expect(onUploaded).not.toHaveBeenCalled();
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    expect(screen.getByLabelText(/Document title/)).toHaveValue('Final inspection');
    expect(screen.getByLabelText(/Revision/)).toHaveValue('B');
    expect(screen.getByLabelText(/Notes/)).toHaveValue('Retain this inspection note');
    expect(screen.getByText(/Final_inspection.pdf/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload and release PDF' })).toBeEnabled();

    submit();
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(savedDocument));
    expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(2);
    expect(mockedApi.uploadDocument.mock.calls[1][0].get('file')).toEqual(file);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not claim completion when an upload fails without a usable server detail', async () => {
    mockedApi.uploadDocument.mockRejectedValue({
      isAxiosError: true,
      response: { data: { detail: [{ msg: 'error' }] } },
    });
    const { onUploaded } = renderUpload();
    await fillValidForm();
    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The upload was not confirmed. Check Documents before trying again.'
    );
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it.each([
    ['a non-PDF extension', new File(['content'], 'drawing.txt', { type: 'application/pdf' }), 'Choose a PDF file.'],
    ['an incompatible MIME type', new File(['content'], 'drawing.pdf', { type: 'image/png' }), 'Choose a PDF file.'],
    [
      'an empty PDF',
      new File([], 'drawing.pdf', { type: 'application/pdf' }),
      'This PDF is empty. Choose a file with content.',
    ],
  ])('blocks %s and clears any previously selected valid upload', async (_label, file, message) => {
    renderUpload();
    await fillValidForm();
    chooseFile(file as File);
    expect(screen.getByRole('alert')).toHaveTextContent(message as string);
    expect(screen.getByLabelText(/PDF file/)).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Upload and release PDF' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form'));
    await act(async () => undefined);
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
  });

  it('blocks a PDF larger than 25 MB without starting an upload', async () => {
    renderUpload();
    await fillValidForm();
    const oversized = new File(['%PDF'], 'large.pdf', { type: 'application/pdf' });
    Object.defineProperty(oversized, 'size', { value: HANK_PDF_MAX_BYTES + 1 });
    chooseFile(oversized);
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a PDF smaller than 25 MB.');
    expect(screen.getByRole('button', { name: 'Upload and release PDF' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('form'));
    await act(async () => undefined);
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
  });

  it('validates missing title and revision before uploading', async () => {
    renderUpload();
    await fillValidForm();
    fireEvent.change(screen.getByLabelText(/Document title/), { target: { value: '  ' } });
    fireEvent.change(screen.getByLabelText(/Revision/), { target: { value: '' } });
    submit();
    expect(await screen.findByText('Enter a document title.')).toBeInTheDocument();
    expect(screen.getByText('Enter a revision.')).toBeInTheDocument();
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
  });

  it('allows retrying a failed document-type load before filing', async () => {
    mockedApi.getDocumentTypes.mockRejectedValueOnce(new Error('Offline'));
    renderUpload();
    expect(await screen.findByRole('alert')).toHaveTextContent('Document types could not be loaded.');
    chooseFile();
    expect(screen.getByRole('button', { name: 'Upload and release PDF' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading types' }));
    await fillValidForm();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(mockedApi.getDocumentTypes).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Upload and release PDF' })).toBeEnabled();
  });

  it('blocks duplicate submissions, file changes, and cancellation while the upload is in flight', async () => {
    const pending = deferred<HankUploadedDocument>();
    mockedApi.uploadDocument.mockReturnValue(pending.promise);
    const { onCancel } = renderUpload();
    const originalFile = await fillValidForm();
    submit();
    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: /Filing PDF…/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel upload' })).toBeDisabled();
    expect(screen.getByLabelText(/PDF file/)).toBeDisabled();
    expect(screen.getByLabelText(/Document title/)).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel upload' }));
    fireEvent.submit(screen.getByRole('form'));
    fireEvent.submit(screen.getByRole('form'));
    chooseFile(new File(['%PDF'], 'Different.pdf', { type: 'application/pdf' }));
    await act(async () => undefined);
    expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1);
    expect(mockedApi.uploadDocument.mock.calls[0][0].get('file')).toEqual(originalFile);
    expect(screen.getByText(/Final_inspection.pdf/)).toBeInTheDocument();
    expect(screen.queryByText(/Different.pdf/)).not.toBeInTheDocument();
    expect(onCancel).not.toHaveBeenCalled();
    await act(async () => pending.resolve(savedDocument));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel upload' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('does not publish a stale completion callback after the form has unmounted', async () => {
    const pending = deferred<HankUploadedDocument>();
    mockedApi.uploadDocument.mockReturnValue(pending.promise);
    const { onUploaded, unmount } = renderUpload();
    await fillValidForm();
    submit();
    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    const signal = mockedApi.uploadDocument.mock.calls[0][1];
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(savedDocument));
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it('requires reopening the upload if the active company changes before submission', async () => {
    sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '1', cid: 4, type: 'access' }))}.signature`);
    const { onUploaded } = renderUpload();
    await fillValidForm();
    sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '1', cid: 5, type: 'access' }))}.signature`);
    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Your company or session changed. Reopen Upload PDF');
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it('suppresses an old company upload receipt when its response arrives after a company switch', async () => {
    sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '1', cid: 4, type: 'access' }))}.signature`);
    const pending = deferred<HankUploadedDocument>();
    mockedApi.uploadDocument.mockReturnValue(pending.promise);
    const { onUploaded } = renderUpload();
    await fillValidForm();
    submit();
    await waitFor(() => expect(mockedApi.uploadDocument).toHaveBeenCalledTimes(1));
    const signal = mockedApi.uploadDocument.mock.calls[0][1];
    expect(signal?.aborted).toBe(false);
    sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '1', cid: 5, type: 'access' }))}.signature`);
    window.dispatchEvent(new Event('werco:auth-token-changed'));
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve(savedDocument));
    expect(onUploaded).not.toHaveBeenCalled();
  });

  it('does not offer publishing in a read-only session even for an administrator', async () => {
    mockRole = 'admin';
    sessionStorage.setItem(
      'token',
      `header.${btoa(JSON.stringify({ sub: '1', cid: 4, ro: true, type: 'access' }))}.signature`
    );
    renderUpload();
    await act(async () => undefined);
    expect(screen.queryByRole('button', { name: 'Upload and release PDF' })).not.toBeInTheDocument();
    expect(mockedApi.uploadDocument).not.toHaveBeenCalled();
  });
});
