import {
  aslStatusLabel,
  basisLabels,
  canWriteAsl,
  derivedIsApproved,
  isOfferedForPurchaseOrders,
  isReevaluationOverdue,
  scopeLabels,
} from './supplierApproval';

describe('supplierApproval helpers', () => {
  afterEach(() => jest.useRealTimers());

  it('labels a missing status as not on the ASL and maps slugs to the QL 7.4 wording', () => {
    expect(aslStatusLabel(null)).toBe('Not on the ASL');
    expect(aslStatusLabel('approved_customer_directed')).toBe('Approved - Customer Directed');
    expect(scopeLabels(['raw_material_metallics', 'hardware'])).toBe('Raw Material - Metallics, Hardware');
    expect(basisLabels(['as9100', 'customer_requirement'])).toBe('AS9100 Cert, Customer Requirement');
    expect(scopeLabels(null)).toBe('');
  });

  it('derives is_approved exactly as the server does', () => {
    expect(derivedIsApproved('approved')).toBe(true);
    expect(derivedIsApproved('approved_customer_directed')).toBe(true);
    expect(derivedIsApproved('conditional')).toBe(false);
    expect(derivedIsApproved('disqualified')).toBe(false);
    // Not on the ASL: the flag is left as it is.
    expect(derivedIsApproved(null)).toBeNull();
  });

  it('offers checkbox-approved and Conditional vendors for purchase orders, never Disqualified', () => {
    expect(isOfferedForPurchaseOrders({ is_approved: true, approval_status: 'approved' })).toBe(true);
    expect(isOfferedForPurchaseOrders({ is_approved: true, approval_status: null })).toBe(true);
    expect(isOfferedForPurchaseOrders({ is_approved: false, approval_status: 'conditional' })).toBe(true);
    expect(isOfferedForPurchaseOrders({ is_approved: false, approval_status: 'disqualified' })).toBe(false);
    expect(isOfferedForPurchaseOrders({ is_approved: true, approval_status: 'disqualified' })).toBe(false);
    expect(isOfferedForPurchaseOrders({ is_approved: false, approval_status: null })).toBe(false);
  });

  it('flags an overdue re-evaluation against Central today, never for Disqualified', () => {
    jest.useFakeTimers();
    // 2026-11-13 03:00 UTC is still 2026-11-12 in Central time.
    jest.setSystemTime(new Date('2026-11-13T03:00:00Z'));
    expect(isReevaluationOverdue({ approval_status: 'approved', reevaluation_due: '2026-11-11' })).toBe(true);
    expect(isReevaluationOverdue({ approval_status: 'approved', reevaluation_due: '2026-11-12' })).toBe(false);
    expect(isReevaluationOverdue({ approval_status: 'conditional', reevaluation_due: '2024-12-10' })).toBe(true);
    expect(isReevaluationOverdue({ approval_status: 'disqualified', reevaluation_due: '2024-12-10' })).toBe(false);
    expect(isReevaluationOverdue({ approval_status: null, reevaluation_due: '2024-12-10' })).toBe(false);
  });

  it('lets only admin, manager, quality and superusers write the ASL', () => {
    expect(canWriteAsl({ role: 'quality' })).toBe(true);
    expect(canWriteAsl({ role: 'manager' })).toBe(true);
    expect(canWriteAsl({ role: 'admin' })).toBe(true);
    expect(canWriteAsl({ role: 'supervisor' })).toBe(false);
    expect(canWriteAsl({ role: 'viewer', is_superuser: true })).toBe(true);
    expect(canWriteAsl(null)).toBe(false);
  });
});
