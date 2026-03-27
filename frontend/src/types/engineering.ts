// ── BOM Types ──────────────────────────────────────────────────────────────

export type LineType = 'component' | 'hardware' | 'consumable' | 'reference';
export type ItemType = 'make' | 'buy' | 'phantom';
export type BOMStatus = 'draft' | 'released' | 'obsolete';

export interface BOMItemPart {
  id: number;
  part_number: string;
  name: string;
  revision: string;
  part_type: string;
  has_bom?: boolean;
}

export interface BOMItem {
  id: number;
  bom_id: number;
  component_part_id: number;
  item_number: number;
  quantity: number;
  item_type: ItemType;
  line_type: LineType;
  unit_of_measure: string;
  find_number?: string;
  reference_designator?: string;
  notes?: string;
  torque_spec?: string;
  installation_notes?: string;
  scrap_factor: number;
  is_optional: boolean;
  is_alternate: boolean;
  component_part?: BOMItemPart;
  // Exploded view fields
  children?: BOMItem[];
  level?: number;
  extended_quantity?: number;
}

export interface BOM {
  id: number;
  part_id: number;
  revision: string;
  description?: string;
  bom_type: string;
  status: BOMStatus;
  is_active: boolean;
  part?: {
    id: number;
    part_number: string;
    name: string;
    revision: string;
    part_type: string;
  };
  items: BOMItem[];
}

// ── Routing Types ──────────────────────────────────────────────────────────

export type RoutingStatus = 'draft' | 'released' | 'obsolete';

export interface WorkCenter {
  id: number;
  code: string;
  name: string;
  work_center_type: string;
  hourly_rate: number;
}

export interface RoutingOperation {
  id: number;
  routing_id: number;
  sequence: number;
  operation_number: string;
  name: string;
  description?: string;
  work_center_id: number;
  work_center?: WorkCenter;
  setup_hours: number;
  run_hours_per_unit: number;
  move_hours: number;
  queue_hours: number;
  is_inspection_point: boolean;
  is_outside_operation: boolean;
  is_active: boolean;
}

export interface Routing {
  id: number;
  part_id: number;
  part?: {
    id: number;
    part_number: string;
    name: string;
    part_type: string;
  };
  revision: string;
  description?: string;
  status: RoutingStatus;
  is_active: boolean;
  total_setup_hours: number;
  total_run_hours_per_unit: number;
  total_labor_cost: number;
  total_overhead_cost: number;
  operations: RoutingOperation[];
  created_at: string;
}

// ── Import Types ───────────────────────────────────────────────────────────

export interface ImportAssembly {
  part_number?: string;
  name?: string;
  revision?: string;
  description?: string;
  drawing_number?: string;
  part_type?: string;
}

export interface ImportItem {
  line_number?: number;
  part_number?: string;
  description?: string;
  quantity?: number;
  unit_of_measure?: string;
  item_type?: string;
  line_type?: LineType;
  reference_designator?: string;
  find_number?: string;
  notes?: string;
}

export interface ImportPreview {
  document_type: 'bom' | 'part';
  assembly: ImportAssembly;
  items: ImportItem[];
  extraction_confidence?: string;
  warnings?: string[];
  raw_columns?: string[];
  raw_rows?: string[][];
  suggested_mapping?: Record<string, number | null>;
  source_format?: string;
}

// ── Display Helpers ────────────────────────────────────────────────────────

export const lineTypeColors: Record<string, string> = {
  component: 'bg-blue-100 text-blue-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
  reference: 'bg-gray-100 text-gray-600',
};

export const lineTypeLabels: Record<string, string> = {
  component: 'Component',
  hardware: 'Hardware',
  consumable: 'Consumable',
  reference: 'Reference',
};

export const partTypeColors: Record<string, string> = {
  manufactured: 'bg-blue-100 text-blue-800',
  purchased: 'bg-green-100 text-green-800',
  assembly: 'bg-purple-100 text-purple-800',
  raw_material: 'bg-yellow-100 text-yellow-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
};

export const partTypeLabels: Record<string, string> = {
  manufactured: 'Manufactured',
  assembly: 'Assembly',
  purchased: 'Purchased',
  raw_material: 'Raw Material',
  hardware: 'Hardware',
  consumable: 'Consumable',
};

export const itemTypeBadge: Record<string, string> = {
  make: 'bg-blue-100 text-blue-800',
  buy: 'bg-gray-100 text-gray-700',
  phantom: 'bg-purple-100 text-purple-800',
};

export const statusColors: Record<string, string> = {
  active: 'bg-green-100 text-green-800',
  draft: 'bg-yellow-100 text-yellow-800',
  released: 'bg-green-100 text-green-800',
  obsolete: 'bg-gray-100 text-gray-800',
  pending_approval: 'bg-yellow-100 text-yellow-800',
};

export function formatHours(hours: number): string {
  if (hours === 0) return '0 min';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  return `${hours.toFixed(2)} hr`;
}
