export interface ConsumablePerson {
  id: number;
  name: string;
  department: string | null;
  is_active: boolean;
}

export interface ConsumableMetadata {
  can_manage: boolean;
  can_export: boolean;
  people: ConsumablePerson[];
}

export interface ConsumableStock {
  inventory_item_id: number | null;
  part_id: number;
  part_number: string;
  part_name: string;
  unit_of_measure: string;
  location: string | null;
  lot_number: string | null;
  quantity_on_hand: number;
  quantity_available: number;
  part_quantity_on_hand: number;
  reorder_point: number;
  is_low_stock: boolean;
  can_checkout: boolean;
  unavailable_reason: string | null;
}

export interface ConsumableCheckout {
  id: number;
  inventory_item_id: number;
  part_id: number;
  part_number: string;
  part_name: string;
  unit_of_measure: string;
  location: string;
  lot_number: string | null;
  employee_id: number;
  employee_name: string;
  department: string | null;
  issued_by: number;
  issued_by_name: string;
  issued_at: string;
  quantity: number;
  notes: string | null;
  request_id: string;
  replayed?: boolean;
}

export interface ConsumablePage<T> {
  items: T[];
  total: number;
}

export interface ConsumableCheckoutCreate {
  inventory_item_id: number;
  employee_id: number;
  quantity: number;
  notes: string | null;
  request_id: string;
}

export interface ConsumableHistoryFilters {
  q?: string;
  employee_id?: number;
  part_id?: number;
  /** Inclusive shop-local calendar dates (America/Chicago). */
  date_from?: string;
  date_to?: string;
}

export interface ConsumableHistoryParams extends ConsumableHistoryFilters {
  skip?: number;
  limit?: number;
}

export interface ConsumableStockParams {
  q?: string;
  low_stock?: boolean;
  skip?: number;
  limit?: number;
}
