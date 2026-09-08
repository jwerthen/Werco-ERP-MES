import { compareSheets, type Quote } from './quoting';

self.onmessage = (event: MessageEvent<{ quote: Quote }>) => {
  try {
    self.postMessage({ ok: true, comparison: compareSheets(event.data.quote) });
  } catch (error) {
    self.postMessage({ ok: false, error: error instanceof Error ? error.message : 'Could not compare sheet sizes.' });
  }
};
