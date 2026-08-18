import { PartType } from '../types';

export const ENGINEERING_PART_TYPES: PartType[] = ['manufactured', 'assembly'];
export const MATERIAL_SUPPLY_PART_TYPES: PartType[] = ['purchased', 'raw_material', 'hardware', 'consumable'];

/**
 * ---------------------------------------------------------------------------
 * WHY RAW STOCK IS ITS OWN TIER, NARROWER THAN "MATERIAL"
 * ---------------------------------------------------------------------------
 * The catalog splits in two at the top level: `ENGINEERING_PART_TYPES` is the
 * Parts catalog (what the shop PRODUCES) and `MATERIAL_SUPPLY_PART_TYPES` is
 * Materials & Supplies (what the shop BUYS). That split answers "which catalog
 * does this part live in", and it is the right split for a catalog page.
 *
 * It is the WRONG split for a picker that chooses stock a work order CONSUMES.
 * Three of the four material-supply types — `purchased`, `hardware`,
 * `consumable` — are bought COMPONENTS that go into the product, not stock it
 * is made from: the seeded catalog types bolts and nuts as `purchased`
 * (`backend/scripts/seed_data.py`). So "material" in this system effectively
 * means "everything you buy", and offering all of it under a heading like
 * "Sheet part" puts every bolt, nut and abrasive pad in front of a planner
 * tying a laser nest.
 *
 * `RAW_STOCK_PART_TYPES` is the narrower set those pickers DEFAULT to: stock
 * that is consumed and transformed rather than assembled in.
 *
 * It is a DEFAULT, never a restriction. Real sheet stock in this system is
 * genuinely typed `purchased` sometimes — both the BOM importer and the
 * PO-upload path fall back to `purchased` when they cannot classify a row — so
 * a hard raw-material-only picker would strand real sheets with no way to tie
 * them. Every picker that narrows to this set therefore ships a "Show all
 * materials" escape hatch alongside it.
 */
export const RAW_STOCK_PART_TYPES: PartType[] = ['raw_material'];

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

/**
 * THE human label for a part type, across BOTH catalogs — the single source.
 *
 * The two option lists above are pickers — each offers only its own catalog —
 * but a screen that DISPLAYS a type has to name whatever the row actually
 * carries, including a class the form it sits in would never offer (a
 * `purchased` component reached through the BOM drill-through, say). Falls back
 * to the raw value de-underscored rather than rendering a bare enum token or,
 * worse, nothing at all.
 *
 * IT IS DERIVED FROM THE OPTION LISTS, not a third table beside them, because
 * two hand-maintained copies is exactly what this replaced: a `partTypeLabels`
 * record exported from `types/engineering.ts` (never imported by anything) and a
 * byte-identical local one in `pages/BOM.tsx`, both of which rendered
 * `purchased` as "Purchased" while the catalog pickers here said "Purchased
 * COTS" — so one part read two ways depending on which screen you were on.
 * "Purchased COTS" is the label kept: it is what `MATERIAL_SUPPLY_PART_TYPE_OPTIONS`
 * offers when someone SETS the class, and a display that renames the value the
 * picker just wrote is the disagreement, whichever wording it prefers. Add a new
 * type to the option list above and every display of it follows; there is no
 * second place to remember.
 */
export function partTypeLabel(partType: string): string {
  const known = [...ENGINEERING_PART_TYPE_OPTIONS, ...MATERIAL_SUPPLY_PART_TYPE_OPTIONS].find(
    (option) => option.value === partType
  );
  return known?.label ?? partType.replace(/_/g, ' ');
}

export function isEngineeringPartType(partType?: string): boolean {
  return ENGINEERING_PART_TYPES.includes(partType as PartType);
}

export function isMaterialSupplyPartType(partType?: string): boolean {
  return MATERIAL_SUPPLY_PART_TYPES.includes(partType as PartType);
}

/**
 * Is this stock a work order CONSUMES (sheet, plate, bar, tube), as opposed to
 * a bought component it assembles in? See the block above `RAW_STOCK_PART_TYPES`.
 *
 * `null` / `undefined` reads FALSE — an unknown type is not known to be raw
 * stock, so it belongs behind the "Show all materials" escape hatch rather than
 * in the default view.
 */
export function isRawStockPartType(partType?: string | null): boolean {
  return RAW_STOCK_PART_TYPES.includes(partType as PartType);
}

/**
 * Is this a part the shop PRODUCES (manufactured or assembly)?
 *
 * The same predicate as `isEngineeringPartType`, named for the question the
 * material-tie pickers actually ask: such a part must never be tied as material
 * and depleted from stock, because it is the output of a job, not an input to
 * one. Accepts `null` so it can be handed a nullable `part_type` off an API
 * response without a coalesce at every call site — and `null` reads FALSE,
 * which is deliberate: an older server that sends no `part_type` at all must
 * degrade to today's behavior rather than have every tie treated as suspect.
 */
export function isProductionPartType(partType?: string | null): boolean {
  return isEngineeringPartType(partType ?? undefined);
}

/**
 * The minimum a row needs in order to be tiered. Structural rather than `Part`
 * so a test can pass a literal and a caller holding a narrower row type is not
 * forced to widen it.
 */
export interface TierablePart {
  id: number;
  part_type?: string | null;
}

