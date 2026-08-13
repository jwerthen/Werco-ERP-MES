/**
 * The per-operation quantity target, client side.
 *
 * MIRRORS the server's ONE rule — `operation_target_quantity` in
 * `backend/app/services/work_order_state_service.py`: an operation that declares its
 * own `component_quantity` (> 0) is targeted at THAT, and only an operation without
 * one inherits the work order's `quantity_ordered`.
 *
 * It exists because that distinction is not cosmetic on two shapes the shop actually
 * runs. A laser nest's operation is targeted at its own sheet runs, and a **batch /
 * pool** work order carries one operation per fabricated line item, each with its own
 * piece count — so a WO ordered as "8 sets" can hold an item that needs 18 pieces.
 * Anything that prints or edits a per-operation quantity from `quantity_ordered`
 * alone tells the operator the wrong number; on the printed traveler it did exactly
 * that (fixed 2026-08-13), because it keyed the fallback off `component_part_number`,
 * which a pool line does not have.
 *
 * The kiosk and shop-floor screens don't need this: the server already resolves the
 * same rule for them and sends the answer as the row's `quantity_ordered`. Use this
 * where the client holds raw operation rows — print views, forms, dashboards.
 */

/** The subset of a work-order operation this rule reads. */
export interface OperationQuantityFields {
  component_quantity?: number | null;
}

export function operationTargetQuantity(
  operation: OperationQuantityFields | null | undefined,
  workOrderQuantityOrdered: number | null | undefined
): number {
  const componentQuantity = Number(operation?.component_quantity ?? 0);
  if (Number.isFinite(componentQuantity) && componentQuantity > 0) {
    return componentQuantity;
  }
  const ordered = Number(workOrderQuantityOrdered ?? 0);
  return Number.isFinite(ordered) ? ordered : 0;
}
