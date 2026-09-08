import type { Part } from './nesting';
import { createBlankQuote, quoteFromFile, quoteToFile, validateQuote, type Quote } from './quoting';
import { autoQuotingSpacing } from './spacing';
import { inToMm } from './units';

export type QuoteGroup = { id: string; quote: Quote };
export type QuoteProject = { name: string; activeGroupId: string; groups: QuoteGroup[] };
export type ImportedPartAssignment = { material: string; thickness: number; partIds: string[] };

const THICKNESS_KEY_STEP_MM = 1e-6;
const MAX_PARTS = 300;
const MAX_VERTICES = 20_000;
const materials = ['Carbon steel', 'Stainless steel', 'Aluminum'];

function check(ok: unknown, message: string): asserts ok {
  if (!ok) throw new Error(message);
}

/** Group identical stock specifications after rounding thickness to 0.000001 mm. */
export function materialThicknessKey(material: string, thickness: number): string {
  check(materials.includes(material), 'Choose a supported material.');
  check(
    Number.isFinite(thickness) && thickness > 0 && thickness <= 100,
    'Enter a positive thickness, up to 3.937 inches.'
  );
  return JSON.stringify([material, Math.round(thickness / THICKNESS_KEY_STEP_MM)]);
}

function cloneQuote(quote: Quote): Quote {
  return {
    ...quote,
    options: quote.options.map(option => ({ ...option })),
    parts: quote.parts.map(part => ({
      ...part,
      loops: part.loops.map(loop =>
        loop.type === 'circle' ? { ...loop } : { ...loop, points: loop.points.map(point => ({ ...point })) }
      ),
      ...(part.referencePaths && {
        referencePaths: part.referencePaths.map(path => path.map(point => ({ ...point }))),
      }),
    })),
  };
}

export function createBlankProject(initialQuote?: Quote): QuoteProject {
  const quote = initialQuote ? cloneQuote(validateQuote(initialQuote)) : createBlankQuote();
  return validateProject({ name: quote.name, activeGroupId: 'group-1', groups: [{ id: 'group-1', quote }] });
}

/** Validate global budgets before the more expensive per-contour validation. */
function checkProjectStructure(value: unknown): QuoteProject {
  const project = value as QuoteProject;
  check(project && typeof project === 'object', 'Invalid material estimate project.');
  check(
    typeof project.name === 'string' && project.name.length > 0 && project.name.length < 200,
    'Enter an estimate name.'
  );
  check(
    Array.isArray(project.groups) && project.groups.length > 0 && project.groups.length <= MAX_PARTS,
    'An estimate must contain 1–300 material groups.'
  );
  const groupIds = new Set<string>();
  const materialKeys = new Set<string>();
  const partIds = new Set<string>();
  let designs = 0;
  let instances = 0;
  let vertices = 0;
  for (const group of project.groups) {
    check(
      group && typeof group.id === 'string' && group.id.length > 0 && group.id.length < 200,
      'Invalid material group ID.'
    );
    check(!groupIds.has(group.id), 'Duplicate material group IDs.');
    groupIds.add(group.id);
    const quote = group.quote;
    check(quote && typeof quote === 'object' && Array.isArray(quote.parts), 'Invalid material group estimate.');
    const key = materialThicknessKey(quote.material, quote.thickness);
    check(!materialKeys.has(key), 'Duplicate material and thickness groups. Combine their parts into one group.');
    materialKeys.add(key);
    designs += quote.parts.length;
    check(designs <= MAX_PARTS, 'Maximum 300 part designs across all material groups.');
    for (const part of quote.parts) {
      check(part && typeof part.id === 'string' && part.id.length > 0, 'Invalid part ID.');
      check(!partIds.has(part.id), 'Duplicate part IDs across material groups.');
      partIds.add(part.id);
      check(Number.isInteger(part.quantity) && part.quantity > 0, 'Enter a positive whole-number part quantity.');
      instances += part.quantity;
      check(instances <= MAX_PARTS, 'Maximum 300 total parts across all material groups.');
      check(Array.isArray(part.loops), 'Invalid part contours.');
      for (const loop of part.loops) {
        check(loop && (loop.type === 'circle' || loop.type === 'poly'), 'Invalid part contour.');
        if (loop.type === 'poly') check(Array.isArray(loop.points), 'Invalid part contour points.');
        vertices += loop.type === 'circle' ? 1 : loop.points.length;
        check(vertices <= MAX_VERTICES, 'Maximum 20,000 geometry vertices across all material groups.');
      }
      if (part.referencePaths !== undefined) {
        check(Array.isArray(part.referencePaths), 'Invalid reference paths.');
        for (const path of part.referencePaths) {
          check(Array.isArray(path), 'Invalid reference path points.');
          vertices += path.length;
          check(vertices <= MAX_VERTICES, 'Maximum 20,000 geometry vertices across all material groups.');
        }
      }
    }
  }
  check(
    typeof project.activeGroupId === 'string' && groupIds.has(project.activeGroupId),
    'Choose an existing active material group.'
  );
  return project;
}

