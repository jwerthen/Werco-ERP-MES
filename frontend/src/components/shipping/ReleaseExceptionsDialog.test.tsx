/**
 * The Ship / Issue CoC confirmation over open quality items (shipping/2). Under test is
 * the server contract the dialog drives, non-optimistically: nothing open sends no
 * body; open items need Admin/Manager/Quality, a ticked acknowledgment and a reason of
 * at least three characters (sent trimmed); a 409 that arrives anyway replaces the
 * list from the server's own detail and asks again; nothing closes it mid-request.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ReleaseExceptionsDialog from './ReleaseExceptionsDialog';
import api from '../../services/api';
import type { ReleaseException, ShipmentDetail } from '../../types/shipping';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getShipment: jest.fn(), markShipped: jest.fn(), issueCoc: jest.fn() },
}));

const mocked = api as jest.Mocked<typeof api>;

const openNcr: ReleaseException = {
  code: 'open_ncr',
  severity: 'high',
  message: 'NCR NCR-0101 on this work order is open (status=open, disposition=pending).',
  reference_type: 'ncr',
  reference_id: 101,
};
const faiNotPassed: ReleaseException = {
  code: 'fai_not_passed',
  severity: 'high',
  message: 'First Article Inspection FAI-0007 on this work order is not passed (status=in_progress).',
  reference_type: 'fai',
  reference_id: 7,
};

function detail(release_exceptions: ReleaseException[]): ShipmentDetail {
  return {
    id: 9,
    shipment_number: 'SHP-TEST-9',
    work_order_id: 3,
    work_order_number: 'WO-TEST-3',
    customer_name: 'Example Aero Co',
    customer_po: 'PO-TEST-1',
    part_number: 'PART-TEST',
    part_name: 'Test bracket',
    lot_number: null,
    status: 'pending',
    ship_to_name: 'Example Aero Co',
    ship_to_address: null,
    ship_to_city: null,
    ship_to_state: null,
    ship_to_zip: null,
    po_line_item: null,
    unit_number: null,
    part_revision: 'B',
    serial_numbers: [],
    carrier: null,
    tracking_number: null,
    quantity_shipped: 2,
    weight_lbs: null,
    num_packages: 1,
    ship_date: null,
    cert_of_conformance: true,
    packing_notes: null,
    created_at: '2026-11-20T15:00:00Z',
    coc_number: null,
    coc_issued_at: null,
    coc_withheld: false,
    release_exceptions,
  };
}

const target = { id: 9, shipment_number: 'SHP-TEST-9', work_order_number: 'WO-TEST-3' };

/** The confirm is on screen (disabled) while the open items load; wait for the read. */
async function loadedConfirm(name: string) {
  await waitFor(() => expect(screen.queryByText(/Checking open quality items/)).not.toBeInTheDocument());
  return screen.getByRole('button', { name });
}

beforeEach(() => {
  jest.clearAllMocks();
});

test('nothing open: Mark shipped sends no acknowledgment and reports the result', async () => {
  mocked.getShipment.mockResolvedValue(detail([]));
  mocked.markShipped.mockResolvedValue({ message: 'ok', shipment_number: 'SHP-TEST-9', coc_withheld: false });
  const onShipped = jest.fn();
  render(
    <ReleaseExceptionsDialog mode="ship" target={target} canAccept={false} onClose={jest.fn()} onShipped={onShipped} />
  );

  expect(screen.getByRole('status')).toHaveTextContent(/Checking open quality items/);
  const confirm = await loadedConfirm('Mark shipped');
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  fireEvent.click(confirm);
  await waitFor(() => expect(onShipped).toHaveBeenCalledTimes(1));
  expect(mocked.markShipped).toHaveBeenCalledWith(9, undefined, undefined);
});

test('open items: Admin/Manager/Quality must tick the acknowledgment and give a reason, sent trimmed', async () => {
  mocked.getShipment.mockResolvedValue(detail([openNcr, faiNotPassed]));
  mocked.markShipped.mockResolvedValue({
    message: 'ok',
    shipment_number: 'SHP-TEST-9',
    coc_withheld: true,
    release_exceptions: [openNcr, faiNotPassed],
  });
  const onShipped = jest.fn();
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept onClose={jest.fn()} onShipped={onShipped} />);

  const list = await screen.findByRole('list', { name: 'Open quality items' });
  expect(within(list).getByText(openNcr.message)).toBeInTheDocument();
  expect(within(list).getByText('First article not passed')).toBeInTheDocument();
  expect(screen.getByText(/withholds the automatic Certificate of Conformance/)).toBeInTheDocument();

  const confirm = screen.getByRole('button', { name: 'Accept and ship' });
  expect(confirm).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /I accept these open quality items/ }));
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: /Reason/ }), { target: { value: '  ok ' } });
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: /Reason/ }), {
    target: { value: '  Customer deviation on file  ' },
  });
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);

  await waitFor(() => expect(onShipped).toHaveBeenCalledTimes(1));
  expect(mocked.markShipped).toHaveBeenCalledWith(9, undefined, {
    acknowledge_exceptions: true,
    reason: 'Customer deviation on file',
  });
});

