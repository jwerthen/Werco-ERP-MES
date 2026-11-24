export interface EmailRecipientUser {
  id: number;
  name: string;
  email: string;
  is_active: boolean;
  email_deliverable: boolean;
}

export interface EmailRecipientEvent {
  event_key: string;
  label: string;
  description: string;
  category: string;
  user_ids: number[] | null;
  is_custom: boolean;
  missing_default_emails: string[];
  /**
   * Who the email ACTUALLY reaches under the SAVED settings. `selected` = the saved (or
   * shipped default) list; `automatic` = the catalog's roles and departments with each
   * person's own email choice applied.
   */
  audience_source: 'selected' | 'automatic';
  /** Sorted user ids with a deliverable address; map through `EmailRecipientsSettings.users`. */
  reaches_user_ids: number[];
  /** Also reaches people derived from each record (e.g. its creator) that no preview can list. */
  depends_on_record: boolean;
  /** True only when `reaches_user_ids` is empty and `depends_on_record` is false. */
  reaches_nobody: boolean;
}

export interface EmailRecipientsSettings {
  events: EmailRecipientEvent[];
  users: EmailRecipientUser[];
}
