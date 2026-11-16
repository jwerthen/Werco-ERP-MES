/**
 * Customers → Export control panel (ADMIN).
 *
 * Locks the contract with `GET /customers/{id}/export-control` and the two
 * contract writes:
 *   1. the badge and summary come from the server's DERIVED `controlled` flag
 *      (including control by a handling instruction, with no controlling contract);
 *   2. add sends the full contract with a REQUIRED reason, dates as the UTC
 *      instant of the Central calendar day, and previews whether the save makes
 *      the customer export-controlled;
 *   3. edit sends only the changed fields plus the reason — un-marking is an
 *      edit that clears a flag; there is no delete;
 *   4. writes are non-optimistic: a refusal (e.g. the 503 required-audit
 *      failure) keeps the dialog open and toasts the server's detail, and the
 *      badge changes only from the server's response.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import CustomerExportControlPanel from './CustomerExportControlPanel';
import { ToastProvider } from '../ui/Toast';
import type { CustomerContract, CustomerExportControl } from '../../types/exportControl';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getCustomerExportControl: jest.fn(),
    createCustomerContract: jest.fn(),
    updateCustomerContract: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const itarContract: CustomerContract = {
  id: 41,
  customer_id: 7,
  contract_number: 'FA8650-26-C-0001',
  contract_name: 'Air Force sustainment',
  itar_controlled: true,
  export_controlled: false,
  contains_cui: false,
  default_data_classification: 'internal',
  effective_date: '2026-01-01T06:00:00Z',
  expiration_date: null,
  active: true,
  handling_instructions: null,
  controlling: true,
  created_by: 1,
  updated_by: 1,
  created_at: '2026-11-01T15:00:00Z',
  updated_at: '2026-11-01T15:00:00Z',
};

const openView: CustomerExportControl = {
  customer_id: 7,
  customer_name: 'Acme Aero',
  customer_is_deleted: false,
  controlled: false,
  data_classification: null,
  controlling_contract_id: null,
  contracts: [],
};

const controlledView: CustomerExportControl = {
  ...openView,
  controlled: true,
  data_classification: 'itar_export_controlled',
  controlling_contract_id: 41,
  contracts: [itarContract],
};

function renderPanel() {
  return render(
    <ToastProvider>
      <CustomerExportControlPanel customerId={7} customerName="Acme Aero" />
    </ToastProvider>
  );
}

async function openAddDialog() {
  // "Add contract" stays disabled until the server's view has loaded.
  await screen.findByTestId('export-control-badge');
  fireEvent.click(screen.getByRole('button', { name: /add contract/i }));
  return screen.findByRole('dialog', { name: 'Add contract' });
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('CustomerExportControlPanel — standing', () => {
  it('shows "Not controlled" and an empty contract list', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    renderPanel();

    expect(await screen.findByTestId('export-control-badge')).toHaveTextContent('Not controlled');
    expect(screen.getByText('No contracts recorded')).toBeInTheDocument();
    expect(screen.getByTestId('export-control-panel')).toHaveTextContent(/see this customer's records in full/);
    expect(mockApi.getCustomerExportControl).toHaveBeenCalledWith(7);
  });

  it('shows the derived controlled badge, the controlling contract and its flags', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(controlledView);
    renderPanel();

    expect(await screen.findByTestId('export-control-badge')).toHaveTextContent(
      'Controlled · ITAR / export controlled'
    );
    const panel = screen.getByTestId('export-control-panel');
    expect(panel).toHaveTextContent('Export-controlled by contract Air Force sustainment');
    const row = screen.getByText('Air Force sustainment').closest('tr') as HTMLElement;
    expect(within(row).getByText('FA8650-26-C-0001')).toBeInTheDocument();
    expect(within(row).getByText('ITAR')).toBeInTheDocument();
    expect(within(row).getByText('Controls')).toBeInTheDocument();
    expect(within(row).getByText(/Jan 1, 2026/)).toBeInTheDocument();
  });

  it('explains control by a classified handling instruction', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue({
      ...openView,
      controlled: true,
      data_classification: 'cui',
      controlling_contract_id: null,
    });
    renderPanel();
    expect(await screen.findByTestId('export-control-badge')).toHaveTextContent('Controlled · CUI');
    expect(screen.getByTestId('export-control-panel')).toHaveTextContent(
      'Export-controlled by a classified handling instruction'
    );
  });

  it('renders an error with a working retry', async () => {
    mockApi.getCustomerExportControl.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(openView);
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: /retry/i }));
    expect(await screen.findByTestId('export-control-badge')).toHaveTextContent('Not controlled');
    expect(mockApi.getCustomerExportControl).toHaveBeenCalledTimes(2);
  });
});

describe('CustomerExportControlPanel — add contract', () => {
  it('marks the customer controlled with a reason, sending UTC instants for Central days', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    mockApi.createCustomerContract.mockResolvedValue(controlledView);
    renderPanel();
    const dialog = await openAddDialog();

    expect(within(dialog).getByTestId('contract-control-preview')).toHaveTextContent(
      'does not make the customer export-controlled'
    );
    fireEvent.change(within(dialog).getByLabelText(/Contract name/), {
      target: { value: 'Air Force sustainment' },
    });
    fireEvent.change(within(dialog).getByLabelText(/Contract number/), { target: { value: 'FA8650-26-C-0001' } });
    fireEvent.click(within(dialog).getByLabelText('ITAR controlled'));
    await waitFor(() =>
      expect(within(dialog).getByTestId('contract-control-preview')).toHaveTextContent(
        'This contract makes Acme Aero export-controlled'
      )
    );
    // March 1 2026 is before DST: Central midnight is 06:00 UTC.
    fireEvent.change(within(dialog).getByLabelText(/Effective date/), { target: { value: '2026-03-01' } });
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), {
      target: { value: 'DD-254 received for this program' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add contract' }));

    await waitFor(() =>
      expect(mockApi.createCustomerContract).toHaveBeenCalledWith(7, {
        contract_name: 'Air Force sustainment',
        contract_number: 'FA8650-26-C-0001',
        itar_controlled: true,
        export_controlled: false,
        contains_cui: false,
        default_data_classification: 'internal',
        effective_date: '2026-03-01T06:00:00.000Z',
        expiration_date: null,
        active: true,
        handling_instructions: null,
        reason: 'DD-254 received for this program',
      })
    );
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add contract' })).not.toBeInTheDocument());
    // The badge follows the server's response, not the form.
    expect(screen.getByTestId('export-control-badge')).toHaveTextContent('Controlled · ITAR / export controlled');
    expect(await screen.findByText(/Acme Aero is export-controlled/)).toBeInTheDocument();
  });

  it('previews control from a CUI classification alone', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    renderPanel();
    const dialog = await openAddDialog();
    fireEvent.change(within(dialog).getByLabelText(/Data classification/), { target: { value: 'cui' } });
    await waitFor(() =>
      expect(within(dialog).getByTestId('contract-control-preview')).toHaveTextContent(
        'makes Acme Aero export-controlled'
      )
    );
  });

  it('requires a contract name and a reason before calling the server', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    renderPanel();
    const dialog = await openAddDialog();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Add contract' }));

    expect(await within(dialog).findByText('A contract name is required.')).toBeInTheDocument();
    expect(within(dialog).getByText('A reason is required.')).toBeInTheDocument();
    expect(mockApi.createCustomerContract).not.toHaveBeenCalled();
  });

  it('refuses an expiration before the effective date', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    renderPanel();
    const dialog = await openAddDialog();
    fireEvent.change(within(dialog).getByLabelText(/Contract name/), { target: { value: 'Program X' } });
    fireEvent.change(within(dialog).getByLabelText(/Effective date/), { target: { value: '2026-05-01' } });
    fireEvent.change(within(dialog).getByLabelText(/Expiration date/), { target: { value: '2026-04-01' } });
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'New program' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add contract' }));

    expect(
      await within(dialog).findByText('The expiration date cannot be before the effective date.')
    ).toBeInTheDocument();
    expect(mockApi.createCustomerContract).not.toHaveBeenCalled();
  });

  it('keeps the dialog open and the badge unchanged when the audit write fails (503)', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(openView);
    mockApi.createCustomerContract.mockRejectedValue({
      response: {
        status: 503,
        data: {
          detail: 'The audit record for this export-control change could not be saved; nothing was changed. Retry.',
        },
      },
    });
    renderPanel();
    const dialog = await openAddDialog();
    fireEvent.change(within(dialog).getByLabelText(/Contract name/), { target: { value: 'Program X' } });
    fireEvent.click(within(dialog).getByLabelText('Contains CUI'));
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'CUI flowdown' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add contract' }));

    expect(await screen.findByText(/could not be saved; nothing was changed/)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Add contract' })).toBeInTheDocument();
    expect(screen.getByTestId('export-control-badge')).toHaveTextContent('Not controlled');
  });
});

describe('CustomerExportControlPanel — edit contract', () => {
  it('un-marks by clearing the flag, sending only the change and the reason', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(controlledView);
    mockApi.updateCustomerContract.mockResolvedValue({
      ...openView,
      contracts: [{ ...itarContract, itar_controlled: false, controlling: false }],
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Edit contract Air Force sustainment' }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit contract' });
    expect(within(dialog).getByLabelText(/Contract name/)).toHaveValue('Air Force sustainment');
    // The stored instant comes back as its Central calendar day.
    expect(within(dialog).getByLabelText(/Effective date/)).toHaveValue('2026-01-01');
    expect(within(dialog).getByLabelText('ITAR controlled')).toBeChecked();

    fireEvent.click(within(dialog).getByLabelText('ITAR controlled'));
    await waitFor(() =>
      expect(within(dialog).getByTestId('contract-control-preview')).toHaveTextContent(
        'This contract will no longer control Acme Aero'
      )
    );
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), {
      target: { value: 'Program declassified' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save contract' }));

    await waitFor(() =>
      expect(mockApi.updateCustomerContract).toHaveBeenCalledWith(7, 41, {
        itar_controlled: false,
        reason: 'Program declassified',
      })
    );
    await waitFor(() => expect(screen.getByTestId('export-control-badge')).toHaveTextContent('Not controlled'));
    expect(await screen.findByText(/Acme Aero is not export-controlled/)).toBeInTheDocument();
  });

  it('sends nothing when no contract field changed', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(controlledView);
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit contract Air Force sustainment' }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit contract' });
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Just looking' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save contract' }));

    expect(await screen.findByText(/nothing to save/)).toBeInTheDocument();
    expect(mockApi.updateCustomerContract).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Edit contract' })).toBeInTheDocument();
  });

  it('offers no delete', async () => {
    mockApi.getCustomerExportControl.mockResolvedValue(controlledView);
    renderPanel();
    await screen.findByText('Air Force sustainment');
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });
});
