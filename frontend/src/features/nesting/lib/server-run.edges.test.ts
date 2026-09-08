import { rect } from './nesting';
import { createBlankProject, projectToFile } from './quote-project';
import * as quoting from './quoting';
import { calculateSavedProject } from './server-run';

const inputHash = 'e'.repeat(64);
function savedInputs() {
  return projectToFile(
    createBlankProject({
      ...quoting.createBlankQuote(),
      margin: 1,
      gap: 1,
      parts: [{ id: 'plate', name: 'Synthetic plate', quantity: 2, rotate: false, color: 0, loops: [rect(12, 18)] }],
      options: [
        { id: 'z-first', enabled: true, width: 100, height: 100, price: 5 },
        { id: 'ignored', enabled: false, width: 60, height: 60, price: null },
        { id: 'a-second', enabled: true, width: 120, height: 120, price: 8 },
      ],
    })
  );
}

afterEach(() => jest.restoreAllMocks());

it('refuses a forged out-of-sheet placement before publishing any checkpoint', () => {
  const calculate = quoting.calculateSheetOption;
  jest.spyOn(quoting, 'calculateSheetOption').mockImplementationOnce((quote, option) => {
    const result = calculate(quote, option);
    result.nest!.placements[0].x = option.width + 1;
    return result;
  });
  const run = calculateSavedProject(savedInputs(), inputHash);
  expect(() => run.next()).toThrow();
  expect(run.next().done).toBe(true);
});

it('refuses a forged completion flag independently of otherwise valid geometry', () => {
  const calculate = quoting.calculateSheetOption;
  jest.spyOn(quoting, 'calculateSheetOption').mockImplementationOnce((quote, option) => ({
    ...calculate(quote, option),
    complete: false,
  }));
  expect(() => calculateSavedProject(savedInputs(), inputHash).next()).toThrow('completion state');
});

it('yields completed work in source order and does not begin the next option until resumed', () => {
  const saved = savedInputs();
  const before = JSON.stringify(saved);
  const calculate = jest.spyOn(quoting, 'calculateSheetOption');
  const run = calculateSavedProject(saved, inputHash);
  expect(calculate).not.toHaveBeenCalled();
  expect(run.next().value).toMatchObject({ type: 'option', option_id: 'z-first', sequence: 1 });
  expect(calculate).toHaveBeenCalledTimes(1);
  expect(run.next().value).toMatchObject({ type: 'option', option_id: 'a-second', sequence: 2 });
  expect(calculate).toHaveBeenCalledTimes(2);
  expect(run.next().value).toMatchObject({
    type: 'summary',
    evaluated_keys: [
      { group_id: 'group-1', option_id: 'z-first' },
      { group_id: 'group-1', option_id: 'a-second' },
    ],
    evaluated_count: 2,
    total_options: 2,
    stop_reason: 'completed',
  });
  expect(run.next().done).toBe(true);
  expect(JSON.stringify(saved)).toBe(before);
});

it('keeps an already yielded checkpoint usable when subsequent work fails validation', () => {
  const calculate = quoting.calculateSheetOption;
  jest
    .spyOn(quoting, 'calculateSheetOption')
    .mockImplementationOnce(calculate)
    .mockImplementationOnce((quote, option) => {
      const result = calculate(quote, option);
      result.nest!.placements[0].partId = 'unknown-part';
      return result;
    });
  const run = calculateSavedProject(savedInputs(), inputHash);
  const first = run.next().value;
  const fingerprint = JSON.stringify(first);
  expect(first).toMatchObject({ type: 'option', option_id: 'z-first', result: { complete: true } });
  expect(() => run.next()).toThrow();
  expect(JSON.stringify(first)).toBe(fingerprint);
  expect(run.next().done).toBe(true);
});
