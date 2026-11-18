/**
 * Comments with @mentions, and record watchers (v1: work orders only).
 *
 * Mirrors `backend/app/schemas/comment.py`. Mentions are STRUCTURED: the client
 * sends user ids chosen from `GET /users/mention-search`, and the server never
 * parses names out of the comment text — so the id list, not the `@Name` text in
 * the body, is what decides who is mentioned (and notified).
 */

/** The record types comments are enabled on. v1 is work orders only. */
export type CommentEntityType = 'work_order';

/** The server's caps (`COMMENT_BODY_MAX_LENGTH`, `MAX_MENTIONS_PER_COMMENT`). */
export const COMMENT_BODY_MAX_LENGTH = 5000;
export const MAX_MENTIONS_PER_COMMENT = 20;

/**
 * A person on a comment — its author or someone it mentions — and also a
 * `GET /users/mention-search` row. Name only: never an email, role or phone.
 */
export interface CommentPerson {
  id: number;
  display_name: string;
}

/** `GET /users/mention-search` row. Same shape as `CommentPerson`. */
export type MentionableUser = CommentPerson;

export interface CommentResponse {
  id: number;
  entity_type: CommentEntityType;
  entity_id: number;
  author: CommentPerson;
  /** Stored verbatim (trimmed). Render as plain text, never as HTML. */
  body: string;
  /** UTC ISO-8601 with a trailing `Z`. */
  created_at: string;
  /** UTC ISO-8601 with a trailing `Z`; null until the author first edits it. */
  edited_at: string | null;
  mentions: CommentPerson[];
  /** Whether THE CALLER may edit this comment (the author only). */
  can_edit: boolean;
  /** Whether THE CALLER may delete this comment (the author, an Admin or a Manager). */
  can_delete: boolean;
}

/** `POST /comments`. */
export interface CommentCreateInput {
  entity_type: CommentEntityType;
  entity_id: number;
  body: string;
  mentioned_user_ids: number[];
}

/** `PATCH /comments/{id}` — the FULL new state; both fields are required. */
export interface CommentUpdateInput {
  body: string;
  mentioned_user_ids: number[];
}

/** `GET` / `POST` / `DELETE /watchers`. */
export interface WatchState {
  watching: boolean;
  watcher_count: number;
}