export interface MaterialTierRequest<T extends TierablePart> {
  /** `true` once the planner has flipped the "Show all materials" escape hatch. */
  showAll?: boolean;
  /**
   * Ids that must survive the narrowed view — whatever the picker currently
   * holds, plus anything a live tie already committed to. Accepts the raw shapes
   * the call sites carry (`''` from a select, a `number` off an allocation,
   * `undefined` when there is no tie) so no caller has to coalesce first.
   */
  pinnedIds?: ReadonlyArray<number | string | null | undefined>;
  /**
   * Ids the caller renders from some OTHER source — a server shortlist pinned
   * above the catalog, a synthetic row for a tie the material load never
   * returned. They are already on screen, so the toggle would not reveal them
   * and `hiddenCount` must not count them.
   */
  alsoShownIds?: ReadonlyArray<number | string | null | undefined>;
  /**
   * Extra narrowing ANDed onto raw stock for the DEFAULT tier only. Rows it
   * rejects fall through to `hiddenTier`, where the toggle still reveals them —
   * it is never an exclusion.
   */
  defaultTierAlso?: (part: T) => boolean;
}

export interface MaterialTiers<T extends TierablePart> {
  /** Shown by default: raw stock (further narrowed by `defaultTierAlso`). */
  defaultTier: T[];
  /** Everything else selectable, sitting behind the "Show all materials" toggle. */
  hiddenTier: T[];
  /**
   * `hiddenTier` rows the caller must render anyway because they are pinned.
   * Empty when `showAll` — the whole hidden tier is on screen then.
   */
  pinned: T[];
  /** What the toggle would actually REVEAL. `0` when `showAll`. */
  hiddenCount: number;
}

/** `''` / `null` / `undefined` / non-numeric all mean "no id", never `NaN`. */
function toPartId(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const id = Number(value);
  return Number.isFinite(id) ? id : null;
}

function partIdSet(values: ReadonlyArray<number | string | null | undefined>): Set<number> {
  const ids = new Set<number>();
  for (const value of values) {
    const id = toPartId(value);
    if (id !== null) ids.add(id);
  }
  return ids;
}

const NO_IDS: ReadonlyArray<number | string | null | undefined> = [];

/**
 * ---------------------------------------------------------------------------
 * ONE TIERING RULE FOR EVERY MATERIAL-TIE PICKER
 * ---------------------------------------------------------------------------
 * Three pickers choose the material a tie points at — the import wizard's
 * `<SheetPartPicker>`, the sheet-part select in `<LaserNestManualModal>`, and
 * the material select in `<OperationMaterialTieModal>` — and all three owe the
 * planner the same four things:
 *
 *   1. a part the shop PRODUCES is never offered, at either tier and with no
 *      escape hatch (see `isProductionPartType`);
 *   2. the default view is raw stock;
 *   3. everything else selectable sits behind one "Show all materials (N more)"
 *      toggle;
 *   4. whatever is already selected survives the narrowed view — a picker that
 *      renders blank is how a live tie gets silently dropped, and on a create
 *      it POSTs an untied nest the planner believes is tied.
 *
 * Rules 3 and 4 interact, and that interaction is what was written three times
 * and drifted: `N` has to be what the toggle would actually REVEAL, so a row
 * pinned back in by rule 4 is on screen already and must not be counted. Each
 * picker pins from a different place — the controlled value, the form field
 * plus the tie read off the server, the draft row — so the pin arrives here as
 * a LIST OF IDS and the divergence stops at the call site.
 *
 * `defaultTierAlso` is the one real difference left: the sheet picker narrows
 * its default tier further, to raw stock that is also sheet-like, because bar
 * and tube are raw stock but not something a nest is cut from.
 *
 * This assembles no options. Labels, group headings, ordering, and where a
 * synthetic row for a part the material load never returned goes are all caller
 * policy — and deliberately so: a part EXCLUDED here must not be re-introduced
 * as one of those synthetic rows (see `LaserNestManualModal`).
 */
export function partitionMaterialTiers<T extends TierablePart>(
  parts: readonly T[],
  {
    showAll = false,
    pinnedIds = NO_IDS,
    alsoShownIds = NO_IDS,
    defaultTierAlso,
  }: MaterialTierRequest<T> = {}
): MaterialTiers<T> {
  // Rule 1, applied before anything else so no later step can reach a
  // production part. `/materials` filters to the material-supply types, so it
  // cannot serve one today and this drops nothing — it is here because the
  // input is only typed as parts, and a future caller could hand over a wider
  // list.
  const selectable = parts.filter((part) => !isProductionPartType(part.part_type));
  const inDefaultTier = (part: T) =>
    isRawStockPartType(part.part_type) && (defaultTierAlso ? defaultTierAlso(part) : true);

  const defaultTier = selectable.filter(inDefaultTier);
  const hiddenTier = selectable.filter((part) => !inDefaultTier(part));

  const pinnedSet = partIdSet(pinnedIds);
  const pinned = showAll ? [] : hiddenTier.filter((part) => pinnedSet.has(part.id));

  const alsoShownSet = partIdSet(alsoShownIds);
  const hiddenCount = showAll
    ? 0
    : hiddenTier.filter((part) => !pinnedSet.has(part.id) && !alsoShownSet.has(part.id)).length;

  return { defaultTier, hiddenTier, pinned, hiddenCount };
}
