import React from 'react';
import { render, screen, within } from '@testing-library/react';
import { SupplierApprovalWarnings } from './SupplierApprovalWarnings';

const DISQUALIFIED =
  'Bramble Metals is Disqualified on the Approved Supplier List; a purchase order cannot be raised to it.';
const CONDITIONAL = { code: 'vendor_conditional', message: 'Bramble Metals is Conditional on the ASL.' };

describe('SupplierApprovalWarnings', () => {
  it('shows a Disqualified warning as the red stop even with no refusal passed (an existing PO)', () => {
    // GET /purchasing/purchase-orders/{id} recomputes the warnings from the vendor's CURRENT
    // status and passes no refusal; a vendor disqualified after the draft was raised must not
    // read as "Advisory: the order can still be raised" while send / approve answer 409.
    render(
      <SupplierApprovalWarnings
        warnings={[{ code: 'vendor_disqualified', message: DISQUALIFIED }, CONDITIONAL]}
        existingOrder
      />
    );
    const stop = screen.getByRole('alert');
    expect(stop).toHaveTextContent(DISQUALIFIED);
    expect(stop).toHaveTextContent('This order cannot be sent, approved, submitted for approval or given new lines');
    const advisory = screen.getByRole('region', { name: 'Approved Supplier List warnings' });
    expect(within(advisory).queryByText(DISQUALIFIED)).not.toBeInTheDocument();
    expect(within(advisory).getByText(CONDITIONAL.message)).toBeInTheDocument();
    // On an existing order the warnings are the vendor's current entry, not what create recorded.
    expect(advisory).toHaveTextContent('from the vendor’s current Approved Supplier List entry');
    expect(advisory).not.toHaveTextContent('can still be raised');
  });

  it('folds the matching warning into a passed refusal on the Create PO form', () => {
    render(
      <SupplierApprovalWarnings
        warnings={[{ code: 'vendor_disqualified', message: DISQUALIFIED }]}
        refusal={DISQUALIFIED}
      />
    );
    expect(screen.getAllByText(DISQUALIFIED)).toHaveLength(1);
    expect(screen.getByRole('alert')).not.toHaveTextContent('This order cannot be sent');
    expect(screen.queryByRole('region', { name: 'Approved Supplier List warnings' })).not.toBeInTheDocument();
  });

  it('keeps advisory-only warnings amber with the create wording, and renders nothing for none', () => {
    const { rerender } = render(<SupplierApprovalWarnings warnings={[CONDITIONAL]} />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Approved Supplier List warnings' })).toHaveTextContent(
      'Advisory: the order can still be raised, and these warnings are recorded with it.'
    );
    rerender(<SupplierApprovalWarnings warnings={[]} />);
    expect(screen.queryByTestId('supplier-approval-warnings')).not.toBeInTheDocument();
  });
});
