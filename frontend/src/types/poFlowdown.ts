/**
 * Purchase-order quality flow-down (AS9100D 8.4.3).
 *
 * Wire shapes for the per-company flow-down profile, the quality-clause catalog, and the
 * flow-down block a printed purchase order carries. Mirrors the backend schemas behind
 * `/purchasing/flowdown-profile`, `/purchasing/quality-clauses` and the
 * `quality_flowdown` field of `GET /print/purchase-orders/{id}/print-data`.
 *
 * Every string here is supplier-facing document text: render it as React text, never as
 * markup.
 */

/**
 * GET /purchasing/flowdown-profile. One per company. Before the first save the server
 * returns an unsaved default (`enabled: false`, `version: 0`, no `id`).
 */
export interface POFlowdownProfile {
  id?: number | null;
  enabled: boolean;
  heading: string;
  document_reference: string | null;
  body: string | null;
  /** Optimistic-lock version. Echo it on PUT; a stale one is a 409. */
  version: number;
  updated_at?: string | null;
  updated_by?: number | null;
}

/**
 * PUT /purchasing/flowdown-profile body (Admin / Manager / Quality). Creates the row on the
 * first save. `version` is the profile's version as read: 409 when someone saved since.
 */
export interface POFlowdownProfileUpdate {
  enabled: boolean;
  heading: string;
  document_reference: string | null;
  body: string | null;
  version: number;
}

/** One entry in the company's quality-clause catalog. Retired, never deleted. */
export interface POQualityClause {
  id: number;
  /** Immutable after create; unique per company. */
  code: string;
  title: string;
  body: string | null;
  /** Pre-checked on a new purchase order; also the set a PO with no explicit choice uses. */
  default_selected: boolean;
  /** `false` = retired: offered on no new purchase order and never printed. */
  is_active: boolean;
  sort_order: number;
  created_by?: number | null;
  updated_by?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** POST /purchasing/quality-clauses body. A duplicate code is a 409. */
export interface POQualityClauseCreate {
  code: string;
  title: string;
  body: string | null;
  default_selected: boolean;
  is_active: boolean;
  sort_order: number;
}

/**
 * PUT /purchasing/quality-clauses/{id} body. Only the fields sent change (an explicit null on
 * a required field is a 422; `body: null` clears the text). The code cannot change.
 */
export interface POQualityClauseUpdate {
  title?: string;
  body?: string | null;
  default_selected?: boolean;
  is_active?: boolean;
  sort_order?: number;
}

/** The flow-down fields a purchase order (POResponse) carries. */
export interface POFlowdownFields {
  /**
   * The buyer's clause choice, stored as given. `null` means "the catalog's active
   * default clauses"; an explicit list (empty included) is the buyer's choice.
   */
  quality_clause_codes?: string[] | null;
  /** `true` once the PO has left DRAFT: the flow-down was frozen at issue and cannot change. */
  flowdown_frozen?: boolean;
}

/** One clause as printed on a purchase order. */
export interface POFlowdownClausePrint {
  code: string;
  title: string;
  body?: string | null;
}

/**
 * `quality_flowdown` on the purchase-order print data: the live block for a DRAFT, the
 * frozen snapshot once issued. `null` (or a block with `enabled: false`) prints nothing.
 */
export interface POFlowdownPrint {
  enabled?: boolean;
  heading?: string | null;
  document_reference?: string | null;
  body?: string | null;
  clauses?: POFlowdownClausePrint[] | null;
}
