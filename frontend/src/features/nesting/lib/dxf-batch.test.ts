import { importDXFBatch, type DXFFile } from './dxf-batch';
import { bounds, rect } from './nesting';
import { compareSheets, demoQuote, quoteFromFile, quoteToFile, validateQuote } from './quoting';

const smallPlate = [
  '0',
  'SECTION',
  '2',
  'ENTITIES',
  '0',
  'LWPOLYLINE',
  '90',
  '4',
  '70',
  '1',
  '10',
  '0',
  '20',
  '0',
  '10',
  '10',
  '20',
  '0',
  '10',
  '10',
  '20',
  '10',
  '10',
  '0',
  '20',
  '10',
  '0',
  'ENDSEC',
  '0',
  'EOF',
  '',
].join('\n');
const file = (name: string, content = smallPlate): DXFFile => ({
  name,
  size: content.length,
  text: jest.fn().mockResolvedValue(content),
});
const options = { units: 'mm' as const, yieldControl: async () => undefined };
const linePlate = [
  0,
  'SECTION',
  2,
  'ENTITIES',
  0,
  'LINE',
  10,
  10,
  20,
  10,
  11,
  10,
  21,
  0,
  0,
  'LINE',
  10,
  0,
  20,
  0,
  11,
  0,
  21,
  10,
  0,
  'LINE',
  10,
  0,
  20,
  0,
  11,
  10,
  21,
  0,
  0,
  'LINE',
  10,
  0,
  20,
  10,
  11,
  10,
  21,
  10,
  0,
  'ENDSEC',
  0,
  'EOF',
  '',
].join('\n');
const openDrawing = [
  0,
  'SECTION',
  2,
  'ENTITIES',
  0,
  'LINE',
  10,
  -5,
  20,
  -2,
  11,
  10,
  21,
  8,
  0,
  'ENDSEC',
  0,
  'EOF',
  '',
].join('\n');

