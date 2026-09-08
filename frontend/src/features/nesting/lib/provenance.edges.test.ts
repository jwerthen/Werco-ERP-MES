import { importDXFBatch } from './dxf-batch';
import { bounds } from './nesting';
import { geometryHash } from './provenance';
import { createBlankQuote, quoteFromFile, quoteToFile } from './quoting';

function drawing(units: 1 | 4, width: number, height: number) {
  return [
    0,
    'SECTION',
    2,
    'HEADER',
    9,
    '$INSUNITS',
    70,
    units,
    0,
    'ENDSEC',
    0,
    'SECTION',
    2,
    'ENTITIES',
    0,
    'LWPOLYLINE',
    90,
    4,
    70,
    1,
    10,
    0,
    20,
    0,
    10,
    width,
    20,
    0,
    10,
    width,
    20,
    height,
    10,
    0,
    20,
    height,
    0,
    'ENDSEC',
    0,
    'EOF',
    '',
  ].join('\n');
}
const file = (name: string, text: string) => ({ name, size: text.length, text: async () => text });
const settings = { units: 'in' as const, yieldControl: async () => undefined };

describe('physical geometry and CAD source identity', () => {
  it('normalizes equivalent metric/imperial drawings without conflating their source bytes or decisions', async () => {
    const files = [file('inch.dxf', drawing(1, 1, 2)), file('metric.dxf', drawing(4, 25.4, 50.8))];
    const { parts, results } = await importDXFBatch(files, [], { ...settings, unitsByFile: ['mm', 'in'] });
    expect(results.every(result => result.status === 'imported')).toBe(true);
    expect(parts).toHaveLength(2);
    expect(parts.map(part => bounds(part.loops[0]))).toEqual([
      expect.objectContaining({ width: 25.4, height: 50.8 }),
      expect.objectContaining({ width: 25.4, height: 50.8 }),
    ]);
    expect(parts[0].provenance!.geometrySha256).toBe(parts[1].provenance!.geometrySha256);
    expect(parts[0].provenance!.sourceSha256).not.toBe(parts[1].provenance!.sourceSha256);
    expect(parts[0].provenance).toMatchObject({ sourceUnits: 'in', resolvedUnits: 'in', unitDecision: 'declared' });
    expect(parts[1].provenance).toMatchObject({ sourceUnits: 'mm', resolvedUnits: 'mm', unitDecision: 'declared' });
    const reordered = await importDXFBatch([...files].reverse(), [], settings);
    expect(reordered.parts.map(part => part.id)).toEqual([...parts].reverse().map(part => part.id));
  });

  it('retains the imported fingerprint when saved geometry is edited, allowing a later mismatch review', async () => {
    const { parts } = await importDXFBatch([file('plate.dxf', drawing(1, 1, 2))], [], settings);
    const original = parts[0].provenance!.geometrySha256;
    const saved = quoteToFile({ ...createBlankQuote(), parts });
    const serialized = JSON.parse(JSON.stringify(saved)) as typeof saved;
    const outer = serialized.parts[0].loops[0];
    if (outer.type !== 'poly') throw new Error('Expected rectangle fixture');
    outer.points[1].x += 0.1;
    const reopened = quoteFromFile(serialized);
    expect(reopened.parts[0].provenance!.geometrySha256).toBe(original);
    expect(await geometryHash(reopened.parts[0])).not.toBe(original);
  });
});
