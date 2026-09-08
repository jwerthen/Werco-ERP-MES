import { rect, validateJob, validatePart, type Job, type Part } from './nesting';
import { createBlankQuote, validateQuote } from './quoting';

describe('combined contour and reference geometry budget', () => {
  it('allows 20,000 total vertices but rejects one extra reference point across otherwise valid parts', () => {
    const parts: Part[] = Array.from({ length: 2 }, (_, index) => ({
      id: `plate-${index}`,
      name: `Plate ${index}`,
      loops: [rect(100, 50)],
      quantity: 1,
      rotate: true,
      color: 0,
      // Four outline corners + 9,996 reference points = 10,000 per part.
      referencePaths: Array.from({ length: 5 }, (_, path) =>
        Array.from({ length: path === 0 ? 2000 : 1999 }, (_, point) => ({
          x: 1 + point / 2000,
          y: 1 + path,
        }))
      ),
    }));
    const quote = { ...createBlankQuote(), parts };
    const job: Job = {
      version: 1,
      name: quote.name,
      material: quote.material,
      thickness: quote.thickness,
      parts,
      bedConfirmed: false,
      stock: { width: 500, height: 500, bedWidth: 500, bedHeight: 500, gap: 1, margin: 1, maxSheets: 1 },
    };
    expect(() => validateQuote(quote)).not.toThrow();
    expect(() => validateJob(job)).not.toThrow();

    parts[0].referencePaths![1].push({ x: 3, y: 2 });
    // The individual part and each reference path still satisfy their own limits.
    expect(() => validatePart(parts[0])).not.toThrow();
    expect(() => validateQuote(quote)).toThrow(/20,000 geometry vertices/);
    expect(() => validateJob(job)).toThrow(/20,000 vertices/);
  });
});
