/**
 * API Endpoints Test
 * 
 * Tests individual API endpoints for performance baselines.
 * Useful for identifying slow endpoints and setting SLOs.
 * 
 * Usage: k6 run api-endpoints.js
 */

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Trend } from 'k6/metrics';
import { BASE_URL, ENDPOINTS, getHeaders, randomInt } from './config.js';

// Per-endpoint metrics
const healthDuration = new Trend('endpoint_health');
const partsDuration = new Trend('endpoint_parts');
const partsSearchDuration = new Trend('endpoint_parts_search');
const workOrdersDuration = new Trend('endpoint_work_orders');
const workOrderDetailDuration = new Trend('endpoint_work_order_detail');
const customersDuration = new Trend('endpoint_customers');
const dashboardDuration = new Trend('endpoint_dashboard');
const analyticsDuration = new Trend('endpoint_analytics');
const workCentersDuration = new Trend('endpoint_work_centers');

export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '1m', target: 10 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    // Per-endpoint thresholds
    'endpoint_health': ['p(95)<100'],
    'endpoint_parts': ['p(95)<300'],
    'endpoint_parts_search': ['p(95)<500'],
    'endpoint_work_orders': ['p(95)<400'],
    'endpoint_customers': ['p(95)<300'],
    'endpoint_dashboard': ['p(95)<500'],
    'endpoint_analytics': ['p(95)<1000'],
    'endpoint_work_centers': ['p(95)<200'],
  },
};

export default function () {
  const headers = getHeaders();

  // Health endpoint
  group('Health Check', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.health}`);
    healthDuration.add(res.timings.duration);
    check(res, {
      'health: status 200': (r) => r.status === 200,
      'health: response < 100ms': (r) => r.timings.duration < 100,
    });
  });
  sleep(0.5);

  // Parts list
  group('Parts List', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.parts}?limit=50`, { headers });
    partsDuration.add(res.timings.duration);
    check(res, {
      'parts: status ok': (r) => r.status === 200 || r.status === 401,
      'parts: response < 300ms': (r) => r.timings.duration < 300,
      'parts: returns array': (r) => {
        if (r.status === 200) {
          try {
            const body = JSON.parse(r.body);
            return Array.isArray(body);
          } catch {
            return false;
          }
        }
        return true;
      },
    });
  });
  sleep(0.5);

  // Parts search
  group('Parts Search', () => {
    const searchTerms = ['CNC', 'LASER', 'WELD', 'ASSY', 'TEST'];
    const term = searchTerms[randomInt(0, searchTerms.length - 1)];
    const res = http.get(`${BASE_URL}${ENDPOINTS.parts}?search=${term}&limit=20`, { headers });
    partsSearchDuration.add(res.timings.duration);
    check(res, {
      'parts search: status ok': (r) => r.status === 200 || r.status === 401,
      'parts search: response < 500ms': (r) => r.timings.duration < 500,
    });
  });
  sleep(0.5);

  // Work orders list
  group('Work Orders List', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.workOrders}?limit=50`, { headers });
    workOrdersDuration.add(res.timings.duration);
    check(res, {
      'work orders: status ok': (r) => r.status === 200 || r.status === 401,
      'work orders: response < 400ms': (r) => r.timings.duration < 400,
    });
  });
  sleep(0.5);

  // Customers
  group('Customers', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.customers}`, { headers });
    customersDuration.add(res.timings.duration);
    check(res, {
      'customers: status ok': (r) => r.status === 200 || r.status === 401,
      'customers: response < 300ms': (r) => r.timings.duration < 300,
    });
  });
  sleep(0.5);

  // Work centers
  group('Work Centers', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.workCenters}`, { headers });
    workCentersDuration.add(res.timings.duration);
    check(res, {
      'work centers: status ok': (r) => r.status === 200 || r.status === 401,
      'work centers: response < 200ms': (r) => r.timings.duration < 200,
    });
  });
  sleep(0.5);

  // Dashboard
  group('Dashboard', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.dashboard}`, { headers });
    dashboardDuration.add(res.timings.duration);
    check(res, {
      'dashboard: status ok': (r) => r.status === 200 || r.status === 401,
      'dashboard: response < 500ms': (r) => r.timings.duration < 500,
    });
  });
  sleep(0.5);

  // Analytics (typically slower due to aggregations)
  group('Analytics', () => {
    const res = http.get(`${BASE_URL}${ENDPOINTS.analytics}?period=30d`, { headers });
    analyticsDuration.add(res.timings.duration);
    check(res, {
      'analytics: status ok': (r) => r.status === 200 || r.status === 401,
      'analytics: response < 1s': (r) => r.timings.duration < 1000,
    });
  });
  sleep(0.5);
}

export function handleSummary(data) {
  const metrics = data.metrics;
  
  console.log('\n=== API Endpoint Performance Report ===\n');
  
  const endpoints = [
    { name: 'Health Check', metric: 'endpoint_health', target: 100 },
    { name: 'Parts List', metric: 'endpoint_parts', target: 300 },
    { name: 'Parts Search', metric: 'endpoint_parts_search', target: 500 },
    { name: 'Work Orders', metric: 'endpoint_work_orders', target: 400 },
    { name: 'Customers', metric: 'endpoint_customers', target: 300 },
    { name: 'Work Centers', metric: 'endpoint_work_centers', target: 200 },
    { name: 'Dashboard', metric: 'endpoint_dashboard', target: 500 },
    { name: 'Analytics', metric: 'endpoint_analytics', target: 1000 },
  ];

  console.log('Endpoint Performance (p95 response time):');
  console.log('─'.repeat(60));
  console.log('Endpoint              │ p50     │ p95     │ Target  │ Status');
  console.log('─'.repeat(60));
  
  endpoints.forEach(ep => {
    const m = metrics[ep.metric];
    if (m) {
      const p50 = m.values.med.toFixed(0);
      const p95 = m.values['p(95)'].toFixed(0);
      const status = parseFloat(p95) <= ep.target ? '✅' : '❌';
      console.log(
        `${ep.name.padEnd(20)} │ ${(p50 + 'ms').padStart(7)} │ ${(p95 + 'ms').padStart(7)} │ ${(ep.target + 'ms').padStart(7)} │ ${status}`
      );
    }
  });
  console.log('─'.repeat(60));

  // SLO recommendations
  console.log('\nSLO Recommendations:');
  endpoints.forEach(ep => {
    const m = metrics[ep.metric];
    if (m) {
      const p99 = m.values['p(99)'];
      const recommended = Math.ceil(p99 * 1.2 / 50) * 50; // Round up to nearest 50ms with 20% buffer
      console.log(`  ${ep.name}: p99 SLO = ${recommended}ms`);
    }
  });

  return {};
}
