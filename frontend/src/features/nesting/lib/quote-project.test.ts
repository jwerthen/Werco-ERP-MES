import { rect, type Part } from './nesting';
import {
  addImportedParts,
  createBlankProject,
  materialThicknessKey,
  projectFromFile,
  projectToFile,
  validateProject,
  type QuoteProject,
} from './quote-project';
import { createBlankQuote, quoteToFile } from './quoting';
import { autoQuotingSpacing } from './spacing';
import { inToMm } from './units';

function part(id: string, quantity = 1): Part {
  return { id, name: id, quantity, color: 0, rotate: true, loops: [rect(12, 8)] };
}

function mixedProject(): QuoteProject {
  const first = createBlankQuote();
  first.parts = [part('steel')];
  first.spacingMode = 'manual';
  first.gap = 4;
  first.margin = 10;
  first.options[0].price = 90;
  const second = createBlankQuote();
  second.material = 'Aluminum';
  second.thickness = 6;
  second.parts = [part('aluminum')];
  second.spacingMode = 'auto';
  Object.assign(second, autoQuotingSpacing(second.thickness));
  return {
    name: 'Mixed material job',
    activeGroupId: 'aluminum',
    groups: [
      { id: 'steel', quote: first },
      { id: 'aluminum', quote: second },
    ],
  };
}

