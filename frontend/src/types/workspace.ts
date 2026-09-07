export type WorkspaceKind = 'view' | 'draft';
export interface WorkspaceRecord<T = Record<string, unknown>> {
  key: string;
  namespace: string;
  kind: WorkspaceKind;
  name: string;
  data: T;
  version: number;
  updated_at: string;
}
export interface WorkspaceWrite<T = Record<string, unknown>> {
  kind: WorkspaceKind;
  name: string;
  data: T;
  version: number;
}
