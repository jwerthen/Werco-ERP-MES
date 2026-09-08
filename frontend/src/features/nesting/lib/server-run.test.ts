import { rect, validateNest, type Part } from './nesting';
import { createBlankProject, projectFromFile, projectToFile, type QuoteProject } from './quote-project';
import { compareSheets, createBlankQuote } from './quoting';
import { calculateSavedProject, type ServerOptionMessage } from './server-run';
import { compareStableText } from './stable-order';

const digest = 'c'.repeat(64);
function project(): QuoteProject {
  const parts: Part[] = [
    {
      id: 'elbow',
      name: 'Elbow',
      quantity: 2,
      rotate: true,
      rotationMode: 'half-turn',
      color: 0,
      loops: [
        {
          type: 'poly',
          points: [
            { x: 0, y: 0 },
            { x: 60, y: 0 },
            { x: 60, y: 20 },
            { x: 20, y: 20 },
            { x: 20, y: 60 },
            { x: 0, y: 60 },
          ],
        },
      ],
    },
    {
      id: 'ring',
      name: 'Ring',
      quantity: 2,
      rotate: false,
      color: 1,
      loops: [
        { type: 'circle', cx: 12, cy: 12, r: 12 },
        { type: 'circle', cx: 12, cy: 12, r: 5 },
      ],
    },
  ];
  return createBlankProject({
    ...createBlankQuote(),
    parts,
    margin: 2,
    gap: 2,
    options: [{ id: 'square', width: 150, height: 150, enabled: true, price: 8 }],
  });
}

it('calculates the same actual profiles as the browser and conserves quantities and hole reservations', () => {
  const input = project();
  const saved = projectToFile(input);
  const restored = projectFromFile(saved);
  const messages = Array.from(calculateSavedProject(saved, digest));
  const option = messages[0] as ServerOptionMessage;
  expect(option.result).toEqual(compareSheets(restored.groups[0].quote).results[0]);
  expect(option.requested).toBe(4);
  expect(option.result.nest!.placements).toHaveLength(4);
  validateNest(input.groups[0].quote.parts, option.stock, option.result.nest!);
  expect(
    option.result.leftovers!.sheets.every(sheet => sheet.regions.every(region => region.classification === 'review'))
  ).toBe(true);
  expect(messages[1]).toMatchObject({
    type: 'summary',
    evaluated_count: 1,
    complete_option_count: 1,
    stop_reason: 'completed',
  });
  expect(Array.from(calculateSavedProject(projectToFile(input), digest))).toEqual(messages);
});

it('finishes search without claiming a complete order when grain rules prevent placement', () => {
  const input = project();
  input.groups[0].quote.parts[0].grainAxis = 'x';
  const messages = Array.from(calculateSavedProject(projectToFile(input), digest));
  const result = (messages[0] as ServerOptionMessage).result;
  expect(result.complete).toBe(false);
  expect(result.nest!.unplaced).toEqual(
    expect.arrayContaining([expect.objectContaining({ partId: 'elbow', count: 2 })])
  );
  expect(messages[1]).toMatchObject({ stop_reason: 'completed', complete_option_count: 0 });
});

it('limits total work across material groups and explicitly identifies unfinished options', () => {
  const quote = createBlankQuote();
  const input: QuoteProject = {
    name: 'Many groups',
    activeGroupId: 'g0',
    groups: Array.from({ length: 37 }, (_, index) => ({
      id: `g${index}`,
      quote: {
        ...quote,
        thickness: index + 1,
        margin: 1,
        gap: 1,
        parts: [{ id: `p${index}`, name: 'Plate', loops: [rect(5, 5)], quantity: 1, rotate: false, color: 0 }],
        options: [{ id: 'stock', width: 20, height: 20, price: null, enabled: true }],
      },
    })),
  };
  const messages = Array.from(calculateSavedProject(projectToFile(input), digest));
  expect(messages).toHaveLength(37);
  expect(messages[36]).toMatchObject({
    type: 'summary',
    evaluated_count: 36,
    total_options: 37,
    stop_reason: 'work_limit',
  });
  expect(messages[35]).toMatchObject({ sequence: 36, group_id: 'g35' });
});

it('refuses unit ambiguity, invalid geometry and empty inputs before yielding a checkpoint', () => {
  const input = projectToFile(project());
  expect(() => Array.from(calculateSavedProject({ ...input, units: 'mm' }, digest))).toThrow('inches');
  expect(() => Array.from(calculateSavedProject(projectToFile(createBlankProject()), digest))).toThrow(
    'at least one part'
  );
  expect(() => Array.from(calculateSavedProject(input, 'not-a-digest'))).toThrow('fingerprint');
  const broken = JSON.parse(JSON.stringify(input));
  broken.groups[0].quote.parts[0].loops[0].points = [
    { x: 0, y: 0 },
    { x: 2, y: 2 },
    { x: 0, y: 2 },
    { x: 2, y: 0 },
  ];
  expect(() => Array.from(calculateSavedProject(broken, digest))).toThrow();
});

it('orders identifiers independently of locale collation', () => {
  expect(['z', 'ä', 'A', 'a'].sort(compareStableText)).toEqual(['A', 'a', 'z', 'ä']);
});
