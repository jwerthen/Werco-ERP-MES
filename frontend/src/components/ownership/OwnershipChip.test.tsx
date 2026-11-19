import React from 'react';
import { render, screen } from '@testing-library/react';
import OwnershipChip from './OwnershipChip';

/**
 * The chip marks property that is NOT the company's (AS9100D 8.5.3). Company ownership is
 * the default and renders nothing, so the few owned rows stand out instead of every row
 * carrying a badge. A record from a server that predates the ownership fields reads as
 * company-owned.
 */
describe('OwnershipChip', () => {
  it.each([
    ['company', { ownership: 'company' as const, owner_name: null }],
    ['missing fields (pre-migration payload)', {}],
    ['null record', null],
  ])('renders nothing for %s', (_label, record) => {
    const { container } = render(<OwnershipChip record={record} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names a customer owner, with the label as its tooltip', () => {
    render(
      <OwnershipChip record={{ ownership: 'customer', owner_customer_id: 4, owner_name: 'Invented Aero Works' }} />
    );
    const chip = screen.getByText('Customer: Invented Aero Works');
    expect(chip.closest('[title]')).toHaveAttribute('title', 'Customer: Invented Aero Works');
  });

  it('names a supplier owner', () => {
    render(
      <OwnershipChip record={{ ownership: 'supplier', owner_vendor_id: 9, owner_name: 'Invented Plate Supply' }} />
    );
    expect(screen.getByText('Supplier: Invented Plate Supply')).toBeInTheDocument();
  });

  it('falls back to the owner id when the name is not resolved, never to company', () => {
    const { rerender } = render(
      <OwnershipChip record={{ ownership: 'customer', owner_customer_id: 12, owner_name: '  ' }} />
    );
    expect(screen.getByText('Customer: #12')).toBeInTheDocument();
    rerender(<OwnershipChip record={{ ownership: 'supplier', owner_vendor_id: null, owner_name: null }} />);
    expect(screen.getByText('Supplier: unknown owner')).toBeInTheDocument();
  });
});
