/**
 * The client-side mirror of the server's `operation_target_quantity`: an operation's
 * OWN `component_quantity` wins, and only an operation without one inherits the work
 * order's `quantity_ordered`.
 */

import { operationTargetQuantity } from './operationQuantity';

describe('operationTargetQuantity', () => {
  it('prefers the operation\'s own target — a pool line item, not the order quantity', () => {
    // A batch WO ordered as 8 "sets" whose line item needs 18 pieces.
    expect(operationTargetQuantity({ component_quantity: 18 }, 8)).toBe(18);
  });

  it('falls back to the work-order quantity when the operation declares no target', () => {
    expect(operationTargetQuantity({}, 25)).toBe(25);
    expect(operationTargetQuantity({ component_quantity: null }, 25)).toBe(25);
    expect(operationTargetQuantity({ component_quantity: 0 }, 25)).toBe(25);
    expect(operationTargetQuantity(null, 25)).toBe(25);
  });

  it('does not treat a component target as optional just because a part number is absent', () => {
    // The traveler bug: a pool line carries a target with NO component part number.
    expect(operationTargetQuantity({ component_quantity: 4 }, 100)).toBe(4);
  });

  it('degrades to 0 rather than NaN on missing/garbage input', () => {
    expect(operationTargetQuantity({}, undefined)).toBe(0);
    expect(operationTargetQuantity({ component_quantity: Number.NaN }, 12)).toBe(12);
  });
});
