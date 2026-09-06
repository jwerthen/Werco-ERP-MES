import { readNestingDraft, readNestingSavedSignature, writeNestingDraft } from './draft';
import { demoQuote } from './lib/quoting';

describe('in-memory nesting draft ownership', () => {
  beforeEach(() => {
    readNestingDraft('clear-between-tests');
  });

  it('returns a current-owner draft without writing geometry to browser storage', () => {
    const storageWrite = jest.spyOn(Storage.prototype, 'setItem');
    const quote = { ...demoQuote, name: 'Current quote' };
    writeNestingDraft('7:10', quote, JSON.stringify(quote));
    expect(readNestingDraft('7:10')).toEqual(quote);
    expect(readNestingSavedSignature('7:10')).toBe(JSON.stringify(quote));
    expect(storageWrite).not.toHaveBeenCalled();
    storageWrite.mockRestore();
  });

  it.each(['8:10', '7:20'])('discards the previous draft after switching ownership to %s', nextOwner => {
    writeNestingDraft('7:10', { ...demoQuote, name: 'Private quote' });
    expect(readNestingDraft(nextOwner)).toEqual(demoQuote);
    expect(readNestingDraft('7:10')).toEqual(demoQuote);
    expect(readNestingSavedSignature('7:10')).toBe(JSON.stringify(demoQuote));
  });
});
