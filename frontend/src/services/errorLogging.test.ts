/**
 * The crash beacon's wire format.
 *
 * A string `navigator.sendBeacon` body is sent as `text/plain;charset=UTF-8`, which
 * the API (FastAPI) does not parse as JSON -- so the global-boundary crashes, the ones
 * that blank the screen, were rejected 422 and never reached a log line. These pin
 * that the beacon now carries an `application/json` Blob, and that a beacon the
 * browser declines falls back to a keepalive fetch instead of dropping the batch.
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

  it('sends a global-boundary crash as an application/json Blob, not a text/plain string', async () => {
    logError({ error: new Error('white screen'), boundaryName: 'App', boundaryLevel: 'global' });

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, payload] = sendBeacon.mock.calls[0];
    expect(url).toMatch(/\/errors\/log$/);
    expect(typeof payload).not.toBe('string');
    expect(payload).toBeInstanceOf(Blob);
    expect((payload as Blob).type).toBe('application/json');

    const parsed = JSON.parse(await readBlobText(payload as Blob));
    expect(parsed.errors).toHaveLength(1);
    expect(parsed.errors[0]).toMatchObject({ message: 'white screen', boundaryName: 'App', boundaryLevel: 'global' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('falls back to a keepalive JSON fetch when the browser throws on the Blob beacon', async () => {
    sendBeacon.mockImplementation(() => {
      throw new DOMException('sendBeacon() with a non-CORS-safelisted Blob type is disabled', 'SecurityError');
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

  it('flushes queued errors on page unload as the same application/json Blob beacon', async () => {
    logError({ error: new Error('queued page error'), boundaryLevel: 'page' });
    // A non-global error only queues; nothing is sent yet.
    expect(sendBeacon).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();

    window.dispatchEvent(new Event('beforeunload'));

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const payload = sendBeacon.mock.calls[0][1];
    expect(payload).toBeInstanceOf(Blob);
    expect((payload as Blob).type).toBe('application/json');
    const parsed = JSON.parse(await readBlobText(payload as Blob));
    expect(parsed.errors.map((e: { message: string }) => e.message)).toEqual(['queued page error']);
    expect(fetchMock).not.toHaveBeenCalled();
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
