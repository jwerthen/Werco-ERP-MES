const mockCallbacks: Record<string, (metric: Record<string, unknown>) => void> = {};
jest.mock('web-vitals', () => ({
  onLCP: jest.fn(callback => {
    mockCallbacks.LCP = callback;
  }),
  onINP: jest.fn(callback => {
    mockCallbacks.INP = callback;
  }),
  onCLS: jest.fn(callback => {
    mockCallbacks.CLS = callback;
  }),
}));

function token(company = 1) {
  return `header.${btoa(JSON.stringify({ sub: '1', cid: company, exp: Date.now() / 1000 + 3600 }))}.signature`;
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('first-party runtime measurements', () => {
  let fetchMock: jest.Mock;
  let removeListeners: (() => void)[];
  beforeEach(() => {
    jest.resetModules();
    jest.useFakeTimers();
    sessionStorage.clear();
    sessionStorage.setItem('token', token());
    window.history.replaceState({}, '', '/work-orders/123?customer=private#notes');
    Object.defineProperty(navigator, 'doNotTrack', { value: '0', configurable: true });
    Object.defineProperty(navigator, 'globalPrivacyControl', { value: false, configurable: true });
    Object.defineProperty(crypto, 'randomUUID', {
      value: jest.fn(() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),
      configurable: true,
    });
    fetchMock = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ enabled: true }) });
    global.fetch = fetchMock;
    removeListeners = [];
    const windowAdd = window.addEventListener.bind(window);
    const documentAdd = document.addEventListener.bind(document);
    jest.spyOn(window, 'addEventListener').mockImplementation((name, callback, options) => {
      windowAdd(name, callback, options);
      removeListeners.push(() => window.removeEventListener(name, callback, options));
    });
    jest.spyOn(document, 'addEventListener').mockImplementation((name, callback, options) => {
      documentAdd(name, callback, options);
      removeListeners.push(() => document.removeEventListener(name, callback, options));
    });
  });
  afterEach(() => {
    removeListeners.forEach(remove => remove());
    jest.restoreAllMocks();
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  test('only allowlisted templates and viewport buckets are exposed', () => {
    const { metricRoute, metricDevice } = require('./runtimeMetrics');
    expect(metricRoute('/work-orders/123?customer=private#notes')).toBe('/work-orders/:id');
    expect(metricRoute('/work-orders/new')).toBe('/work-orders/new');
    expect(metricRoute('/parts/123/edit')).toBe('/parts/:id/edit');
    expect(metricRoute('/secret/customer@example.test')).toBeNull();
    expect(metricRoute('/login?returnTo=/parts')).toBeNull();
    expect([metricDevice(390), metricDevice(800), metricDevice(1440)]).toEqual(['mobile', 'tablet', 'desktop']);
  });

  test('uses original navigation template and sends a bounded payload without attribution or URLs', async () => {
    const { startRuntimeMetrics } = require('./runtimeMetrics');
    startRuntimeMetrics();
    startRuntimeMetrics();
    await settle();
    window.history.replaceState({}, '', '/parts?private=another');
    mockCallbacks.LCP({
      id: 'library-metric-id',
      name: 'LCP',
      value: 1234.56789,
      navigationType: 'navigate',
      navigationStartTime: 0,
      entries: [{ url: 'private', element: 'customer' }],
    });
    jest.advanceTimersByTime(2000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toMatch(/\/runtime-metrics\/samples$/);
    expect(options.keepalive).toBe(true);
    expect(JSON.parse(options.body).samples[0]).toEqual({
      metric_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      name: 'LCP',
      route: '/work-orders/:id',
      device: 'desktop',
      navigation: 'document',
      release: 'development',
      value: 1234.5679,
      sequence: 1,
    });
    expect(options.body).not.toMatch(/customer|private|entries|library-metric|123\?/);
  });

  test('company switch drops pending measurements and earlier document data', async () => {
    require('./runtimeMetrics').startRuntimeMetrics();
    await settle();
    mockCallbacks.INP({ id: 'inp', name: 'INP', value: 100, navigationType: 'navigate', navigationStartTime: 0 });
    sessionStorage.setItem('token', token(2));
    window.dispatchEvent(new Event('werco:auth-token-changed'));
    await settle();
    mockCallbacks.CLS({ id: 'cls', name: 'CLS', value: 0.1, navigationType: 'navigate', navigationStartTime: 0 });
    jest.advanceTimersByTime(2000);
    expect(fetchMock.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(0);
  });

  test('supported soft navigation uses the metric navigation URL even after another route opens', async () => {
    require('./runtimeMetrics').startRuntimeMetrics();
    await settle();
    mockCallbacks.INP({
      id: 'inp',
      name: 'INP',
      value: 100,
      navigationType: 'soft-navigation',
      navigationStartTime: 100,
      navigationURL: 'https://example.test/parts/456?name=private',
    });
    window.dispatchEvent(new Event('pagehide'));
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).samples[0]).toMatchObject({
      route: '/parts/:id',
      navigation: 'soft',
    });
  });

  test.each(['doNotTrack', 'globalPrivacyControl'])('respects %s without initiating requests', signal => {
    Object.defineProperty(navigator, signal, { value: signal === 'doNotTrack' ? '1' : true, configurable: true });
    require('./runtimeMetrics').startRuntimeMetrics();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test('disabled collection and signed-out sessions produce no samples', async () => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ enabled: false }) });
    require('./runtimeMetrics').startRuntimeMetrics();
    await settle();
    mockCallbacks.LCP({ id: 'lcp', name: 'LCP', value: 100 });
    jest.advanceTimersByTime(2000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    sessionStorage.clear();
    window.dispatchEvent(new Event('werco:auth-token-changed'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