test('open items and a role that cannot accept: no acknowledgment controls, the confirm stays disabled', async () => {
  mocked.getShipment.mockResolvedValue(detail([openNcr]));
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept={false} onClose={jest.fn()} />);

  expect(
    await screen.findByText(/Admin, Manager or Quality must accept these items with a reason/)
  ).toBeInTheDocument();
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Accept and ship' })).toBeDisabled();
});

test('Quality with nothing open to accept cannot ship', async () => {
  mocked.getShipment.mockResolvedValue(detail([]));
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept qualityOnly onClose={jest.fn()} />);

  expect(
    await screen.findByText(/Quality marks a shipment shipped only to accept open quality items/)
  ).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark shipped' })).toBeDisabled();
});

test('a 409 that arrives anyway replaces the list from the server detail and asks for the acknowledgment again', async () => {
  mocked.getShipment.mockResolvedValue(detail([]));
  mocked.markShipped.mockRejectedValue({
    response: {
      status: 409,
      data: {
        detail: {
          code: 'release_exceptions_open',
          message: 'This job has open quality items. Admin, Manager or Quality must accept them with a reason to ship.',
          release_exceptions: [openNcr],
        },
      },
    },
  });
  const onShipped = jest.fn();
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept onClose={jest.fn()} onShipped={onShipped} />);

  fireEvent.click(await loadedConfirm('Mark shipped'));
  expect(await screen.findByRole('alert')).toHaveTextContent(/must accept them with a reason to ship/);
  expect(screen.getByText(openNcr.message)).toBeInTheDocument();
  expect(screen.getByRole('checkbox', { name: /I accept/ })).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Accept and ship' })).toBeDisabled();
  expect(onShipped).not.toHaveBeenCalled();
});

test('while the request is in flight Cancel and Escape cannot close it; a refusal shows the server text', async () => {
  mocked.getShipment.mockResolvedValue(detail([]));
  let fail!: (error: unknown) => void;
  mocked.markShipped.mockImplementation(
    () =>
      new Promise((_, reject) => {
        fail = reject;
      })
  );
  const onClose = jest.fn();
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept onClose={onClose} />);

  fireEvent.click(await loadedConfirm('Mark shipped'));
  await waitFor(() => expect(mocked.markShipped).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(onClose).not.toHaveBeenCalled();

  await act(async () =>
    fail({
      response: {
        status: 403,
        data: { detail: 'Only Admin, Manager or Quality can accept open quality items on a job.' },
      },
    })
  );
  expect(screen.getByRole('alert')).toHaveTextContent('Only Admin, Manager or Quality can accept');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test('Issue CoC over open items sends the acknowledgment to the certificate route', async () => {
  mocked.getShipment.mockResolvedValue(detail([faiNotPassed]));
  mocked.issueCoc.mockResolvedValue({ id: 4, coc_number: 'COC-TEST-0004', shipment_id: 9, work_order_id: 3 });
  const onIssued = jest.fn();
  render(<ReleaseExceptionsDialog mode="coc" target={target} canAccept onClose={jest.fn()} onIssued={onIssued} />);

  expect(
    await screen.findByRole('heading', { name: 'Issue Certificate of Conformance for SHP-TEST-9' })
  ).toBeInTheDocument();
  fireEvent.click(
    await screen.findByRole('checkbox', { name: /I accept these open quality items on the certificate/ })
  );
  fireEvent.change(screen.getByRole('textbox', { name: /Reason/ }), {
    target: { value: 'Customer accepted FAI by email' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Accept and issue' }));

  await waitFor(() => expect(onIssued).toHaveBeenCalledWith(expect.objectContaining({ coc_number: 'COC-TEST-0004' })));
  expect(mocked.issueCoc).toHaveBeenCalledWith(9, {
    acknowledge_exceptions: true,
    reason: 'Customer accepted FAI by email',
  });
  expect(mocked.markShipped).not.toHaveBeenCalled();
});

test('a failed read offers a retry', async () => {
  mocked.getShipment.mockRejectedValueOnce(new Error('down')).mockResolvedValueOnce(detail([]));
  render(<ReleaseExceptionsDialog mode="ship" target={target} canAccept onClose={jest.fn()} />);

  fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
  expect(await loadedConfirm('Mark shipped')).toBeEnabled();
  expect(mocked.getShipment).toHaveBeenCalledTimes(2);
});
