// Read-only deterministic audit harness. No live HTTP or repository writes.
// Run from the repository root: node docs/codebase-audit-2026-09-13/evidence/frontend-session-repro.cjs
// Transpiles the canonical current frontend/src/services/api.ts in memory.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = process.cwd();
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
const source = fs.readFileSync(path.join(root, 'frontend/src/services/api.ts'), 'utf8');
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021, esModuleInterop: true },
}).outputText;

function deferred() {
  let resolve, reject;
  const promise = new Promise((r, j) => { resolve = r; reject = j; });
  return { promise, resolve, reject };
}

function harness() {
  const store = new Map();
  const hooks = {};
  const calls = [];
  const rawRefreshes = [];
  let getHandler;
  const instance = config => { calls.push(config); return Promise.resolve({ data: { ok: true }, config }); };
  Object.assign(instance, {
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: f => hooks.request = f }, response: { use: (f, e) => hooks.response = e } },
    get: (...args) => getHandler(...args),
  });
  const axios = {
    create: () => instance,
    post: (...args) => { const d = deferred(); rawRefreshes.push({ ...d, args }); return d.promise; },
  };
  const exports = {};
  const context = {
    exports,
    require: n => {
      if (n === 'axios') return { __esModule: true, default: axios };
      if (n === '../utils/apiError') return { normalizeAxiosErrorDetail: x => x };
      throw new Error(`Unmocked runtime import: ${n}`);
    },
    sessionStorage: {
      getItem: k => store.get(k) ?? null,
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    window: { dispatchEvent() {}, location: { pathname: '/kiosk', search: '', hash: '' } },
    Event: class {}, process: { env: {} }, console: { warn() {}, error() {} },
    Date, URLSearchParams, Map, Array, JSON, Promise, Blob,
  };
  vm.runInNewContext(output, context);
  return { api: exports.default, hooks, calls, rawRefreshes, store, setGet: f => getHandler = f };
}

(async () => {
  let h = harness();
  h.api.setTokens('A', 'RA', 30);
  let a = h.hooks.request({ url: '/inventory/', headers: {} });
  let b = h.hooks.request({ url: '/parts/', headers: {} });
  console.log('SIMULTANEOUS_REFRESH_COUNT', h.rawRefreshes.length);
  h.rawRefreshes.forEach((d, i) => d.resolve({ data: { access_token: `A${i}`, refresh_token: `RA${i}`, expires_in: 3600 } }));
  await Promise.all([a, b]);

  h = harness();
  h.api.setTokens('A', 'RA', 30);
  a = h.hooks.request({ url: '/inventory/', headers: {} });
  h.api.logout();
  h.rawRefreshes[0].resolve({ data: { access_token: 'A-resurrected', refresh_token: 'RA-new', expires_in: 3600 } });
  await a;
  console.log('TOKEN_AFTER_LOGOUT_AND_LATE_REFRESH', h.store.get('token'));

  h = harness();
  h.api.setTokens('A', 'RA', 30);
  a = h.hooks.request({ url: '/inventory/', headers: {} });
  h.api.logout();
  h.api.setTokens('B', 'RB', 3600);
  h.rawRefreshes[0].resolve({ data: { access_token: 'A-overwrote-B', refresh_token: 'RA-new', expires_in: 3600 } });
  await a;
  console.log('TOKEN_AFTER_NEW_LOGIN_AND_LATE_OLD_REFRESH', h.store.get('token'));

  h = harness();
  h.api.setTokens('B', 'RB', 3600);
  a = h.hooks.response({
    response: { status: 401, data: {} },
    config: { url: '/inventory/receive', method: 'post', data: { quantity: 1 }, headers: { Authorization: 'Bearer A' } },
  });
  h.rawRefreshes[0].resolve({ data: { access_token: 'B-new', refresh_token: 'RB-new', expires_in: 3600 } });
  await a;
  console.log('OLD_COMPANY_MUTATION_RETRY_AUTH', h.calls[0].headers.Authorization);

  h = harness();
  h.api.setTokens('A', 'RA', 3600);
  const old = deferred();
  h.setGet(() => old.promise);
  a = h.api.fetchWithCache('/shop-floor/dashboard');
  h.api.setTokens('B', 'RB', 3600);
  h.api.clearCache();
  old.resolve({ status: 200, headers: { etag: 'A-etag' }, data: { company: 'A', rows: ['A-data'] } });
  await a;
  h.setGet(() => Promise.reject(new Error('offline B')));
  console.log('LATE_RESPONSE_CACHE_AFTER_SWITCH', JSON.stringify(await h.api.fetchWithCache('/shop-floor/dashboard')));

  h = harness();
  h.setGet(() => Promise.resolve({ status: 200, headers: { etag: 'etag' }, data: { private: 'data' } }));
  await h.api.fetchWithCache('/shop-floor/dashboard');
  h.setGet(() => Promise.reject({ response: { status: 403, data: { detail: 'No longer permitted' } } }));
  console.log('CACHE_ON_403', JSON.stringify(await h.api.fetchWithCache('/shop-floor/dashboard')));
})().catch(error => { console.error(error); process.exitCode = 1; });
