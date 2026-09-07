import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../services/api';
import { DocumentDeliveryComposer } from './DocumentDeliveryComposer';
import { DocumentDelivery } from '../types/documentDelivery';

jest.mock('./ui/PdfPreview', () => ({
  __esModule: true,
  default: ({ title, url }: { title: string; url: string }) => (
    <div title={title}>
      <a href={url}>Download PDF</a>
    </div>
  ),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    listDocumentDeliveries: jest.fn(),
    previewDocumentDelivery: jest.fn(),
    sendDocumentDelivery: jest.fn(),
    getDocumentDeliveryAttachment: jest.fn(),
    reconcileDocumentDelivery: jest.fn(),
  },
}));
const list = api.listDocumentDeliveries as jest.Mock;
const preview = api.previewDocumentDelivery as jest.Mock;
const send = api.sendDocumentDelivery as jest.Mock;
const attachment = api.getDocumentDeliveryAttachment as jest.Mock;
const onAccepted = jest.fn();
const row: DocumentDelivery = {
  id: 11,
  entity_type: 'quote',
  entity_id: 5,
  document_number: 'Q-2026-001',
  recipient: 'buyer@example.test',
  subject: 'Quote Q-2026-001',
  body: 'Please review the attached quote.',
  attachment_name: 'Q-2026-001.pdf',
  attachment_sha256: 'abc',
  attachment_size: 500,
  status: 'prepared',
  status_detail: null,
  version: 1,
  provider_message_id: null,
  created_at: '2026-09-07T12:00:00',
  attempted_at: null,
  accepted_at: null,
  send_available: true,
  unavailable_reason: null,
  delivered: null,
  replayed: false,
  manually_verified: false,
  verification_note: null,
  verified_at: null,
};
function mount() {
  return render(
    <DocumentDeliveryComposer entityType="quote" entityId={5} onClose={jest.fn()} onAccepted={onAccepted} />
  );
}
async function prepare() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare email for review' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Prepare email for review' }));
  await screen.findByTitle('Reviewed document PDF');
}
beforeEach(() => {
  jest.clearAllMocks();
  list.mockResolvedValue([]);
  preview.mockResolvedValue(row);
  attachment.mockResolvedValue(new Blob(['PDF'], { type: 'application/pdf' }));
  URL.createObjectURL = jest.fn(() => 'blob:reviewed-pdf');
  URL.revokeObjectURL = jest.fn();
  send.mockResolvedValue({
    ...row,
    status: 'accepted',
    version: 3,
    accepted_at: '2026-09-07T12:01:00',
    send_available: false,
  });
});

test('opening reads history and sends nothing until a document is prepared and explicitly reviewed', async () => {
  mount();
  await prepare();
  expect(preview).toHaveBeenCalledTimes(1);
  expect(send).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Send email' }));
  await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1));
  expect(send).toHaveBeenCalledWith(
    11,
    expect.objectContaining({
      expected_version: 1,
      recipient: 'buyer@example.test',
      body: row.body,
      subject: row.subject,
    })
  );
  expect(screen.getByText('The mail server accepted this email. Inbox delivery has not been confirmed.')).toBeVisible();
});

test('changing the recipient clears review acknowledgement', async () => {
  mount();
  await prepare();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.change(screen.getByLabelText('Recipient email'), { target: { value: 'other@example.test' } });
  expect(screen.getByRole('checkbox')).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
});

test('missing SMTP configuration keeps the prepared PDF reviewable and sending disabled', async () => {
  preview.mockResolvedValue({ ...row, send_available: false, unavailable_reason: 'SMTP is not configured.' });
  mount();
  await prepare();
  fireEvent.click(screen.getByRole('checkbox'));
  expect(screen.getByText('SMTP is not configured.')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
  expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'blob:reviewed-pdf');
});

test('double send is prevented while the provider response is pending', async () => {
  let resolve!: (value: unknown) => void;
  send.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  mount();
  await prepare();
  fireEvent.click(screen.getByRole('checkbox'));
  const button = screen.getByRole('button', { name: 'Send email' });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(send).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled();
  await act(async () => resolve({ ...row, status: 'accepted', send_available: false }));
});

