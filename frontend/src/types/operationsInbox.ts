export type OperationalSource =
  | 'late_work_order'
  | 'blocker'
  | 'low_stock'
  | 'quality_ncr'
  | 'overdue_po_line'
  | 'mrp_shortage';

export interface OperationalInboxItem {
  key: string;
  source_kind: OperationalSource;
  source_id: number;
  occurrence: string;
  title: string;
  detail: string;
  severity: 'high' | 'medium' | 'low';
  href: string;
  suggested_action: string;
  owner_id: number | null;
  owner_name: string | null;
  next_action: string;
  acknowledged: boolean;
  snoozed_until: string | null;
  version: number;
  can_manage: boolean;
}

export interface OperationalInboxResponse {
  items: OperationalInboxItem[];
  assignees: { id: number; name: string; sources: OperationalSource[] }[];
  checked_at: string;
  truncated_sources: string[];
}

export interface OperationalInboxUpdate {
  expected_version: number;
  occurrence: string;
  owner_id?: number | null;
  next_action?: string;
  acknowledge?: boolean;
  snooze_hours?: number;
}
