export type HankWatchCondition = 'blockers_cleared' | 'pdf_attached';

export interface HankWatchCreate {
  expected_company_id: number;
  request_key: string;
  work_order_id: number;
  condition: HankWatchCondition;
  document_type?: string | null;
}

export type HankWatchCommand = 'check' | 'snooze' | 'resume' | 'cancel';
