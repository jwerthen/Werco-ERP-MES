export type PriceHistoryTrend = 'all' | 'up' | 'down' | 'unchanged' | 'new';
export type PriceHistorySort = 'recent' | 'increase' | 'decrease' | 'name';

export interface MaterialPriceHistoryParams {
  search?: string;
  part_type?: string;
  trend?: PriceHistoryTrend;
  sort?: PriceHistorySort;
  page?: number;
  page_size?: number;
}

export interface MaterialPriceHistoryDetailParams {
  vendor_id?: number;
  start_date?: string;
  end_date?: string;
  page?: number;
  page_size?: number;
}

export interface PriceHistoryPoint {
  purchase_order_id: number;
  order_date: string;
  unit_price: number;
}

export interface MaterialPriceSummary {
  part_id: number;
  part_number: string;
  part_name: string;
  part_type: string;
  unit_of_measure: string | null;
  currency: string | null;
  latest_unit_price: number;
  previous_unit_price: number | null;
  price_change: number | null;
  price_change_percent: number | null;
  last_order_date: string;
  latest_po_id: number;
  latest_po_number: string;
  latest_vendor_id: number;
  latest_vendor_name: string;
  order_count: number;
  total_quantity: number;
  total_spend: number;
  sparkline: PriceHistoryPoint[];
}

export interface MaterialPriceHistoryResponse {
  items: MaterialPriceSummary[];
  total: number;
  page: number;
  page_size: number;
  summary: {
    tracked_parts: number;
    price_increases: number;
    price_decreases: number;
    unchanged_parts: number;
    new_parts: number;
  };
}

export interface MaterialPricePurchase {
  purchase_order_id: number;
  po_number: string;
  order_date: string;
  status: string;
  vendor_id: number;
  vendor_name: string;
  quantity_ordered: number;
  unit_price: number;
  extended_price: number;
  line_count: number;
  previous_unit_price: number | null;
  price_change: number | null;
  price_change_percent: number | null;
  unit_of_measure: string | null;
  currency: string | null;
}

export interface MaterialPriceHistoryDetail {
  part: MaterialPriceSummary;
  history: MaterialPricePurchase[];
  total: number;
  page: number;
  page_size: number;
  stats: {
    latest_unit_price: number | null;
    previous_unit_price: number | null;
    price_change: number | null;
    price_change_percent: number | null;
    lowest_unit_price: number | null;
    highest_unit_price: number | null;
    weighted_average_unit_price: number | null;
    total_quantity: number;
    total_spend: number;
    order_count: number;
  };
  chart: (PriceHistoryPoint & { vendor_name: string; quantity_ordered: number; po_number: string })[];
  chart_truncated: boolean;
  vendor_options: { id: number; name: string }[];
  notes: string[];
}
