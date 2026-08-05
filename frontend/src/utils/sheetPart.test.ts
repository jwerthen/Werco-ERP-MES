/**
 * sheetPart — recognizing flat stock and reading its dimensions out of the
 * shop's part-numbering convention.
 *
 * Both functions are heuristics over free text, so what matters is not only
 * that they read the shop's real sheet part numbers, but that they FAIL CLOSED
 * on everything else in the material catalog. The wizard stamps
 * `deriveSheetSpec`'s answer onto the thickness field an operator loads a
 * machine from, so a confident wrong read is worse than no read — which is why
 * the negative cases below (angle, tube, beam, bar, pipe, hardware) carry as
 * much weight here as the positive ones.
 *
 * The two are also tested TOGETHER, because the safety property is a property
 * of the pair: `deriveSheetSpec` on its own can still parse a triple out of a
 * name that happens to start with one, and what keeps that off a nest row is
 * that the wizard only pulls a spec through for a part `isSheetLikePart`
 * accepts.
 */

import { deriveSheetSpec, isSheetLikePart, SheetPartLike } from './sheetPart';

const NO_SPEC = { thickness: null, sheetSize: null };

describe('deriveSheetSpec — the shop’s sheet numbering', () => {
  it('reads a decimal thickness with a dash separator', () => {
    expect(deriveSheetSpec({ part_number: '0.188-72X144-A36' })).toEqual({
      thickness: '0.188',
      sheetSize: '72x144',
    });
  });

  it('reads a decimal thickness with an X separator', () => {
    expect(deriveSheetSpec({ part_number: '0.06X60X144-304SS' })).toEqual({
      thickness: '0.06',
      sheetSize: '60x144',
    });
  });

  it('reads the gauge form and labels it as gauge, not as a decimal', () => {
    expect(deriveSheetSpec({ part_number: '10GA-72X120-CS' })).toEqual({
      thickness: '10 ga',
      sheetSize: '72x120',
    });
  });

  it('reads a spaced gauge form', () => {
    expect(deriveSheetSpec({ part_number: '16 GA X 60 X 144' })).toEqual({
      thickness: '16 ga',
      sheetSize: '60x144',
    });
  });

  it('is case-insensitive', () => {
    expect(deriveSheetSpec({ part_number: '0.188-72x144-a36' })).toEqual({
      thickness: '0.188',
      sheetSize: '72x144',
    });
    expect(deriveSheetSpec({ part_number: '10ga-72x120-cs' })).toEqual({
      thickness: '10 ga',
      sheetSize: '72x120',
    });
  });

  it('strips inch marks — straight and curly — and the THK noise word', () => {
    // THK has to go BEFORE the dimension match, or it breaks the thickness away
    // from the two dimensions that follow it.
    expect(deriveSheetSpec({ part_number: '0.188" THK x 60 x 120' })).toEqual({
      thickness: '0.188',
      sheetSize: '60x120',
    });
    // Excel round-trips turn the inch mark into a curly quote.
    expect(deriveSheetSpec({ part_number: '0.250” x 48 x 96' })).toEqual({
      thickness: '0.250',
      sheetSize: '48x96',
    });
    expect(deriveSheetSpec({ part_number: '0.125 inches x 48 x 96' })).toEqual({
      thickness: '0.125',
      sheetSize: '48x96',
    });
  });

  it('returns the thickness verbatim — never normalized or rounded', () => {
    // These land in a free-text field an operator compares against a rack tag.
    expect(deriveSheetSpec({ part_number: '0.250-48X96-A36' }).thickness).toBe('0.250');
    expect(deriveSheetSpec({ part_number: '.125-48X96-A36' }).thickness).toBe('.125');
  });

  it('emits the sheet size in the same shape the extractor already uses', () => {
    // `96x48` is what the AI read of a nest report produces, so a pulled-through
    // value has to be comparable to it at a glance.
    expect(deriveSheetSpec({ part_number: '0.188-72X144-A36' }).sheetSize).toBe('72x144');
  });

  it('ignores trailing grade / supplier noise after the dimensions', () => {
    expect(deriveSheetSpec({ part_number: '0.1875-60X120-A572-50-RYERSON' })).toEqual({
      thickness: '0.1875',
      sheetSize: '60x120',
    });
  });
});