describe('material estimate projects', () => {
  it('starts empty and keeps supplied estimates and fresh stock options independent', () => {
    const initial = createBlankQuote();
    initial.parts = [part('initial')];
    initial.parts[0].referencePaths = [
      [
        { x: 1, y: 1 },
        { x: 2, y: 1 },
      ],
    ];
    const project = createBlankProject(initial);
    const fresh = createBlankProject();
    project.groups[0].quote.options[0].price = 123;
    project.groups[0].quote.parts[0].referencePaths![0][0].x = 3;
    expect(initial.options[0].price).toBeNull();
    expect(initial.parts[0].referencePaths![0][0].x).toBe(1);
    expect(fresh.groups[0].quote.parts).toEqual([]);
    expect(fresh.groups[0].quote.options[0].price).toBeNull();
    expect(validateProject(fresh)).toBe(fresh);
  });

  it('round-trips every group, active selection, prices, spacing modes, and contour metadata', () => {
    const project = mixedProject();
    const source = project.groups[0].quote.parts[0];
    source.referencePaths = [
      [
        { x: 1, y: 1 },
        { x: 2, y: 1 },
      ],
    ];
    source.geometryToleranceMm = 0.00254;
    const file = projectToFile(project);
    expect(file).toMatchObject({ version: 15, units: 'in', activeGroupId: 'aluminum' });
    expect(file.groups[0].quote.version).toBe(14);
    const restored = projectFromFile(JSON.parse(JSON.stringify(file)));
    expect(restored.name).toBe(project.name);
    expect(restored.activeGroupId).toBe('aluminum');
    expect(restored.groups.map(group => group.quote.spacingMode)).toEqual(['manual', 'auto']);
    expect(restored.groups[0].quote.options[0].price).toBe(90);
    expect(restored.groups[0].quote.gap).toBeCloseTo(4, 12);
    expect(restored.groups[0].quote.parts[0].referencePaths![0][1].x).toBeCloseTo(2, 12);
    expect(restored.groups[0].quote.parts[0].geometryToleranceMm).toBeCloseTo(0.00254, 12);
    expect(restored.groups[1].quote.thickness).toBeCloseTo(6, 12);
  });

  it('opens a legacy single quote without changing its spacing, prices, or quantity', () => {
    const quote = mixedProject().groups[0].quote;
    delete quote.geometryProfile;
    quote.parts[0].quantity = 8;
    const project = projectFromFile(quoteToFile(quote));
    expect(project.groups).toHaveLength(1);
    expect(project.activeGroupId).toBe(project.groups[0].id);
    expect(project.name).toBe(quote.name);
    expect(project.groups[0].quote.parts[0].quantity).toBe(8);
    expect(project.groups[0].quote.gap).toBeCloseTo(4, 12);
    expect(project.groups[0].quote.options[0].price).toBe(90);
  });

  it.each(['', 'missing'])('rejects invalid active group %s', activeGroupId => {
    expect(() => validateProject({ ...mixedProject(), activeGroupId })).toThrow(/active material group/);
  });

  it('rejects empty groups and invalid project names', () => {
    expect(() => validateProject({ ...mixedProject(), groups: [] })).toThrow(/1–300/);
    expect(() => validateProject({ ...mixedProject(), name: '' })).toThrow(/name/);
  });

  it('rejects duplicate group IDs, material keys, and part IDs across groups', () => {
    const duplicateGroup = mixedProject();
    duplicateGroup.groups[1].id = duplicateGroup.groups[0].id;
    expect(() => validateProject(duplicateGroup)).toThrow(/Duplicate material group IDs/);
    const duplicateMaterial = mixedProject();
    duplicateMaterial.groups[1].quote.material = duplicateMaterial.groups[0].quote.material;
    duplicateMaterial.groups[1].quote.thickness = duplicateMaterial.groups[0].quote.thickness + 0.0000002;
    expect(() => validateProject(duplicateMaterial)).toThrow(/Duplicate material and thickness/);
    const duplicatePart = mixedProject();
    duplicatePart.groups[1].quote.parts[0].id = duplicatePart.groups[0].quote.parts[0].id;
    expect(() => validateProject(duplicatePart)).toThrow(/Duplicate part IDs/);
  });

  it('limits total designs across otherwise separate groups', () => {
    const project = mixedProject();
    project.groups[0].quote.parts = Array.from({ length: 150 }, (_, index) => part(`a-${index}`));
    project.groups[1].quote.parts = Array.from({ length: 151 }, (_, index) => part(`b-${index}`));
    expect(() => validateProject(project)).toThrow(/300 part designs across all/);
  });

  it('limits total instances across groups and rejects fractional quantities', () => {
    const project = mixedProject();
    project.groups.forEach(group => {
      group.quote.parts[0].quantity = 151;
    });
    expect(() => validateProject(project)).toThrow(/300 total parts across all/);
    project.groups[0].quote.parts[0].quantity = 1.5;
    expect(() => validateProject(project)).toThrow(/whole-number/);
  });

  it('counts reference geometry in the global 20,000-point budget before contour checks', () => {
    const project = mixedProject();
    project.groups.forEach(group => {
      group.quote.parts[0].referencePaths = Array.from({ length: 5 }, () =>
        Array.from({ length: 2000 }, (_, index) => ({ x: index / 2000, y: 1 }))
      );
    });
    expect(() => validateProject(project)).toThrow(/20,000 geometry vertices across all/);
  });

  it('rejects mismatched file units, currency, and nested estimate versions', () => {
    const file = projectToFile(mixedProject());
    expect(() => projectFromFile({ ...file, units: 'mm' })).toThrow(/declare inches/);
    expect(() => projectFromFile({ ...file, currency: 'EUR' })).toThrow(/USD/);
    expect(() => projectFromFile({ ...file, groups: [{ ...file.groups[0], quote: { version: 2 } }] })).toThrow(
      /version 3/
    );
  });

  it('checks saved project budgets before individual quote geometry', () => {
    const file = projectToFile(mixedProject());
    file.groups.forEach(group => {
      group.quote.parts[0].quantity = 151;
      group.quote.parts[0].loops = [];
    });
    expect(() => projectFromFile(file)).toThrow(/300 total parts across all/);
  });
});

