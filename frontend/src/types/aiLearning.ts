export type AIEventType =
  | 'suggestion_shown'
  | 'accepted'
  | 'edited'
  | 'rejected'
  | 'ignored'
  | 'feedback'
  | 'outcome_observed';

export type AIRecommendationStatus = 'pending' | 'accepted' | 'dismissed' | 'stale' | 'snoozed';
export type AIRecommendationPriority = 'high' | 'medium' | 'low' | 'info';

export interface AICorrectionInput {
  field_path: string;
  proposed_value?: unknown;
  final_value?: unknown;
  correction_reason?: string;
  confidence_score?: number;
}

export interface AIInteractionEventInput {
  event_type: AIEventType;
  source_module: string;
  ai_feature?: string;
  surface?: string;
  entity_type?: string;
  entity_id?: number;
  recommendation_id?: number;
  context_summary?: string;
  event_payload?: Record<string, unknown>;
  confidence_score?: number;
  prompt_version?: string;
  model_version?: string;
  corrections?: AICorrectionInput[];
}

export interface AIRecommendation {
  id: number;
  source_module: string;
  recommendation_type: string;
  status: AIRecommendationStatus;
  priority: AIRecommendationPriority;
  title: string;
  summary: string;
  rationale?: string;
  target_entity_type?: string;
  target_entity_id?: number;
  suggested_action?: Record<string, unknown>;
  evidence?: Array<Record<string, unknown>>;
  impact?: Record<string, unknown>;
  confidence_score: number;
  prompt_version?: string;
  model_version?: string;
  status_reason?: string;
  created_by?: number;
  accepted_by?: number;
  dismissed_by?: number;
  created_at: string;
  updated_at: string;
  acted_at?: string;
  expires_at?: string;
  /** Deterministic Action Inbox ranking score; only populated by the list endpoint. */
  score?: number | null;
}

export interface AIRecommendationApplyResult {
  recommendation: AIRecommendation;
  applied: boolean;
  apply_result?: Record<string, unknown> | null;
  apply_error?: string | null;
}

/** True when Accept can run an allowlisted ERP mutation. */
export function recommendationIsApplyable(rec: AIRecommendation): boolean {
  const action = rec.suggested_action || {};
  const autonomy = String(action.autonomy || 'suggest_only');
  const type = String(action.type || '');
  if (!type) return false;
  return (
    autonomy === 'apply_on_accept' ||
    autonomy === 'execute_controlled' ||
    autonomy === 'auto_execute'
  );
}

export interface AIRecommendationInput {
  source_module: string;
  recommendation_type: string;
  priority?: AIRecommendationPriority;
  title: string;
  summary: string;
  rationale?: string;
  target_entity_type?: string;
  target_entity_id?: number;
  suggested_action?: Record<string, unknown>;
  evidence?: Array<Record<string, unknown>>;
  impact?: Record<string, unknown>;
  confidence_score?: number;
  prompt_version?: string;
  model_version?: string;
  expires_at?: string;
}

export interface AIRecommendationFeedbackInput {
  feedback: string;
  rating?: number;
  event_payload?: Record<string, unknown>;
}

export interface AIOutcomeInput {
  recommendation_id?: number;
  source_module: string;
  outcome_type: string;
  entity_type?: string;
  entity_id?: number;
  metric_name?: string;
  metric_value?: number;
  baseline_value?: number;
  target_value?: number;
  outcome_payload?: Record<string, unknown>;
  observed_at?: string;
}
