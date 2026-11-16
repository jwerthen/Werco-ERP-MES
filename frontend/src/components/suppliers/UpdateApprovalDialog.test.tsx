import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import api from '../../services/api';
import { UpdateApprovalDialog, buildApprovalPayload } from './UpdateApprovalDialog';
import type { AslVendor } from '../../types/supplierApproval';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { updateVendorApproval: jest.fn() },
}));
const mockedApi = api as jest.Mocked<typeof api>;

const vendor: AslVendor = {
  id: 7,
  code: 'VND-007',
  name: 'Bramble Metals',
  is_approved: false,
  approval_status: null,
  approved_scopes: null,
  approval_bases: null,
  approval_date: null,
  reevaluation_due: null,
  last_performance_evaluation: null,
  approval_notes: null,
  updated_at: '2026-11-01T15:00:00.123456Z',
};

const blankForm = {
  approval_status: '' as const,
  approved_scopes: [],
  approval_bases: [],
  approval_date: '',
  reevaluation_due: '',
  last_performance_evaluation: '',
  approval_notes: '',
};

beforeEach(() => jest.clearAllMocks());

describe('buildApprovalPayload', () => {
  it('always sends status, reason and the CAS stamp, and only the fields that changed', () => {
    const payload = buildApprovalPayload(
      blankForm,
      { ...blankForm, approval_status: 'approved', approved_scopes: ['hardware', 'adhesives_bonding'] },
      '  QL 7.4 review  ',
      '2026-11-01T15:00:00Z',
      false
    );
    expect(payload).toEqual({
      approval_status: 'approved',
      reason: 'QL 7.4 review',
      expected_updated_at: '2026-11-01T15:00:00Z',
      // Canonical dropdown order, whatever order they were ticked in.
      approved_scopes: ['adhesives_bonding', 'hardware'],
    });
    // An untouched blank approval date is NOT sent, so the server records today on approval.
    expect('approval_date' in payload).toBe(false);
  });

  it('sends an explicit null to clear a field the person emptied, and null status for off-the-ASL', () => {
    const initial = { ...blankForm, approval_status: 'conditional' as const, reevaluation_due: '2024-12-10' };
    const payload = buildApprovalPayload(
      initial,
      { ...initial, approval_status: '', reevaluation_due: '' },
      'r',
      null,
      false
    );
    expect(payload).toEqual({ approval_status: null, reason: 'r', expected_updated_at: null, reevaluation_due: null });
  });

  it('always sends the shown approval date when the save makes a not-approved vendor approved', () => {
    // A Conditional vendor carrying a date from the ASL import. Left out, the server would
    // record TODAY for this approval; so a date the field shows is sent even when it equals
    // the one on file, and only a blank field is left for the server to date.
    const initial = { ...blankForm, approval_status: 'conditional' as const, approval_date: '2021-03-01' };
    const kept = buildApprovalPayload(initial, { ...initial, approval_status: 'approved' }, 'r', null, false);
    expect(kept.approval_date).toBe('2021-03-01');
    const blank = buildApprovalPayload(
      initial,
      { ...initial, approval_status: 'approved', approval_date: '' },
      'r',
      null,
      false
    );
    expect('approval_date' in blank).toBe(false);
    // An already-approved vendor is not BECOMING approved: an untouched date stays unsent.
    const listed = { ...initial, approval_status: 'approved' as const };
    const resaved = buildApprovalPayload(
      listed,
      { ...listed, approval_status: 'approved_customer_directed' },
      'r',
      null,
      true
    );
    expect('approval_date' in resaved).toBe(false);
  });
});

