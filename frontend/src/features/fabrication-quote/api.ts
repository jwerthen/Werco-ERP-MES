import api from '../../services/api';
import type { ActualObservation, CalculationResult, ProcessProfile, ProcessProfileWrite, QuotePlan, QuoteRecord, QuoteSummary, QuoteWrite, RevisionSummary } from './types';

const base = '/fabrication-quotes';
export const fabricationQuoteApi = {
  profiles: () => api.request<{ items: ProcessProfile[]; total: number }>({ method: 'GET', url: '/fabrication-quote-profiles', params: { per_page: 200 } }),
  saveProfile: (profile: ProcessProfileWrite) => api.request<ProcessProfile>({ method: 'POST', url: '/fabrication-quote-profiles', data: profile }),
  list: (params: { page?: number; per_page?: number; search?: string } = {}) => api.request<{ items: QuoteSummary[]; total: number }>({ method: 'GET', url: base, params }),
  capabilities: () => api.request<{ can_write: boolean }>({ method: 'GET', url: `${base}/capabilities` }),
  get: (id: number) => api.request<QuoteRecord>({ method: 'GET', url: `${base}/${id}` }),
  create: (write: QuoteWrite, request_key?: string) => api.request<QuoteRecord>({ method: 'POST', url: base, data: { ...write, request_key } }),
  save: (id: number, expected_revision: number, write: QuoteWrite) => api.request<QuoteRecord>({ method: 'PUT', url: `${base}/${id}`, data: { ...write, expected_revision } }),
  calculate: (plan: QuotePlan, quote_id?: number) => api.request<CalculationResult>({ method: 'POST', url: `${base}/calculate`, data: { plan, quote_id } }),
  approve: (id: number, expected_revision: number, review_note: string) => api.request<QuoteRecord>({ method: 'POST', url: `${base}/${id}/approve`, data: { expected_revision, review_note } }),
  revise: (id: number, expected_revision: number) => api.request<QuoteRecord>({ method: 'POST', url: `${base}/${id}/revise`, data: { expected_revision } }),
  handoff: (id: number, expected_revision: number) => api.request<QuoteRecord>({ method: 'POST', url: `${base}/${id}/handoff`, data: { expected_revision } }),
  upload: (id: number, expected_revision: number, file: File, units_override?: string) => {
    const data = new FormData(); data.append('file', file); data.append('expected_revision', String(expected_revision));
    if (units_override) data.append('units_override', units_override);
    return api.request<QuoteRecord>({ method: 'POST', url: `${base}/${id}/files`, data, headers: { 'Content-Type': undefined } });
  },
  download: (id: number, fileId: number) => api.request<Blob>({ method: 'GET', url: `${base}/${id}/files/${fileId}/content`, responseType: 'blob' }),
  nest: (data: unknown) => api.request<Record<string, unknown>>({ method: 'POST', url: `${base}/nest`, data }),
  saveNest: (id: number, expected_revision: number, input: unknown) => api.request<QuoteRecord>({ method: 'POST', url: `${base}/${id}/nests`, data: { expected_revision, input } }),
  revisions: (id: number) => api.request<{ items: RevisionSummary[] }>({ method: 'GET', url: `${base}/${id}/revisions` }),
  revision: (id: number, revision: number) => api.request<Record<string, unknown>>({ method: 'GET', url: `${base}/${id}/revisions/${revision}` }),
  export: (id: number) => api.request<Record<string, unknown>>({ method: 'GET', url: `${base}/${id}/export` }),
  actuals: (id: number) => api.request<{ items: Record<string, unknown>[] }>({ method: 'GET', url: `${base}/${id}/actuals` }),
  addActual: (id: number, observation: ActualObservation) => api.request<Record<string, unknown>>({ method: 'POST', url: `${base}/${id}/actuals`, data: observation }),
};
