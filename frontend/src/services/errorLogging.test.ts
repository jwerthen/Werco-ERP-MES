/**
 * The crash beacon's wire format.
 *
 * The beacon was a bare string, and the API (FastAPI) did not parse a `text/plain` body
 * as JSON -- so the global-boundary crashes, the ones that blank the screen, were
 * rejected 422 and never reached a log line. The API now reads `text/plain` on
 * `POST /errors/log` as JSON, and that label is the supported beacon contract: it is
 * CORS-safelisted, so the cross-origin beacon needs no preflight, whereas Chromium refuses
 * an `application/json` Blob beacon outright. These pin that the beacon carries a
 * `text/plain` Blob of the JSON batch, that a beacon the browser declines falls back to a
 * keepalive fetch instead of dropping the batch, that a retryable failure is kept for the
 * next flush, and that no entry is sent in a shape the server would reject.
 */
import { logError } from './errorLogging';

const readBlobText = (blob: Blob): Promise<string> => {
  if (typeof blob.text === 'function') return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
};

// Let the service's async flush settle so its in-flight guard is released between tests.
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

describe('errorLogging crash beacon', () => {
  const originalSendBeacon = navigator.sendBeacon;
  const originalFetch = global.fetch;
  let sendBeacon: jest.Mock;
  let fetchMock: jest.Mock;

  beforeEach(() => {
    sendBeacon = jest.fn().mockReturnValue(true);
    fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200 });
    Object.defineProperty(navigator, 'sendBeacon', { configurable: true, writable: true, value: sendBeacon });
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(async () => {
    await settle();
    Object.defineProperty(navigator, 'sendBeacon', {
      configurable: true,
      writable: true,
      value: originalSendBeacon,
    });
    global.fetch = originalFetch;
  });

  it('sends a global-boundary crash as a text/plain Blob of the JSON batch (the CORS-safelisted beacon)', async () => {
    logError({ error: new Error('white screen'), boundaryName: 'App', boundaryLevel: 'global' });

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, payload] = sendBeacon.mock.calls[0];
    expect(url).toMatch(/\/errors\/log$/);
    expect(typeof payload).not.toBe('string');
    expect(payload).toBeInstanceOf(Blob);
    // The Blob constructor lowercases its type; the label is text/plain either way.
    expect((payload as Blob).type.toLowerCase()).toBe('text/plain;charset=utf-8');

    const parsed = JSON.parse(await readBlobText(payload as Blob));
    expect(parsed.errors).toHaveLength(1);
    expect(parsed.errors[0]).toMatchObject({ message: 'white screen', boundaryName: 'App', boundaryLevel: 'global' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('falls back to a keepalive JSON fetch when the browser throws on the beacon', async () => {
    sendBeacon.mockImplementation(() => {
      throw new DOMException('beacon refused', 'SecurityError');
    });

    logError({ error: new Error('refused beacon'), boundaryLevel: 'global' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/errors\/log$/);
    expect(init).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
    });
    expect(JSON.parse(init.body).errors[0].message).toBe('refused beacon');
  });

  it('falls back to a keepalive fetch when the browser declines to queue the beacon', async () => {
    sendBeacon.mockReturnValue(false);

    logError({ error: new Error('over quota'), boundaryLevel: 'global' });
    await settle();

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ keepalive: true });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).errors[0].message).toBe('over quota');
  });

  it('falls back to a keepalive JSON fetch when the browser has no sendBeacon at all', async () => {
    Object.defineProperty(navigator, 'sendBeacon', { configurable: true, writable: true, value: undefined });

    logError({ error: new Error('no beacon api'), boundaryLevel: 'global' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: true,
    });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).errors[0].message).toBe('no beacon api');
  });

  it('keeps a crash whose beacon was declined and whose fallback fetch failed, and sends it with the next flush', async () => {
    sendBeacon.mockReturnValue(false);
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    logError({ error: new Error('first crash'), boundaryLevel: 'global' });
    await settle();
    logError({ error: new Error('second crash'), boundaryLevel: 'global' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const delivered = JSON.parse(fetchMock.mock.calls[1][1].body).errors.map((e: { message: string }) => e.message);
    expect(delivered).toEqual(['first crash', 'second crash']);
  });

  it('flushes queued errors on page unload as the same text/plain Blob beacon', async () => {
    logError({ error: new Error('queued page error'), boundaryLevel: 'page' });
    // A non-global error only queues; nothing is sent yet.
    expect(sendBeacon).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();

    window.dispatchEvent(new Event('beforeunload'));

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const payload = sendBeacon.mock.calls[0][1];
    expect(payload).toBeInstanceOf(Blob);
    expect((payload as Blob).type.toLowerCase()).toBe('text/plain;charset=utf-8');
    const parsed = JSON.parse(await readBlobText(payload as Blob));
    expect(parsed.errors.map((e: { message: string }) => e.message)).toEqual(['queued page error']);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([429, 500, 503])(
    'keeps a batch the server answered %i to, and sends it with the next flush',
    async status => {
      sendBeacon.mockReturnValue(false);
      fetchMock.mockResolvedValueOnce({ ok: false, status });

      logError({ error: new Error('first crash'), boundaryLevel: 'global' });
      await settle();
      logError({ error: new Error('second crash'), boundaryLevel: 'global' });
      await settle();

      expect(fetchMock).toHaveBeenCalledTimes(2);
      const delivered = JSON.parse(fetchMock.mock.calls[1][1].body).errors.map((e: { message: string }) => e.message);
      expect(delivered).toEqual(['first crash', 'second crash']);
    }
  );

  it.each([413, 422])('does not resend a batch the server refused with %i (it would refuse it again)', async status => {
    sendBeacon.mockReturnValue(false);
    fetchMock.mockResolvedValueOnce({ ok: false, status });

    logError({ error: new Error('refused batch'), boundaryLevel: 'global' });
    await settle();
    logError({ error: new Error('next crash'), boundaryLevel: 'global' });
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const delivered = JSON.parse(fetchMock.mock.calls[1][1].body).errors.map((e: { message: string }) => e.message);
    expect(delivered).toEqual(['next crash']);
  });

  it("clamps every field to the server's cap, so one oversized entry cannot 422 the whole batch", async () => {
    const error = new Error('m'.repeat(5000));
    error.stack = 's'.repeat(25000);
    logError({
      error,
      errorInfo: { componentStack: 'c'.repeat(25000) },
      boundaryName: 'b'.repeat(300),
      boundaryLevel: 'global',
      url: `https://app.example/work-orders?${'q'.repeat(3000)}`,
      userAgent: 'u'.repeat(1500),
      userId: 'i'.repeat(100),
      sessionId: 'x'.repeat(200),
    });

    const [entry] = JSON.parse(await readBlobText(sendBeacon.mock.calls[0][1] as Blob)).errors;
    // Caps from ErrorLogEntry in backend/app/api/endpoints/errors.py.
    expect(entry.message).toHaveLength(4000);
    expect(entry.stack).toHaveLength(20000);
    expect(entry.componentStack).toHaveLength(20000);
    expect(entry.boundaryName).toHaveLength(200);
    expect(entry.url).toHaveLength(2000);
    expect(entry.userAgent).toHaveLength(1000);
    expect(entry.userId).toHaveLength(64);
    expect(entry.sessionId).toHaveLength(128);
    expect(entry.boundaryLevel).toBe('global');
    expect(entry.id.length).toBeLessThanOrEqual(128);
    expect(entry.timestamp.length).toBeLessThanOrEqual(64);
  });

  it('never sends the half-typed form contents a form boundary preserved', async () => {
    logError({
      error: new Error('form crash'),
      boundaryLevel: 'global',
      preservedData: { customer_notes: 'confidential draft text' },
    });

    const text = await readBlobText(sendBeacon.mock.calls[0][1] as Blob);
    expect(text).not.toContain('confidential draft text');
    expect(JSON.parse(text).errors[0].metadata).not.toHaveProperty('preservedData');
  });

  it('reports a thrown non-Error value (`throw "text"`) instead of throwing inside the error listener', async () => {
    expect(() => logError({ error: 'plain text thrown' as unknown as Error, boundaryLevel: 'global' })).not.toThrow();

    const [entry] = JSON.parse(await readBlobText(sendBeacon.mock.calls[0][1] as Blob)).errors;
    expect(entry.message).toBe('plain text thrown');
  });
});

/**
 * The periodic (non-sync) flush: a page-level error is not a crash and the page is not
 * going away, so it must go out as an ordinary fetch -- never a beacon, and never a
 * keepalive request, which would spend the browser's shared keepalive quota that an
 * unload-time crash report may need. The service schedules its interval when the module
 * loads, so each test loads a fresh copy under fake timers.
 */
describe('errorLogging periodic flush', () => {
  const originalSendBeacon = navigator.sendBeacon;
  const originalFetch = global.fetch;
  let sendBeacon: jest.Mock;
  let fetchMock: jest.Mock;
  let removeListeners: Array<() => void> = [];

  beforeEach(() => {
    jest.useFakeTimers();
    sendBeacon = jest.fn().mockReturnValue(true);
    fetchMock = jest.fn().mockResolvedValue({ ok: true, status: 200 });
    Object.defineProperty(navigator, 'sendBeacon', { configurable: true, writable: true, value: sendBeacon });
    global.fetch = fetchMock as unknown as typeof fetch;
    // The fresh module copy registers its own window listeners; remove them afterwards
    // so they cannot answer events dispatched by later tests.
    const windowAdd = window.addEventListener.bind(window);
    jest.spyOn(window, 'addEventListener').mockImplementation((name, callback, options) => {
      windowAdd(name, callback, options);
      removeListeners.push(() => window.removeEventListener(name, callback, options));
    });
  });

  afterEach(() => {
    removeListeners.forEach(remove => remove());
    removeListeners = [];
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
    Object.defineProperty(navigator, 'sendBeacon', {
      configurable: true,
      writable: true,
      value: originalSendBeacon,
    });
    global.fetch = originalFetch;
  });

  it('posts a page-level error on the interval with a plain JSON fetch, never a beacon or keepalive', () => {
    let isolatedLogError: typeof logError = () => undefined;
    jest.isolateModules(() => {
      isolatedLogError = require('./errorLogging').logError;
    });

    isolatedLogError({ error: new Error('page-level'), boundaryLevel: 'page' });
    expect(fetchMock).not.toHaveBeenCalled();

    jest.advanceTimersByTime(5000);

    expect(sendBeacon).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/errors\/log$/);
    expect(init).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      keepalive: false,
    });
    expect(JSON.parse(init.body).errors[0].message).toBe('page-level');
  });
});
