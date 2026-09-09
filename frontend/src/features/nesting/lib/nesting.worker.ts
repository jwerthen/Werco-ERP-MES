import { compareSheets, type Quote } from './quoting';
import { calculateRemnantProject } from './remnant-planning';

type Request = { quote: Quote } | { kind: 'project-stages'; estimate: unknown; inputSha256: string };
self.onmessage = async (event: MessageEvent<Request>) => {
  try {
    if ('kind' in event.data) {
      const { estimate, inputSha256 } = event.data;
      for await (const message of calculateRemnantProject(estimate, inputSha256)) self.postMessage(message);
    } else self.postMessage({ ok: true, comparison: compareSheets(event.data.quote) });
  } catch (error) {
    self.postMessage({ ok: false, error: error instanceof Error ? error.message : 'Could not compare sheet sizes.' });
  }
};
