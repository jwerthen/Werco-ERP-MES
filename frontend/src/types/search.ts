export const SEARCH_TYPES = [
  ['part', 'Parts'],
  ['work_order', 'Work orders'],
  ['customer', 'Customers'],
  ['bom', 'BOMs'],
  ['routing', 'Routings'],
  ['user', 'People'],
  ['vendor', 'Suppliers'],
  ['purchase_order', 'Purchase orders'],
  ['quote', 'Quotes'],
] as const;
export interface EntitySearchResult {
  id: number;
  type: string;
  title: string;
  subtitle?: string | null;
  matched_alias?: string | null;
  url: string;
  icon: string;
}
export interface EntitySearchResponse {
  query: string;
  total: number;
  results: EntitySearchResult[];
  categories: Record<string, number>;
  offset: number;
  limit: number;
  has_more: boolean;
}
export const SEARCH_TYPE_PATHS: Record<string, string> = {
  part: '/parts',
  work_order: '/work-orders',
  customer: '/customers',
  bom: '/bom',
  routing: '/routing',
  user: '/users',
  vendor: '/purchasing',
  purchase_order: '/purchasing',
  quote: '/quotes',
};
