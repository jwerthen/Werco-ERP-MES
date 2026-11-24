/**
 * One row of `GET /inventory/low-stock` — the one low-stock rule
 * (`backend/app/services/low_stock_service.py`).
 *
 * A live part with a reorder point above zero whose COMPANY-OWNED on hand, summed over
 * every active stock row, is at or below that point. A part with no stock row at all reads
 * 0 and is listed with `out_of_stock: true`. Rows arrive critical first, then by shortage.
 */
export interface LowStockAlert {
  part_id: number;
  part_number: string;
  part_name: string;
  /** Company-owned on hand (customer/supplier property excluded). */
  quantity_on_hand: number;
  reorder_point: number;
  reorder_quantity: number | null;
  safety_stock: number | null;
  shortage: number;
  /** At or below safety stock (0 when none is set). */
  is_critical: boolean;
  /** Company-owned on hand is zero or below. */
  out_of_stock: boolean;
}
