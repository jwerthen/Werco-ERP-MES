export interface HankBriefingItem {
  key: string;
  source_kind: string;
  source_id: number;
  title: string;
  detail: string;
  severity: 'high' | 'medium' | 'low';
  href: string;
  suggested_action: string;
  owner_name: string | null;
  is_mine: boolean;
}

export interface HankBriefing {
  checked_at: string;
  role: string;
  headline: string;
  summary: string;
  sections: Array<{
    key: string;
    title: string;
    description: string;
    total: number;
    truncated: boolean;
    items: HankBriefingItem[];
  }>;
  coverage_notes: string[];
}