export function validateProject(value: unknown): QuoteProject {
  const project = checkProjectStructure(value);
  project.groups.forEach(group => validateQuote(group.quote));
  return project;
}

export function projectToFile(project: QuoteProject) {
  validateProject(project);
  return {
    version: 4,
    units: 'in',
    currency: 'USD',
    name: project.name,
    activeGroupId: project.activeGroupId,
    groups: project.groups.map(group => ({ id: group.id, quote: quoteToFile(group.quote) })),
  };
}

export function projectFromFile(input: unknown): QuoteProject {
  check(input && typeof input === 'object', 'Invalid estimate file.');
  const data = input as Record<string, unknown>;
  if (data.version !== 4) return createBlankProject(quoteFromFile(input));
  check(data.units === 'in', 'Estimate project must explicitly declare inches.');
  check(data.currency === undefined || data.currency === 'USD', 'This estimate uses USD sheet prices.');
  check(
    Array.isArray(data.groups) && data.groups.length > 0 && data.groups.length <= MAX_PARTS,
    'An estimate must contain 1–300 material groups.'
  );
  const savedGroups = data.groups.map(value => {
    check(value && typeof value === 'object', 'Invalid material group.');
    const group = value as { id: unknown; quote: unknown };
    check(
      group.quote && typeof group.quote === 'object' && (group.quote as { version?: unknown }).version === 3,
      'Material groups must contain version 3 estimates.'
    );
    const quote = group.quote as Record<string, unknown>;
    check(quote.units === 'in', 'Material group estimates must explicitly declare inches.');
    return { id: group.id, quote };
  });
  // Count raw saved geometry before quoteFromFile validates individual contours.
  // A project must not multiply a single quote's geometry budget by its group count.
  checkProjectStructure({
    name: data.name,
    activeGroupId: data.activeGroupId,
    groups: savedGroups.map(group => ({
      ...group,
      quote: {
        ...group.quote,
        thickness: typeof group.quote.thickness === 'number' ? inToMm(group.quote.thickness) : NaN,
      },
    })),
  });
  const groups = savedGroups.map(group => ({ id: group.id, quote: quoteFromFile(group.quote) }));
  return validateProject({ name: data.name, activeGroupId: data.activeGroupId, groups });
}

/** Add an entire assigned import atomically; invalid assignments leave the project unchanged. */
export function addImportedParts(project: QuoteProject, rows: ImportedPartAssignment[], parts: Part[]): QuoteProject {
  validateProject(project);
  check(Array.isArray(parts) && parts.length <= MAX_PARTS, 'Maximum 300 imported designs.');
  check(Array.isArray(rows) && rows.length <= MAX_PARTS, 'Invalid material assignments.');
  if (parts.length === 0 && rows.length === 0) return project;
  const imported = new Map<string, Part>();
  const existingIds = new Set(project.groups.flatMap(group => group.quote.parts.map(part => part.id)));
  for (const part of parts) {
    check(part && typeof part.id === 'string' && part.id.length > 0, 'Invalid imported part ID.');
    check(!imported.has(part.id) && !existingIds.has(part.id), 'Duplicate imported part IDs.');
    imported.set(part.id, part);
  }

  const assigned = new Set<string>();
  for (const row of rows) {
    check(row && Array.isArray(row.partIds) && row.partIds.length > 0, 'Assign at least one part to each row.');
    materialThicknessKey(row.material, row.thickness);
    for (const id of row.partIds) {
      check(imported.has(id), 'A material assignment refers to an unknown imported part.');
      check(!assigned.has(id), 'Each imported part must have exactly one material assignment.');
      assigned.add(id);
    }
  }
  check(assigned.size === imported.size, 'Assign a material and thickness to every imported part.');

  const source = project.groups.find(group => group.id === project.activeGroupId)!.quote;
  const groups = project.groups.map(group => ({ ...group, quote: { ...group.quote, parts: [...group.quote.parts] } }));
  const byMaterial = new Map(
    groups.map(group => [materialThicknessKey(group.quote.material, group.quote.thickness), group])
  );
  const groupIds = new Set(groups.map(group => group.id));
  let nextId = 1;
  let firstTargetId: string | undefined;
  for (const row of rows) {
    const key = materialThicknessKey(row.material, row.thickness);
    let group = byMaterial.get(key);
    if (!group) {
      while (groupIds.has(`group-${nextId}`)) nextId += 1;
      const id = `group-${nextId}`;
      groupIds.add(id);
      group = {
        id,
        quote: {
          ...createBlankQuote(),
          name: project.name,
          material: row.material,
          thickness: row.thickness,
          ...autoQuotingSpacing(row.thickness),
          spacingMode: 'auto',
          objective: source.objective,
          options: source.options.map(option => ({ ...option, price: null })),
        },
      };
      groups.push(group);
      byMaterial.set(key, group);
    }
    firstTargetId ??= group.id;
    group.quote.parts.push(...row.partIds.map(id => imported.get(id)!));
  }
  return validateProject({ ...project, activeGroupId: firstTargetId ?? project.activeGroupId, groups });
}
