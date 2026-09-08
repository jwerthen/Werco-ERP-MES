import { compareSheets, type Quote } from '../features/nesting/lib/quoting';

/** Jest does not run Vite worker bundles; exercise the same computation asynchronously. */
export default class NestingWorkerMock {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  private terminated = false;
  postMessage(message: { quote: Quote }) {
    void Promise.resolve().then(() => {
      if (this.terminated) return;
      try {
        this.onmessage?.({ data: { ok: true, comparison: compareSheets(message.quote) } } as MessageEvent);
      } catch (error) {
        this.onmessage?.({
          data: { ok: false, error: error instanceof Error ? error.message : 'Failed' },
        } as MessageEvent);
      }
    });
  }
  terminate() {
    this.terminated = true;
  }
}