describe('UpdateApprovalDialog', () => {
  it('saves the decision and hands back what the server stored', async () => {
    const saved = { ...vendor, approval_status: 'approved' as const, is_approved: true, approval_date: '2026-11-13' };
    mockedApi.updateVendorApproval.mockResolvedValue(saved);
    const onSaved = jest.fn();
    render(<UpdateApprovalDialog vendor={vendor} onClose={jest.fn()} onSaved={onSaved} />);

    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'approved' } });
    expect(within(dialog).getByTestId('derived-approval')).toHaveTextContent('Purchase orders can be raised');
    fireEvent.click(within(dialog).getByLabelText('Hardware'));
    fireEvent.click(within(dialog).getByLabelText('AS9100 Cert'));
    const save = within(dialog).getByRole('button', { name: 'Save approval' });
    expect(save).toBeDisabled(); // the reason is required
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Annual review' } });
    fireEvent.click(save);

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved));
    expect(mockedApi.updateVendorApproval).toHaveBeenCalledWith(7, {
      approval_status: 'approved',
      approved_scopes: ['hardware'],
      approval_bases: ['as9100'],
      reason: 'Annual review',
      expected_updated_at: '2026-11-01T15:00:00.123456Z',
    });
  });

  it('says what each status does to the Approved flag, including taking a listed vendor off', () => {
    const listed = { ...vendor, approval_status: 'approved' as const, is_approved: true };
    render(<UpdateApprovalDialog vendor={listed} onClose={jest.fn()} onSaved={jest.fn()} />);
    const dialog = screen.getByRole('dialog');
    const derived = () => within(dialog).getByTestId('derived-approval');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'conditional' } });
    expect(derived()).toHaveTextContent('can still be raised, each carrying a Conditional warning');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: '' } });
    expect(derived()).toHaveTextContent('Taking the vendor off the ASL turns its Approved flag off');
  });

  it('keeps the dialog open and shows the server refusal (stale vendor 409)', async () => {
    mockedApi.updateVendorApproval.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: 'This vendor changed since you opened it. Reload it before saving the approval.' },
      },
    });
    const onSaved = jest.fn();
    render(<UpdateApprovalDialog vendor={vendor} onClose={jest.fn()} onSaved={onSaved} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'disqualified' } });
    expect(within(dialog).getByTestId('derived-approval')).toHaveTextContent(
      'purchase orders to this vendor are refused'
    );
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Audit failure' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save approval' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('This vendor changed since you opened it');
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('never silently replaces the date on file when approving a Conditional vendor', async () => {
    // The ASL import put an APPROVED DATE on a Conditional vendor. Approving it records a new
    // approval: the old date must not sit in the field while the server stores today.
    const conditional = {
      ...vendor,
      approval_status: 'conditional' as const,
      is_approved: false,
      approval_date: '2021-03-01',
    };
    mockedApi.updateVendorApproval.mockResolvedValue({
      ...conditional,
      approval_status: 'approved',
      is_approved: true,
    });
    render(<UpdateApprovalDialog vendor={conditional} onClose={jest.fn()} onSaved={jest.fn()} />);
    const dialog = screen.getByRole('dialog');
    const date = within(dialog).getByLabelText('Approved date') as HTMLInputElement;
    expect(date.value).toBe('2021-03-01');

    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'approved' } });
    expect(date.value).toBe('');
    expect(within(dialog).getByText(/Approving records today, not the Mar 1, 2021 on file/)).toBeInTheDocument();

    // Back to a non-approving status: the date on file comes back.
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'conditional' } });
    expect(date.value).toBe('2021-03-01');

    // Approve, and keep the old date on purpose by typing it: it is SENT, so it is stored.
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'approved' } });
    fireEvent.change(date, { target: { value: '2021-03-01' } });
    expect(within(dialog).getByText('Approving records this date.')).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Upgraded' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save approval' }));
    await waitFor(() => expect(mockedApi.updateVendorApproval).toHaveBeenCalled());
    expect(mockedApi.updateVendorApproval.mock.calls[0][1]).toEqual(
      expect.objectContaining({ approval_status: 'approved', approval_date: '2021-03-01' })
    );
  });

  it('leaves a cleared date out, so the server records today as the field says', async () => {
    const conditional = {
      ...vendor,
      approval_status: 'conditional' as const,
      is_approved: false,
      approval_date: '2021-03-01',
    };
    mockedApi.updateVendorApproval.mockResolvedValue({
      ...conditional,
      approval_status: 'approved',
      is_approved: true,
    });
    render(<UpdateApprovalDialog vendor={conditional} onClose={jest.fn()} onSaved={jest.fn()} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('ASL status'), { target: { value: 'approved' } });
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Upgraded' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save approval' }));
    await waitFor(() => expect(mockedApi.updateVendorApproval).toHaveBeenCalled());
    expect('approval_date' in mockedApi.updateVendorApproval.mock.calls[0][1]).toBe(false);
  });

  it('renders nothing without a vendor', () => {
    const { container } = render(<UpdateApprovalDialog vendor={null} onClose={jest.fn()} onSaved={jest.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
