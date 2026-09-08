import { canonicalJSON, dxfUnitDecision, geometryHash, sha256, validateProvenance } from './provenance';
import { importDXFBatch } from './dxf-batch';
import { rect, type Loop } from './nesting';
import { createBlankQuote, quoteFromFile, quoteToFile } from './quoting';

const drawing = (header = '') =>
  `${header}0\nSECTION\n2\nENTITIES\n0\nCIRCLE\n10\n0\n20\n0\n40\n1\n0\nENDSEC\n0\nEOF\n`;
const unitHeader = (value: number) => `0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n${value}\n0\nENDSEC\n`;
const file = (text: string) => ({ name: 'part.dxf', size: text.length, text: async () => text });
const options = { units: 'in' as const, yieldControl: async () => undefined };

describe('CAD input fingerprints', () => {
  it('uses standard SHA-256 and stable canonical JSON', async () => {
    expect(await sha256('abc')).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(canonicalJSON({ z: 2, a: [true, -0, null] })).toBe('{"a":[true,0,null],"z":2}');
    expect(() => canonicalJSON({ x: NaN })).toThrow();
    expect(() => canonicalJSON({ x: undefined })).toThrow();
  });

  it('normalizes translation, loop direction/start and hole order without erasing handedness', async () => {
    const outer: Loop = {
      type: 'poly',
      points: [
        { x: 0, y: 0 },
        { x: 10, y: 0 },
        { x: 7, y: 8 },
        { x: 0, y: 5 },
      ],
    };
    const holes: Loop[] = [
      { type: 'circle', cx: 2, cy: 2, r: 0.2 },
      { type: 'circle', cx: 4, cy: 2, r: 0.3 },
    ];
    const shifted: Loop = {
      type: 'poly',
      points: [...outer.points.slice(2), ...outer.points.slice(0, 2)]
        .reverse()
        .map(p => ({ x: p.x + 30, y: p.y - 12 })),
    };
    const shiftedHoles = [...holes].reverse().map(loop => {
      if (loop.type !== 'circle') throw new Error('Fixture');
      return { ...loop, cx: loop.cx + 30, cy: loop.cy - 12 };
    });
    expect(await geometryHash({ loops: [outer, ...holes] })).toBe(
      await geometryHash({ loops: [shifted, ...shiftedHoles] })
    );
    expect(await geometryHash({ loops: [outer] })).not.toBe(
      await geometryHash({ loops: [{ type: 'poly', points: outer.points.map(p => ({ x: 10 - p.x, y: p.y })) }] })
    );
    expect(await geometryHash({ loops: [rect(10, 5)] })).not.toBe(await geometryHash({ loops: [rect(10, 5.001)] }));
  });

  it('records header units, explicit fallback and refuses conflicting declarations', () => {
    expect(dxfUnitDecision(drawing(unitHeader(4)), 'in')).toEqual({
      sourceUnits: 'mm',
      resolvedUnits: 'mm',
      unitDecision: 'declared',
    });
    expect(dxfUnitDecision(drawing(), 'in')).toEqual({
      sourceUnits: 'unitless',
      resolvedUnits: 'in',
      unitDecision: 'assigned',
    });
    expect(() => dxfUnitDecision(drawing(unitHeader(1) + unitHeader(4)), 'in')).toThrow('Conflicting');
    expect(() => dxfUnitDecision(drawing(unitHeader(6)), 'in')).toThrow('unsupported');
  });

  it('fingerprints original bytes and preserves their source identity through imperial save/open', async () => {
    const bytes = new TextEncoder().encode('\uFEFF' + drawing(unitHeader(4)));
    const text = jest.fn();
    const imported = await importDXFBatch(
      [{ name: 'part.dxf', size: bytes.length, text, arrayBuffer: async () => bytes.buffer }],
      [],
      options
    );
    expect(text).not.toHaveBeenCalled();
    expect(imported.results[0].status).toBe('imported');
    const part = imported.parts[0];
    expect(part.provenance).toMatchObject({
      sourceHashBasis: 'original-bytes',
      sourceUnits: 'mm',
      resolvedUnits: 'mm',
      unitDecision: 'declared',
    });
    expect(part.provenance!.sourceSha256).toBe(await sha256(bytes.buffer));
    const reopened = quoteFromFile(
      JSON.parse(JSON.stringify(quoteToFile({ ...createBlankQuote(), parts: [{ ...part, revision: 'B' }] })))
    );
    expect(reopened.parts[0].revision).toBe('B');
    expect(reopened.parts[0].provenance).toEqual(part.provenance);
    expect(await geometryHash(reopened.parts[0])).toBe(part.provenance!.geometrySha256);
  });

  it('uses per-file source units, stable import IDs and distinct duplicate identities', async () => {
    const files = [file(drawing()), file(drawing())];
    const first = await importDXFBatch(files, [], { ...options, unitsByFile: ['in', 'mm'] });
    const again = await importDXFBatch(files, [], { ...options, unitsByFile: ['in', 'mm'] });
    expect(first.parts.map(p => p.id)).toEqual(again.parts.map(p => p.id));
    expect(first.parts[0].provenance!.geometrySha256).not.toBe(first.parts[1].provenance!.geometrySha256);
    expect(first.parts[1].provenance).toMatchObject({
      sourceUnits: 'unitless',
      resolvedUnits: 'mm',
      sourceHashBasis: 'utf8-text',
    });
    const duplicate = await importDXFBatch([files[0]], first.parts, options);
    expect(duplicate.parts[0].id).not.toBe(first.parts[0].id);
    expect(duplicate.parts[0].provenance).toEqual(first.parts[0].provenance);
  });

  it('rejects malformed source decisions and actual oversized input despite a forged size', async () => {
    const imported = await importDXFBatch([file(drawing())], [], options);
    const provenance = imported.parts[0].provenance!;
    expect(() => validateProvenance({ ...provenance, sourceSha256: 'bad' })).toThrow();
    expect(() => validateProvenance({ ...provenance, sourceUnits: 'mm', resolvedUnits: 'in' })).toThrow();
    expect(() => validateProvenance({ ...provenance, warnings: new Array(101).fill('warning') })).toThrow();
    const large = await importDXFBatch(
      [{ name: 'large.dxf', size: 20, text: async () => 'x'.repeat(5_000_000) }],
      [],
      options
    );
    expect(large.parts).toHaveLength(0);
    expect(large.results[0].message).toContain('5 MB');
  });
});
