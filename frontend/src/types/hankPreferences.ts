export interface HankPreferencesValues {
  briefing_detail: 'concise' | 'standard';
  focus_area: 'role_default' | 'my_work' | 'shop' | 'quality' | 'purchasing' | 'inventory' | 'shipping';
  handoff_format: 'bullets' | 'checklist';
  follow_up_alerts: boolean;
}

export interface HankPreferencesResponse {
  company_id: number;
  version: number;
  preferences: HankPreferencesValues;
  updated_at: string | null;
  can_edit: boolean;
}

export interface HankPreferencesCommand {
  expected_company_id: number;
  expected_version: number;
}

export interface HankPreferencesUpdate extends HankPreferencesCommand {
  preferences: HankPreferencesValues;
}

export const DEFAULT_HANK_PREFERENCES: HankPreferencesValues = {
  briefing_detail: 'standard',
  focus_area: 'role_default',
  handoff_format: 'bullets',
  follow_up_alerts: true,
};
