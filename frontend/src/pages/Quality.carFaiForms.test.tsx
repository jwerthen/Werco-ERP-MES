/**
 * The Quality page's create forms for the CAR form fields (migration 122) and the FAI
 * that can be finished end to end (work order, revision, serial, due date), plus the FAI
 * tab's "Log" view (the FAI log of record) and its role-gated export.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityPage from './Quality';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { selectComboBoxOption } from '../test-utils/comboBox';
import type { UserRole } from '../types';

let mockRole: UserRole = 'quality';
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: mockRole, isSuperuser: false, can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
    getWorkOrders: jest.fn(),
    getFAILog: jest.fn(),
    exportFAILog: jest.fn(),
    createNCR: jest.fn(),
    createCAR: jest.fn(),
    createFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const PARTS = [
  { id: 5, part_number: 'PN-5', name: 'Bracket', revision: 'c' },
  { id: 6, part_number: 'PN-6', name: 'Hinge', revision: 'A' },
];

function renderQuality(entry = '/quality') {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={[entry]}>
        <QualityPage />
      </MemoryRouter>
    </ToastProvider>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'quality';
  mockedApi.getNCRs.mockResolvedValue([]);
  mockedApi.getCARs.mockResolvedValue([]);
  mockedApi.getFAIs.mockResolvedValue([]);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 0 });
  mockedApi.getParts.mockResolvedValue(PARTS as any);
  mockedApi.getCustomerNames.mockResolvedValue([{ id: 3, name: 'Acme Aerospace' }]);
  mockedApi.getVendors.mockResolvedValue([]);
  mockedApi.getFAILog.mockResolvedValue([]);
  mockedApi.getWorkOrders.mockResolvedValue([
    { id: 55, work_order_number: 'WO-1055', part_id: 5, customer_name: 'Acme Aerospace', customer_po: 'PO-889' },
    // A search hit on another part (its customer PO happened to match) is not offered.
    { id: 56, work_order_number: 'WO-1056', part_id: 6, customer_name: 'PN-5 Holdings' },
  ]);
  mockedApi.createFAI.mockResolvedValue({ id: 1 });
  mockedApi.createCAR.mockResolvedValue({ id: 1 });
});

async function openNewFai() {
  renderQuality('/quality?tab=fai');
  fireEvent.click((await screen.findAllByRole('button', { name: /new fai/i }))[0]);
  return screen.findByRole('dialog');
}

test('a new FAI links its job, defaults the revision from the part and carries serial and due date', async () => {
  const dialog = await openNewFai();

  selectComboBoxOption(within(dialog).getByLabelText(/^Part/), /PN-5/);
  expect(within(dialog).getByLabelText(/^Revision inspected/)).toHaveValue('C');

  const workOrder = within(dialog).getByLabelText(/^Work order/);
  await waitFor(() => expect(mockedApi.getWorkOrders).toHaveBeenCalledWith({ search: 'PN-5', limit: 200 }));
  await waitFor(() => expect(workOrder).toBeEnabled());
  fireEvent.click(workOrder);
  const list = document.getElementById(workOrder.getAttribute('aria-controls') || '')!;
  expect(within(list).queryByRole('option', { name: /WO-1056/ })).not.toBeInTheDocument();
  fireEvent.click(within(list).getByRole('option', { name: /WO-1055/ }));

  fireEvent.change(within(dialog).getByLabelText(/^Serial number/), { target: { value: 'SN-0001' } });
  fireEvent.change(within(dialog).getByLabelText(/^Due date/), { target: { value: '2026-12-01' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create FAI' }));

  await waitFor(() =>
    expect(mockedApi.createFAI).toHaveBeenCalledWith({
      part_id: 5,
      work_order_id: 55,
      part_revision: 'C',
      serial_number: 'SN-0001',
      due_date: '2026-12-01',
      fai_type: 'full',
      reason: 'new_part',
      customer_approval_required: false,
    })
  );
});

test('a revision that is not letters and digits is refused before the request', async () => {
  const dialog = await openNewFai();
  selectComboBoxOption(within(dialog).getByLabelText(/^Part/), /PN-6/);
  fireEvent.change(within(dialog).getByLabelText(/^Revision inspected/), { target: { value: 'B-1' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create FAI' }));

  expect(await within(dialog).findByRole('alert')).toHaveTextContent('Revision must be 1-20 letters or digits');
  expect(mockedApi.createFAI).not.toHaveBeenCalled();
});

test('a new CAR sends the controlled-form fields, never the server-owned ones', async () => {
  renderQuality('/quality?tab=car');
  fireEvent.click((await screen.findAllByRole('button', { name: /new car/i }))[0]);
  const dialog = await screen.findByRole('dialog');

  fireEvent.change(within(dialog).getByLabelText(/^Title/), { target: { value: 'Recurring burr on PN-5' } });
  fireEvent.change(within(dialog).getByLabelText(/^Problem Description/), {
    target: { value: 'Burrs found on three consecutive lots at final inspection.' },
  });
  fireEvent.change(within(dialog).getByLabelText('Reason for initiation'), { target: { value: 'customer_feedback' } });
  fireEvent.change(within(dialog).getByLabelText('RMA #'), { target: { value: 'RMA-778' } });
  selectComboBoxOption(within(dialog).getByLabelText('Part number (if applicable)'), /PN-5/);
  fireEvent.change(within(dialog).getByLabelText('Labor cost'), { target: { value: '120.00' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create CAR' }));

  await waitFor(() => expect(mockedApi.createCAR).toHaveBeenCalledTimes(1));
  const payload = mockedApi.createCAR.mock.calls[0][0];
  expect(payload).toMatchObject({
    title: 'Recurring burr on PN-5',
    car_type: 'corrective',
    priority: 3,
    initiation_reason: 'customer_feedback',
    rma_number: 'RMA-778',
    part_id: 5,
    cost_labor: '120.00',
    // Blank Date Submitted: the server records today (Central).
    submitted_date: null,
  });
  for (const key of ['customer_name', 'cost_total', 'quality_approved_by', 'quality_approved_at']) {
    expect(payload).not.toHaveProperty(key);
  }
});

test.each<[UserRole, boolean]>([
  ['admin', true],
  ['manager', true],
  ['quality', false],
  ['supervisor', false],
])('the FAI Log view is the log of record; %s export: %s', async (role, canExport) => {
  mockRole = role;
  renderQuality('/quality?tab=fai');

  fireEvent.click(await screen.findByRole('button', { name: 'Log' }));
  expect(await screen.findByTestId('fai-log-view')).toBeInTheDocument();
  await waitFor(() => expect(mockedApi.getFAILog).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: 'Log' })).toHaveAttribute('aria-pressed', 'true');
  expect(!!screen.queryByRole('button', { name: /Export log/ })).toBe(canExport);

  fireEvent.click(screen.getByRole('button', { name: 'Inspections' }));
  await waitFor(() => expect(screen.queryByTestId('fai-log-view')).not.toBeInTheDocument());
});
