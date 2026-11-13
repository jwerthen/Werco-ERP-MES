/**
 * The CAR detail carries Werco's controlled CAR form: its fields, the printable CAR
 * and the server-stamped Quality Manager approval (POST /quality/car/{id}/quality-approval).
 * The approval is server-gated (once, never on a VOID CAR, never by an API token), so it
 * stays non-optimistic: nothing changes until the server answers, and a refusal is shown
 * in the server's own words.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityRecordDetail from './QualityRecordDetail';
import api from '../../services/api';
import type { CARRecord } from '../../types/quality';

let mockCan = true;
jest.mock('../../hooks/usePermissions', () => ({
  __esModule: true,
  default: () => ({ can: () => mockCan }),
}));

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNCR: jest.fn(),
    getCAR: jest.fn(),
    updateNCR: jest.fn(),
    updateCAR: jest.fn(),
    approveCARQuality: jest.fn(),
    fetchCARPdf: jest.fn(),
    getParts: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
  },
}));

// The pdf.js renderer is not what is under test here; the preview's wiring is.
jest.mock('../ui/PdfPreview', () => ({
  __esModule: true,
  default: ({ url, fileName }: { url: string; fileName: string }) => (
    <div data-testid="pdf-preview" data-url={url}>
      {fileName}
    </div>
  ),
}));

const mocked = api as jest.Mocked<typeof api>;

const car: CARRecord = {
  id: 9,
  car_number: 'CAR-20261105-001',
  car_type: 'corrective',
  title: 'Corrective action for edge burr',
  problem_description: 'Repeated edge burrs at final inspection.',
  status: 'corrective_action',
  priority: 3,
  root_cause_analysis: null,
  root_cause: 'Worn deburr fixture.',
  containment_action: null,
  corrective_action: null,
  preventive_action: null,
  verification_method: null,
  verification_results: null,
  effectiveness_check: null,
  due_date: null,
  verification_due: null,
  closed_date: null,
  created_at: '2026-10-01T15:00:00Z',
  updated_at: '2026-11-01T15:00:00Z',
  initiated_by: 2,
  initiated_by_name: 'Dana Quality',
  initiation_reason: 'customer_feedback',
  initiation_reason_other: null,
  rma_number: 'RMA-778',
  submitted_date: '2026-10-01',
  part_id: 12,
  part_number: 'PN-12',
  part_name: 'Bracket',
  part_current_revision: 'C',
  customer_id: 3,
  customer_name: 'Acme Aerospace',
  vendor_id: 40,
  vendor_name: 'Old Plating Co',
  vendor_po_number: 'PO-5512',
  cost_labor: '120.00',
  cost_material: '30.50',
  cost_outside_services: null,
  cost_shipping: null,
  cost_total: '150.50',
  management_action: null,
  vendor_approval_name: null,
  vendor_approval_title: null,
  vendor_approval_date: null,
  customer_approval_name: null,
  customer_approval_date: null,
  quality_approved_by: null,
  quality_approved_by_name: null,
  quality_approved_at: null,
};

const renderDetail = (onSaved = jest.fn()) =>
  render(
    <MemoryRouter>
      <QualityRecordDetail kind="car" id={9} onClose={jest.fn()} onSaved={onSaved} />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockCan = true;
  mocked.getParts.mockResolvedValue([]);
  mocked.getCustomerNames.mockResolvedValue([{ id: 3, name: 'Acme Aerospace' }]);
  // The CAR's vendor has since been removed: the lookup no longer returns it.
  mocked.getVendors.mockResolvedValue([{ id: 41, code: 'NEW', name: 'New Plating LLC', is_active: true }]);
  mocked.fetchCARPdf.mockResolvedValue('blob:car-pdf');
});

test('the form fields load from the record and save with the rest of the CAR', async () => {
  mocked.getCAR.mockResolvedValue(car);
  mocked.updateCAR.mockResolvedValue({ ...car, rma_number: 'RMA-779' });
  renderDetail();

  expect(await screen.findByLabelText('Reason for initiation')).toHaveValue('customer_feedback');
  expect(screen.getByLabelText('RMA #')).toHaveValue('RMA-778');
  expect(screen.getByLabelText('Date submitted')).toHaveValue('2026-10-01');
  expect(screen.getByLabelText('Vendor P.O. number')).toHaveValue('PO-5512');
  expect(screen.getByLabelText('Labor cost')).toHaveValue(120);
  // The summary shows the server's own total (computed at read time, never stored).
  expect(screen.getByText('Cost total').nextElementSibling).toHaveTextContent('$150.50');
  // The part's revision is read from the part TODAY, and labelled so: the CAR records none.
  expect(screen.getByText('PN-12 (current Rev C) — Bracket')).toBeInTheDocument();
  // A removed vendor the CAR names stays readable in its picker.
  await waitFor(() => expect(screen.getByLabelText('Vendor')).toHaveValue('Old Plating Co'));

  fireEvent.change(screen.getByLabelText('RMA #'), { target: { value: 'RMA-779' } });
  fireEvent.submit(screen.getByRole('button', { name: 'Save changes' }).closest('form')!);

  await waitFor(() => expect(mocked.updateCAR).toHaveBeenCalledTimes(1));
  const [, payload] = mocked.updateCAR.mock.calls[0];
  expect(payload).toMatchObject({
    rma_number: 'RMA-779',
    initiation_reason: 'customer_feedback',
    part_id: 12,
    customer_id: 3,
    vendor_id: 40,
    cost_labor: '120.00',
    cost_outside_services: null,
    expected_updated_at: car.updated_at,
  });
  // Server-owned: snapshotted name, computed total, stamped approval.
  for (const key of ['customer_name', 'cost_total', 'quality_approved_by', 'quality_approved_at']) {
    expect(payload).not.toHaveProperty(key);
  }
});

test('Print CAR opens the PDF preview of the saved record', async () => {
  mocked.getCAR.mockResolvedValue(car);
  renderDetail();

  fireEvent.click(await screen.findByRole('button', { name: /Print CAR/ }));

  const preview = await screen.findByTestId('pdf-preview');
  expect(mocked.fetchCARPdf).toHaveBeenCalledWith(9);
  expect(preview).toHaveAttribute('data-url', 'blob:car-pdf');
  expect(preview).toHaveTextContent('CAR-20261105-001.pdf');
});

test('recording the quality approval is confirmed, server-stamped and shown only once saved', async () => {
  const onSaved = jest.fn();
  mocked.getCAR.mockResolvedValue(car);
  mocked.approveCARQuality.mockResolvedValue({
    ...car,
    updated_at: '2026-11-13T16:00:00Z',
    quality_approved_by: 2,
    quality_approved_by_name: 'Dana Quality',
    quality_approved_at: '2026-11-13T16:00:00Z',
  });
  renderDetail(onSaved);

  const panel = await screen.findByTestId('car-quality-approval');
  expect(panel).toHaveTextContent('Not yet approved');
  fireEvent.click(within(panel).getByRole('button', { name: 'Record quality approval' }));
  // Nothing is sent until the confirmation.
  expect(mocked.approveCARQuality).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: 'Record approval' }));

  await waitFor(() => expect(mocked.approveCARQuality).toHaveBeenCalledWith(9));
  await waitFor(() => expect(screen.getByTestId('car-quality-approval')).toHaveTextContent('Approved by Dana Quality'));
  expect(
    within(screen.getByTestId('car-quality-approval')).queryByRole('button', { name: 'Record quality approval' })
  ).not.toBeInTheDocument();
  expect(onSaved).toHaveBeenCalled();
});

test('a refused approval shows the server detail and changes nothing', async () => {
  const detail = 'CAR CAR-20261105-001 already has its Quality Manager approval; it is recorded once.';
  mocked.getCAR.mockResolvedValue(car);
  mocked.approveCARQuality.mockRejectedValue({ response: { status: 409, data: { detail } } });
  renderDetail();

  fireEvent.click(await screen.findByRole('button', { name: 'Record quality approval' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Record approval' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(detail);
  expect(screen.getByTestId('car-quality-approval')).toHaveTextContent('Not yet approved');
});

test('the approval waits for unsaved edits to be saved or discarded', async () => {
  mocked.getCAR.mockResolvedValue(car);
  renderDetail();

  const button = await screen.findByRole('button', { name: 'Record quality approval' });
  expect(button).toBeEnabled();
  fireEvent.change(screen.getByLabelText('RMA #'), { target: { value: 'RMA-900' } });
  expect(button).toBeDisabled();
});

test.each([
  ['an approved CAR', { quality_approved_at: '2026-11-02T15:00:00Z', quality_approved_by_name: 'Dana Quality' }, true],
  ['a VOID CAR', { status: 'void' }, true],
  ['a reader without quality:approve', {}, false],
])('%s offers no approval action', async (_label, overrides, can) => {
  mockCan = can;
  mocked.getCAR.mockResolvedValue({ ...car, ...overrides });
  renderDetail();

  await screen.findByTestId('car-quality-approval');
  expect(screen.queryByRole('button', { name: 'Record quality approval' })).not.toBeInTheDocument();
  // Printing is a read: always offered.
  expect(screen.getByRole('button', { name: /Print CAR/ })).toBeInTheDocument();
});
