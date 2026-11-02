import { extractDrawing, issues, matchPart } from './model';
import { reviewedPlan, savedPart } from '../../test-utils/hankEngineering';

test('extracts labeled drawing identifiers, component quantities and manufacturing notes with pages', () => {
  const p = reviewedPlan();
  expect(p.number).toBe('TEST-ASM');
  expect(p.revision).toBe('B');
  expect(p.components).toEqual([expect.objectContaining({ pn: 'TEST-DETAIL', quantity: '2', uom: 'each', page: 1 })]);
  expect(p.requirements[0]).toMatchObject({ text: 'DEBURR ALL EDGES', page: 1 });
  expect(issues(p)).toEqual([]);
});
test('does not invent job quantity, make/buy, units, revisions, routing or review', () => {
  const p = extractDrawing('a.pdf', 'x', ['DRAWING NUMBER: D-1\nTITLE: Bracket']);
  expect(p.quantity).toBe('');
  expect(p.revision).toBe('');
  expect(p.parts).toEqual([]);
  expect(p.operations).toEqual([]);
  expect(p.reviewed).toBe(false);
  expect(issues(p)).not.toEqual([]);
});
test('scanned sheets and revision conflicts carry explicit review flags', () => {
  const p = extractDrawing('a.pdf', 'x', ['REV: A', 'REV: B', '']);
  expect(p.flags.join(' ')).toMatch(/revision differs/);
  expect(p.flags.join(' ')).toMatch(/Page 3 has little/);
});
test.each(['ITAR', 'CUI', 'DO NOT TRANSMIT', 'Distribution statement D'])(
  'restriction %s blocks even a checked review',
  marker => {
    const p = reviewedPlan();
    p.pages[0] += `\n${marker}`;
    expect(issues(p).join(' ')).toMatch(/Restriction marking/);
  }
);
test.each(['0', '-1', 'NaN', 'Infinity', ''])('invalid BOM quantity %s is refused', quantity => {
  const p = reviewedPlan();
  p.components[0].quantity = quantity;
  expect(issues(p).join(' ')).toMatch(/positive quantity/);
});
test('raw material uses grade only and retains specification', () => {
  const p = reviewedPlan();
  Object.assign(p.parts[1], { type: 'raw_material', pn: 'ASTM-A36', grade: 'A36' });
  expect(issues(p).join(' ')).toMatch(/grade as SKU/);
});
test('proposals need explicit page evidence and supplied-value review', () => {
  const p = reviewedPlan();
  p.parts[0].confirmed = false;
  p.operations[0].page = 4;
  expect(issues(p).join(' ')).toMatch(/confirm page evidence/);
});
test('unknown labor standards do not become zero; requirements cannot disappear on save', () => {
  const p = reviewedPlan();
  p.operations[0].setup = '';
  p.operations[0].instructions = '';
  expect(issues(p).join(' ')).toMatch(/unknown times/);
  expect(issues(p).join(' ')).toMatch(/Carry each confirmed/);
});
test('same PN different revision/type/unit/name is a conflict, never a new part', () => {
  const p = reviewedPlan();
  const existing = savedPart(p, 0);
  expect(matchPart(p.parts[0], [existing], p)?.id).toBe(1);
  for (const change of [
    { revision: 'C' },
    { is_active: false },
    { unit_of_measure: 'sheets' },
    { name: 'Different' },
    { drawing_number: 'other' },
  ])
    expect(() => matchPart(p.parts[0], [{ ...existing, ...change }], p)).toThrow(/conflicts/);
});
test('never silently picks one of multiple exact PNs', () => {
  const p = reviewedPlan();
  expect(() => matchPart(p.parts[0], [savedPart(p, 0), savedPart(p, 0)], p)).toThrow(/multiple/);
});

test('accepts explicit table columns in another order and preserves source quantity', () => {
  const p = extractDrawing('columns.pdf', 'x', [
    'ITEM\tDESCRIPTION\tQTY\tPART NO\tUOM\n10\tBracket\t2.5\tBR-1\tinches',
  ]);
  expect(p.components).toEqual([
    expect.objectContaining({ pn: 'BR-1', quantity: '2.5', uom: 'inches', confirmed: false }),
  ]);
});
