import { importDXFBatch, type DXFFile } from './dxf-batch';
import { rect } from './nesting';
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

describe('DXF batch boundaries in the native ERP feature', () => {
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
