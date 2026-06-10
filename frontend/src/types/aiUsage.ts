/**
 * AI usage / cost telemetry contracts.
 *
 * Mirrors backend/app/schemas/ai_usage.py (GET /api/v1/ai-usage/summary):
 * read-only aggregates over the AI usage ledger for the active company.
 */

/** Aggregated usage for one bucket (a task, a model, or the whole window). */
export interface AIUsageAggregate {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  /** Estimated spend in USD; null when no priced calls landed in the bucket. */
  estimated_cost_usd: number | null;
  avg_latency_ms: number | null;
  /** Failed calls / total calls, 0.0-1.0. */
  error_rate: number;
}

export interface AIUsageTaskSummary extends AIUsageAggregate {
  task: string;
}

export interface AIUsageModelSummary extends AIUsageAggregate {
  model: string;
}

export interface AIUsageSummaryResponse {
  window_days: number;
  /** ISO datetime — start of the aggregation window. */
  since: string;
  totals: AIUsageAggregate;
  by_task: AIUsageTaskSummary[];
  by_model: AIUsageModelSummary[];
}
