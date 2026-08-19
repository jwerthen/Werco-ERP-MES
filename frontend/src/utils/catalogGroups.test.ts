import {
  ENGINEERING_PART_TYPE_OPTIONS,
  ENGINEERING_PART_TYPES,
  MATERIAL_SUPPLY_PART_TYPE_OPTIONS,
  MATERIAL_SUPPLY_PART_TYPES,
  RAW_STOCK_PART_TYPES,
  isEngineeringPartType,
  isMaterialSupplyPartType,
  isProductionPartType,
  isRawStockPartType,
  partTypeLabel,
  partitionMaterialTiers,
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

  it('names a type from EITHER catalog, so a display can show what a picker would not offer', () => {
    // A screen that DISPLAYS a type has to name whatever the row carries — the
    // `purchased` component reached through the BOM drill-through included —
    // even when the form it sits in only offers the engineering pair.
    expect(partTypeLabel('manufactured')).toBe('Manufactured');
    expect(partTypeLabel('assembly')).toBe('Assembly');
    expect(partTypeLabel('purchased')).toBe('Purchased COTS');
    expect(partTypeLabel('raw_material')).toBe('Raw Material');
    expect(partTypeLabel('hardware')).toBe('Hardware');
    expect(partTypeLabel('consumable')).toBe('Consumable');
  });

  it('de-underscores an unknown type rather than rendering a bare enum token', () => {
    expect(partTypeLabel('some_future_type')).toBe('some future type');
  });
});

/**
 * The third tier the material-tie pickers need, narrower than "material".
 *
 * `MATERIAL_SUPPLY_PART_TYPES` answers "which catalog does this live in", which
 * is the right question for a catalog page and the WRONG one for a picker that
 * chooses stock a work order CONSUMES: three of its four types are bought
 * components (the seeded catalog types bolts and nuts as `purchased`).
 */
describe('raw stock is a narrower tier than material supply', () => {
  it('is raw_material only — deliberately not the whole material-supply set', () => {
    expect(RAW_STOCK_PART_TYPES).toEqual(['raw_material']);

    expect(isRawStockPartType('raw_material')).toBe(true);
    // The three bought-component types are material supply but NOT raw stock.
    expect(isRawStockPartType('purchased')).toBe(false);
    expect(isRawStockPartType('hardware')).toBe(false);
    expect(isRawStockPartType('consumable')).toBe(false);
    expect(isRawStockPartType('manufactured')).toBe(false);
    expect(isRawStockPartType('assembly')).toBe(false);
  });

  it('reads an unknown type as NOT raw stock, so it lands behind the escape hatch', () => {
    // Not raw stock ≠ excluded. An unknown-typed part is still offered under
    // "Show all materials"; it just is not in the default view.
    expect(isRawStockPartType(null)).toBe(false);
    expect(isRawStockPartType(undefined)).toBe(false);
    expect(isRawStockPartType('')).toBe(false);
    expect(isRawStockPartType('sheet')).toBe(false);
  });

  it('stays a strict subset of the material-supply catalog', () => {
    // If raw stock ever admitted a type the materials catalog does not serve,
    // the pickers would default to a tier `/materials` can never return.
    RAW_STOCK_PART_TYPES.forEach(type => expect(MATERIAL_SUPPLY_PART_TYPES).toContain(type));
  });
});

/**
 * The exclusion tier. `isProductionPartType` is the question the pickers ask
 * before offering anything at all: a part the shop PRODUCES is the output of a
 * job, never an input to one, so tying it as material and depleting it from
 * stock at completion is not a preference.
 */
describe('production parts are excluded, unknown types are not', () => {
  it('names exactly the two engineering types', () => {
    expect(isProductionPartType('manufactured')).toBe(true);
    expect(isProductionPartType('assembly')).toBe(true);

    expect(isProductionPartType('raw_material')).toBe(false);
    expect(isProductionPartType('purchased')).toBe(false);
    expect(isProductionPartType('hardware')).toBe(false);
    expect(isProductionPartType('consumable')).toBe(false);
  });

  it('reads a missing part_type as NOT a production part', () => {
    // THE compatibility fallback, and the one assertion here that would be a
    // bug if it were inverted. `MaterialAllocation.part_type` is absent entirely
    // from a server older than the field. Reading absent as "suspect" would
    // silently untie every nest on a work order the moment the client ran ahead
    // of the API — a far worse failure than the one the exclusion prevents.
    expect(isProductionPartType(null)).toBe(false);
    expect(isProductionPartType(undefined)).toBe(false);
    expect(isProductionPartType('')).toBe(false);
    expect(isProductionPartType('some_future_type')).toBe(false);
  });

  it('agrees with isEngineeringPartType on every catalog type', () => {
    // They are the same predicate under two names — one asks "which catalog",
    // the other "may this be consumed". A divergence would mean a part the
    // Parts catalog owns is tiable as material somewhere.
    [...ENGINEERING_PART_TYPES, ...MATERIAL_SUPPLY_PART_TYPES].forEach(type => {
      expect(isProductionPartType(type)).toBe(isEngineeringPartType(type));
    });
  });
});

/**
 * The one tiering rule the three material-tie pickers share.
 *
 * It was written three times — in `SheetPartPicker`, `LaserNestManualModal` and
 * `OperationMaterialTieModal` — with three different pin rules, which is how a
 * hidden count and a pinned selection can silently disagree between two screens
 * that are supposed to behave identically. These assertions are the contract
 * all three now depend on.
 */
