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
      '2026-11-01T15:00:00Z'
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
    const payload = buildApprovalPayload(initial, { ...initial, approval_status: '', reevaluation_due: '' }, 'r', null);
    expect(payload).toEqual({ approval_status: null, reason: 'r', expected_updated_at: null, reevaluation_due: null });
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
    expect(within(dialog).getByTestId('derived-approval')).toHaveTextContent('every new purchase order');
    fireEvent.change(within(dialog).getByLabelText(/Reason for this change/), { target: { value: 'Audit failure' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save approval' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('This vendor changed since you opened it');
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('renders nothing without a vendor', () => {
    const { container } = render(<UpdateApprovalDialog vendor={null} onClose={jest.fn()} onSaved={jest.fn()} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
