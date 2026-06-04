import { PartType } from '../types';

export const ENGINEERING_PART_TYPES: PartType[] = ['manufactured', 'assembly'];
export const MATERIAL_SUPPLY_PART_TYPES: PartType[] = ['purchased', 'raw_material', 'hardware', 'consumable'];

export const ENGINEERING_PART_TYPE_OPTIONS = [
  { value: 'manufactured' as PartType, label: 'Manufactured' },
  { value: 'assembly' as PartType, label: 'Assembly' },
];

export const MATERIAL_SUPPLY_PART_TYPE_OPTIONS = [
  { value: 'raw_material' as PartType, label: 'Raw Material' },
  { value: 'hardware' as PartType, label: 'Hardware' },
  { value: 'consumable' as PartType, label: 'Consumable' },
  { value: 'purchased' as PartType, label: 'Purchased COTS' },
];

export function isEngineeringPartType(partType?: string): boolean {
  return ENGINEERING_PART_TYPES.includes(partType as PartType);
}

export function isMaterialSupplyPartType(partType?: string): boolean {
  return MATERIAL_SUPPLY_PART_TYPES.includes(partType as PartType);
}
