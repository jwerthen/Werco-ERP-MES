import { compareSheetsInWorker } from './nesting-worker-client';
import { createBlankQuote, type Comparison } from './quoting';

type WorkerReply = { ok: boolean; comparison?: Comparison; error?: string };
type ControlledWorker = {
  onmessage: ((event: MessageEvent<WorkerReply>) => void) | null;
  onerror: (() => void) | null;
  postMessage: jest.Mock;
  terminate: jest.Mock;
};
const mockWorkers: ControlledWorker[] = [];
let mockStartupError: Error | undefined;
const mockNewWorker = jest.fn(() => {
  const worker: ControlledWorker = {
    onmessage: null,
    onerror: null,
    postMessage: jest.fn(() => {
      if (mockStartupError) throw mockStartupError;
    }),
    terminate: jest.fn(),
  };
  mockWorkers.push(worker);
  return worker;
});
jest.mock('./nesting.worker?worker', () => ({
  __esModule: true,
  default: jest.fn().mockImplementation(() => mockNewWorker()),
}));

const comparison: Comparison = { results: [], recommendedId: null, requested: 0, reason: 'Empty test estimate' };
function reply(worker: ControlledWorker, value: WorkerReply) {
  worker.onmessage?.(new MessageEvent<WorkerReply>('message', { data: value }));
}

describe('nesting worker lifecycle', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockWorkers.length = 0;
    mockNewWorker.mockClear();
    mockStartupError = undefined;
  });
  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('sends a quote to a worker and releases its timer and abort listener on success', async () => {
    const quote = createBlankQuote();
    const controller = new AbortController();
    const removeListener = jest.spyOn(controller.signal, 'removeEventListener');
    const pending = compareSheetsInWorker(quote, { signal: controller.signal });
    const worker = mockWorkers[0];
    expect(worker.postMessage).toHaveBeenCalledWith({ quote });
    expect(worker.terminate).not.toHaveBeenCalled();
    reply(worker, { ok: true, comparison });
    await expect(pending).resolves.toEqual(comparison);
    expect(worker.terminate).toHaveBeenCalledTimes(1);
    expect(removeListener).toHaveBeenCalledWith('abort', expect.any(Function));
    expect(jest.getTimerCount()).toBe(0);
    controller.abort();
    reply(worker, { ok: true, comparison: { ...comparison, requested: 99 } });
    expect(worker.terminate).toHaveBeenCalledTimes(1);
  });

  it('does not start work when its signal is already cancelled', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(compareSheetsInWorker(createBlankQuote(), { signal: controller.signal })).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(mockNewWorker).not.toHaveBeenCalled();
    expect(jest.getTimerCount()).toBe(0);
  });

  it('cancels only the matching calculation and ignores its late result', async () => {
    const controller = new AbortController();
    const cancelled = compareSheetsInWorker(createBlankQuote(), { signal: controller.signal });
    const active = compareSheetsInWorker(createBlankQuote());
    const rejection = expect(cancelled).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    reply(mockWorkers[0], { ok: true, comparison });
    await rejection;
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(mockWorkers[1].terminate).not.toHaveBeenCalled();
    reply(mockWorkers[1], { ok: true, comparison });
    await expect(active).resolves.toEqual(comparison);
    expect(jest.getTimerCount()).toBe(0);
  });

  it.each([
    [{ ok: false, error: 'Part exceeds stock size.' }, 'Part exceeds stock size.'],
    [{ ok: true }, 'Could not compare sheet sizes.'],
  ] as const)('reports an unsuccessful worker reply and terminates it', async (response, message) => {
    const pending = compareSheetsInWorker(createBlankQuote());
    reply(mockWorkers[0], response);
    await expect(pending).rejects.toThrow(message);
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('reports worker runtime errors and releases resources', async () => {
    const pending = compareSheetsInWorker(createBlankQuote());
    mockWorkers[0].onerror?.();
    await expect(pending).rejects.toThrow(/worker could not finish/i);
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('terminates a worker when posting the input fails', async () => {
    mockStartupError = new Error('Input could not be cloned.');
    await expect(compareSheetsInWorker(createBlankQuote())).rejects.toThrow('Input could not be cloned.');
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('terminates a stalled calculation at the deadline and ignores late completion', async () => {
    const pending = compareSheetsInWorker(createBlankQuote());
    const rejection = expect(pending).rejects.toThrow(/calculation time limit/i);
    jest.advanceTimersByTime(119999);
    expect(mockWorkers[0].terminate).not.toHaveBeenCalled();
    jest.advanceTimersByTime(1);
    await rejection;
    reply(mockWorkers[0], { ok: true, comparison });
    expect(mockWorkers[0].terminate).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });
});
