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
  /**
   * Server-computed link to the record this recommendation is about ("/work-orders/12",
   * "/parts/7"), or null. Derived from the target entity, never from the stored
   * `suggested_action.href`. Only populated by the list endpoint.
   */
  target_href?: string | null;
  /**
   * What a person's Accept & apply would do RIGHT NOW, resolved server-side from live
   * records. Non-null only on pending action cards in the list endpoint; null on
   * suggest-only cards and on every accept/apply/dismiss/snooze response.
   */
  apply_action?: AIRecommendationApplyAction | null;
}

/** First server refusal that stops this person's Accept & apply. */
export type AIApplyBlockedKind = 'credential' | 'read_only' | 'egress' | 'role' | 'target';

export interface AIRecommendationApplyAction {
  action_type: string;
  /** Server-resolved from live records, never from the stored action's labels. */
  summary: string;
  /** e.g. "{part} already has open PO(s): PO-...". */
  warnings: string[];
  /** Send back as `expected_fingerprint`; a 409 means the plan changed in between. */
  fingerprint: string;
  /** True only when nothing blocks the apply (blocked_kind is null). */
  can_apply: boolean;
  /** Whether this person may close the card with a record-only Accept ("Mark handled"). */
  can_mark_handled: boolean;
  blocked_reason?: string | null;
  blocked_kind?: AIApplyBlockedKind | null;
}

export interface AIRecommendationApplyResult {
  recommendation: AIRecommendation;
  applied: boolean;
  apply_result?: Record<string, unknown> | null;
  /**
   * Always null since owner ruling 3 (2026-11-05): a refused or failed apply is a 403/409
   * that leaves the card pending, never a 200 that records "accepted" while nothing ran.
   * Kept for wire compatibility; nothing reads it.
   */
  apply_error?: string | null;
}

/**
 * True when this person's Accept & apply would run right now. The SERVER decides
 * (`apply_action.can_apply`): the client never reads `suggested_action.autonomy`,
 * because legacy rows still carry retired tiers and only the server knows the live
 * plan, this person's role, the credential and the company's AI egress setting.
 */
export function recommendationIsApplyable(rec: AIRecommendation): boolean {
  return rec.apply_action?.can_apply === true;
}

/**
 * What the card's primary button does:
 *  - 'apply'        Accept & apply: runs the server-resolved plan as this person.
 *  - 'mark_handled' Mark handled: record-only; the plan cannot run (AI egress off, or
 *                   the target no longer needs it), but this person could have applied it.
 *  - 'accept'       Accept: record-only, on a suggest-only card (no apply_action).
 *  - 'none'         No accept button; the server's blocked_reason says why.
 */
export type AIRecommendationAcceptMode = 'apply' | 'mark_handled' | 'accept' | 'none';

export function recommendationAcceptMode(rec: AIRecommendation): AIRecommendationAcceptMode {
  const action = rec.apply_action;
  if (!action) return 'accept';
  if (action.can_apply) return 'apply';
  if (action.can_mark_handled) return 'mark_handled';
  return 'none';
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