describe('partitionMaterialTiers', () => {
  const RAW_SHEET = { id: 1, part_type: 'raw_material' };
  const RAW_BAR = { id: 2, part_type: 'raw_material' };
  const PURCHASED_SHEET = { id: 3, part_type: 'purchased' };
  const SCREW = { id: 4, part_type: 'hardware' };
  const BRACKET = { id: 5, part_type: 'manufactured' };
  const WELDMENT = { id: 6, part_type: 'assembly' };
  const UNKNOWN = { id: 7, part_type: null };

  const CATALOG = [RAW_SHEET, RAW_BAR, PURCHASED_SHEET, SCREW, BRACKET, WELDMENT, UNKNOWN];

  const ids = (parts: Array<{ id: number }>) => parts.map(part => part.id);

  it('defaults to raw stock and puts everything else behind the toggle', () => {
    const { defaultTier, hiddenTier, hiddenCount } = partitionMaterialTiers(CATALOG);

    expect(ids(defaultTier)).toEqual([1, 2]);
    // An unknown type is NOT raw stock, so it lands behind the hatch — hidden,
    // never excluded.
    expect(ids(hiddenTier)).toEqual([3, 4, 7]);
    expect(hiddenCount).toBe(3);
  });

  it('excludes a part the shop PRODUCES from BOTH tiers, with no escape hatch', () => {
    const narrowed = partitionMaterialTiers(CATALOG);
    const widened = partitionMaterialTiers(CATALOG, { showAll: true });

    [narrowed, widened].forEach(({ defaultTier, hiddenTier }) => {
      expect(ids([...defaultTier, ...hiddenTier])).not.toContain(BRACKET.id);
      expect(ids([...defaultTier, ...hiddenTier])).not.toContain(WELDMENT.id);
    });
    // …and it is not counted as something the toggle would reveal either.
    expect(narrowed.hiddenCount).toBe(3);
  });

  it('pins a hidden-tier selection so narrowing cannot blank it', () => {
    const { pinned, hiddenCount } = partitionMaterialTiers(CATALOG, { pinnedIds: [PURCHASED_SHEET.id] });

    expect(ids(pinned)).toEqual([3]);
    // The pinned row is on screen already, so the count must not advertise it.
    expect(hiddenCount).toBe(2);
  });

  it('takes the pin in whatever shape the call site carries it', () => {
    // The three callers pin from a `<select>` string, an allocation's numeric
    // `part_id`, and a draft row that may hold neither. None of them should have
    // to coalesce first, and `''` must never become `NaN` and match nothing.
    const { pinned } = partitionMaterialTiers(CATALOG, {
      pinnedIds: ['3', SCREW.id, '', null, undefined, 'not-a-number'],
    });

    expect(ids(pinned)).toEqual([3, 4]);
  });

  it('never pins a production part back in, whatever the caller passes', () => {
    // The exclusion runs before the pin, so an id naming a produced part cannot
    // resurrect it — the pin is an id list, not an escape hatch.
    const { pinned } = partitionMaterialTiers(CATALOG, { pinnedIds: [BRACKET.id, WELDMENT.id] });

    expect(pinned).toEqual([]);
  });

  it('reports nothing pinned and nothing hidden once the hatch is open', () => {
    // With the whole hidden tier on screen the caller renders it directly, so a
    // pin would duplicate a row and a non-zero count would be a lie.
    const { pinned, hiddenCount } = partitionMaterialTiers(CATALOG, {
      showAll: true,
      pinnedIds: [PURCHASED_SHEET.id],
    });

    expect(pinned).toEqual([]);
    expect(hiddenCount).toBe(0);
  });

  it('does not count a row the caller renders from somewhere else', () => {
    // A server shortlist pinned above the catalog, or a synthetic row for a tie
    // the material load never returned: already on screen, so the toggle would
    // not reveal them.
    const { hiddenCount } = partitionMaterialTiers(CATALOG, {
      alsoShownIds: [String(PURCHASED_SHEET.id), UNKNOWN.id],
    });

    expect(hiddenCount).toBe(1);
  });

  it('narrows only the DEFAULT tier with defaultTierAlso — it is not an exclusion', () => {
    // The sheet picker's extra filter: bar is raw stock but not something a nest
    // is cut from, so it drops to the hidden tier where the toggle still reveals
    // it. Nothing becomes unreachable.
    const { defaultTier, hiddenTier, hiddenCount } = partitionMaterialTiers(CATALOG, {
      defaultTierAlso: part => part.id !== RAW_BAR.id,
    });

    expect(ids(defaultTier)).toEqual([1]);
    expect(ids(hiddenTier)).toEqual([2, 3, 4, 7]);
    expect(hiddenCount).toBe(4);
  });

  it('preserves the caller’s order and never mutates the input', () => {
    // Catalog order is the caller's — `/materials` returns it sorted the way the
    // picker means to render it, and a helper that re-ordered rows would shuffle
    // the list under the planner.
    const shuffled = [SCREW, RAW_BAR, BRACKET, UNKNOWN, RAW_SHEET, PURCHASED_SHEET];
    const input = [...shuffled];
    const { defaultTier, hiddenTier, pinned } = partitionMaterialTiers(input, {
      pinnedIds: [UNKNOWN.id, SCREW.id],
    });

    expect(input).toEqual(shuffled);
    expect(ids(defaultTier)).toEqual([2, 1]);
    expect(ids(hiddenTier)).toEqual([4, 7, 3]);
    expect(ids(pinned)).toEqual([4, 7]);
  });

  it('handles an empty catalog without inventing a count', () => {
    // A failed material load must show nothing AND offer no toggle — a "(0
    // more)" button is worse than no button.
    expect(partitionMaterialTiers([], { pinnedIds: [1] })).toEqual({
      defaultTier: [],
      hiddenTier: [],
      pinned: [],
      hiddenCount: 0,
    });
  });
});
