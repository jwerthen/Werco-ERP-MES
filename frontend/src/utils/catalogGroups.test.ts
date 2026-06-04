import {
  ENGINEERING_PART_TYPE_OPTIONS,
  ENGINEERING_PART_TYPES,
  MATERIAL_SUPPLY_PART_TYPE_OPTIONS,
  MATERIAL_SUPPLY_PART_TYPES,
  isEngineeringPartType,
  isMaterialSupplyPartType,
} from './catalogGroups';

describe('catalogGroups', () => {
  it('separates engineering part types from materials and supplies', () => {
    expect(ENGINEERING_PART_TYPES).toEqual(['manufactured', 'assembly']);
    expect(MATERIAL_SUPPLY_PART_TYPES).toEqual(['purchased', 'raw_material', 'hardware', 'consumable']);

    expect(isEngineeringPartType('manufactured')).toBe(true);
    expect(isEngineeringPartType('assembly')).toBe(true);
    expect(isEngineeringPartType('raw_material')).toBe(false);
    expect(isEngineeringPartType('hardware')).toBe(false);

    expect(isMaterialSupplyPartType('purchased')).toBe(true);
    expect(isMaterialSupplyPartType('raw_material')).toBe(true);
    expect(isMaterialSupplyPartType('hardware')).toBe(true);
    expect(isMaterialSupplyPartType('consumable')).toBe(true);
    expect(isMaterialSupplyPartType('assembly')).toBe(false);
  });

  it('exposes create-form options for the correct catalog only', () => {
    expect(ENGINEERING_PART_TYPE_OPTIONS.map(option => option.value)).toEqual(['manufactured', 'assembly']);
    expect(MATERIAL_SUPPLY_PART_TYPE_OPTIONS.map(option => option.value)).toEqual([
      'raw_material',
      'hardware',
      'consumable',
      'purchased',
    ]);
  });
});