describe('assigning imported parts to stock groups', () => {
  it('merges only matching material/thickness while preserving manual allowances and prices', () => {
    const project = mixedProject();
    const before = JSON.stringify(project);
    const imported = [part('new-steel', 2), part('thick-steel'), part('new-aluminum')];
    const next = addImportedParts(
      project,
      [
        { material: 'Carbon steel', thickness: inToMm(0.125) + 0.0000002, partIds: ['new-steel'] },
        { material: 'Carbon steel', thickness: 6, partIds: ['thick-steel'] },
        { material: 'Aluminum', thickness: 6, partIds: ['new-aluminum'] },
      ],
      imported
    );
    expect(JSON.stringify(project)).toBe(before);
    expect(next.groups).toHaveLength(3);
    expect(next.activeGroupId).toBe('steel');
    expect(next.groups[0].quote.parts.map(item => item.id)).toEqual(['steel', 'new-steel']);
    expect(next.groups[0].quote).toMatchObject({ spacingMode: 'manual', gap: 4, margin: 10 });
    expect(next.groups[0].quote.options[0].price).toBe(90);
    expect(next.groups[1].quote.parts.map(item => item.id)).toEqual(['aluminum', 'new-aluminum']);
    expect(next.groups[2].quote).toMatchObject({
      material: 'Carbon steel',
      thickness: 6,
      spacingMode: 'auto',
      gap: 6,
      margin: 12,
    });
    expect(next.groups[2].quote.options.every(option => option.price === null)).toBe(true);
    next.groups[2].quote.options[0].width = 999;
    expect(project.groups[1].quote.options[0].width).not.toBe(999);
  });

  it('merges multiple assignment rows for the same new stock specification', () => {
    const next = addImportedParts(
      createBlankProject(),
      [
        { material: 'Stainless steel', thickness: 4, partIds: ['one'] },
        { material: 'Stainless steel', thickness: 4.0000001, partIds: ['two'] },
      ],
      [part('one'), part('two')]
    );
    expect(next.groups).toHaveLength(2);
    expect(next.groups[1].quote.parts).toHaveLength(2);
    expect(next.activeGroupId).toBe(next.groups[1].id);
  });

  it('rejects missing, repeated, unknown, or duplicate imported IDs atomically', () => {
    const project = createBlankProject();
    const before = JSON.stringify(project);
    const row = { material: 'Carbon steel', thickness: 4, partIds: ['one'] };
    expect(() => addImportedParts(project, [], [part('one')])).toThrow(/every imported part/);
    expect(() => addImportedParts(project, [row, row], [part('one')])).toThrow(/exactly one/);
    expect(() => addImportedParts(project, [{ ...row, partIds: ['missing'] }], [part('one')])).toThrow(/unknown/);
    expect(() => addImportedParts(project, [row], [part('one'), part('one')])).toThrow(/Duplicate imported/);
    expect(JSON.stringify(project)).toBe(before);
  });

  it('rejects IDs already in another group and over-budget imported quantities', () => {
    const project = mixedProject();
    const row = { material: 'Carbon steel', thickness: 4, partIds: ['aluminum'] };
    expect(() => addImportedParts(project, [row], [part('aluminum')])).toThrow(/Duplicate imported/);
    expect(() => addImportedParts(project, [{ ...row, partIds: ['large'] }], [part('large', 300)])).toThrow(
      /300 total parts/
    );
    expect(project.groups).toHaveLength(2);
  });

  it('leaves an empty batch unchanged and normalizes grouping keys without merging materials', () => {
    const project = createBlankProject();
    expect(addImportedParts(project, [], [])).toBe(project);
    expect(materialThicknessKey('Carbon steel', 6.0000001)).toBe(materialThicknessKey('Carbon steel', 6));
    expect(materialThicknessKey('Carbon steel', 6)).not.toBe(materialThicknessKey('Aluminum', 6));
    expect(() => materialThicknessKey('Unknown', 6)).toThrow(/supported material/);
    expect(() => materialThicknessKey('Carbon steel', NaN)).toThrow(/thickness/);
  });
});
