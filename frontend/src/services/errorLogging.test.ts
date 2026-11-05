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
});
