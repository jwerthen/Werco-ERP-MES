import { rect } from './nesting';
import { compareSheets, createBlankQuote, standardOptions, validateQuote } from './quoting';

describe('fresh material estimates', () => {
  it('starts valid and empty, without a recommended sheet order or placed parts', () => {
    const quote = createBlankQuote();
    expect(validateQuote(quote)).toEqual(quote);
    expect(quote.parts).toEqual([]);
    const comparison = compareSheets(quote);
    expect(comparison).toMatchObject({ requested: 0, recommendedId: null });
    expect(comparison.results.every(result => !result.complete && result.nest?.placements.length === 0)).toBe(true);
  });

  it('isolates edited stock options and parts from other estimates and the standard defaults', () => {
    const defaults = standardOptions.map(option => ({ ...option }));
    const edited = createBlankQuote();
    const other = createBlankQuote();
    edited.options[0].enabled = !edited.options[0].enabled;
    edited.options[0].price = 275;
    edited.options[0].width = 1000;
    edited.options.push({ id: 'custom', width: 1500, height: 1000, price: 125, enabled: true });
    edited.parts.push({
      id: 'customer-plate',
      name: 'Customer plate',
      loops: [rect(100, 50)],
      quantity: 3,
      rotate: true,
      color: 0,
    });

    expect(other.options).toEqual(defaults);
    expect(other.parts).toEqual([]);
    expect(standardOptions).toEqual(defaults);
    const nextEntry = createBlankQuote();
    expect(nextEntry.options).toEqual(defaults);
    expect(nextEntry.parts).toEqual([]);
  });
});
