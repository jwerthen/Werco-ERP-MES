export type ImprovementStatus =
  | 'new'
  | 'under_review'
  | 'approved'
  | 'in_progress'
  | 'implemented'
  | 'on_hold'
  | 'declined';
export type ImprovementPriority = 'low' | 'medium' | 'high';

export interface ImprovementOption {
  value: string;
  label: string;
  description?: string | null;
}

export interface ImprovementMetadata {
  categories: ImprovementOption[];
  statuses: ImprovementOption[];
  priorities: ImprovementOption[];
  owners: { id: number; name: string }[];
  can_manage: boolean;
}

export interface ImprovementSuggestion {
  id: number;
  company_id: number;
  title: string;
  problem: string;
  proposed_solution: string;
  expected_benefit: string;
  category: string;
  priority: ImprovementPriority;
  area: string | null;
  status: ImprovementStatus;
  owner_id: number | null;
  owner_name: string | null;
  target_date: string | null;
  implementation_notes: string | null;
  created_by: number;
  created_by_name: string;
  updated_by: number;
  updated_by_name: string;
  created_at: string;
  updated_at: string;
  reviewed_at: string | null;
  implemented_at: string | null;
  version: number;
}

export interface ImprovementActivity {
  id: number;
  kind: 'submitted' | 'updated' | 'status_changed' | 'comment';
  actor_id: number;
  actor_name: string;
  created_at: string;
  body: string | null;
  changes: Record<string, { from: unknown; to: unknown }>;
}

export interface ImprovementDetail extends ImprovementSuggestion {
  history: ImprovementActivity[];
}

export interface ImprovementList {
  items: ImprovementSuggestion[];
  total: number;
  status_counts: Record<ImprovementStatus, number>;
}

export interface ImprovementFilters {
  q?: string;
  status?: ImprovementStatus;
  category?: string;
  priority?: ImprovementPriority;
  owner_id?: number;
  skip?: number;
  limit?: number;
}

export interface ImprovementCreate {
  title: string;
  problem: string;
  proposed_solution: string;
  expected_benefit: string;
  category: string;
  priority: ImprovementPriority;
  area: string | null;
  owner_id: number | null;
  target_date: string | null;
}

export interface ImprovementUpdate extends Partial<ImprovementCreate> {
  expected_version: number;
  status?: ImprovementStatus;
  implementation_notes?: string | null;
  change_note?: string;
}
