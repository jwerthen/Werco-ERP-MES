import { onCLS, onINP, onLCP, type Metric } from 'web-vitals';
import routes from '../data/runtimeMetricRoutes.json';
import type { MetricDevice, MetricName, RuntimeMetricSample } from '../types/runtimeMetrics';

const templates = [...routes].sort(
  (a, b) => b.split('/').length - a.split('/').length || a.split(':').length - b.split(':').length
);

/** Only return source-controlled templates. Never send a URL, query, hash or record ID. */
export function metricRoute(url: string): string | null {
  let pathname: string;
  try {
    pathname = new URL(url, window.location.origin).pathname.replace(/\/$/, '') || '/';
  } catch {
    return null;
  }
  const parts = pathname.split('/');
  return (
    templates.find(template => {
      const pattern = template.split('/');
      return (
        pattern.length === parts.length &&
        pattern.every((part, index) =>
          part.startsWith(':') ? /^[a-zA-Z0-9_-]+$/.test(parts[index]) : part === parts[index]
        )
      );
    }) || null
  );
}

export function metricDevice(width: number): MetricDevice {
  return width < 768 ? 'mobile' : width < 1024 ? 'tablet' : 'desktop';
}

function session() {
  try {
    const token = sessionStorage.getItem('token');
    if (!token) return null;
    const claims = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    if (!/^\d+$/.test(claims.sub) || !claims.cid || !claims.exp || claims.exp * 1000 <= Date.now()) return null;
    return { token, key: `${claims.sub}:${claims.cid}` };
  } catch {
    return null;
  }
}

let started = false;

/** Called once at boot; no attribution build, analytics vendor, cookie or persistent queue. */
export function startRuntimeMetrics() {
  if (started || typeof window === 'undefined' || typeof fetch !== 'function') return;
  started = true;
  if (
    navigator.doNotTrack === '1' ||
    (navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl
  )
    return;
  const configured = process.env.REACT_APP_API_URL || 'http://localhost:8000';
  const base = `${configured.replace(/\/$/, '').replace(/\/api\/v1$/, '')}/api/v1/runtime-metrics`;
  const release = /^[a-f0-9]{40}$/.test(process.env.REACT_APP_RELEASE || '')
    ? process.env.REACT_APP_RELEASE!
    : 'development';
  const initialUrl = window.location.href;
  const device = metricDevice(window.innerWidth);
  let context = session();
  let changedAt = context ? -1 : performance.now();
  let enabled = false;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const pending = new Map<string, RuntimeMetricSample>();
  const identities = new Map<string, { id: string; sequence: number }>();

  const refreshConfig = () => {
    const active = context;
    if (!active) return;
    const version = generation;
    void fetch(`${base}/config`, { headers: { Authorization: `Bearer ${active.token}` }, credentials: 'omit' })
      .then(response => (response.ok ? response.json() : null))
      .then(config => {
        if (version === generation) enabled = config?.enabled === true;
      })
      .catch(() => {
        /* Measurement must never disrupt a work session. */
      });
  };
  const syncContext = () => {
    const next = session();
    if (next?.key !== context?.key) {
      generation += 1;
      changedAt = performance.now();
      pending.clear();
      identities.clear();
      enabled = false;
    }
    context = next;
    refreshConfig();
  };
  const flush = () => {
    if (timer) clearTimeout(timer);
    timer = undefined;
    const active = session();
    if (!enabled || !active || active.key !== context?.key || !pending.size) {
      pending.clear();
      return;
    }
    const samples = Array.from(pending.values()).slice(0, 10);
    pending.clear();
    // Keepalive survives a page hide. Authorization is captured for this company;
    // there is no token-refresh retry which could move a sample to a different tenant.
    void fetch(`${base}/samples`, {
      method: 'POST',
      credentials: 'omit',
      keepalive: true,
      headers: { Authorization: `Bearer ${active.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ samples }),
    }).catch(() => {
      /* Drop failed analytics; never add network recovery work for users. */
    });
  };
  const report = (metric: Metric) => {
    if (!enabled || !context || !['LCP', 'INP', 'CLS'].includes(metric.name)) return;
    // A login/company change must not reattribute earlier document measurements.
    if ((metric.navigationStartTime || 0) <= changedAt) return;
    const route = metricRoute(metric.navigationURL || initialUrl);
    if (!route || !Number.isFinite(metric.value) || metric.value < 0) return;
    const identity = identities.get(metric.id) || { id: crypto.randomUUID(), sequence: 0 };
    identity.sequence += 1;
    if (identities.size >= 500 && !identities.has(metric.id)) return;
    identities.set(metric.id, identity);
    pending.set(metric.id, {
      metric_id: identity.id,
      name: metric.name as MetricName,
      route,
      device,
      navigation: metric.navigationType === 'soft-navigation' ? 'soft' : 'document',
      release,
      value: Math.round(metric.value * 10000) / 10000,
      sequence: identity.sequence,
    });
    if (document.visibilityState === 'hidden' || pending.size >= 10) flush();
    else if (!timer) timer = setTimeout(flush, 2000);
  };
  // The browser's soft-navigation metrics carry their own navigationURL. Other
  // browsers retain document metrics, shown separately in the admin report.
  onLCP(report, { reportSoftNavs: true });
  onINP(report, { reportSoftNavs: true });
  onCLS(report, { reportSoftNavs: true });
  window.addEventListener('werco:auth-token-changed', syncContext);
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush();
    else refreshConfig();
  });
  refreshConfig();
}