describe('DXF batch boundaries in the native ERP feature', () => {
  it('imports 100 LINE drawings and keeps all quantities, dimensions, and comparisons after save/reopen', async () => {
    const progress = jest.fn();
    const files = Array.from({ length: 100 }, (_, i) => file(`line-plate-${i + 1}.dxf`, linePlate));
    const imported = await importDXFBatch(files, demoQuote.parts, { ...options, onProgress: progress });
    expect(imported.parts).toHaveLength(100);
    expect(imported.results).toHaveLength(100);
    expect(imported.results.every(result => result.status === 'imported' && !result.footprintOnly)).toBe(true);
    expect(files.every(item => jest.mocked(item.text).mock.calls.length === 1)).toBe(true);
    expect(progress).toHaveBeenLastCalledWith({ completed: 100, total: 100, name: 'line-plate-100.dxf' });
    const restored = quoteFromFile(
      JSON.parse(
        JSON.stringify(
          quoteToFile({
            ...demoQuote,
            parts: [...demoQuote.parts, ...imported.parts],
          })
        )
      )
    );
    expect(restored.parts).toHaveLength(104);
    expect(new Set(restored.parts.map(part => part.id)).size).toBe(104);
    for (const part of restored.parts.slice(4)) {
      expect(part.quantity).toBe(1);
      expect(bounds(part.loops[0]).width).toBeCloseTo(10, 10);
      expect(bounds(part.loops[0]).height).toBeCloseTo(10, 10);
    }
    expect(
      compareSheets(restored).results.every(result => result.complete && result.nest?.placements.length === 128)
    ).toBe(true);
  });

  it('propagates footprint warnings and persists their geometry basis without discarding valid neighboring files', async () => {
    const imported = await importDXFBatch(
      [file('exact.dxf', linePlate), file('open.dxf', openDrawing), file('invalid.dxf', 'invalid')],
      [],
      options
    );
    expect(imported.results.map(result => result.status)).toEqual(['imported', 'imported', 'skipped']);
    expect(imported.results[0].footprintOnly).toBe(false);
    expect(imported.results[1]).toMatchObject({ name: 'open.dxf', designs: 1, footprintOnly: true });
    expect(imported.results[1].warnings?.join(' ')).toMatch(/open|footprint/i);
    expect(imported.parts[1].importMode).toBe('drawing-bounds');
    const restored = quoteFromFile(
      JSON.parse(
        JSON.stringify(
          quoteToFile({
            ...demoQuote,
            parts: imported.parts.map(part => ({ ...part, quantity: 3 })),
          })
        )
      )
    );
    expect(restored.parts[0].importMode).toBeUndefined();
    expect(restored.parts[1].importMode).toBe('drawing-bounds');
    expect(bounds(restored.parts[1].loops[0]).x).toBe(0);
    expect(bounds(restored.parts[1].loops[0]).y).toBe(0);
    expect(bounds(restored.parts[1].loops[0]).width).toBeCloseTo(15, 12);
    expect(bounds(restored.parts[1].loops[0]).height).toBeCloseTo(10, 12);
    expect(
      compareSheets(restored).results.every(result => result.complete && result.nest?.placements.length === 6)
    ).toBe(true);
  });

  it('counts existing quantities, imports only complete files that fit, and keeps the saved estimate nestable at 300 instances', async () => {
    const existing = [{ ...demoQuote.parts[0], loops: [rect(10, 10)], quantity: 299 }];
    const second = file('overflow.dxf');
    const imported = await importDXFBatch([file('fits.dxf'), second], existing, options);
    expect(imported.results.map(result => result.status)).toEqual(['imported', 'skipped']);
    expect(imported.results[1].message).toMatch(/300 total parts/);
    expect(second.text).not.toHaveBeenCalled();
    expect(existing[0].quantity).toBe(299);
    const quote = { ...demoQuote, parts: [...existing, ...imported.parts] };
    const restored = quoteFromFile(JSON.parse(JSON.stringify(quoteToFile(quote))));
    expect(
      compareSheets(restored).results.every(result => result.complete && result.nest?.placements.length === 300)
    ).toBe(true);
    expect(() =>
      validateQuote({ ...restored, parts: [...restored.parts, { ...restored.parts[1], id: 'extra' }] })
    ).toThrow(/300 total parts/);
  });

  it('reports failures per filename while retaining valid files on both sides of an invalid drawing', async () => {
    const imported = await importDXFBatch(
      [file('duplicate.dxf'), file('broken.dxf', 'invalid'), file('wrong-extension.txt'), file('duplicate.dxf')],
      [],
      options
    );
    expect(imported.results.map(result => [result.name, result.status])).toEqual([
      ['duplicate.dxf', 'imported'],
      ['broken.dxf', 'skipped'],
      ['wrong-extension.txt', 'skipped'],
      ['duplicate.dxf', 'imported'],
    ]);
    expect(imported.parts).toHaveLength(2);
    expect(imported.parts[0].id).not.toBe(imported.parts[1].id);
    expect(imported.results[1].message).toBeTruthy();
  });

  it('keeps completed imports when cancelled and never reads remaining files', async () => {
    const controller = new AbortController();
    const files = [file('first.dxf'), file('second.dxf'), file('third.dxf')];
    const imported = await importDXFBatch(files, [], {
      ...options,
      signal: controller.signal,
      onProgress: ({ completed }) => {
        if (completed === 1) controller.abort();
      },
    });
    expect(imported.cancelled).toBe(true);
    expect(imported.parts).toHaveLength(1);
    expect(imported.results.map(result => result.status)).toEqual(['imported', 'skipped', 'skipped']);
    expect(files[1].text).not.toHaveBeenCalled();
    expect(files[2].text).not.toHaveBeenCalled();
    expect(imported.results[2].message).toMatch(/cancelled/);
  });
});
