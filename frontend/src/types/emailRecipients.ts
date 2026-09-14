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
}

export interface EmailRecipientsSettings {
  events: EmailRecipientEvent[];
  users: EmailRecipientUser[];
}