describe('deriveSheetSpec — where it deliberately gives no answer', () => {
  it.each([
    ['angle', 'ANG-A36-1.5X1.5X.25'],
    ['tube', 'TB-1.5X1.5X.188'],
    ['beam', 'BM-W6X15-A992-20FT'],
    ['bar', 'BAR-A36-.5-20FT'],
    ['pipe', 'PI-4XSCH5X20'],
    ['hardware', 'HW-001'],
  ])('reads nothing off a %s part number', (_kind, part_number) => {
    // The grammar is ANCHORED at the start of the string. Unanchored, every one
    // of these contains something that looks like a triple.
    expect(deriveSheetSpec({ part_number })).toEqual(NO_SPEC);
  });

  it('requires an X between width and length, not another dash', () => {
    expect(deriveSheetSpec({ part_number: '0.188-72-144-A36' })).toEqual(NO_SPEC);
  });

  it('reads nothing from a thickness with no two dimensions after it', () => {
    expect(deriveSheetSpec({ part_number: '0.188-A36' })).toEqual(NO_SPEC);
    expect(deriveSheetSpec({ part_number: '0.188-72-A36' })).toEqual(NO_SPEC);
  });

  it('handles null, undefined and blank input without throwing', () => {
    expect(deriveSheetSpec(null)).toEqual(NO_SPEC);
    expect(deriveSheetSpec(undefined)).toEqual(NO_SPEC);
    expect(deriveSheetSpec({})).toEqual(NO_SPEC);
    expect(deriveSheetSpec({ part_number: '', name: '', description: '' })).toEqual(NO_SPEC);
    expect(deriveSheetSpec({ part_number: null, name: null })).toEqual(NO_SPEC);
    expect(deriveSheetSpec({ part_number: '   ' })).toEqual(NO_SPEC);
  });

  it('never reads a spec out of the description, however it is worded', () => {
    // Only the part number and the name are consulted; the description is a
    // sheet-likeness signal only.
    expect(
      deriveSheetSpec({
        part_number: 'SHT-A36-STD',
        name: 'A36 sheet',
        description: '0.188 x 72 x 144 hot rolled',
      })
    ).toEqual(NO_SPEC);
  });
});

describe('deriveSheetSpec — part number wins, name is the fallback', () => {
  it('prefers the part number when both state a spec', () => {
    // The catalog contains at least one part whose number says 144 and whose
    // name says 120. The number is keyed once and reused; the name drifts.
    expect(
      deriveSheetSpec({ part_number: '0.188-72X144-A36', name: '0.188 x 72 x 120 A36 plate' })
    ).toEqual({ thickness: '0.188', sheetSize: '72x144' });
  });

  it('falls back to the name when the part number is off-convention', () => {
    expect(deriveSheetSpec({ part_number: 'SHT-A36-0188', name: '0.188 x 72 x 144 A36 plate' })).toEqual({
      thickness: '0.188',
      sheetSize: '72x144',
    });
  });

  it('applies the SAME anchored grammar to the name, not a looser one', () => {
    // A description-style name that mentions its dimensions mid-sentence is not
    // a spec — this is the shape every angle and tube in the catalog has.
    expect(deriveSheetSpec({ part_number: 'ANG-A36-1.5X1.5X.25', name: 'A36 Angle 1.5 x 1.5 x .25' })).toEqual(
      NO_SPEC
    );
  });
});

