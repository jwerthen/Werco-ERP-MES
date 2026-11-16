/**
 * Long-lived, per-user API tokens (`/api/v1/api-tokens`, ADMIN only).
 *
 * Mirrors `backend/app/schemas/api_token.py`. The raw JWT appears exactly once,
 * on `ApiTokenIssued.token` from the create call; every other shape is metadata
 * only. `jti_prefix` is a correlation handle (the first eight characters of the
 * JWT id), not a credential.
 */

/**
 * `full` = everything the bound user's role allows. `read_only` = GET routes
 * only, and never a route under a tag in WERCO_MCP_READ_ONLY_DENY_TAGS. A scope
 * only narrows the user; it is fixed at mint (revoke and re-mint to change it).
 */
export type ApiTokenScope = 'full' | 'read_only';

export interface ApiToken {
  id: number;
  label: string;
  /** The user this token acts AS — it carries exactly that user's role. */
  user_id: number;
  jti_prefix: string;
  scope: ApiTokenScope;
  /**
   * The token's answers go to an outside AI service: export-control redaction
   * and the company AI-egress switch apply. Always read-only.
   */
  ai_external: boolean;
  /** `null` = never expires. */
  expires_at: string | null;
  revoked: boolean;
  revoked_at: string | null;
  revoked_by: number | null;
  revoke_reason: string | null;
  /** Touched at most once per five minutes — a liveness marker, not a hit counter. */
  last_used_at: string | null;
  created_by: number;
  created_at: string;
}

/** Returned ONLY from `POST /api-tokens/`. `token` is never retrievable again. */
export interface ApiTokenIssued extends ApiToken {
  token: string;
}

export interface ApiTokenCreateInput {
  user_id: number;
  label: string;
  /** 1..3650. Omit (or null) for a token that never expires. */
  expires_days?: number | null;
  scope: ApiTokenScope;
  /**
   * Omit for the server default (true for read_only, false for full).
   * `true` with scope `full` is refused 422.
   */
  ai_external?: boolean;
}

export interface ApiTokenListResponse {
  api_tokens: ApiToken[];
}

export interface ApiTokenListParams {
  userId?: number;
  includeRevoked?: boolean;
}

/** Server bound on `expires_days` (`MAX_API_TOKEN_EXPIRES_DAYS`). */
export const MAX_API_TOKEN_EXPIRES_DAYS = 3650;