test('an interrupted send requires checking status and never automatically retries', async () => {
  send.mockRejectedValue(new Error('interrupted'));
  mount();
  await prepare();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Send email' }));
  await screen.findByText(/send response was interrupted/);
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Prepare new email' })).toBeDisabled();
  list.mockResolvedValue([{ ...row, status: 'unknown', send_available: false }]);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh email status' }));
  await screen.findByText(/An earlier email has an unknown outcome/);
  expect(send).toHaveBeenCalledTimes(1);
  expect(onAccepted).not.toHaveBeenCalled();
});

test('history loads accepted status without creating or sending another email', async () => {
  list.mockResolvedValue([{ ...row, status: 'accepted', send_available: false }]);
  const rendered = mount();
  await screen.findByTitle('Reviewed document PDF');
  expect(preview).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
  rendered.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:reviewed-pdf');
});

test('a failed PDF load cannot be acknowledged for sending', async () => {
  attachment.mockRejectedValue(new Error('offline'));
  mount();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare email for review' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Prepare email for review' }));
  await screen.findByText(/reviewed PDF could not be loaded/);
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(send).not.toHaveBeenCalled();
});

test('refresh retries a failed attachment for the same prepared id and keeps message edits', async () => {
  attachment.mockRejectedValueOnce(new Error('offline'));
  mount();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare email for review' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Prepare email for review' }));
  await screen.findByText(/reviewed PDF could not be loaded/);
  fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'My reviewed custom message' } });
  list.mockResolvedValue([row]);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh email status' }));
  await screen.findByTitle('Reviewed document PDF');
  expect(attachment).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText('Message')).toHaveValue('My reviewed custom message');
  fireEvent.click(screen.getByRole('checkbox'));
  expect(screen.getByRole('button', { name: 'Send email' })).toBeEnabled();
});

test('manager can record a checked outcome without sending an email', async () => {
  list.mockResolvedValue([{ ...row, status: 'unknown', version: 3, send_available: false }]);
  const reconcile = api.reconcileDocumentDelivery as jest.Mock;
  reconcile.mockResolvedValue({
    ...row,
    status: 'accepted',
    version: 4,
    manually_verified: true,
    verification_note: 'Confirmed through mail server logs.',
    send_available: false,
  });
  render(
    <DocumentDeliveryComposer
      entityType="quote"
      entityId={5}
      onClose={jest.fn()}
      onAccepted={onAccepted}
      canReconcile
    />
  );
  await screen.findByLabelText('Verification note');
  fireEvent.change(screen.getByLabelText('Verified outcome'), { target: { value: 'accepted' } });
  fireEvent.change(screen.getByLabelText('Verification note'), {
    target: { value: 'Confirmed through mail server logs.' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Record verified outcome' }));
  await waitFor(() => expect(onAccepted).toHaveBeenCalledTimes(1));
  expect(reconcile).toHaveBeenCalledWith(11, {
    expected_version: 3,
    outcome: 'accepted',
    verification_note: 'Confirmed through mail server logs.',
  });
  expect(send).not.toHaveBeenCalled();
  expect(
    screen.queryByText('The mail server accepted this email. Inbox delivery has not been confirmed.')
  ).not.toBeInTheDocument();
});

test('a history-only reader can inspect the PDF without preparing or sending', async () => {
  list.mockResolvedValue([{ ...row, send_available: false }]);
  render(
    <DocumentDeliveryComposer
      entityType="purchase_order"
      entityId={5}
      onClose={jest.fn()}
      onAccepted={onAccepted}
      canPrepare={false}
    />
  );
  await screen.findByTitle('Reviewed document PDF');
  expect(screen.getByRole('link', { name: 'Download PDF' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Prepare new email' })).toBeDisabled();
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Send email' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Prepare new email' }));
  expect(preview).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
});