describe('isSheetLikePart', () => {
  it('accepts a part whose NUMBER parses as an anchored triple', () => {
    expect(isSheetLikePart({ part_number: '0.188-72X144-A36', name: 'HR A36' })).toBe(true);
    expect(isSheetLikePart({ part_number: '10GA-72X120-CS', name: 'Cold rolled' })).toBe(true);
    expect(isSheetLikePart({ part_number: '0.06X60X144-304SS', name: '304 stainless' })).toBe(true);
  });

  it.each(['sheet', 'sheets', 'plate', 'plates', 'coil'])('accepts a part whose text says "%s"', (word) => {
    expect(isSheetLikePart({ part_number: 'MISC-1', name: `A36 ${word} stock` })).toBe(true);
  });

  it('reads the word out of the part number or the description too', () => {
    expect(isSheetLikePart({ part_number: 'PLATE-A36-STD', name: 'A36' })).toBe(true);
    expect(isSheetLikePart({ part_number: 'MISC-2', name: 'A36', description: 'Hot rolled plate, mill finish' })).toBe(
      true
    );
  });

  it('is case-insensitive about the keyword', () => {
    expect(isSheetLikePart({ part_number: 'MISC-3', name: 'A36 SHEET' })).toBe(true);
    expect(isSheetLikePart({ part_number: 'MISC-4', name: 'a36 Plate' })).toBe(true);
  });

  it.each([
    ['angle', 'ANG-A36-1.5X1.5X.25', 'A36 Angle 1.5 x 1.5 x .25'],
    ['tube', 'TB-1.5X1.5X.188', 'A500 Square Tube 1.5 x 1.5 x .188'],
    ['beam', 'BM-W6X15-A992-20FT', 'A992 Wide Flange Beam W6x15 x 20ft'],
    ['bar', 'BAR-A36-.5-20FT', 'A36 Round Bar .5 x 20ft'],
    ['pipe', 'PI-4XSCH5X20', 'Pipe 4in Sch 5 x 20ft'],
    ['hardware', 'HW-001', '3/8-16 x 1 Hex Bolt Zinc'],
  ])('rejects %s — the stock the picker exists to hide', (_kind, part_number, name) => {
    expect(isSheetLikePart({ part_number, name })).toBe(false);
  });

  it('requires a whole word, so a fabricated part is not mistaken for stock', () => {
    expect(isSheetLikePart({ part_number: 'BRK-100', name: 'Sheetmetal bracket' })).toBe(false);
    expect(isSheetLikePart({ part_number: 'TPL-1', name: 'Drill template' })).toBe(false);
  });

  it('handles null, undefined and blank input without throwing', () => {
    expect(isSheetLikePart(null)).toBe(false);
    expect(isSheetLikePart(undefined)).toBe(false);
    expect(isSheetLikePart({})).toBe(false);
    expect(isSheetLikePart({ part_number: '', name: '', description: '' })).toBe(false);
    expect(isSheetLikePart({ part_number: null, name: null, description: null })).toBe(false);
  });

  it('excludes nothing by keyword — recognition is only ever a POSITIVE test', () => {
    // "Plate" wins even next to a word a veto list would have blacklisted: a
    // veto list silently drops the stock it forgot to name, which is the failure
    // that hides a real sheet from the planner who needs it.
    expect(isSheetLikePart({ part_number: 'MISC-5', name: 'Tube-laser drop plate' })).toBe(true);
  });
});

describe('the pair, as the wizard composes them', () => {
  /** What the wizard writes onto a nest row: a spec, but only for sheet stock. */
  const pullThrough = (part: SheetPartLike) => (isSheetLikePart(part) ? deriveSheetSpec(part) : NO_SPEC);

  it('stamps a sheet part’s real dimensions', () => {
    expect(pullThrough({ part_number: '0.188-72X144-A36', name: 'A36 HR plate' })).toEqual({
      thickness: '0.188',
      sheetSize: '72x144',
    });
  });

  it('stamps NOTHING for non-sheet stock, even when the grammar would match', () => {
    // The one case the anchored grammar can still mis-read: a name that HAPPENS
    // to lead with its dimensions. `deriveSheetSpec` alone parses it; the
    // sheet-likeness gate is what keeps angle-iron dimensions off a nest row.
    const angle: SheetPartLike = {
      part_number: 'ANG-A36-1.5X1.5X.25',
      name: '1.50" x 1.50" x 0.250" THK A36 Angle',
    };
    expect(deriveSheetSpec(angle)).toEqual({ thickness: '1.50', sheetSize: '1.50x0.250' });
    expect(isSheetLikePart(angle)).toBe(false);
    expect(pullThrough(angle)).toEqual(NO_SPEC);
  });

  it('stamps nothing for a sheet whose number states no dimensions', () => {
    // Recognized as flat stock (so it stays in the picker) but nothing to pull
    // through — the extractor's read of the nest report stands.
    const part: SheetPartLike = { part_number: 'SHT-304-125', name: '304 SS 0.125 Sheet' };
    expect(isSheetLikePart(part)).toBe(true);
    expect(pullThrough(part)).toEqual(NO_SPEC);
  });
});
