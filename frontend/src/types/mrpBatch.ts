import { SupplyDraft } from '../pages/MRPSupplyReview';

export interface MRPPurchaseReviewLine {
  action_id: number;
  part_number: string;
  part_name: string;
  mrp_run_number: string;
  quantity: number;
  due_date: string;
  review_token: string;
  vendor_id: number | null;
  unit_price: number;
  blocked_reason: string | null;
  existing_draft: SupplyDraft | null;
  vendors: Array<{ id: number; code: string; name: string }>;
}
export interface MRPPurchaseBatchPayload {
  request_key: string;
  lines: Array<{
    action_id: number;
    review_token: string;
    quantity: number;
    due_date: string;
    vendor_id: number;
    unit_price: number;
    notes: string;
  }>;
}
export interface MRPPurchaseBatchResult {
  drafts: SupplyDraft[];
  purchase_orders: Array<{
    id: number;
    number: string;
    url: string;
    vendor_id: number;
    action_ids: number[];
    total: number;
    status: string;
  }>;
  replayed: boolean;
}
